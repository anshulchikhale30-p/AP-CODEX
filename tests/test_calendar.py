"""Tests for the Google Calendar OAuth2 connector."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import unittest  # noqa: E402

from connectors import CalendarClient  # noqa: E402

EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class FakeTransport:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self._script:
            return 500, {"error": "unexpected request"}
        return self._script.pop(0)


class CalendarConnectorTestCase(unittest.TestCase):
    def make_client(self, transport=None, base_dir=None, **kwargs):
        base_dir = base_dir or tempfile.mkdtemp()
        kwargs.setdefault("client_id", "client-id-123")
        kwargs.setdefault("client_secret", "client-secret-abc")
        if transport is not None:
            kwargs["transport"] = transport
        return CalendarClient(base_dir=base_dir, **kwargs)

    def authorized_client(self, script):
        transport = FakeTransport([
            (200, {"access_token": "AT", "refresh_token": "RT",
                   "expires_in": 3600, "scope": "https://www.googleapis.com/auth/calendar.events"})
        ] + script)
        client = self.make_client(transport)
        self.assertTrue(client.exchange_code("code", "http://localhost:8123/connectors/calendar/callback")["ok"])
        return client, transport

    # ------------------------------------------------------------ oauth flow
    def test_authorization_url_has_calendar_scope(self):
        client = self.make_client()
        result = client.authorization_url("http://localhost:8123/connectors/calendar/callback", "st")
        self.assertTrue(result["ok"])
        self.assertIn("calendar.events", result["url"])
        self.assertIn("access_type=offline", result["url"])

    def test_create_event_requires_authorization(self):
        client = self.make_client()
        result = client.create_event(summary="Standup", start="2026-09-15T09:00:00")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 401)

    def test_create_event_posts_payload(self):
        client, transport = self.authorized_client([
            (200, {"id": "evt-1", "summary": "Standup",
                   "start": {"dateTime": "2026-09-15T09:00:00Z"}, "status": "confirmed",
                   "htmlLink": "https://calendar.google.com/event/evt-1"})
        ])
        result = client.create_event(
            summary="Team standup", start="2026-09-15T09:00:00", duration_min=30,
            location="Zoom", attendees=["alice@example.com", "bob@example.com"],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["event_id"], "evt-1")
        method, url, kwargs = transport.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(url, EVENTS_URL)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer AT")
        body = kwargs["json"]
        self.assertEqual(body["summary"], "Team standup")
        self.assertEqual(body["location"], "Zoom")
        self.assertEqual(body["attendees"], [{"email": "alice@example.com"}, {"email": "bob@example.com"}])
        self.assertEqual(body["start"]["dateTime"], "2026-09-15T09:00:00+00:00")
        self.assertEqual(body["end"]["dateTime"], "2026-09-15T09:30:00+00:00")

    def test_rejects_missing_summary_or_start(self):
        client = self.make_client()
        self.assertEqual(client.create_event(start="2026-09-15T09:00:00")["status"], 422)
        self.assertEqual(client.create_event(summary="x")["status"], 422)

    def test_create_event_attaches_meet_conference(self):
        client, transport = self.authorized_client([
            (200, {"id": "evt-m1", "summary": "Client call",
                   "start": {"dateTime": "2026-09-16T04:30:00Z"},
                   "conferenceData": {"entryPoints": [
                       {"uri": "https://meet.google.com/abc-defg-hij"}]}})
        ])
        result = client.create_event(
            summary="Client call", start="2026-09-16T04:30:00", duration_min=30,
            conference_url="https://meet.google.com/abc-defg-hij",
            conference_code="abc-defg-hij",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["conference_uri"], "https://meet.google.com/abc-defg-hij")
        method, url, kwargs = transport.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(url, EVENTS_URL)
        conf = kwargs["json"]["conferenceData"]
        self.assertEqual(conf["conferenceId"], "abc-defg-hij")
        self.assertEqual(conf["conferenceSolution"]["key"]["type"], "hangoutsMeet")
        self.assertEqual(conf["entryPoints"][0]["entryPointType"], "video")
        self.assertEqual(conf["entryPoints"][0]["uri"], "https://meet.google.com/abc-defg-hij")

    def test_conference_code_inferred_from_url(self):
        client, transport = self.authorized_client([
            (200, {"id": "evt-m2", "summary": "Standup",
                   "start": {"dateTime": "2026-09-16T04:30:00Z"}})
        ])
        result = client.create_event(
            summary="Standup", start="2026-09-16T04:30:00",
            conference_url="https://meet.google.com/xyz-pqr-stu",
        )
        self.assertTrue(result["ok"])
        conf = transport.calls[-1][2]["json"]["conferenceData"]
        self.assertEqual(conf["conferenceId"], "xyz-pqr-stu")

    def test_list_events(self):
        events = [{"id": "e1", "summary": "Standup", "status": "confirmed",
                   "start": {"dateTime": "2026-09-15T09:00:00Z"},
                   "end": {"dateTime": "2026-09-15T09:30:00Z"}}]
        client, transport = self.authorized_client([(200, {"items": events})])
        result = client.list_events(time_min="2026-09-15T00:00:00", max_results=5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["count"], 1)
        self.assertEqual(result["data"]["events"][0]["summary"], "Standup")
        method, url, kwargs = transport.calls[-1]
        self.assertEqual(method, "GET")
        self.assertEqual(url, EVENTS_URL)
        self.assertEqual(kwargs["params"]["maxResults"], 5)
        self.assertIn("timeMin", kwargs["params"])

    def test_calendar_api_error_surfaces(self):
        client, _ = self.authorized_client([
            (403, {"error": {"message": "Calendar API is not enabled"}})
        ])
        result = client.create_event(summary="Standup", start="2026-09-15T09:00:00")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 403)
        self.assertIn("Calendar API is not enabled", result["error"])

    def test_tokens_persist_to_calendar_file(self):
        transport = FakeTransport([
            (200, {"access_token": "AT", "refresh_token": "RT",
                   "expires_in": 3600, "scope": "calendar.events"})
        ])
        base_dir = tempfile.mkdtemp()
        client = self.make_client(transport, base_dir=base_dir)
        client.exchange_code("code", "http://cb")
        self.assertTrue(Path(base_dir, "calendar_tokens.json").exists())
        tokens = json.loads(Path(base_dir, "calendar_tokens.json").read_text(encoding="utf-8"))
        self.assertEqual(tokens["access_token"], "AT")
        self.assertEqual(tokens["refresh_token"], "RT")


if __name__ == "__main__":
    unittest.main()