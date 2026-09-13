"""The AP-CODEX tool-calling agent loop.

Bridges the frontend chat, tool discovery, and the connector pipeline:

``chat -> AgentLoop -> LLM (tools=connector schemas) -> execute_connector_action
      -> tool results fed back -> final reply``

Everything the loop emits is a JSON-serializable dict event.  No exception
escapes; failures are surfaced as ``{"kind": "error", ...}`` events or a
non-ok :class:`AgentResult`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from connectors import ConnectorManager

from .llm import BaseLLM, LLMStreamEvent
from .types import (
    AgentResult,
    ChatMessage,
    MessageRole,
    ToolCallArg,
    ToolExecution,
    normalize_messages,
)

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are AP-CODEX, a hands-on AI agent. You solve tasks end to end. When a "
    "task needs external data or actions, use the provided tools instead of "
    "guessing. If a tool fails, retry with corrected arguments or say what "
    "went wrong. Keep final answers concise and grounded in tool results."
)


class AgentLoop:
    """Orchestrates LLM planning + connector tool execution.

    Args:
        manager: A :class:`ConnectorManager` providing tool discovery and routing.
        llm: A :class:`BaseLLM` implementation.
        system_prompt: System instruction (defaults to the AP-CODEX persona).
        max_iterations: Hard cap on LLM<->tool rounds before the loop stops.
        tool_cache_ttl: Seconds tool schemas are cached before re-discovery.
    """

    def __init__(
        self,
        manager: ConnectorManager,
        llm: BaseLLM,
        *,
        system_prompt: Optional[str] = None,
        max_iterations: int = 8,
        tool_cache_ttl: float = 30.0,
    ):
        self._manager = manager
        self._llm = llm
        self._system = system_prompt or DEFAULT_SYSTEM_PROMPT
        self._max_iterations = max_iterations
        self._tool_cache_ttl = tool_cache_ttl
        self._tools_cache: Optional[List[dict]] = None
        self._tools_ts: float = 0.0

    # ---------------------------------------------------------------- public
    async def run(self, messages: Union[List[dict], List[ChatMessage]]) -> AgentResult:
        """Run the loop to completion and return a structured result."""
        reply: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        iterations = 0
        ok = True
        error = None
        async for ev in self.stream(messages):
            kind = ev["kind"]
            if kind == "delta":
                reply.append(ev["text"])
            elif kind == "tool" and ev.get("status") in ("ok", "error"):
                tool_calls.append(ev)
            elif kind == "done":
                iterations = ev["iterations"]
                ok = ev["ok"]
                if ev["reply"]:
                    reply = [ev["reply"]]
            elif kind == "error":
                ok = False
                error = ev["message"]
        return AgentResult(
            reply="".join(reply).strip() or ("I hit an error while processing your request." if error else ""),
            tool_calls=tool_calls,
            iteration_count=iterations,
            ok=ok,
            error=error,
        )

    async def stream(self, messages: Union[List[dict], List[ChatMessage]]) -> AsyncIterator[dict]:
        """Yield live event dicts: status/delta/tool/done/error."""
        try:
            transcript = [ChatMessage(role=MessageRole.SYSTEM, content=self._system)]
            transcript.extend(normalize_messages(messages))
            executed: List[Dict[str, Any]] = []
            iterations = 0

            yield {"kind": "status", "state": "thinking"}

            while True:
                iterations += 1
                if iterations > self._max_iterations:
                    note = "I've reached my iteration limit and am stopping here."
                    yield {"kind": "delta", "text": note}
                    yield self._done(note, executed, iterations, stopped=True)
                    return

                tools = await self._tool_schemas()
                reply_text, tool_calls, turn_usage = [], [], {}

                async for event in self._llm.stream(self._llm_messages(transcript), tools):
                    if event.kind == "text" and event.text:
                        yield {"kind": "delta", "text": event.text}
                        reply_text.append(event.text)
                    elif event.kind == "tool_call" and event.tool_call:
                        tool_calls.append(event.tool_call)
                    elif event.kind == "done":
                        usage = event.response.usage if event.response else None
                        if usage:
                            turn_usage = usage
                        if not tool_calls and not reply_text and event.response and event.response.choice.content:
                            reply_text.append(event.response.choice.content)

                assistant_content = "".join(reply_text).strip()

                if not tool_calls:
                    final = assistant_content or _fallback_reply()
                    yield self._done(final, executed, iterations, usage=turn_usage or None)
                    return

                transcript.append(
                    ChatMessage(role=MessageRole.ASSISTANT, content=assistant_content,
                                tool_calls=[ToolCallArg(t.id, t.name, t.arguments) for t in tool_calls])
                )

                for tool_call in tool_calls:
                    async for event in self._execute_one(tool_call, transcript):
                        if event.get("status") in ("ok", "error"):
                            executed.append(event)
                        yield event

        except Exception as exc:  # noqa: BLE001 - the loop must never crash
            logger.exception("agent loop failure")
            yield {"kind": "error", "message": str(exc)}

    # -------------------------------------------------------------- internals
    async def _tool_schemas(self) -> List[dict]:
        now = time.monotonic()
        if self._tools_cache is None or now - self._tools_ts > self._tool_cache_ttl:
            self._tools_cache = await asyncio.to_thread(self._manager.discover_tools)
            self._tools_ts = now
        return self._tools_cache or []

    def _llm_messages(self, transcript: List[ChatMessage]) -> List[dict]:
        return [m.to_dict() for m in transcript]

    async def _execute_one(self, tool_call, transcript: List[ChatMessage]):
        """Yield a 'started' event, run the connector action, then yield the
        finished 'ok'/'error' event and append the tool result to the transcript."""
        connector_id, action = _split_tool_name(tool_call.name)
        yield {
            "kind": "tool",
            "status": "started",
            "name": tool_call.name,
            "connector": connector_id,
            "action": action,
            "tool_call_id": tool_call.id,
        }

        try:
            result = await asyncio.to_thread(
                self._manager.execute_connector_action, connector_id, action, tool_call.arguments
            )
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "connector": connector_id, "action": action,
                      "error": f"connector execution raised: {exc}"}

        execution = ToolExecution(
            tool_call_id=tool_call.id, name=tool_call.name,
            arguments=tool_call.arguments, result=result,
        )
        content = json.dumps(result, ensure_ascii=False, default=str)
        transcript.append(
            ChatMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id,
                        name=tool_call.name, content=content)
        )

        yield {
            "kind": "tool",
            "status": "ok" if result.get("ok") else "error",
            "name": tool_call.name,
            "connector": connector_id,
            "action": action,
            "tool_call_id": tool_call.id,
            "result": execution.summary(),
        }

    @staticmethod
    def _done(reply: str, executed: List[dict], iterations: int, stopped: bool = False,
              usage: Optional[dict] = None) -> dict:
        out = {
            "kind": "done",
            "reply": reply or "",
            "tool_calls": [e for e in executed if e.get("status") in ("ok", "error")],
            "iterations": iterations,
            "ok": True,
            "stopped": stopped,
        }
        if usage:
            out["usage"] = usage
        return out


def _split_tool_name(name: str) -> tuple:
    if "." in name:
        return tuple(name.split(".", 1))
    return name, name


def _fallback_reply() -> str:
    return "I wasn't able to generate a response — please try again."