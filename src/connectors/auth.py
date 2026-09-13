"""Secure auth header construction for AP-CODEX API connectors.

Tokens are resolved exclusively at request time from the process environment
(``os.environ``) or an in-memory secrets map supplied by the caller.  Nothing
here persists, serializes, or logs credential material.

Supported auth modes (``auth.type``):

* ``bearer``                     -> ``Authorization: Bearer <token>``
* ``header``                     -> arbitrary header name/value, both from env
* ``oauth2_client_credentials``  -> RFC 6749 token exchange + in-memory cache
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from .errors import AuthError

logger = logging.getLogger(__name__)


class TokenProvider:
    """Builds an ``Authorization`` / custom header for a single connector.

    Instances are intentionally tiny and stateless apart from the in-memory
    OAuth2 token cache, so one provider can be built per request if desired.
    """

    _REDACTED = "[REDACTED]"

    def __init__(
        self,
        auth_config: Optional[Dict[str, Any]] = None,
        secrets: Optional[Dict[str, str]] = None,
    ):
        self._cfg = auth_config or {}
        self._secrets = secrets or {}
        self._oauth_token: Optional[str] = None
        self._oauth_expiry: float = 0.0

    # ------------------------------------------------------------------ public
    def header(self) -> Dict[str, str]:
        """Return the auth header dict for the configured mode (may be empty)."""
        mode = self._cfg.get("type", "none")
        if mode in ("none", None, ""):
            return {}
        if mode == "bearer":
            token = self._resolve("token_env")
            return {"Authorization": "Bearer " + token}
        if mode == "header":
            name = self._cfg.get("header_name") or "Authorization"
            value = self._resolve("value_env")
            return {name: value}
        if mode == "oauth2_client_credentials":
            token = self._oauth_access_token()
            return {"Authorization": "Bearer " + token}
        raise AuthError(f"Unsupported auth type: {mode!r}")

    def redact(self, text: str) -> str:
        """Replace any secrets the provider knows about with a redaction marker."""
        for value in self._known_secrets():
            if value:
                text = text.replace(value, self._REDACTED)
        return text

    # ---------------------------------------------------------------- internal
    def _resolve(self, env_key: str) -> str:
        ref = self._cfg.get(env_key)
        if not ref:
            raise AuthError(f"auth config missing {env_key!r} (expected an env var name)")
        value = self._secrets.get(ref) or os.environ.get(ref)
        if not value:
            raise AuthError(
                f"required credential environment variable {ref!r} is not set"
            )
        self._secrets[ref] = value
        return value

    def _known_secrets(self) -> list:
        values = []
        for key in ("token_env", "value_env", "client_id_env", "client_secret_env"):
            ref = self._cfg.get(key)
            if ref:
                values.append(self._secrets.get(ref) or os.environ.get(ref) or "")
        if self._oauth_token:
            values.append(self._oauth_token)
        return values

    def _oauth_access_token(self) -> str:
        if self._oauth_token and time.time() < self._oauth_expiry:
            return self._oauth_token

        token_url = self._cfg.get("token_url")
        client_id = self._resolve("client_id_env")
        client_secret = self._resolve("client_secret_env")
        if not token_url:
            raise AuthError("oauth2_client_credentials requires a 'token_url'")

        payload = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                **(self._cfg.get("scopes") and {"scope": " ".join(self._cfg["scopes"])} or {}),
            }
        ).encode()
        request = urllib.request.Request(
            token_url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._cfg.get("timeout", 15)) as resp:
                token_data: Dict[str, Any] = json_loads(resp.read())
        except urllib.error.HTTPError as exc:  # pragma: no cover - remote only
            raise AuthError(f"OAuth token endpoint returned HTTP {exc.code}") from exc
        except OSError as exc:
            raise AuthError(f"OAuth token endpoint unreachable: {exc}") from exc

        token = token_data.get("access_token")
        if not token:
            raise AuthError("OAuth token response did not include 'access_token'")
        self._oauth_token = str(token)
        self._oauth_expiry = time.time() + max(int(token_data.get("expires_in", 3600)) - 60, 10)
        return self._oauth_token


def json_loads(raw: bytes):
    """Small, library-agnostic JSON decode used by auth + api clients."""
    import json

    return json.loads(raw.decode("utf-8"))