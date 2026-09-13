"""Google Calendar connector for AP-CODEX (OAuth2, real reads/writes).

Uses the same OAuth client as Gmail (credentials in ``gmail_credentials.json``),
with the ``calendar.events`` scope.  Tokens are persisted to
``calendar_tokens.json`` (gitignored).  Signed-in user must approve the
calendar scope; the Calendar API must be enabled for the Google project.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ._oauth import GoogleOAuthConnector, error_text

_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
_SCOPE = "https://www.googleapis.com/auth/calendar.events"


class CalendarClient(GoogleOAuthConnector):
    """Create and list events on the signed-in user's primary Google calendar."""

    id = "calendar"
    name = "Google Calendar (OAuth2)"
    _SCOPES = (_SCOPE,)

    def _env_tokens_path(self) -> Optional[str]:
        return os.environ.get("AP_CODEX_CALENDAR_TOKENS")

    def _default_tokens_name(self) -> str:
        return "calendar_tokens.json"

    # -------------------------------------------------------------- interface
    def discover_tools(self) -> List[Dict[str, Any]]:
        return [
            self._tool("create_event",
                       "Create a calendar event on the primary calendar. Optionally "
                       "attach an existing Google Meet space via conference_url so the "
                       "event shows a Meet link and schedules it on Meet.",
                       {"summary": {"type": "string", "description": "Event title."},
                        "start": {"type": "string",
                                  "description": "Start time in ISO 8601, e.g. 2026-09-15T09:00:00."},
                        "end": {"type": "string",
                                "description": "Optional end time in ISO 8601."},
                        "duration_min": {"type": "integer", "description": "Event length in minutes (default 60)."},
                        "description": {"type": "string", "description": "Optional details."},
                        "location": {"type": "string", "description": "Optional location."},
                        "attendees": {"type": "array",
                                      "items": {"type": "string"},
                                      "description": "Optional attendee emails."},
                        "conference_url": {"type": "string",
                                           "description": "Google Meet join URL to attach as the event video conference."},
                        "conference_code": {"type": "string",
                                            "description": "Google Meet code (e.g. abc-defg-hij) for the conference."}}),
            self._tool("list_events",
                       "List upcoming events on the primary calendar.",
                       {"time_min": {"type": "string", "description": "ISO 8601 lower bound (default now)."},
                        "time_max": {"type": "string", "description": "Optional ISO 8601 upper bound."},
                        "max_results": {"type": "integer", "description": "Maximum events (default 10)."}}),
        ]

    def call_function(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        if action == "list_events":
            return self.list_events(
                time_min=payload.get("time_min"),
                time_max=payload.get("time_max"),
                max_results=payload.get("max_results"),
            )
        if action == "create_event":
            return self.create_event(
                summary=payload.get("summary"),
                start=payload.get("start"),
                end=payload.get("end"),
                duration_min=payload.get("duration_min"),
                description=payload.get("description"),
                location=payload.get("location"),
                attendees=payload.get("attendees"),
                conference_url=payload.get("conference_url"),
                conference_code=payload.get("conference_code"),
            )
        return {"ok": False, "connector": self.id, "action": action,
                "error": f"calendar has no action '{action}'"}

    # ------------------------------------------------------------- actions
    def create_event(self, summary: Optional[str] = None, start: Optional[str] = None,
                     end: Optional[str] = None, duration_min: Optional[int] = None,
                     description: Optional[str] = None, location: Optional[str] = None,
                     attendees: Optional[List[Any]] = None,
                     conference_url: Optional[str] = None,
                     conference_code: Optional[str] = None) -> Dict[str, Any]:
        summary = str(summary or "").strip()
        if not summary:
            return {"ok": False, "connector": self.id, "action": "create_event",
                    "status": 422, "error": "event summary is required"}
        if not start:
            return {"ok": False, "connector": self.id, "action": "create_event",
                    "status": 422, "error": "event start time is required (ISO 8601)"}

        body: Dict[str, Any] = {"summary": summary}
        body["start"] = {"dateTime": _iso(start)}
        if end:
            body["end"] = {"dateTime": _iso(end)}
        else:
            duration = int(duration_min or 60)
            start_dt = datetime.fromisoformat(_iso(start))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            body["end"] = {"dateTime": (start_dt + timedelta(minutes=duration)).isoformat()}
        if description:
            body["description"] = str(description)
        if location:
            body["location"] = str(location)
        if attendees:
            body["attendees"] = [{"email": str(a)} for a in attendees]
        if conference_url:
            body["conferenceData"] = {
                "conferenceSolution": {"key": {"type": "hangoutsMeet"}, "name": "Google Meet"},
                "conferenceId": str(conference_code or "").strip() or _meeting_code(conference_url),
                "entryPoints": [
                    {"entryPointType": "video", "uri": str(conference_url),
                     "label": "Join with Google Meet"},
                ],
            }

        access_token = self._get_access_token()
        if not access_token:
            return {"ok": False, "connector": self.id, "action": "create_event", "status": 401,
                    "error": "Calendar not authorized. Open /connectors/calendar/authorize "
                             "in a browser to sign in."}

        try:
            status, data = self._request(
                "POST", f"{_CALENDAR_BASE}/calendars/primary/events",
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json=body,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "connector": self.id, "action": "create_event",
                    "status": 0, "error": f"network error: {exc}"}
        if status >= 400:
            return {"ok": False, "connector": self.id, "action": "create_event",
                    "status": status, "error": error_text(data)}
        entry_points = ((data or {}).get("conferenceData") or {}).get("entryPoints") or []
        return {"ok": True, "connector": self.id, "action": "create_event", "status": status,
                "data": {"event_id": (data or {}).get("id"), "summary": summary,
                         "start": (data or {}).get("start", {}).get("dateTime"),
                         "html_link": (data or {}).get("htmlLink"),
                         "conference_uri": (entry_points[0].get("uri") if entry_points else None),
                         "status": (data or {}).get("status")}}

    def list_events(self, time_min: Optional[str] = None, time_max: Optional[str] = None,
                    max_results: Optional[int] = None) -> Dict[str, Any]:
        access_token = self._get_access_token()
        if not access_token:
            return {"ok": False, "connector": self.id, "action": "list_events", "status": 401,
                    "error": "Calendar not authorized"}
        params: Dict[str, Any] = {
            "maxResults": int(max_results or 10),
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        if time_min:
            params["timeMin"] = _iso(time_min)
        if time_max:
            params["timeMax"] = _iso(time_max)
        try:
            status, data = self._request(
                "GET", f"{_CALENDAR_BASE}/calendars/primary/events",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "connector": self.id, "action": "list_events",
                    "status": 0, "error": f"network error: {exc}"}
        if status >= 400:
            return {"ok": False, "connector": self.id, "action": "list_events",
                    "status": status, "error": error_text(data)}
        items = (data or {}).get("items", []) or []
        events = [{
            "id": item.get("id"),
            "summary": item.get("summary"),
            "start": item.get("start", {}).get("dateTime") or item.get("start", {}).get("date"),
            "end": item.get("end", {}).get("dateTime") or item.get("end", {}).get("date"),
            "location": item.get("location"),
        } for item in items]
        return {"ok": True, "connector": self.id, "action": "list_events", "status": status,
                "data": {"count": len(events), "events": events}}


def _meeting_code(url: str) -> str:
    """Extract the Meet code from a join URL, e.g. abc-defg-hij."""
    return str(url).strip().rstrip("/").rsplit("/", 1)[-1]


def _iso(value: str) -> str:
    """Coerce a user/LLM timestamp into a numeric ISO 8601 string."""
    text = str(value).strip()
    if text.upper().endswith("Z"):
        return text.replace("Z", "+00:00")
    if "T" in text and not _has_tz(text) and text.endswith(":00"):
        return text + "+00:00"
    return text


def _has_tz(text: str) -> bool:
    return text.endswith(("Z", "+00:00", "+01:00", "+02:00", "+03:00", "+04:00",
                          "+05:00", "+05:30", "+06:00", "+07:00", "+08:00", "+09:00",
                          "+09:30", "+10:00", "+11:00", "+12:00", "-01:00", "-02:00",
                          "-03:00", "-04:00", "-05:00", "-06:00", "-07:00", "-08:00",
                          "-09:00", "-10:00", "-11:00", "-12:00"))