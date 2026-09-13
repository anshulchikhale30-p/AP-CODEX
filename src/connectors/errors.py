"""Exception types shared across the AP-CODEX connector pipeline.

Every exception subclasses :class:`ConnectorError` so callers can catch the
whole pipeline with a single ``except ConnectorError`` and keep agent loops
clean.
"""


class ConnectorError(Exception):
    """Base class for all connector pipeline errors."""


class ConnectorNotFoundError(ConnectorError):
    """Raised when a caller references a connector id that is not registered."""


class AuthError(ConnectorError):
    """Raised when a token or credential cannot be resolved/satisfied."""


class ApiConnectorError(ConnectorError):
    """Raised for transport/HTTP failures surfaced by :mod:`api_client`.

    Attributes:
        status_code: HTTP status code when the failure came from the remote.
        retryable: True when a retry may resolve the failure.
    """

    def __init__(self, message, status_code=None, retryable=False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class McpError(ConnectorError):
    """Raised when the MCP SDK is unavailable or an MCP exchange fails."""