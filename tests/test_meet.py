"""Tests for the Google Meet REST API connector (OAuth2, developer preview)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import unittest  # noqa: E402

from connectors import MeetClient  # noqa: E402

MEET_BASE = "https://meet.googleapis.com/v2"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class FakeTransport:
    def __init__(self, script=None):
        self._script = list(script or [])
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self._script:
            return 500, {"error": "unexpected request"}
        return self._script.pop(0)


class MeetConnectorTestCase(unittest.TestCase):
    def make_client(self, transport=None, base_dir=None, **kwargs):
        base_dir = base_dir or tempfile.mkdtemp()
        kwargs.setdefault("client_id", "client-id-123")
        kwargs.setdefault("client_secret", "client-secret-abc")
        if transport is not None:
            kwargs["transport"] = transport
        return MeetClient(base_dir=base_dir, **kwargs)

    def authorized_client(self, script):
        transport = FakeTransport([
            (200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
                   "scope": "https://www.googleapis.com/auth/meetings.space.created "
                            "https://www.googleapis.com/auth/meetings.space.readonly"})
        ] + script)
        client = self.make_client(transport)
        self.assertTrue(client.exchange_code("code", "http://localhost:8123/connectors/meet/callback")["ok"])
        return client, transport

    # ------------------------------------------------------------ oauth flow
    def test_authorization_url_has_meet_scopes(self):
        client = self.make_client()
        result = client.authorization_url("http://localhost:8123/connectors/meet/callback", "mt-st")
        self.assertTrue(result["ok"])
        self.assertIn("meetings.space.created", result["url"])
        self.assertIn("meetings.space.readonly", result["url"])
        self.assertIn("access_type=offline", result["url"])

    def test_create_space_requires_authorization(self):
        client = self.make_client()
        result = client.create_space(title="Standup")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 401)
        self.assertIn("meet/authorize", result["error"])

    def test_create_space_posts_payload(self):
        client, transport = self.authorized_client([
            (200, {"name": "spaces/AbCd123", "meetingUri": "https://meet.google.com/abc-defg-hij",
                   "meetingCode": "abc-defg-hij"})
        ])
        result = client.create_space(title="Client call")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["meeting_uri"], "https://meet.google.com/abc-defg-hij")
        self.assertEqual(result["data"]["meeting_code"], "abc-defg-hij")
        self.assertEqual(result["data"]["title"], "Client call")
        method, url, kwargs = transport.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{MEET_BASE}/spaces")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer AT")
        self.assertEqual(kwargs["json"], {})  # v2 spaces.create takes an empty body

    def test_get_space_normalizes_name(self):
        client, transport = self.authorized_client([
            (200, {"name": "spaces/AbCd123", "meetingUri": "https://meet.google.com/abc-defg-hij",
                   "meetingCode": "abc-defg-hij"})
        ])
        result = client.get_space("AbCd123")
        self.assertTrue(result["ok"])
        method, url, _ = transport.calls[-1]
        self.assertEqual(url, f"{MEET_BASE}/spaces/AbCd123")

    def test_get_space_requires_name(self):
        client = self.make_client()
        self.assertEqual(client.get_space("")["status"], 422)

    def test_list_conference_records(self):
        client, transport = self.authorized_client([
            (200, {"conferenceRecords": [
                {"name": "conferenceRecords/x", "startTime": "2026-09-14T09:00:00Z",
                 "endTime": "2026-09-14T09:30:00Z", "space": "spaces/AbCd123"}
            ]})
        ])
        result = client.list_conference_records(max_results=5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["count"], 1)
        self.assertEqual(result["data"]["records"][0]["space"], "spaces/AbCd123")
        method, url, kwargs = transport.calls[-1]
        self.assertEqual(method, "GET")
        self.assertEqual(url, f"{MEET_BASE}/conferenceRecords")
        self.assertEqual(kwargs["params"]["pageSize"], 5)

    def test_meet_api_preview_error_surfaces(self):
        client, _ = self.authorized_client([
            (403, {"error": {"message": "PERMISSION_DENIED: meet.googleapis.com is not enabled "
                                        "for this project"}})
        ])
        result = client.create_space(title="Standup")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 403)
        self.assertIn("PERMISSION_DENIED", result["error"])

    def test_tokens_persist_to_meet_file(self):
        transport = FakeTransport([
            (200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
                   "scope": "meetings.space.created"})
        ])
        base_dir = tempfile.mkdtemp()
        client = self.make_client(transport, base_dir=base_dir)
        client.exchange_code("code", "http://cb")
        self.assertTrue(Path(base_dir, "meet_tokens.json").exists())


if __name__ == "__main__":
    unittest.main()