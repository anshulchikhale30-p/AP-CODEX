"""Tests for the Gmail OAuth2 connector."""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
from email import message_from_bytes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import unittest  # noqa: E402

from connectors import GmailClient  # noqa: E402

TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


def _pad(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


class FakeTransport:
    """Serves a scripted sequence of (status, json) responses, recording calls."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self._script:
            return 500, {"error": "unexpected request"}
        return self._script.pop(0)


class GmailConnectorTestCase(unittest.TestCase):
    def make_client(self, transport=None, base_dir=None, **kwargs):
        base_dir = base_dir or tempfile.mkdtemp()
        kwargs.setdefault("client_id", "client-id-123")
        kwargs.setdefault("client_secret", "client-secret-abc")
        kwargs.setdefault("transport", transport)
        return GmailClient(base_dir=base_dir, **kwargs)

    # ------------------------------------------------------------ oauth flow
    def test_authorization_url_builds_consent_link(self):
        client = self.make_client()
        result = client.authorization_url("http://localhost:8123/connectors/gmail/callback", "st8")
        self.assertTrue(result["ok"])
        url = result["url"]
        self.assertIn("client_id=client-id-123", url)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A8123%2Fconnectors%2Fgmail%2Fcallback", url)
        self.assertIn("scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.send", url)
        self.assertIn("access_type=offline", url)
        self.assertIn("state=st8", url)

    def test_authorization_url_requires_credentials(self):
        client = GmailClient(base_dir=tempfile.mkdtemp())
        result = client.authorization_url("http://localhost/cb")
        self.assertFalse(result["ok"])
        self.assertIn("client_id", result["error"])

    # ------------------------------------------------------------ send gating
    def test_send_without_authorization_returns_401(self):
        client = self.make_client()
        result = client.send(to="a@b.com", subject="hi", body="late")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 401)
        self.assertIn("authorize", result["error"])

    # ------------------------------------------------- exchange + real send
    def test_exchange_code_then_send(self):
        transport = FakeTransport([
            (200, {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600,
                   "scope": "https://www.googleapis.com/auth/gmail.send"}),
            (200, {"id": "gmsg-9", "threadId": "thr-9"}),
        ])
        client = self.make_client(transport)

        exchanged = client.exchange_code("auth-code", "http://localhost:8123/connectors/gmail/callback")
        self.assertTrue(exchanged["ok"])
        self.assertTrue(client.authorized)
        self.assertTrue(Path(client._tokens_path).exists())

        sent = client.send(to="manager@example.com", subject="Running late", body="See you soon.")
        self.assertTrue(sent["ok"])
        self.assertEqual(sent["data"]["message_id"], "gmsg-9")
        self.assertTrue(sent["data"]["gmail_thread"])

        token_call = transport.calls[0]
        self.assertEqual(token_call[0], "POST")
        self.assertEqual(token_call[1], TOKEN_URL)
        self.assertEqual(token_call[2]["data"]["code"], "auth-code")
        self.assertEqual(token_call[2]["data"]["grant_type"], "authorization_code")

        send_call = transport.calls[1]
        self.assertEqual(send_call[1], SEND_URL)
        self.assertEqual(send_call[2]["headers"]["Authorization"], "Bearer AT1")
        raw = send_call[2]["json"]["raw"]
        message = message_from_bytes(_pad(raw))
        self.assertEqual(message["To"], "manager@example.com")
        self.assertEqual(message["Subject"], "Running late")
        self.assertIn("See you soon.", message.get_payload())

    # --------------------------------------------------- refresh on expiry
    def test_refreshes_expired_token(self):
        base_dir = tempfile.mkdtemp()
        Path(base_dir, "gmail_tokens.json").write_text(json.dumps({
            "access_token": "OLD", "refresh_token": "RTZ", "expires_at": time.time() - 100,
        }), encoding="utf-8")
        transport = FakeTransport([
            (200, {"access_token": "NEW", "expires_in": 3600}),
            (200, {"id": "gmsg-2", "threadId": "thr-2"}),
        ])
        client = self.make_client(transport, base_dir=base_dir)

        sent = client.send(to="a@b.com", subject="s", body="b")
        self.assertTrue(sent["ok"])
        refresh_call = transport.calls[0]
        self.assertEqual(refresh_call[2]["data"]["grant_type"], "refresh_token")
        self.assertEqual(refresh_call[2]["data"]["refresh_token"], "RTZ")
        send_call = transport.calls[1]
        self.assertEqual(send_call[2]["headers"]["Authorization"], "Bearer NEW")
        tokens = json.loads(Path(base_dir, "gmail_tokens.json").read_text(encoding="utf-8"))
        self.assertEqual(tokens["access_token"], "NEW")
        self.assertEqual(tokens["refresh_token"], "RTZ")

    # ---------------------------------------------------------- error paths
    def test_gmail_api_error_surfaces(self):
        transport = FakeTransport([
            (200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}),
            (403, {"error": {"message": "Insufficient Permission"}}),
        ])
        client = self.make_client(transport)
        client.exchange_code("code", "http://localhost/cb")
        sent = client.send(to="a@b.com", subject="s", body="b")
        self.assertFalse(sent["ok"])
        self.assertEqual(sent["status"], 403)
        self.assertIn("Insufficient Permission", sent["error"])

    def test_missing_credentials_exchange_fails_cleanly(self):
        client = GmailClient(base_dir=tempfile.mkdtemp())
        result = client.exchange_code("code", "http://localhost/cb")
        self.assertFalse(result["ok"])
        self.assertIn("client_id", result["error"])

    def test_profile_unauthorized(self):
        client = self.make_client()
        result = client.profile()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 401)


if __name__ == "__main__":
    unittest.main()