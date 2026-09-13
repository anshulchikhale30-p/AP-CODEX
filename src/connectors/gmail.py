"""Gmail connector for AP-CODEX (OAuth2, real sends).

Uses Google's OAuth2 authorization-code flow with the ``gmail.send`` scope.
Credentials come from ``gmail_credentials.json`` (or ``AP_CODEX_GMAIL_CLIENT_ID``
/ ``AP_CODEX_GMAIL_CLIENT_SECRET``); obtained tokens are persisted to
``gmail_tokens.json``.  Both files are gitignored — never commit them.
"""

from __future__ import annotations

import base64
import os
import time
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

from ._oauth import GoogleOAuthConnector

_GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
_SCOPE = "https://www.googleapis.com/auth/gmail.send"


class GmailClient(GoogleOAuthConnector):
    """Send email through a user's Gmail account via the Gmail API."""

    id = "gmail"
    name = "Gmail (OAuth2)"
    _SCOPES = (_SCOPE,)

    def _env_tokens_path(self) -> Optional[str]:
        return os.environ.get("AP_CODEX_GMAIL_TOKENS")

    def _default_tokens_name(self) -> str:
        return "gmail_tokens.json"

    # -------------------------------------------------------------- interface
    def discover_tools(self) -> List[Dict[str, Any]]:
        return [
            self._tool("send", "Send an email via the signed-in Gmail account.",
                       {"to": {"type": "string", "description": "Recipient address."},
                        "subject": {"type": "string", "description": "Subject line."},
                        "body": {"type": "string", "description": "Message body."}}),
            self._tool("profile", "Show the signed-in Gmail account and mailbox size.", {}),
        ]

    def call_function(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        if action == "send":
            return self.send(to=payload.get("to"), subject=payload.get("subject"),
                             body=payload.get("body"))
        if action == "profile":
            return self.profile()
        return {"ok": False, "connector": self.id, "action": action,
                "error": f"gmail has no action '{action}'"}

    # ------------------------------------------------------------- actions
    def send(self, to: Optional[str] = None, subject: Optional[str] = None,
             body: Optional[str] = None) -> Dict[str, Any]:
        to = str(to or "").strip()
        subject = str(subject or "Message from AP-CODEX").strip()
        body = str(body or "").strip()

        if not body:
            return {"ok": False, "connector": self.id, "action": "send",
                    "status": 422, "error": "email body is empty"}

        access_token = self._get_access_token()
        if not access_token:
            return {"ok": False, "connector": self.id, "action": "send", "status": 401,
                    "error": "Gmail not authorized. Open /connectors/gmail/authorize "
                             "in a browser to sign in."}

        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")

        try:
            status, data = self._request(
                "POST", _GMAIL_SEND_URL,
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json={"raw": raw},
            )
        except Exception as exc:  # noqa: BLE001 - never crash the agent loop
            return {"ok": False, "connector": self.id, "action": "send",
                    "status": 0, "error": f"network error: {exc}"}

        if status >= 400:
            from ._oauth import error_text
            return {"ok": False, "connector": self.id, "action": "send",
                    "status": status, "error": error_text(data)}

        message_id = (data or {}).get("id")
        return {"ok": True, "connector": self.id, "action": "send", "status": status,
                "data": {"message_id": message_id, "to": to, "subject": subject,
                         "gmail_thread": (data or {}).get("threadId"), "sent_via": "gmail-api"}}

    def profile(self) -> Dict[str, Any]:
        access_token = self._get_access_token()
        if not access_token:
            return {"ok": False, "connector": self.id, "action": "profile", "status": 401,
                    "error": "Gmail not authorized"}
        try:
            status, data = self._request(
                "GET", _GMAIL_PROFILE_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "connector": self.id, "action": "profile",
                    "status": 0, "error": f"network error: {exc}"}
        if status >= 400:
            from ._oauth import error_text
            return {"ok": False, "connector": self.id, "action": "profile",
                    "status": status, "error": error_text(data)}
        return {"ok": True, "connector": self.id, "action": "profile", "status": status,
                "data": data}