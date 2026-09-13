"""Connector orchestrator for AP-CODEX.

Responsibilities:

* register connectors (either instances or from declarative config)
* aggregate tool discovery so the LLM backend sees one flat tool list
* dispatch invocations through :func:`execute_connector_action`, the single
  entry point the LangGraph / agent loop calls
* clean error surfaces (JSON dicts, never exceptions)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Union

from .api_client import APIClient
from .errors import ConnectorNotFoundError
from .mcp_client import MCPClient

logger = logging.getLogger(__name__)


class ConnectorManager:
    """Registry + router for all registered connectors."""

    def __init__(self, secrets: Optional[Dict[str, str]] = None):
        self._connectors: Dict[str, Any] = {}
        self._secrets = secrets or {}

    # ------------------------------------------------------------- lifecycle
    def register(self, connector, replace: bool = True) -> str:
        """Register a connector instance (must expose ``id``)."""
        connector_id = getattr(connector, "id", None)
        if not connector_id:
            raise ValueError("connector must expose an 'id' attribute")
        if connector_id in self._connectors and not replace:
            raise ValueError(f"connector {connector_id!r} is already registered")
        self._connectors[connector_id] = connector
        logger.info("registered connector %s (%s)", connector_id, type(connector).__name__)
        return connector_id

    def unregister(self, connector_id: str) -> bool:
        connector = self._connectors.pop(connector_id, None)
        if connector is not None and hasattr(connector, "close"):
            try:
                connector.close()
            except Exception:  # noqa: BLE001
                logger.warning("error closing connector %s", connector_id, exc_info=True)
        return connector is not None

    def close_all(self):
        for connector_id in list(self._connectors):
            self.unregister(connector_id)

    # ------------------------------------------------------------------ build
    def load_from_config(
        self,
        config: Union[Dict[str, Any], List[Dict[str, Any]], str, os.PathLike] = None,
        *,
        start_mcp: bool = False,
    ) -> List[str]:
        """Register connectors from config.

        Accepts a JSON file path, a dict, or a list of connector dicts.
        A dict may be keyed by connector id or wrapped as ``{"connectors": [...]}``.

        Returns the list of registered connector ids.
        """
        raw = self._resolve_config(config)
        entries = self._to_entries(raw)

        registered = []
        for entry in entries:
            connector = self.build_connector(entry)
            if connector is None:
                continue
            connector_id = self.register(connector)
            if start_mcp and isinstance(connector, MCPClient):
                connector.start()
            registered.append(connector_id)
        return registered

    def build_connector(self, config: Dict[str, Any]):
        if "endpoints" in config:
            return APIClient(config, self._secrets)
        if "base_url" in config:
            return APIClient(
                {**config, "id": config.get("id") or "api",
                 "endpoints": config.get("endpoints") or {}},
                self._secrets,
            )
        if config.get("type") == "api":
            return APIClient(config, self._secrets)
        if config.get("type") == "mcp":
            return MCPClient(config)
        if "transport" in config or "url" in config:
            return MCPClient(config)
        logger.warning("skipping connector config without a recognized type: %s",
                       config.get("id") or config)
        return None

    # ---------------------------------------------------------------- tools
    def discover_tools(self) -> List[Dict[str, Any]]:
        """Aggregate tool schemas from every connector, deduplicated by name.

        API endpoints are prefixed ``<connector_id>.<action>``.  MCP tools are
        prefixed the same way on ingestion so the names are stable and
        collision-free across connectors.
        """
        tools: Dict[str, Dict[str, Any]] = {}
        for connector in self._connectors.values():
            if hasattr(connector, "discover_tools"):
                try:
                    for schema in connector.discover_tools():
                        if isinstance(connector, APIClient):
                            name = schema["function"]["name"]  # already prefixed
                        else:
                            fn = dict(schema["function"])
                            fn["name"] = f"{connector.id}.{fn['name']}"
                            schema = {"type": "function", "function": fn}
                            name = fn["name"]
                        tools.setdefault(name, schema)
                except Exception:  # noqa: BLE001
                    logger.exception("tool discovery failed for connector %s", connector.id)
        return list(tools.values())

    # ------------------------------------------------------------ invocation
    def execute_connector_action(
        self, connector_id: str, action_name: str, payload: Any = None
    ) -> Dict[str, Any]:
        """Invoke a connector action.  Core entry point used by the agent loop.

        * ``action_name`` may be bare (``fetch_order``) or prefixed
          (``orders_api.fetch_order``); the prefix is stripped and validated.
        * Always returns a JSON-serializable dict, never raises into the loop.
        """
        connector = self._connectors.get(connector_id)
        if connector is None:
            return {
                "ok": False,
                "connector": connector_id,
                "action": action_name,
                "error": f"connector {connector_id!r} is not registered",
            }

        bare = self._strip_prefix(connector_id, action_name)
        call = getattr(connector, "call_function", None)
        if call is None:
            return {
                "ok": False,
                "connector": connector_id,
                "action": action_name,
                "error": f"connector {connector_id!r} has no call_function",
            }
        try:
            result = call(bare, payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception("connector %s action %s raised", connector_id, action_name)
            return {
                "ok": False,
                "connector": connector_id,
                "action": action_name,
                "error": f"internal error: {exc}",
            }
        if not isinstance(result, dict):
            result = {"ok": True, "connector": connector_id,
                      "action": action_name, "data": result}
        if result.get("action") == bare:
            result["action"] = action_name
        return result

    # ------------------------------------------------------------- internals
    @staticmethod
    def _strip_prefix(connector_id: str, action_name: str) -> str:
        prefix = connector_id + "."
        if action_name.startswith(prefix):
            return action_name[len(prefix):]
        return action_name

    @staticmethod
    def _resolve_config(config):
        if config is None:
            return []
        if isinstance(config, (str, os.PathLike)):
            with open(config, "r", encoding="utf-8") as fh:
                return json.load(fh)
        return config

    @staticmethod
    def _to_entries(raw) -> List[Dict[str, Any]]:
        if isinstance(raw, dict):
            if isinstance(raw.get("connectors"), list):
                return raw["connectors"]
            if "endpoints" in raw or "id" in raw:
                return [raw]
            return [
                {"id": key, **(value if isinstance(value, dict) else {})}
                for key, value in raw.items()
            ]
        if isinstance(raw, list):
            return raw
        return []


# ----------------------------------------------------------------------------
# Module-level default manager + seamless router for the agent loop.
# ----------------------------------------------------------------------------
_default_manager = ConnectorManager()


def register_api_connector(config: Dict[str, Any]) -> str:
    """Build + register an API connector and return its id."""
    return _default_manager.register(_default_manager.build_connector(config))


def register_mcp_connector(config: Dict[str, Any]) -> str:
    """Build + register an MCP connector and return its id."""
    return _default_manager.register(_default_manager.build_connector(config))


def execute_connector_action(
    connector_id: str, action_name: str, payload: Any = None
) -> Dict[str, Any]:
    """Route a tool call to a registered connector.

    This is the function the AP-CODEX LangGraph backend binds to the LLM::

        tools = [{"type": "function", "function": schema} for schema in manager.discover_tools()]
        ...tool call...
        result = execute_connector_action(connector_id, action_name, payload)
    """
    return _default_manager.execute_connector_action(connector_id, action_name, payload)