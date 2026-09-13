"""Shared data structures for the AP-CODEX orchestration layer.

These live between the API layer and the connector pipeline so the agent loop,
LLM adapter, and frontend share one vocabulary of message/result shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCallArg:
    """A formatted function call emitted by the LLM."""

    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class ChatMessage:
    """One message in the conversation transcript (transport-agnostic)."""

    role: MessageRole
    content: str = ""
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[List[ToolCallArg]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.name:
            payload["name"] = self.name
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": _json_dumps(tc.arguments)},
                }
                for tc in self.tool_calls
            ]
        return payload


@dataclass
class ToolExecution:
    """Result of running one connector action on behalf of the LLM."""

    tool_call_id: str
    name: str
    arguments: Dict[str, Any]
    result: Dict[str, Any]

    def summary(self, limit: int = 400) -> Dict[str, Any]:
        """Frontend-friendly trimmed view (never includes raw secrets)."""
        out = {
            "name": self.name,
            "connector": self.result.get("connector"),
            "action": self.result.get("action"),
            "ok": bool(self.result.get("ok")),
            "status": self.result.get("status"),
        }
        if self.result.get("ok"):
            out["summary"] = _truncate(json_dumps(self.result.get("data")), limit)
        else:
            out["error"] = _truncate(str(self.result.get("error")), limit)
        return out


@dataclass
class AgentResult:
    """Final structured output of a full agent run."""

    reply: str
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    iteration_count: int = 0
    ok: bool = True
    error: Optional[str] = None
    usage: Optional[Dict[str, int]] = None


def normalize_messages(messages: Union[List[Dict[str, Any]], List[ChatMessage]]) -> List[ChatMessage]:
    """Accept raw dicts or ChatMessage objects from any layer."""
    out: List[ChatMessage] = []
    for item in messages:
        if isinstance(item, ChatMessage):
            out.append(item)
            continue
        role = MessageRole(str(item.get("role", "user")).lower())
        content = item.get("content") or ""
        tool_calls = None
        raw_calls = item.get("tool_calls")
        if raw_calls:
            tool_calls = [
                ToolCallArg(
                    id=str(tc.get("id", "")),
                    name=str((tc.get("function") or {}).get("name", "")),
                    arguments=_parse_json((tc.get("function") or {}).get("arguments")),
                )
                for tc in raw_calls
                if isinstance(tc, dict)
            ]
        out.append(
            ChatMessage(
                role=role,
                content=str(content),
                tool_call_id=item.get("tool_call_id"),
                name=item.get("name"),
                tool_calls=tool_calls,
            )
        )
    return out


import json as _json  # noqa: E402  (kept at bottom to avoid import gymnastics)


def _json_dumps(value: Any) -> str:
    return _json.dumps(value, ensure_ascii=False)


def _parse_json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = _json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (TypeError, ValueError):
        return {"raw": value}


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def json_dumps(value: Any) -> str:
    return _json_dumps(value)