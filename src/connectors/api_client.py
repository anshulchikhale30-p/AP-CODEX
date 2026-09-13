"""Generic REST/JSON connector for AP-CODEX.

Configured declaratively from plain dicts.  Each connector exposes one
``action`` per declared endpoint; the agent backend ingests those actions as
native LLM tools via :meth:`APIClient.discover_tools` and invokes them through
:meth:`APIClient.call_function`.

Minimal configuration::

    {
        "id": "orders_api",
        "type": "api",
        "base_url": "https://api.example.com",
        "timeout": 20,
        "retries": 2,
        "auth": {"type": "bearer", "token_env": "ORDERS_API_TOKEN"},
        "headers": {"Accept": "application/json"},
        "endpoints": {
            "fetch_order": {
                "description": "Fetch a single order by id",
                "method": "GET",
                "path": "/orders/{order_id}",
                "params": ["order_id"]
            },
            "create_order": {
                "description": "Create a new order",
                "method": "POST",
                "path": "/orders",
                "body": {"source": "ap-codex"}
            }
        }
    }

Path templates (``{name}``) are filled from the payload.  Keys declared in
``params`` are turned into a query string.  ``body`` merges as a template that
is overlaid by any remaining payload keys.  Credentials are never embedded in
this config; they are injected from the environment at request time.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, Optional

from .auth import TokenProvider
from .errors import ApiConnectorError, ConnectorError

logger = logging.getLogger(__name__)

_RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}
_NO_BODY_METHODS = {"GET", "HEAD", "DELETE"}


class APIClient:
    """One configured REST/JSON connector."""

    def __init__(self, config: Dict[str, Any], secrets: Optional[Dict[str, str]] = None):
        self.config = config
        self.id: str = config.get("id") or "api"
        self.name: str = config.get("name") or self.id
        self._secrets = secrets or {}
        self._provider = TokenProvider(config.get("auth"), self._secrets)
        self._endpoints: Dict[str, Dict[str, Any]] = config.get("endpoints") or {}

    # ------------------------------------------------------------------ tools
    def discover_tools(self) -> Iterable[Dict[str, Any]]:
        """Yield OpenAI-style function schemas, one per declared endpoint."""
        for action_name, endpoint in self._endpoints.items():
            spec = endpoint.get("input_schema") or self._auto_schema(endpoint)
            yield {
                "type": "function",
                "function": {
                    "name": f"{self.id}.{action_name}",
                    "description": endpoint.get("description") or self._default_description(endpoint),
                    "parameters": spec,
                },
            }

    # ------------------------------------------------------------------ call
    def call_function(self, action_name: str, payload: Any = None) -> Dict[str, Any]:
        """Execute an endpoint.

        Returns a JSON-serializable result dict of the shape::

            {"ok": True,  "connector": id, "action": name, "status": 200, "data": ...}
            {"ok": False, "connector": id, "action": name, "error": "...", "status": code|None}

        No exception escapes; failures are returned as clean, redacted errors
        so the agent loop can keep running.
        """
        payload = {} if payload is None else payload
        endpoint = self._endpoints.get(action_name)
        if endpoint is None:
            return {"ok": False, "connector": self.id, "action": action_name,
                    "error": f"unknown action {action_name!r} for connector {self.id!r}"}

        try:
            request, headers = self._build_request(endpoint, payload)
            status, data = self._send_with_retries(request, headers)
        except ApiConnectorError as exc:
            return self._error(action_name, str(exc), exc.status_code, secret_safe=True)
        except ConnectorError as exc:
            return self._error(action_name, str(exc), None, secret_safe=True)
        except Exception as exc:  # defensive: never let a crash reach the loop
            logger.exception("unexpected failure in connector %s action %s", self.id, action_name)
            return self._error(action_name, f"internal error: {exc}", None)

        return {"ok": True, "connector": self.id, "action": action_name,
                "status": status, "data": data}

    # -------------------------------------------------------------- internals
    def _build_request(self, endpoint: Dict[str, Any], payload: Any):
        method = str(endpoint.get("method", "GET")).upper()
        path = endpoint.get("path") or "/"
        base = self.config.get("base_url", "").rstrip("/")

        request_payload = payload if isinstance(payload, dict) else {}
        consumed = set()

        for key, value in request_payload.items():
            token = "{" + key + "}"
            if token in path:
                path = path.replace(token, urllib.parse.quote(str(value)))
                consumed.add(key)

        query_names = [n for n in endpoint.get("params", []) if isinstance(n, str)]
        query, consumed_q = self._build_query(request_payload, query_names)
        consumed |= consumed_q

        body = self._build_body(endpoint, request_payload, consumed)
        url = base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)

        headers = dict(self.config.get("headers") or {})
        headers.update(endpoint.get("headers") or {})
        headers.update(self._provider.header())

        if body is not None and "Content-Type" not in {k.lower() for k in headers}:
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, method=method, headers=headers)
        if body is not None:
            request.data = body if isinstance(body, bytes) else json.dumps(body).encode()
        return request, headers

    @staticmethod
    def _build_query(payload: Dict[str, Any], names) -> tuple:
        query = {}
        for name in names:
            if name in payload:
                query[name] = payload[name]
        return query, set(names)

    def _build_body(self, endpoint, payload: Dict[str, Any], consumed: set):
        if endpoint.get("method", "GET").upper() in _NO_BODY_METHODS:
            return None
        if not isinstance(payload, dict):
            return payload  # caller supplied a raw list/number/str body
        template = endpoint.get("body")
        if template is None:
            template = {}
        if not isinstance(template, dict):
            return template
        remaining = {k: v for k, v in payload.items() if k not in consumed}
        merged = dict(template)
        merged.update(remaining)
        return merged if merged else {}

    def _send_with_retries(self, request, headers):
        retries = int(self.config.get("retries", 1))
        retries = max(0, retries)
        delay = float(self.config.get("backoff_base", 0.3))
        timeout = float(self.config.get("timeout", 30))

        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(request, timeout=timeout) as resp:
                    raw = resp.read()
                    return resp.status, self._decode(raw)
            except urllib.error.HTTPError as exc:
                if exc.code not in _RETRYABLE_HTTP or attempt >= retries:
                    raise ApiConnectorError(
                        f"HTTP {exc.code} {exc.reason} for {request.full_url}",
                        status_code=exc.code,
                        retryable=exc.code in _RETRYABLE_HTTP,
                    ) from exc
                attempt += 1
                self._sleep(delay, attempt)
            except (urllib.error.URLError, OSError) as exc:
                if attempt >= retries:
                    raise ApiConnectorError(
                        f"connection error for {request.full_url}: {exc}", retryable=True
                    ) from exc
                attempt += 1
                self._sleep(delay, attempt)

    @staticmethod
    def _sleep(delay, attempt):
        backoff = delay * (2 ** (attempt - 1)) + random.uniform(0, delay / 2)
        time.sleep(backoff)

    @staticmethod
    def _decode(raw: bytes):
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _auto_schema(endpoint: Dict[str, Any]) -> Dict[str, Any]:
        props: Dict[str, Any] = {}
        for name in endpoint.get("params", []) or []:
            props[name] = {"type": "string", "description": f"Query/path param {name}"}
        body = endpoint.get("body")
        if isinstance(body, dict):
            for key in body:
                props[key] = {"type": "string", "description": f"Body field {key}"}
        return {"type": "object", "properties": props}

    @staticmethod
    def _default_description(endpoint):
        method = endpoint.get("method", "GET").upper()
        return f"{method} {endpoint.get('path', '/')}"

    def _error(self, action_name, message, status, secret_safe=False):
        out = {"ok": False, "connector": self.id, "action": action_name, "error": message}
        if status is not None:
            out["status"] = status
        if secret_safe:
            out["error"] = self._provider.redact(message)
        return out