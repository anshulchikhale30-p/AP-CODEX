"""Shared OAuth2 base for Google-scoped connectors (Gmail, Calendar, ...)."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

_DEFAULT_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

Transport = Callable[[str, str, Dict[str, Any]], Tuple[int, Any]]


class GoogleOAuthConnector:
    """Base class implementing the Google OAuth2 authorization-code flow.

    Subclasses set ``id``, ``name`` and ``_SCOPES`` (tuple of scope strings),
    implement ``discover_tools()`` / ``call_function()``, and use the helpers
    ``_get_access_token()`` and ``_request()`` to hit Google APIs.  Never
    raises on user-facing actions.
    """

    _SCOPES: Tuple[str, ...] = ()

    def __init__(
        self,
        *,
        base_dir: Optional[str] = None,
        credentials_path: Optional[str] = None,
        tokens_path: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        transport: Optional[Transport] = None,
    ):
        env = os.environ
        self._base_dir = base_dir or env.get("AP_CODEX_GMAIL_DIR") or _DEFAULT_BASE
        self._credentials_path = (
            credentials_path
            or env.get("AP_CODEX_GMAIL_CREDENTIALS")
            or os.path.join(self._base_dir, "gmail_credentials.json")
        )
        self._tokens_path = (
            tokens_path
            or self._env_tokens_path()
            or os.path.join(self._base_dir, self._default_tokens_name())
        )
        self._transport = transport
        self._client_id = client_id or env.get("AP_CODEX_GMAIL_CLIENT_ID")
        self._client_secret = client_secret or env.get("AP_CODEX_GMAIL_CLIENT_SECRET")
        if not self._client_id or not self._client_secret:
            credentials = self._load_json(self._credentials_path)
            if credentials:
                self._client_id = self._client_id or credentials.get("client_id")
                self._client_secret = self._client_secret or credentials.get("client_secret")
        self._tokens = self._load_json(self._tokens_path) or {}

    # ------------------------------------------------------- subclass hooks
    def _env_tokens_path(self) -> Optional[str]:
        return None

    def _default_tokens_name(self) -> str:
        return "google_tokens.json"

    # ------------------------------------------------------------- properties
    @property
    def authorized(self) -> bool:
        token = self._tokens.get("access_token")
        if not token:
            return False
        expires_at = self._tokens.get("expires_at") or 0
        if expires_at > time.time() + 60:
            return True
        return bool(self._tokens.get("refresh_token"))

    @property
    def authorised(self) -> bool:  # British spelling, mirrors `authorized`
        return self.authorized

    # -------------------------------------------------------------- oauth flow
    def authorization_url(self, redirect_uri: str, state: Optional[str] = None) -> Dict[str, Any]:
        if not self._client_id:
            return {"ok": False, "connector": self.id, "action": "authorize",
                    "error": "no OAuth client_id configured "
                             "(set AP_CODEX_GMAIL_CLIENT_ID / AP_CODEX_GMAIL_CLIENT_SECRET "
                             "or provide gmail_credentials.json)"}
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self._SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        if state:
            params["state"] = state
        return {"ok": True, "connector": self.id, "action": "authorize",
                "url": AUTH_URL + "?" + urlencode(params)}

    def exchange_code(self, code: str, redirect_uri: str) -> Dict[str, Any]:
        if not self._client_id:
            return {"ok": False, "connector": self.id, "action": "authorize",
                    "error": "no OAuth client_id configured"}
        return self._token_request(
            {
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            action="authorize",
        )

    # ------------------------------------------------------------- internals
    def _token_request(self, data: Dict[str, Any], action: str) -> Dict[str, Any]:
        try:
            status, body = self._request("POST", TOKEN_URL, data=data)
        except Exception as exc:  # noqa: BLE001 - never crash the agent loop
            logger.warning("%s token request failed: %s", self.id, exc)
            return {"ok": False, "connector": self.id, "action": action,
                    "status": 0, "error": f"network error: {exc}"}
        if status >= 400 or not (body or {}).get("access_token"):
            error = error_text(body) or f"token endpoint returned status {status}"
            logger.warning("%s token request rejected: %s", self.id, error)
            return {"ok": False, "connector": self.id, "action": action,
                    "status": status, "error": error}

        self._store_tokens(body)
        return {"ok": True, "connector": self.id, "action": action, "status": status,
                "data": {"scope": body.get("scope"), "token_type": body.get("token_type")}}

    def _get_access_token(self) -> Optional[str]:
        token = self._tokens.get("access_token")
        expires_at = self._tokens.get("expires_at") or 0
        if token and expires_at > time.time() + 60:
            return token
        refresh_token = self._tokens.get("refresh_token")
        if not refresh_token or not self._client_id:
            return None
        try:
            status, body = self._request(
                "POST", TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s token refresh failed: %s", self.id, exc)
            return None
        if status >= 400 or not (body or {}).get("access_token"):
            logger.warning("%s token refresh rejected: %s", self.id, body)
            self._tokens = {}
            return None
        new_tokens = dict(self._tokens)
        new_tokens.update(body)
        self._store_tokens(new_tokens)
        return new_tokens.get("access_token")

    def _store_tokens(self, body: Dict[str, Any]) -> None:
        new_tokens = {
            "access_token": body.get("access_token"),
            "refresh_token": body.get("refresh_token", self._tokens.get("refresh_token")),
            "scope": body.get("scope"),
            "token_type": body.get("token_type") or self._tokens.get("token_type") or "Bearer",
            "expires_at": time.time() + int(body.get("expires_in") or 3600),
        }
        self._tokens = new_tokens
        try:
            with open(self._tokens_path, "w", encoding="utf-8") as fh:
                json.dump(new_tokens, fh, ensure_ascii=False, indent=2)
        except OSError:
            logger.warning("could not persist %s tokens to %s", self.id, self._tokens_path)

    def _request(self, method: str, url: str, **kwargs) -> Tuple[int, Any]:
        if self._transport is not None:
            return self._transport(method, url, **kwargs)
        with httpx.Client(timeout=15) as client:
            response = client.request(method, url, **kwargs)
            try:
                body = response.json()
            except ValueError:
                body = response.text
            return response.status_code, body

    @staticmethod
    def _load_json(path: str) -> Optional[Dict[str, Any]]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _tool(name: str, description: str, props: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": props,
                               "required": list(props)},
            },
        }


def error_text(data: Any) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error:
            return str(error)
        return str(data.get("error_description") or "")
    return str(data)