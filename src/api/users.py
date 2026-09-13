"""User account persistence + in-memory sessions for AP-CODEX.

Users are stored as a JSON array in ``users.json`` (gitignored — it holds
password hashes).  Passwords are never stored in plain text: each one is
salted and stretched with PBKDF2-HMAC-SHA256 via the stdlib, so no extra
runtime dependencies are required.

A user record looks like::

    {
      "email": "ada@example.com",
      "name": "Ada Lovelace",
      "password_hash": "pbkdf2$sha256$120000$<salt>|<digest>",
      "source": "google" | "local",
      "created_at": "2026-09-14T12:00:00Z"
    }

Google sign-in users have an empty ``password_hash`` and ``source == "google"``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_ITERATIONS = 120_000
_ALGO = "sha256"


class UserExistsError(Exception):
    """Raised when an email is already registered."""


class UserStore:
    """Load/save the ``users.json`` file and verify credentials."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or "users.json"
        self._lock = threading.Lock()
        self._users: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for user in data:
                email = str(user.get("email", "")).strip().lower()
                if email:
                    self._users[email] = user
        except (OSError, ValueError):
            self._users = {}

    def _save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(list(self._users.values()), fh,
                              ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            except OSError:
                logger.warning("could not persist users to %s", self.path)

    # ---------------------------------------------------------------- public
    def find(self, email: str) -> Optional[Dict[str, Any]]:
        return self._users.get(norm_email(email))

    def create_local(self, email: str, name: str, password: str) -> Dict[str, Any]:
        """Create a user from the sign-up form; raises :class:`UserExistsError`."""
        key = norm_email(email)
        with self._lock:
            if key in self._users:
                raise UserExistsError("an account with that email already exists")
            user = {
                "email": key,
                "name": name.strip() or key.split("@")[0],
                "password_hash": hash_password(password),
                "source": "local",
                "created_at": now_iso(),
            }
            self._users[key] = user
        self._save()
        return public_user(user)

    def create_or_update_google(self, email: str, name: str) -> Dict[str, Any]:
        """Create a Google user, or merge the Google profile into an existing one."""
        key = norm_email(email)
        with self._lock:
            user = self._users.get(key)
            if user is None:
                user = {
                    "email": key,
                    "name": (name or "").strip() or key.split("@")[0],
                    "password_hash": None,
                    "source": "google",
                    "created_at": now_iso(),
                }
                self._users[key] = user
            else:
                if (name or "").strip():
                    user["name"] = name.strip()
                user["source"] = "google"
        self._save()
        return public_user(user)

    def verify_local(self, email: str, password: str) -> Optional[Dict[str, Any]]:
        """Return the public user if ``email``/``password`` match, else ``None``."""
        user = self._users.get(norm_email(email))
        if not user or not user.get("password_hash"):
            return None
        if not verify_password(password, user["password_hash"]):
            return None
        return public_user(user)


# --------------------------------------------------------------- sessions
class SessionStore:
    """In-memory bearer-token sessions (``token -> email``)."""

    def __init__(self):
        self._tokens: Dict[str, str] = {}
        self._lock = threading.Lock()

    def create(self, email: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[token] = norm_email(email)
        return token

    def resolve(self, token: str) -> Optional[str]:
        if not token:
            return None
        with self._lock:
            return self._tokens.get(token)

    def revoke(self, token: str) -> None:
        if not token:
            return
        with self._lock:
            self._tokens.pop(token, None)


# ----------------------------------------------------------- password hashing
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        _ALGO, password.encode("utf-8"), salt, _ITERATIONS
    )
    return "pbkdf2${}${}${}${}".format(
        _ALGO,
        _ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, algo, iterations, salt_b64, digest_b64 = stored.split("$")
        if scheme != "pbkdf2":
            return False
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(digest_b64)
        digest = hashlib.pbkdf2_hmac(
            algo, password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------------ helpers
def norm_email(email: str) -> str:
    return str(email or "").strip().lower()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    """The fields safe to return to the client (never password hashes)."""
    return {
        "email": user.get("email", ""),
        "name": user.get("name", ""),
        "source": user.get("source", "local"),
        "created_at": user.get("created_at", ""),
    }