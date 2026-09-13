"""Tests for the Twitter (X) OAuth 1.0a connector."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import unittest  # noqa: E402

from connectors import TwitterClient  # noqa: E402
from connectors.twitter import _enc  # noqa: E402

CREDS = {
    "api_key": "consumer-key",
    "api_secret": "consumer-secret",
    "access_token": "user-token",
    "access_token_secret": "user-token-secret",
}


class FakeTransport:
    def __init__(self, script=None):
        self._script = list(script or [])
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self._script:
            return 500, {"error": "unexpected request"}
        return self._script.pop(0)


class TwitterTestCase(unittest.TestCase):
    def make_client(self, transport=None, creds=None, **kwargs):
        base_dir = tempfile.mkdtemp()
        kwargs.setdefault("transport", transport)
        client = TwitterClient(base_dir=base_dir, **kwargs)
        c = creds or dict(CREDS)
        client._api_key = c["api_key"]
        client._api_secret = c["api_secret"]
        client._access_token = c["access_token"]
        client._access_token_secret = c["access_token_secret"]
        return client

    def verify_signature(self, method, url, header, params=None):
        """Independently re-derive the OAuth signature the connector sent."""
        fields = dict(item.split("=", 1) for item in header[len("OAuth "):].split(", "))
        oauth = {k: _unquote(v).strip('"') for k, v in fields.items()}
        parsed = urlsplit(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        combined = {k: v for k, v in oauth.items() if k != "oauth_signature"}
        combined.update(params or {})
        pairs = sorted((_enc(k), _enc(v)) for k, v in combined.items())
        normalized = "&".join(f"{k}={v}" for k, v in pairs)
        base_string = "&".join([method.upper(), _enc(base_url), _enc(normalized)])
        key = f"{_enc(CREDS['api_secret'])}&{_enc(CREDS['access_token_secret'])}"
        expected = base64.b64encode(
            hmac.new(key.encode(), base_string.encode(), hashlib.sha1).digest()
        ).decode()
        self.assertEqual(oauth["oauth_signature"], expected)
        return oauth

    # ------------------------------------------------------------ setup paths
    def test_loads_credentials_from_file(self):
        base_dir = tempfile.mkdtemp()
        Path(base_dir, "twitter_credentials.json").write_text(json.dumps(CREDS), encoding="utf-8")
        client = TwitterClient(base_dir=base_dir)
        self.assertTrue(client.authorized)

    def test_missing_credentials_tool_errors(self):
        client = TwitterClient(base_dir=tempfile.mkdtemp())
        result = client.me()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 401)
        self.assertIn("twitter_credentials.json", result["error"])

    # ---------------------------------------------------------- readiness
    def test_oauth1a_access_tokens_make_client_ready(self):
        base_dir = tempfile.mkdtemp()
        Path(base_dir, "twitter_credentials.json").write_text(json.dumps(CREDS), encoding="utf-8")
        client = TwitterClient(base_dir=base_dir)
        self.assertTrue(client.can_post)
        self.assertEqual(client.status()["data"]["oauth1a_user_context"], True)
        self.assertEqual(client.status()["data"]["can_post"], True)

    def test_status_bearer_only_not_ready(self):
        client = self.make_bearer_client()
        status = client.status()["data"]
        self.assertTrue(status["has_bearer_token"])
        self.assertFalse(status["can_post"])
        self.assertIn("access_token", status["hint"])

    def test_status_does_not_leak_secrets(self):
        client = self.make_client()
        flat = json.dumps(client.status())
        for secret in ("consumer-key", "consumer-secret", "user-token", "user-token-secret"):
            self.assertNotIn(secret, flat)

    def test_status_tool_registered(self):
        client = self.make_client()
        names = [t["function"]["name"] for t in client.discover_tools()]
        self.assertIn("status", names)
        result = client.call_function("status")
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "status")

    # ------------------------------------------------------------- signing
    def test_authorization_signature_is_valid(self):
        transport = FakeTransport([(200, {"data": {"id": "1", "name": "N", "username": "u"}})])
        client = self.make_client(transport)
        result = client.me()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["username"], "u")
        method, url, kwargs = transport.calls[0]
        oauth = self.verify_signature(method, url, kwargs["headers"]["Authorization"],
                                      params={"user.fields": "id,name,username,verified,public_metrics"})
        self.assertEqual(oauth["oauth_consumer_key"], "consumer-key")
        self.assertEqual(oauth["oauth_token"], "user-token")
        self.assertEqual(oauth["oauth_signature_method"], "HMAC-SHA1")

    def test_post_tweet_signs_json_post(self):
        transport = FakeTransport([(201, {"data": {"id": "tid1", "text": "hello world"}})])
        client = self.make_client(transport)
        result = client.post_tweet("hello world")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["tweet_id"], "tid1")
        method, url, kwargs = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.twitter.com/2/tweets")
        self.assertEqual(kwargs["json"], {"text": "hello world"})
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.verify_signature(method, url, kwargs["headers"]["Authorization"])

    def test_post_tweet_validation(self):
        client = self.make_client()
        self.assertEqual(client.post_tweet("")["status"], 422)
        self.assertEqual(client.post_tweet("x" * 281)["status"], 422)

    def test_api_error_surfaces(self):
        transport = FakeTransport([(403, {"errors": [{"message": "You are not allowed to tweet"}]})])
        client = self.make_client(transport)
        result = client.post_tweet("hello")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 403)

    def test_my_tweets(self):
        transport = FakeTransport([
            (200, {"data": {"id": "u1", "name": "N", "username": "u"}}),
            (200, {"data": [{"id": "t1", "text": "first",
                             "created_at": "2026-01-01T00:00:00Z",
                             "public_metrics": {"like_count": 3}}]}),
        ])
        client = self.make_client(transport)
        result = client.my_tweets(max_results=5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["count"], 1)
        self.assertEqual(result["data"]["tweets"][0]["text"], "first")
        send_url = transport.calls[1][1]
        self.assertIn("/users/u1/tweets", send_url)

    # ---------------------------------------------------------- Bearer path
    def make_bearer_client(self, transport=None, base_dir=None):
        base_dir = base_dir or tempfile.mkdtemp()
        client = TwitterClient(base_dir=base_dir, transport=transport,
                               bearer_token="bt-123")
        return client

    def test_bearer_only_cannot_post(self):
        transport = FakeTransport()
        client = self.make_bearer_client(transport)
        self.assertTrue(client.authorized)
        self.assertFalse(client.can_post)
        result = client.post_tweet("hi")
        self.assertEqual(result["status"], 401)
        self.assertIn("access_token", result["error"])
        self.assertEqual(transport.calls, [])  # no request attempted

    def test_user_by_username_uses_bearer(self):
        transport = FakeTransport([
            (200, {"data": {"id": "9001", "username": "devrel",
                            "public_metrics": {"followers_count": 42}}})
        ])
        client = self.make_bearer_client(transport)
        result = client.user_by_username("@devrel")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["id"], "9001")
        method, url, kwargs = transport.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer bt-123")
        self.assertIn("devrel", url)

    def test_user_tweets_chains_lookup_then_list(self):
        transport = FakeTransport([
            (200, {"data": {"id": "9001", "username": "devrel"}}),
            (200, {"data": [{"id": "t9", "text": "shine bright"}]}),
        ])
        client = self.make_bearer_client(transport)
        result = client.user_tweets("devrel", max_results=3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["count"], 1)
        self.assertIn("/users/9001/tweets", transport.calls[1][1])
        self.assertEqual(transport.calls[1][2]["params"]["max_results"], 3)

    def test_bearer_token_url_decode(self):
        client = TwitterClient(base_dir=tempfile.mkdtemp(),
                               bearer_token="a%2Fb%3Dc")
        self.assertEqual(client._bearer_token, "a/b=c")


def _unquote(value: str) -> str:
    from urllib.parse import unquote
    return unquote(value)


if __name__ == "__main__":
    unittest.main()