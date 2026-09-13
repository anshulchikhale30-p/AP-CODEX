"""AP-CODEX connector pipeline: errors, auth, REST/MCP clients, and orchestrator."""

from .errors import (
    AuthError,
    ConnectorError,
    ConnectorNotFoundError,
    McpError,
    ApiConnectorError,
)
from .auth import TokenProvider
from .api_client import APIClient
from .mcp_client import MCPClient
from .shopping import ShoppingClient
from .rides import RideClient
from .email import EmailClient
from .gmail import GmailClient
from .calendar import CalendarClient
from .meet import MeetClient
from .twitter import TwitterClient
from .manager import (
    ConnectorManager,
    execute_connector_action,
    register_api_connector,
    register_mcp_connector,
)
from typing import List


def register_builtin_connectors(manager: ConnectorManager, **email_kwargs) -> List[str]:
    """Register the always-available lifestyle connectors on ``manager``.

    Returns the list of registered connector ids (currently shopping, rides,
    email).  ``email_kwargs`` is forwarded to :class:`EmailClient` so callers
    can point the simulated outbox wherever they like.
    """
    ids = []
    for connector in (
        ShoppingClient(),
        RideClient(),
        EmailClient(**email_kwargs),
        GmailClient(),
        CalendarClient(),
        MeetClient(),
        TwitterClient(),
    ):
        try:
            ids.append(manager.register(connector))
        except Exception:  # noqa: BLE001
            from .manager import logger

            logger.exception("failed to register builtin connector %s", connector.id)
    return ids


__all__ = [
    "AuthError",
    "ConnectorError",
    "ConnectorNotFoundError",
    "McpError",
    "ApiConnectorError",
    "TokenProvider",
    "APIClient",
    "MCPClient",
    "ShoppingClient",
    "RideClient",
    "EmailClient",
    "GmailClient",
    "CalendarClient",
    "MeetClient",
    "TwitterClient",
    "ConnectorManager",
    "execute_connector_action",
    "register_api_connector",
    "register_mcp_connector",
    "register_builtin_connectors",
]