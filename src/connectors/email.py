"""Email connector for AP-CODEX.

Two modes, never crashes:

* **SMTP** — when ``AP_CODEX_SMTP_HOST`` (plus user/password) is configured, a
  real message is sent over TLS via ``smtplib``.  Credentials come only from
  the environment.
* **Simulated** — otherwise the message is archived to an in-memory outbox
  (and persisted to ``ap-codex-outbox.json`` when a path is given) and the
  result is marked ``"simulated": true``.  This keeps the demo runnable with
  zero configuration.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import smtplib
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class EmailClient:
    """Send messages via SMTP or archive them to a simulated outbox."""

    id = "email"
    name = "Email (SMTP / simulated)"

    def __init__(
        self,
        *,
        outbox_path: Optional[str] = None,
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None,
        smtp_user: Optional[str] = None,
        smtp_password: Optional[str] = None,
        smtp_from: Optional[str] = None,
        default_to: Optional[str] = None,
    ):
        env = os.environ
        self._host = smtp_host or env.get("AP_CODEX_SMTP_HOST")
        self._port = smtp_port or int(env.get("AP_CODEX_SMTP_PORT") or 587)
        self._user = smtp_user or env.get("AP_CODEX_SMTP_USER")
        self._password = smtp_password or env.get("AP_CODEX_SMTP_PASSWORD")
        self._from = smtp_from or env.get("AP_CODEX_SMTP_FROM") or (
            env.get("AP_CODEX_SMTP_USER") or "ap-codex@example.com"
        )
        self._default_to = default_to or env.get("AP_CODEX_MANAGER_EMAIL") or "manager@example.com"
        self._outbox_path = outbox_path
        self._outbox: List[Dict[str, Any]] = []

    # -------------------------------------------------------------- interface
    def discover_tools(self) -> List[Dict[str, Any]]:
        return [
            self._tool("send", "Send an email to one recipient.",
                       {"to": {"type": "string", "description": "Recipient address."},
                        "subject": {"type": "string", "description": "Subject line."},
                        "body": {"type": "string", "description": "Message body."}}),
            self._tool("outbox", "List messages sent this session.", {}),
        ]

    def call_function(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        if action == "send":
            return self.send(to=payload.get("to"), subject=payload.get("subject"),
                             body=payload.get("body"))
        if action == "outbox":
            return {"ok": True, "connector": self.id, "action": "outbox",
                    "data": {"count": len(self._outbox),
                             "messages": [summary(m) for m in self._outbox]}}
        return {"ok": False, "connector": self.id, "action": action,
                "error": f"email has no action '{action}'"}

    # ------------------------------------------------------------- actions
    def send(self, to: Optional[str] = None, subject: Optional[str] = None,
             body: Optional[str] = None) -> Dict[str, Any]:
        to = (to or self._default_to).strip()
        subject = str(subject or "Message from AP-CODEX").strip()
        body = str(body or "").strip()

        if not body:
            return {"ok": False, "connector": self.id, "action": "send",
                    "status": 422, "error": "email body is empty"}

        message_id = "APX-mail-" + hashlib.sha1(
            f"{to}|{subject}|{body}|{time.time()}".encode()
        ).hexdigest()[:8]

        if self._host and self._user and self._password:
            try:
                return self._send_smtp(to, subject, body, message_id, smtp_port=self._port)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SMTP send failed, falling back to outbox: %s", exc)
                return self._archive(to, subject, body, message_id, simulated=True,
                                     note=f"smtp error: {exc}")

        return self._archive(to, subject, body, message_id, simulated=True,
                             note="no SMTP credentials configured")

    # ------------------------------------------------------------- internals
    def _send_smtp(self, to: str, subject: str, body: str, message_id: str,
                   smtp_port: Optional[int] = None) -> Dict[str, Any]:
        message = (
            f"From: {self._from}\r\nTo: {to}\r\n"
            f"Subject: {subject}\r\nMessage-ID: <{message_id}@ap-codex.local>\r\n"
            f"Date: {time.strftime('%a, %d %b %Y %H:%M:%S %z')}\r\n\r\n{body}"
        )
        with smtplib.SMTP(self._host, smtp_port or self._port, timeout=10) as client:
            client.starttls()
            client.login(self._user, self._password)
            client.sendmail(self._from, [to], message)
        record = self._record(to, subject, body, message_id, simulated=False)
        return {"ok": True, "connector": self.id, "action": "send", "status": 200,
                "data": {"message_id": message_id, "to": to, "subject": subject,
                         "sent_via": f"smtp://{self._host}", "archived": record}}

    def _archive(self, to: str, subject: str, body: str, message_id: str,
                 simulated: bool, note: str) -> Dict[str, Any]:
        record = self._record(to, subject, body, message_id, simulated=simulated)
        return {"ok": True, "connector": self.id, "action": "send", "status": 200,
                "data": {"message_id": message_id, "to": to, "subject": subject,
                         "simulated": simulated, "note": note, "archived": record}}

    def _record(self, to: str, subject: str, body: str, message_id: str,
                simulated: bool) -> Dict[str, Any]:
        record = {"message_id": message_id, "to": to, "subject": subject,
                  "body": body, "sent_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        self._outbox.append(record)
        if self._outbox_path:
            try:
                with open(self._outbox_path, "w", encoding="utf-8") as fh:
                    json.dump(self._outbox, fh, ensure_ascii=False, indent=2)
            except OSError:  # never let persistence break the flow
                logger.warning("could not persist outbox to %s", self._outbox_path)
        return summary(record)

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


def summary(record: Dict[str, Any]) -> Dict[str, Any]:
    body = str(record.get("body", ""))
    return {
        "message_id": record.get("message_id"),
        "to": record.get("to"),
        "subject": record.get("subject"),
        "sent_at": record.get("sent_at"),
        "snippet": body[:80] + ("\u2026" if len(body) > 80 else ""),
    }