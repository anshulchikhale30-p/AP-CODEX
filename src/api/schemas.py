"""Pydantic request/response models for the AP-CODEX API."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ConfigDict

_CONF = ConfigDict(extra="forbid")


class ChatTurn(BaseModel):
    role: Literal["user", "assistant", "tool"] = "user"
    content: str = Field(default="", max_length=100_000)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None

    model_config = _CONF


class ChatRequest(BaseModel):
    messages: List[ChatTurn] = Field(min_length=1, max_length=200)

    model_config = _CONF


class ToolEvent(BaseModel):
    name: str
    connector: Optional[str] = None
    action: Optional[str] = None
    ok: bool
    status: Optional[int] = None
    summary: Optional[str] = None
    error: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str
    tool_calls: List[ToolEvent] = Field(default_factory=list)
    iteration_count: int = 0
    ok: bool = True
    error: Optional[str] = None
    usage: Optional[Dict[str, int]] = None


class RegisterConnectorRequest(BaseModel):
    config: Dict[str, Any]

    model_config = _CONF


class RegisterConnectorResponse(BaseModel):
    ok: bool
    id: Optional[str] = None
    error: Optional[str] = None


class ConnectorInfo(BaseModel):
    id: str
    name: str
    kind: str
    tool_count: int = 0


class HealthResponse(BaseModel):
    status: str = "ok"
    connectors: int = 0
    tools: int = 0
    model: Optional[str] = None


# --------------------------------------------------------------------- auth
class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=200)

    model_config = _CONF


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)

    model_config = _CONF


class UserInfo(BaseModel):
    email: str
    name: str
    source: Literal["google", "local"]
    created_at: str = ""


class AuthResponse(BaseModel):
    ok: bool
    token: Optional[str] = None
    user: Optional[UserInfo] = None
    error: Optional[str] = None