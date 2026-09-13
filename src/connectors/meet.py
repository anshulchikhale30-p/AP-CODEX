"""Google Meet REST API connector for AP-CODEX (OAuth2, developer preview).

Uses the same OAuth client as Gmail (credentials in ``gmail_credentials.json``),
with Meet scopes.  Tokens are persisted to ``meet_tokens.json`` (gitignored).

Note: the Google Meet REST API (``meet.googleapis.com/v2``) is in *developer
preview*.  Your Google Cloud project must be on the allowlist and have the
Google Meet API enabled, or calls return 403 ``PERMISSION_DENIED`` / 404.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ._oauth import GoogleOAuthConnector, error_text

_MEET_BASE = "https://meet.googleapis.com/v2"
_SCOPE_CREATED = "https://www.googleapis.com/auth/meetings.space.created"
_SCOPE_READONLY = "https://www.googleapis.com/auth/meetings.space.readonly"


class MeetClient(GoogleOAuthConnector):
    """Create and inspect Google Meet spaces and conference records."""

    id = "meet"
    name = "Google Meet (OAuth2)"
    _SCOPES = (_SCOPE_CREATED, _SCOPE_READONLY)

    def _env_tokens_path(self) -> Optional[str]:
        return os.environ.get("AP_CODEX_MEET_TOKENS")

    def _default_tokens_name(self) -> str:
        return "meet_tokens.json"

    # -------------------------------------------------------------- interface
    def discover_tools(self) -> List[Dict[str, Any]]:
        return [
            self._tool("create_space",
                       "Create a Google Meet space and return its join link/code. "
                       "v2 spaces.create takes an empty body (title is not supported).",
                       {"title": {"type": "string",
                                  "description": "Ignored by the API; kept for callers."}}),
            self._tool("get_space",
                       "Get details of a Meet space by its name (e.g. 'spaces/abc-...').",
                       {"space": {"type": "string", "description": "Space name."}}),
            self._tool("list_conference_records",
                       "List the signed-in user's recent Meet conference records.",
                       {"max_results": {"type": "integer", "description": "Max records (default 10)."}}),
            self._tool("status", "Report Meet API readiness (authorized, api_ready).", {}),
        ]

    def call_function(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        if action == "create_space":
            return self.create_space(title=payload.get("title"))
        if action == "get_space":
            return self.get_space(space=payload.get("space"))
        if action == "list_conference_records":
            return self.list_conference_records(max_results=payload.get("max_results"))
        if action == "status":
            return self.status()
        return {"ok": False, "connector": self.id, "action": action,
                "error": f"meet has no action '{action}'"}

    # ------------------------------------------------------------- actions
    def create_space(self, title: Optional[str] = None) -> Dict[str, Any]:
        # v2 spaces.create accepts an empty Space; the title parameter is
        # accepted for API-shape stability but is not sent (meeting titles are
        # not part of the v2 SpaceConfig yet).
        status, data = self._authed_request(
            "POST", f"{_MEET_BASE}/spaces", json={}, action="create_space",
        )
        if status >= 400:
            return {"ok": False, "connector": self.id, "action": "create_space",
                    "status": status, "error": error_text(data)}
        return {"ok": True, "connector": self.id, "action": "create_space", "status": status,
                "data": {"name": (data or {}).get("name"),
                         "meeting_uri": (data or {}).get("meetingUri"),
                         "meeting_code": (data or {}).get("meetingCode"),
                         "title": str(title or "") or None}}

    def get_space(self, space: Optional[str] = None) -> Dict[str, Any]:
        space = str(space or "").strip()
        if not space:
            return {"ok": False, "connector": self.id, "action": "get_space",
                    "status": 422, "error": "space name is required (e.g. spaces/abc-xyz)"}
        if not space.startswith("spaces/"):
            space = f"spaces/{space}"
        status, data = self._authed_request(
            "GET", f"{_MEET_BASE}/{space.strip('/')}", action="get_space",
        )
        if status >= 400:
            return {"ok": False, "connector": self.id, "action": "get_space",
                    "status": status, "error": error_text(data)}
        return {"ok": True, "connector": self.id, "action": "get_space", "status": status,
                "data": {"name": (data or {}).get("name"),
                         "meeting_uri": (data or {}).get("meetingUri"),
                         "meeting_code": (data or {}).get("meetingCode"),
                         "title": ((data or {}).get("config") or {}).get("title")}}

    def list_conference_records(self, max_results: Optional[int] = None) -> Dict[str, Any]:
        status, data = self._authed_request(
            "GET", f"{_MEET_BASE}/conferenceRecords",
            params={"pageSize": max(1, min(int(max_results or 10), 100))},
            action="list_conference_records",
        )
        if status >= 400:
            return {"ok": False, "connector": self.id, "action": "list_conference_records",
                    "status": status, "error": error_text(data)}
        items = (data or {}).get("conferenceRecords") or []
        records = [{
            "name": item.get("name"),
            "start": item.get("startTime"),
            "end": item.get("endTime"),
            "space": item.get("space"),
        } for item in items]
        return {"ok": True, "connector": self.id, "action": "list_conference_records",
                "status": status,
                "data": {"count": len(records), "records": records}}

    # ------------------------------------------------------------- internals
    def _authed_request(self, method: str, url: str, action: str,
                        **kwargs) -> tuple:
        access_token = self._get_access_token()
        if not access_token:
            return (401, {"error": "Meet not authorized. Open /connectors/meet/authorize "
                                   "in a browser to sign in."})
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {access_token}"
        kwargs["headers"] = headers
        try:
            return self._request(method, url, **kwargs)
        except Exception as exc:  # noqa: BLE001
            return (0, {"error": f"network error: {exc}"})

    def status(self) -> Dict[str, Any]:
        """Readiness report — never exposes any credential values."""
        return {"ok": True, "connector": self.id, "action": "status",
                "data": {"authorized": self.authorized,
                         "api_ready": False,  # Meet REST API is developer-preview
                         "client_id_set": bool(self._client_id),
                         "hint": ("Google Meet API is in developer preview — ensure "
                                  "the Meet API is enabled and your project is "
                                  "allowlisted. Open /connectors/meet/authorize to "
                                  "sign in.") if not self.authorized else (
                             "Signed in. If create_space returns 403 PERMISSION_DENIED, "
                             "your project still needs Meet API access approval.")}}