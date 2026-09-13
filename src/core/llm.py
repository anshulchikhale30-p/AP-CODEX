"""LLM adapter for the AP-CODEX agent loop.

A thin, provider-agnostic client for OpenAI-compatible ``/chat/completions``
endpoints (OpenAI, Ollama, LM Studio, vLLM, etc.) with both blocking and
streaming variants.  The agent loop depends only on the small
:class:`BaseLLM` interface, so tests can inject a deterministic fake and a
different provider can be swapped in without touching orchestration code.

Secrets: the API key is read from the environment at call time and is never
included in exception messages.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import httpx
except ImportError:  # pragma: no cover - required at runtime
    httpx = None  # type: ignore[assignment]


class LLMError(Exception):
    """Transport/upstream failure while talking to the LLM provider."""


@dataclass
class LLMOptions:
    api_base: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key_env: Optional[str] = "OPENAI_API_KEY"
    api_key: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout: float = 120.0

    @classmethod
    def from_env(cls) -> "LLMOptions":
        return cls(
            api_base=os.environ.get("AP_CODEX_API_BASE", cls.api_base),
            model=os.environ.get("AP_CODEX_MODEL", cls.model),
            api_key_env=os.environ.get("AP_CODEX_API_KEY_ENV") or cls.api_key_env,
            api_key=os.environ.get("AP_CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY"),
            temperature=float(os.environ.get("AP_CODEX_TEMPERATURE", cls.temperature)),
            max_tokens=int(os.environ.get("AP_CODEX_MAX_TOKENS", cls.max_tokens)),
            timeout=float(os.environ.get("AP_CODEX_TIMEOUT", cls.timeout)),
        )

    def resolve_key(self) -> Optional[str]:
        if self.api_key:
            return self.api_key
        env = self.api_key_env or ""
        return os.environ.get(env) or None


@dataclass
class LLMToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMChoice:
    content: Optional[str] = None
    tool_calls: List[LLMToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None


@dataclass
class LLMResponse:
    choice: LLMChoice
    usage: Optional[Dict[str, int]] = None


@dataclass
class LLMStreamEvent:
    kind: str  # "text" | "tool_call" | "done"
    text: str = ""
    tool_call: Optional[LLMToolCall] = None
    response: Optional[LLMResponse] = None


class BaseLLM:
    """Interface contract used by :class:`~src.core.agent.AgentLoop`."""

    async def complete(self, messages: List[dict], tools: Optional[List[dict]] = None) -> LLMResponse:
        raise NotImplementedError

    async def stream(
        self, messages: List[dict], tools: Optional[List[dict]] = None
    ) -> AsyncIterator[LLMStreamEvent]:
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator contract


class OpenAICompatibleLLM(BaseLLM):
    """OpenAI-compatible chat completions client (httpx, never blocks)."""

    def __init__(self, options: Optional[LLMOptions] = None):
        if httpx is None:  # pragma: no cover
            raise LLMError("httpx is required for LLM calls; pip install httpx")
        self.options = options or LLMOptions.from_env()

    # -------------------------------------------------------------- internals
    @property
    def _endpoint(self) -> str:
        return self.options.api_base.rstrip("/") + "/chat/completions"

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = self.options.resolve_key()
        if key:
            headers["Authorization"] = "Bearer " + key
        return headers

    def _payload(self, messages, tools, *, stream: bool) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.options.model,
            "messages": messages,
            "temperature": self.options.temperature,
            "max_tokens": self.options.max_tokens,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    @staticmethod
    def _parse_tool_calls(raw_calls: List[dict]) -> List[LLMToolCall]:
        out: List[LLMToolCall] = []
        for tc in raw_calls or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            arguments = fn.get("arguments") or "{}"
            try:
                parsed = json.loads(arguments)
            except (TypeError, ValueError):
                parsed = {"raw": arguments}
            out.append(
                LLMToolCall(
                    id=str(tc.get("id") or ""),
                    name=str(fn.get("name") or ""),
                    arguments=parsed if isinstance(parsed, dict) else {"value": parsed},
                )
            )
        return out

    @staticmethod
    def _parse_response(body: Dict[str, Any]) -> LLMResponse:
        choices = body.get("choices") or [{}]
        message = (choices[0].get("message") or {}) if choices else {}
        return LLMResponse(
            choice=LLMChoice(
                content=message.get("content"),
                tool_calls=OpenAICompatibleLLM._parse_tool_calls(message.get("tool_calls") or []),
                finish_reason=choices[0].get("finish_reason"),
            ),
            usage=body.get("usage"),
        )

    @staticmethod
    def _error_text(resp) -> str:
        try:
            body = resp.text[:500]
        except Exception:  # noqa: BLE001
            body = ""
        return f"LLM HTTP {resp.status_code}" + (f": {body}" if body else "")

    # ------------------------------------------------------------------ call
    async def complete(self, messages: List[dict], tools: Optional[List[dict]] = None) -> LLMResponse:
        payload = self._payload(messages, tools, stream=False)
        async with httpx.AsyncClient(timeout=self.options.timeout) as client:
            try:
                resp = await client.post(self._endpoint, json=payload, headers=self._headers())
            except httpx.HTTPError as exc:
                raise LLMError(f"LLM connection error: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(self._error_text(resp))
        try:
            return self._parse_response(resp.json())
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMError(f"LLM returned an unparseable response: {exc}") from exc

    async def stream(
        self, messages: List[dict], tools: Optional[List[dict]] = None
    ) -> AsyncIterator[LLMStreamEvent]:
        payload = self._payload(messages, tools, stream=True)
        builder: Dict[int, dict] = {}

        async with httpx.AsyncClient(timeout=self.options.timeout) as client:
            try:
                async with client.stream("POST", self._endpoint, json=payload, headers=self._headers()) as resp:
                    if resp.status_code != 200:
                        raise LLMError(self._error_text(resp))
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if not data or data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        if chunk.get("usage"):
                            continue  # handled at assembly time if ever needed
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        text = delta.get("content")
                        if text:
                            yield LLMStreamEvent(kind="text", text=text)
                        for frag in delta.get("tool_calls") or []:
                            if not isinstance(frag, dict):
                                continue
                            index = frag.get("index", 0)
                            entry = builder.setdefault(int(index), {"id": "", "name": "", "arguments": ""})
                            if frag.get("id"):
                                entry["id"] = frag["id"]
                            fn = frag.get("function") or {}
                            if fn.get("name"):
                                entry["name"] += fn["name"]
                            if fn.get("arguments"):
                                entry["arguments"] += fn["arguments"]
            except httpx.HTTPError as exc:
                raise LLMError(f"LLM streaming connection error: {exc}") from exc

        tool_calls: List[LLMToolCall] = []
        for index in sorted(builder):
            entry = builder[index]
            try:
                parsed = json.loads(entry["arguments"]) if entry["arguments"] else {}
            except ValueError:
                parsed = {"raw": entry["arguments"]}
            tool_calls.append(
                LLMToolCall(
                    id=entry["id"] or f"call_{index}",
                    name=entry["name"],
                    arguments=parsed if isinstance(parsed, dict) else {"value": parsed},
                )
            )

        yield LLMStreamEvent(
            kind="done",
            response=LLMResponse(
                choice=LLMChoice(content=None, tool_calls=tool_calls),
                usage=None,
            ),
        )