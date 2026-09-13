"""Account routes + Google Sign-In for AP-CODEX.

Two ways to authenticate:

* local sign-up/login (name, email, password) — saved to ``users.json``
* "Continue with Google" — OAuth2 authorization-code flow that reuses the
  project's Google client credentials and persists the profile (email + name)
  to the same user store.

Sessions are bearer tokens returned in the response body and stored by the
frontend in ``localStorage``; clients send ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from connectors._oauth import GoogleOAuthConnector

from .schemas import AuthResponse, LoginRequest, SignupRequest
from .users import SessionStore, UserExistsError, UserStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_CALLBACK_PATH = "/auth/google/callback"


class GoogleSignInConnector(GoogleOAuthConnector):
    """OAuth2 for Google Sign-In: the ``openid email profile`` scopes.

    Stores its own tokens under ``google_signin_tokens.json`` but shares the
    project's Google OAuth client id/secret (``gmail_credentials.json``).
    """

    id = "google-signin"
    name = "Google Sign-In"
    _SCOPES = ("openid", "email", "profile")

    def _default_tokens_name(self) -> str:
        return "google_signin_tokens.json"


# -------------------------------------------------------------- app state
def _store(request: Request) -> UserStore:
    return request.app.state.users


def _sessions(request: Request) -> SessionStore:
    return request.app.state.sessions


def _signin(request: Request) -> Optional[GoogleSignInConnector]:
    return getattr(request.app.state, "signin", None)


def _callback_uri(request: Request, explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    configured = os.environ.get("AP_CODEX_AUTH_REDIRECT_URI")
    if configured:
        return configured
    return f"{str(request.base_url).rstrip('/')}{_CALLBACK_PATH}"


def _bearer_token(request: Request) -> Optional[str]:
    auth: str = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip() or None
    return None


def _as_user_info(user: Dict[str, Any]):
    from .schemas import UserInfo

    return UserInfo(
        email=user.get("email", ""),
        name=user.get("name", ""),
        source=user.get("source", "local"),
        created_at=user.get("created_at", ""),
    )


# ------------------------------------------------------------- local auth
@router.post("/signup", response_model=AuthResponse)
async def signup(payload: SignupRequest, request: Request):
    email = payload.email.strip().lower()
    try:
        user = _store(request).create_local(email, payload.name, payload.password)
    except UserExistsError as exc:
        return AuthResponse(ok=False, error=str(exc))
    token = _sessions(request).create(email)
    return AuthResponse(ok=True, token=token, user=_as_user_info(user))


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginRequest, request: Request):
    user = _store(request).verify_local(
        payload.email.strip().lower(), payload.password
    )
    if user is None:
        return AuthResponse(ok=False, error="invalid email or password")
    token = _sessions(request).create(user["email"])
    return AuthResponse(ok=True, token=token, user=_as_user_info(user))


@router.post("/logout", response_model=AuthResponse)
async def logout(request: Request):
    _sessions(request).revoke(_bearer_token(request) or "")
    return AuthResponse(ok=True)


@router.get("/me", response_model=AuthResponse)
async def me(request: Request):
    email = _sessions(request).resolve(_bearer_token(request) or "")
    if not email:
        return AuthResponse(ok=False, error="not authenticated")
    user = _store(request).find(email)
    if user is None:
        return AuthResponse(ok=False, error="user not found")
    return AuthResponse(ok=True, user=_as_user_info(user))


# ----------------------------------------------------------- google sign-in
@router.get("/google/authorize")
async def google_authorize(request: Request, redirect_uri: Optional[str] = None):
    signin = _signin(request)
    if signin is None:
        return {"ok": False, "error": "google sign-in is not configured"}
    callback = _callback_uri(request, redirect_uri)
    result = signin.authorization_url(callback, state="auth-" + uuid.uuid4().hex[:8])
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error")}
    return RedirectResponse(result["url"])


@router.get("/google/callback")
async def google_callback(request: Request, code: str = "",
                          state: str = "", redirect_uri: Optional[str] = None):
    """Exchange the Google code, fetch the profile, persist, and redirect home."""
    signin = _signin(request)
    if signin is None:
        return RedirectResponse("/?auth_error=google+sign-in+is+not+configured")
    callback = _callback_uri(request, redirect_uri)

    result = signin.exchange_code(code, callback)
    if not result.get("ok"):
        error = quote(str(result.get("error") or "google+sign-in+failed"))
        return RedirectResponse(f"/?auth_error={error}")

    access_token = signin._get_access_token()
    if not access_token:
        return RedirectResponse("/?auth_error=google+sign-in+failed")
    try:
        status, body = signin._request(
            "GET", USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except Exception as exc:  # noqa: BLE001 - redirect back to the app
        logger.warning("google userinfo failed: %s", exc)
        return RedirectResponse("/?auth_error=google+sign-in+failed")
    if status >= 400 or not isinstance(body, dict):
        return RedirectResponse("/?auth_error=google+sign-in+failed")

    email = str(body.get("email") or "").strip().lower()
    if not email:
        return RedirectResponse("/?auth_error=google+account+has+no+email")
    user = _store(request).create_or_update_google(email, str(body.get("name") or ""))
    token = _sessions(request).create(email)
    return RedirectResponse(f"/?auth={token}&name={quote(user['name'])}")