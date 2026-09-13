"""Tests for local sign-up/login + Google Sign-In auth routes."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.app import create_app  # noqa: E402
from api.auth import GoogleSignInConnector  # noqa: E402
from connectors._oauth import TOKEN_URL  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

_SIGNUP = {"name": "Ada Lovelace", "email": "Ada@Example.com", "password": "correct-horse"}
_SIGNIN_ERROR_TXT = "/?auth_error="


def _google_transport(status: int = 200, body=None):
    body = body or {"email": "ada@example.com", "name": "Ada Lovelace"}

    def transport(method, url, **kwargs):
        if url == TOKEN_URL:
            return 200, {"access_token": "test-access", "expires_in": 3600}
        if url == USERINFO_URL:
            return status, body
        return 404, {"error": "unexpected url"}

    return transport


class AuthTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.users_path = os.path.join(self._tmp.name, "users.json")
        signin = GoogleSignInConnector(
            client_id="test-client", client_secret="test-secret",
            tokens_path=os.path.join(self._tmp.name, "signin_tokens.json"),
            transport=_google_transport(),
        )
        self.app = create_app(llm=None, users_path=self.users_path,
                              signin=signin)
        self.client = TestClient(self.app)

    def _signup(self, **overrides):
        payload = dict(_SIGNUP, **overrides)
        return self.client.post("/auth/signup", json=payload)

    def test_signup_creates_user_and_normalizes_email(self):
        resp = self._signup()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["token"])
        self.assertEqual(body["user"]["email"], "ada@example.com")
        self.assertEqual(body["user"]["name"], "Ada Lovelace")
        self.assertEqual(body["user"]["source"], "local")
        self.assertFalse("password" in body["user"])
        with open(self.users_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["password_hash"].split("$")[0], "pbkdf2")
        self.assertNotEqual(saved[0]["password_hash"], "correct-horse")

    def test_signup_duplicate_email_rejected(self):
        self._signup()
        resp = self._signup()
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertIn("already exists", body["error"])

    def test_signup_short_password_rejected(self):
        resp = self._signup(password="short")
        self.assertEqual(resp.status_code, 422)

    def test_login_round_trip(self):
        self._signup()
        resp = self.client.post("/auth/login", json={
            "email": "ada@example.com", "password": "correct-horse",
        })
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["token"])
        self.assertEqual(body["user"]["email"], "ada@example.com")

    def test_login_wrong_password_rejected(self):
        self._signup()
        resp = self.client.post("/auth/login", json={
            "email": "ada@example.com", "password": "nope-nope",
        })
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertIsNone(body["token"])

    def test_me_requires_bearer_token(self):
        resp = self.client.get("/auth/me")
        self.assertFalse(resp.json()["ok"])

    def test_me_with_token(self):
        token = self._signup().json()["token"]
        resp = self.client.get("/auth/me", headers={"Authorization": "Bearer " + token})
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["user"]["email"], "ada@example.com")

    def test_logout_revokes_session(self):
        token = self._signup().json()["token"]
        resp = self.client.post("/auth/logout", headers={"Authorization": "Bearer " + token})
        self.assertTrue(resp.json()["ok"])
        resp = self.client.get("/auth/me", headers={"Authorization": "Bearer " + token})
        self.assertFalse(resp.json()["ok"])

    def test_google_authorize_redirects(self):
        resp = self.client.get("/auth/google/authorize", follow_redirects=False)
        self.assertEqual(resp.status_code, 307)
        self.assertTrue(resp.headers["location"].startswith("https://accounts.google.com/"))

    def test_google_callback_saves_user_and_sessions(self):
        resp = self.client.get("/auth/google/callback?code=abc", follow_redirects=False)
        self.assertEqual(resp.status_code, 307)
        location = resp.headers["location"]
        self.assertTrue(location.startswith("/?auth="))
        token = location.split("auth=")[1].split("&")[0]
        self.assertTrue(token)

        resp = self.client.get("/auth/me", headers={"Authorization": "Bearer " + token})
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["user"]["email"], "ada@example.com")
        self.assertEqual(body["user"]["name"], "Ada Lovelace")
        self.assertEqual(body["user"]["source"], "google")

        with open(self.users_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved[0]["email"], "ada@example.com")
        self.assertIsNone(saved[0]["password_hash"])

    def test_google_callback_error_redirects_home(self):
        resp = self.client.get("/auth/google/callback?code=bad", follow_redirects=False)
        self.assertEqual(resp.status_code, 307)
        self.assertTrue(resp.headers["location"].startswith(_SIGNIN_ERROR_TXT))


class GoogleSignInFailureCase(unittest.TestCase):
    """Userinfo endpoint fails -> still redirect back, no crash, no user saved."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        signin = GoogleSignInConnector(
            client_id="test-client", client_secret="test-secret",
            tokens_path=os.path.join(cls._tmp.name, "tokens.json"),
            transport=_google_transport(status=500),
        )
        cls.app = create_app(llm=None, users_path=os.path.join(cls._tmp.name, "users.json"),
                             signin=signin)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_userinfo_failure_redirects_without_crash(self):
        resp = self.client.get("/auth/google/callback?code=abc", follow_redirects=False)
        self.assertEqual(resp.status_code, 307)
        self.assertTrue(resp.headers["location"].startswith(_SIGNIN_ERROR_TXT))


if __name__ == "__main__":
    unittest.main()