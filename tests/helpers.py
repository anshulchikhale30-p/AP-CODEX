"""Shared test doubles: a scripted FakeLLM and a tiny echo HTTP server."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, AsyncIterator, Dict, List, Optional

from core.llm import BaseLLM, LLMChoice, LLMResponse, LLMStreamEvent, LLMToolCall


class FakeLLM(BaseLLM):
    """Deterministic LLM: each call pops the next queued :class:`LLMResponse`."""

    def __init__(self, default_content: str = "This is a fake response."):
        self.queue: List[LLMResponse] = []
        self.calls: List[tuple] = []
        self.default_content = default_content

    # ------------------------------------------------------------- builders
    @staticmethod
    def tool_call(name: str, arguments: Optional[Dict[str, Any]] = None,
                  call_id: str = "call_1") -> LLMToolCall:
        return LLMToolCall(id=call_id, name=name, arguments=arguments or {})

    def enqueue(self, *responses: LLMResponse):
        self.queue.extend(responses)

    def respond_tools(self, tool_calls: List[LLMToolCall], content: str = "") -> LLMResponse:
        return LLMResponse(
            choice=LLMChoice(content=content or None, tool_calls=tool_calls,
                             finish_reason="tool_calls")
        )

    def respond_text(self, content: str) -> LLMResponse:
        return LLMResponse(choice=LLMChoice(content=content, finish_reason="stop"))

    # -------------------------------------------------------------- contract
    def _next(self, messages: List[dict], tools) -> LLMResponse:
        self.calls.append((list(messages), tools))
        if self.queue:
            return self.queue.pop(0)
        return self.respond_text(self.default_content)

    async def complete(self, messages: List[dict], tools=None) -> LLMResponse:
        return self._next(messages, tools)

    async def stream(self, messages: List[dict], tools=None) -> AsyncIterator[LLMStreamEvent]:
        response = self._next(messages, tools)
        if response.choice.content:
            yield LLMStreamEvent(kind="text", text=response.choice.content)
        for tc in response.choice.tool_calls:
            yield LLMStreamEvent(kind="tool_call", tool_call=tc)
        yield LLMStreamEvent(kind="done", response=response)


# ---------------------------------------------------------------------------
class _EchoHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence test output
        pass

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        path, _, query = self.path.partition("?")
        if path == "/echo":
            return self._json(200, {
                "ok": True,
                "path": path,
                "query": dict(urllib.parse.parse_qsl(query)),
                "auth": self.headers.get("Authorization", ""),
            })
        if path == "/fail":
            return self._json(500, {"error": "boom"})
        return self._json(404, {"error": "not found"})

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()


class EchoServer:
    """Local HTTP server that echoes back what a connector sent."""

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def url(self, path: str = "") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def api_connector(echo: EchoServer, connector_id: str = "testapi") -> dict:
    """A ready-to-register API connector aimed at the echo server."""
    return {
        "id": connector_id,
        "base_url": echo.url(),
        "timeout": 5,
        "retries": 0,
        "endpoints": {
            "echo": {
                "description": "Echo a query string",
                "method": "GET",
                "path": "/echo",
                "params": ["q"],
            },
            "fails": {
                "description": "Always 500s",
                "method": "GET",
                "path": "/fail",
            },
        },
    }