"""Twitter (X) connector for AP-CODEX — OAuth 1.0a user-context.

Signs requests with the app's consumer key/secret plus the user's access
token/secret (HMAC-SHA1).  Credentials come from ``twitter_credentials.json``
or the ``AP_CODEX_TWITTER_*`` env vars; the file is gitignored.

Requires an X app with read + write (Tweet) permissions and, for posting, a
paid tier that allows create Tweet.  Actions never raise: every call returns
a standard ``{"ok": bool, ...}`` result.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://api.twitter.com/2"
_DEFAULT_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TwitterClient:
    """Post tweets and read the authenticated user's Twitter data."""

    id = "twitter"
    name = "Twitter / X (OAuth 1.0a)"

    def __init__(
        self,
        *,
        base_dir: Optional[str] = None,
        credentials_path: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        bearer_token: Optional[str] = None,
        access_token: Optional[str] = None,
        access_token_secret: Optional[str] = None,
        transport=None,
    ):
        env = os.environ
        self._base_dir = base_dir or env.get("AP_CODEX_TWITTER_DIR") or _DEFAULT_BASE
        self._credentials_path = (
            credentials_path
            or env.get("AP_CODEX_TWITTER_CREDENTIALS")
            or os.path.join(self._base_dir, "twitter_credentials.json")
        )
        self._transport = transport
        self._api_key = api_key or env.get("AP_CODEX_TWITTER_API_KEY")
        self._api_secret = api_secret or env.get("AP_CODEX_TWITTER_API_SECRET")
        self._bearer_token = bearer_token or env.get("AP_CODEX_TWITTER_BEARER_TOKEN")
        self._access_token = access_token or env.get("AP_CODEX_TWITTER_ACCESS_TOKEN")
        self._access_token_secret = access_token_secret or env.get("AP_CODEX_TWITTER_ACCESS_SECRET")
        if not (self._configured or self._bearer_token):
            credentials = self._load_credentials()
            if credentials:
                self._api_key = self._api_key or credentials.get("api_key")
                self._api_secret = self._api_secret or credentials.get("api_secret")
                self._bearer_token = self._bearer_token or credentials.get("bearer_token")
                self._access_token = self._access_token or credentials.get("access_token")
                self._access_token_secret = (
                    self._access_token_secret or credentials.get("access_token_secret")
                )
        try:
            self._bearer_token = (self._bearer_token or "").strip().replace("%2F", "/").replace("%3D", "=")
        except AttributeError:
            self._bearer_token = None
        self._my_user_id: Optional[str] = None

    # ------------------------------------------------------------ properties
    @property
    def _configured(self) -> bool:
        return bool(self._api_key and self._api_secret
                    and self._access_token and self._access_token_secret)

    @property
    def authorized(self) -> bool:
        return bool(self._configured or self._bearer_token)

    @property
    def can_post(self) -> bool:
        return self._configured

    @property
    def authorised(self) -> bool:  # British spelling, mirrors `authorized`
        return self.authorized

    # -------------------------------------------------------------- interface
    def discover_tools(self) -> List[Dict[str, Any]]:
        return [
            self._tool("post_tweet", "Post a new tweet to the authenticated account.",
                       {"text": {"type": "string", "description": "Tweet text (max 280 chars)."}}),
            self._tool("me", "Show the authenticated Twitter user (id, name, username).", {}),
            self._tool("my_tweets", "List the authenticated user's most recent tweets.",
                       {"max_results": {"type": "integer", "description": "Max tweets (default 10)."}}),
            self._tool("user_by_username", "Look up a public user by username (app-only read).",
                       {"username": {"type": "string", "description": "Screen name without @."}}),
            self._tool("user_tweets", "List a public user's recent tweets (app-only read).",
                       {"username": {"type": "string", "description": "Screen name without @."},
                        "max_results": {"type": "integer", "description": "Max tweets (default 10)."}}),
            self._tool("status", "Report X API readiness (does it have user-context tokens + bearer).", {}),
        ]

    def call_function(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        if action == "post_tweet":
            return self.post_tweet(text=payload.get("text"))
        if action == "me":
            return self.me()
        if action == "my_tweets":
            return self.my_tweets(max_results=payload.get("max_results"))
        if action == "user_by_username":
            return self.user_by_username(username=payload.get("username"))
        if action == "user_tweets":
            return self.user_tweets(username=payload.get("username"),
                                    max_results=payload.get("max_results"))
        if action == "status":
            return self.status()
        return {"ok": False, "connector": self.id, "action": action,
                "error": f"twitter has no action '{action}'"}

    # ------------------------------------------------------------- actions
    def post_tweet(self, text: Optional[str] = None) -> Dict[str, Any]:
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "connector": self.id, "action": "post_tweet",
                    "status": 422, "error": "tweet text is empty"}
        if len(text) > 280:
            return {"ok": False, "connector": self.id, "action": "post_tweet",
                    "status": 422, "error": f"tweet text is {len(text)} chars (max 280)"}
        status, data = self._signed_request("POST", f"{_API_BASE}/tweets", json={"text": text})
        if status >= 400:
            return {"ok": False, "connector": self.id, "action": "post_tweet",
                    "status": status, "error": _error_text(data)}
        tweet = (data or {}).get("data") or {}
        return {"ok": True, "connector": self.id, "action": "post_tweet", "status": status,
                "data": {"tweet_id": tweet.get("id"), "text": tweet.get("text")}}

    def me(self) -> Dict[str, Any]:
        status, data = self._signed_request(
            "GET", f"{_API_BASE}/users/me",
            params={"user.fields": "id,name,username,verified,public_metrics"},
        )
        if status >= 400:
            return {"ok": False, "connector": self.id, "action": "me",
                    "status": status, "error": _error_text(data)}
        user = (data or {}).get("data") or {}
        self._my_user_id = user.get("id") or self._my_user_id
        return {"ok": True, "connector": self.id, "action": "me", "status": status,
                "data": {"id": user.get("id"), "name": user.get("name"),
                         "username": user.get("username"), "verified": user.get("verified"),
                         "followers_count": ((user.get("public_metrics") or {}).get("followers_count"))}}

    def my_tweets(self, max_results: Optional[int] = None) -> Dict[str, Any]:
        user_id = self._my_user_id
        if not user_id:
            profile = self.me()
            if not profile.get("ok"):
                return profile
            user_id = self._my_user_id
        status, data = self._signed_request(
            "GET", f"{_API_BASE}/users/{user_id}/tweets",
            params={"max_results": int(max_results or 10),
                    "tweet.fields": "created_at,public_metrics"},
        )
        if status >= 400:
            return {"ok": False, "connector": self.id, "action": "my_tweets",
                    "status": status, "error": _error_text(data)}
        items = (data or {}).get("data") or []
        tweets = [{"id": t.get("id"), "text": t.get("text"),
                   "created_at": t.get("created_at"),
                   "likes": (t.get("public_metrics") or {}).get("like_count")}
                  for t in items]
        return {"ok": True, "connector": self.id, "action": "my_tweets", "status": status,
                "data": {"count": len(tweets), "tweets": tweets}}

    # -------------------------------------------- app-only (Bearer) read
    def user_by_username(self, username: Optional[str] = None) -> Dict[str, Any]:
        username = str(username or "").strip().lstrip("@")
        if not username:
            return {"ok": False, "connector": self.id, "action": "user_by_username",
                    "status": 422, "error": "username is required"}
        status, data = self._bearer_request(
            "GET", f"{_API_BASE}/users/by/username/{username}",
            params={"user.fields": "id,name,username,verified,public_metrics"},
        )
        if status >= 400:
            return {"ok": False, "connector": self.id, "action": "user_by_username",
                    "status": status, "error": _error_text(data)}
        user = (data or {}).get("data") or {}
        return {"ok": True, "connector": self.id, "action": "user_by_username", "status": status,
                "data": {"id": user.get("id"), "name": user.get("name"),
                         "username": user.get("username"), "verified": user.get("verified"),
                         "followers_count": ((user.get("public_metrics") or {}).get("followers_count"))}}

    def user_tweets(self, username: Optional[str] = None, max_results: Optional[int] = None) -> Dict[str, Any]:
        username = str(username or "").strip().lstrip("@")
        if not username:
            return {"ok": False, "connector": self.id, "action": "user_tweets",
                    "status": 422, "error": "username is required"}
        lookup = self.user_by_username(username)
        if not lookup.get("ok"):
            return lookup
        user_id = (lookup.get("data") or {}).get("id")
        status, data = self._bearer_request(
            "GET", f"{_API_BASE}/users/{user_id}/tweets",
            params={"max_results": int(max_results or 10),
                    "tweet.fields": "created_at,public_metrics"},
        )
        if status >= 400:
            return {"ok": False, "connector": self.id, "action": "user_tweets",
                    "status": status, "error": _error_text(data)}
        items = (data or {}).get("data") or []
        tweets = [{"id": t.get("id"), "text": t.get("text"),
                   "created_at": t.get("created_at"),
                   "likes": (t.get("public_metrics") or {}).get("like_count")}
                  for t in items]
        return {"ok": True, "connector": self.id, "action": "user_tweets", "status": status,
                "data": {"username": username, "count": len(tweets), "tweets": tweets}}

    def status(self) -> Dict[str, Any]:
        """Readiness report — never exposes any credential values."""
        return {"ok": True, "connector": self.id, "action": "status",
                "data": {
                    "can_post": self._configured,
                    "has_bearer_token": bool(self._bearer_token),
                    "api_key_set": bool(self._api_key),
                    "oauth1a_user_context": self._configured,
                    "hint": ("Add access_token and access_token_secret to "
                             "twitter_credentials.json (Keys and tokens tab) and restart "
                             "the server to post.") if not self._configured else "Ready to post.",
                }}

    # ----------------------------------------------------- OAuth 1.0a signing
    def _signed_request(self, method: str, url: str, params: Optional[Dict[str, Any]] = None,
                        json: Optional[Dict[str, Any]] = None):
        if not self._configured:
            return 401, {"error": "Twitter user context not configured. Fill "
                                  "twitter_credentials.json with access_token and "
                                  "access_token_secret (or set AP_CODEX_TWITTER_ACCESS_TOKEN "
                                  "/ AP_CODEX_TWITTER_ACCESS_SECRET) to post and read your own account."}
        header = self._oauth_authorization(method, url, params or {})
        kwargs: Dict[str, Any] = {"headers": {"Authorization": header}}
        if json is not None:
            kwargs["headers"]["Content-Type"] = "application/json"
            kwargs["json"] = json
        if params:
            kwargs["params"] = params
        try:
            if self._transport is not None:
                return self._transport(method, url, **kwargs)
            with httpx.Client(timeout=15) as client:
                response = client.request(method, url, **kwargs)
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                return response.status_code, body
        except Exception as exc:  # noqa: BLE001 - never crash the agent loop
            return 0, {"error": f"network error: {exc}"}

    def _bearer_request(self, method: str, url: str, params: Optional[Dict[str, Any]] = None):
        if not self._bearer_token:
            return 401, {"error": "Twitter bearer token not configured. Fill "
                                  "twitter_credentials.json with bearer_token (or set "
                                  "AP_CODEX_TWITTER_BEARER_TOKEN)."}
        kwargs: Dict[str, Any] = {"headers": {"Authorization": f"Bearer {self._bearer_token}"}}
        if params:
            kwargs["params"] = params
        try:
            if self._transport is not None:
                return self._transport(method, url, **kwargs)
            with httpx.Client(timeout=15) as client:
                response = client.request(method, url, **kwargs)
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                return response.status_code, body
        except Exception as exc:  # noqa: BLE001 - never crash the agent loop
            return 0, {"error": f"network error: {exc}"}

    def _oauth_authorization(self, method: str, url: str, params: Dict[str, Any]) -> str:
        oauth = {
            "oauth_consumer_key": self._api_key,
            "oauth_nonce": secrets.token_hex(8),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": self._access_token,
            "oauth_version": "1.0",
        }
        parsed = urlsplit(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        combined = dict(oauth)
        combined.update(params or {})
        normalized = self._normalize_params(combined)
        base_string = "&".join([method.upper(), _enc(base_url), _enc(normalized)])
        signing_key = f"{_enc(self._api_secret)}&{_enc(self._access_token_secret)}"
        signature = base64.b64encode(
            hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
        ).decode()
        parts = [(k, v) for k, v in oauth.items()]
        parts.append(("oauth_signature", signature))
        return "OAuth " + ", ".join(f'{k}="{_enc(v)}"' for k, v in parts if v)

    @staticmethod
    def _normalize_params(params: Dict[str, Any]) -> str:
        pairs = sorted((_enc(str(k)), _enc(str(v))) for k, v in params.items())
        return "&".join(f"{k}={v}" for k, v in pairs)

    # ------------------------------------------------------------- internals
    def _load_credentials(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self._credentials_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _tool(name: str, description: str, props: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": props,
                               "required": list(props)},
            },
        }


def _enc(value: Any) -> str:
    return quote(str(value), safe="")


def _error_text(data: Any) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error:
            return str(error)
        detail = data.get("detail")
        if detail:
            return str(detail)
        title = data.get("title")
        if title:
            return str(title)
    return str(data)