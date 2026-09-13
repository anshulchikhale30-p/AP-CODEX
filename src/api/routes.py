"""HTTP routes for the AP-CODEX orchestration backend."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, AsyncIterator, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from connectors import APIClient, MCPClient
from core.agent import AgentLoop
from core.scenario import ScenarioRunner
from core.types import AgentResult

from .schemas import (
    ChatRequest,
    ChatResponse,
    ConnectorInfo,
    HealthResponse,
    RegisterConnectorRequest,
    RegisterConnectorResponse,
    ToolEvent,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _agent(request: Request) -> AgentLoop:
    return request.app.state.agent


def _scenario(request: Request) -> ScenarioRunner:
    return getattr(request.app.state, "scenario", None)


def _gmail(request: Request):
    manager = request.app.state.manager
    return getattr(manager, "_connectors", {}).get("gmail")


def _calendar(request: Request):
    manager = request.app.state.manager
    return getattr(manager, "_connectors", {}).get("calendar")


def _meet(request: Request):
    manager = request.app.state.manager
    return getattr(manager, "_connectors", {}).get("meet")


def _twitter(request: Request):
    manager = request.app.state.manager
    return getattr(manager, "_connectors", {}).get("twitter")


def _oauth_redirect_uri(request: Request, explicit: Optional[str] = None) -> str:
    """Resolve the OAuth redirect URI: explicit param > env > request host."""
    if explicit:
        return explicit
    configured = os.environ.get("AP_CODEX_GMAIL_REDIRECT_URI")
    if configured:
        return configured
    return f"{request.base_url}connectors/gmail/callback"


def _last_user_text(messages: List[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content", ""))
    return ""


def _chat_response(result: AgentResult) -> ChatResponse:
    tool_calls = []
    for event in result.tool_calls:
        summary = event.get("result") or {}
        tool_calls.append(ToolEvent(**{k: v for k, v in summary.items()
                                       if k in ToolEvent.model_fields}))
    return ChatResponse(
        reply=result.reply,
        tool_calls=tool_calls,
        iteration_count=result.iteration_count,
        ok=result.ok,
        error=result.error,
        usage=result.usage,
    )


# -------------------------------------------------------------------- health
@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    manager = request.app.state.manager
    tools = await asyncio.to_thread(manager.discover_tools)
    llm = request.app.state.llm
    model = getattr(getattr(llm, "options", None), "model", None)
    return HealthResponse(
        status="ok",
        connectors=len(manager._connectors),
        tools=len(tools),
        model=model,
    )


# -------------------------------------------------------------------- tools
@router.get("/tools")
async def list_tools(request: Request):
    manager = request.app.state.manager
    tools = await asyncio.to_thread(manager.discover_tools)
    return {"count": len(tools), "tools": tools}


@router.get("/connectors", response_model=List[ConnectorInfo])
async def list_connectors(request: Request):
    manager = request.app.state.manager
    out: List[ConnectorInfo] = []
    for connector in manager._connectors.values():
        if isinstance(connector, APIClient):
            kind, count = "api", len(connector._endpoints)
        elif isinstance(connector, MCPClient):
            kind, count = "mcp", 0
            try:
                tools = await asyncio.to_thread(lambda: list(connector.discover_tools()))
                count = len(tools)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP discovery failed for %s: %s", connector.id, exc)
        else:
            kind, count = "custom", 0
        out.append(ConnectorInfo(id=connector.id, name=connector.name, kind=kind, tool_count=count))
    return out


@router.post("/connectors/register", response_model=RegisterConnectorResponse)
async def register_connector(payload: RegisterConnectorRequest, request: Request):
    manager = request.app.state.manager
    config = payload.config
    try:
        connector = await asyncio.to_thread(manager.build_connector, config)
        if connector is None:
            return RegisterConnectorResponse(
                ok=False, error=f"cannot build a connector from config: {config.get('id') or config}"
            )
        connector_id = await asyncio.to_thread(manager.register, connector)
        return RegisterConnectorResponse(ok=True, id=connector_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("connector registration failed")
        return RegisterConnectorResponse(ok=False, error=str(exc))


# -------------------------------------------------------------- gmail oauth
@router.get("/connectors/gmail/authorize")
async def gmail_authorize(request: Request, redirect_uri: Optional[str] = None):
    gmail = _gmail(request)
    if gmail is None:
        return {"ok": False, "error": "gmail connector is not registered"}
    callback = _oauth_redirect_uri(request, redirect_uri)
    result = gmail.authorization_url(callback, state=uuid.uuid4().hex[:8])
    if result.get("ok"):
        result["hint"] = (
            f"Open the url in a browser and sign in. The redirect URI is "
            f"{callback} — it must be listed in the Google Cloud Console."
        )
    return result


@router.get("/connectors/gmail/callback")
async def gmail_callback(request: Request, code: str,
                         redirect_uri: Optional[str] = None, state: str = ""):
    """Single registered callback path; the state prefix decides which connector."""
    manager = request.app.state.manager
    callback = _oauth_redirect_uri(request, redirect_uri)
    if state.startswith("cal"):
        calendar = _calendar(request)
        if calendar is None:
            return {"ok": False, "error": "calendar connector is not registered"}
        return calendar.exchange_code(code, callback)
    if state.startswith("mt"):
        meet = _meet(request)
        if meet is None:
            return {"ok": False, "error": "meet connector is not registered"}
        return meet.exchange_code(code, callback)
    gmail = _gmail(request)
    if gmail is None:
        return {"ok": False, "error": "gmail connector is not registered"}
    return gmail.exchange_code(code, callback)


@router.get("/connectors/calendar/authorize")
async def calendar_authorize(request: Request, redirect_uri: Optional[str] = None):
    calendar = _calendar(request)
    if calendar is None:
        return {"ok": False, "error": "calendar connector is not registered"}
    callback = _oauth_redirect_uri(request, redirect_uri)
    result = calendar.authorization_url(callback, state="cal-" + uuid.uuid4().hex[:8])
    if result.get("ok"):
        result["hint"] = (
            f"Open the url in a browser and sign in. The redirect URI is "
            f"{callback} — it must be listed in the Google Cloud Console."
        )
    return result


@router.get("/connectors/meet/authorize")
async def meet_authorize(request: Request, redirect_uri: Optional[str] = None):
    meet = _meet(request)
    if meet is None:
        return {"ok": False, "error": "meet connector is not registered"}
    callback = _oauth_redirect_uri(request, redirect_uri)
    result = meet.authorization_url(callback, state="mt-" + uuid.uuid4().hex[:8])
    if result.get("ok"):
        result["hint"] = (
            f"Open the url in a browser and sign in. The redirect URI is "
            f"{callback} — it must be listed in the Google Cloud Console. Note: the "
            f"Google Meet REST API is in developer preview and requires an approved "
            f"project; enable the Google Meet API in Cloud Console first."
        )
    return result


# ------------------------------------------------------------ twitter status
@router.get("/connectors/twitter/status")
async def twitter_status(request: Request):
    twitter = _twitter(request)
    if twitter is None:
        return {"ok": False, "error": "twitter connector is not registered"}
    return twitter.status()


@router.get("/connectors/meet/status")
async def meet_status(request: Request):
    meet = _meet(request)
    if meet is None:
        return {"ok": False, "error": "meet connector is not registered"}
    return meet.status()


# --------------------------------------------------------------------- chat
@router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request):
    agent = _agent(request)
    scenario = _scenario(request)
    messages = [turn.model_dump(exclude_none=True) for turn in payload.messages]
    text = _last_user_text(messages)

    if scenario is not None and scenario.matches(text):
        result = await scenario.run(text)
        if result is not None:
            return _chat_response(result)

    try:
        result: AgentResult = await agent.run(messages)
    except Exception as exc:  # noqa: BLE001 - outer safety net
        logger.exception("agent.run failed")
        return ChatResponse(reply="", ok=False, error=f"agent loop failed: {exc}")
    return _chat_response(result)


@router.post("/chat/stream")
async def chat_stream(payload: ChatRequest, request: Request) -> StreamingResponse:
    agent = _agent(request)
    scenario = _scenario(request)
    messages = [turn.model_dump(exclude_none=True) for turn in payload.messages]
    text = _last_user_text(messages)

    if scenario is not None and scenario.matches(text):
        source = scenario.stream(text)
    else:
        source = agent.stream(messages)

    async def generate() -> AsyncIterator[str]:
        try:
            async for event in source:
                yield _sse(event["kind"], {k: v for k, v in event.items() if k != "kind"})
        except Exception as exc:  # noqa: BLE001
            logger.exception("chat stream failed")
            yield _sse("error", {"message": str(exc)})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )