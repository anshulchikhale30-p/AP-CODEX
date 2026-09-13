"""MCP (Model Context Protocol) client for AP-CODEX.

Connects to external MCP servers and exposes their tools to the agent loop as
native LLM functions.  Two transports are supported:

* ``stdio``     -> a local MCP server process (e.g. ``npx -y <server>``)
* ``http``      -> a remote server speaking Streamable HTTP (with ``sse``
                  accepted as an alias for older SSE-based servers)

The official ``mcp`` Python SDK is used for the wire protocol; it must be
installed (``pip install mcp``).  If it is missing, every operational method
returns a clean error instead of raising an ImportError into the agent loop.

Example configuration::

    {
        "id": "filesystem_mcp",
        "type": "mcp",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        "env": {"FOO": "bar"}
    }
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, Dict, Iterable, Optional

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    _MCP_SDK = True
except ImportError:  # pragma: no cover - exercised on machines without the SDK
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None
    _MCP_SDK = False

try:
    from mcp.client.streamable_http import streamable_http_client
except ImportError:  # newer SDKs
    streamable_http_client = None

try:
    from mcp.client.sse import sse_client
except ImportError:  # optional legacy transport
    sse_client = None

try:
    import inspect as _inspect

    _HTTP_HEADERS_KWARG = bool(
        streamable_http_client
        and "headers" in _inspect.signature(streamable_http_client).parameters
    )
except Exception:  # pragma: no cover - introspection failure is impossible in practice
    _HTTP_HEADERS_KWARG = True

logger = logging.getLogger(__name__)


class MCPClient:
    """A persistent connection to a single MCP server.

    The SDK is async.  We run one dedicated background thread event loop with a
    single long-lived "runner" task that *owns* the transport + session exit
    stack, so everything is entered and closed inside the same asyncio task
    (required by anyio under the hood) while the public API stays synchronous.
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.id: str = config.get("id") or "mcp"
        self.name: str = config.get("name") or self.id
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._runner_task = None
        self._session = None
        self._tools: Dict[str, Any] = {}
        self._ready_event: Optional[asyncio.Event] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._start_error: Optional[BaseException] = None

    # ------------------------------------------------------------- lifecycle
    def start(self, timeout: float = 120.0):
        if self._session is not None or (self._runner_task and not self._runner_task.done()):
            return
        if not _MCP_SDK:
            raise RuntimeError(
                "the 'mcp' SDK is required for MCP connectors; install with "
                "'pip install mcp'"
            )
        self._start_error = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name=f"mcp-{self.id}", daemon=True
        )
        self._thread.start()
        self._runner_task = asyncio.run_coroutine_threadsafe(self._runner(), self._loop)

        # Block until the runner either connects or fails.
        self._submit(self._wait_ready(), timeout).result()
        if self._start_error is not None:
            err = self._start_error
            self._start_error = None
            raise RuntimeError(f"MCP connector {self.id} failed to start: {err}") from err
        logger.info("MCP connector %s connected (%s)", self.id, self.config.get("transport"))

    def close(self, timeout: float = 20.0):
        if self._loop is None or not self._loop.is_running():
            return
        try:
            self._submit(self._request_stop(), 5).result()
        except Exception:  # noqa: BLE001
            logger.debug("error signalling MCP stop for %s", self.id, exc_info=True)
        if self._runner_task is not None:
            try:
                self._runner_task.result(timeout)  # drains the exit stack in-task
            except Exception:  # noqa: BLE001
                logger.debug("MCP runner did not stop cleanly for %s", self.id, exc_info=True)
                self._runner_task.cancel()
        self._session = None
        self._tools = {}
        self._stop_event = None
        self._ready_event = None
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ tools
    def discover_tools(self) -> Iterable[Dict[str, Any]]:
        """Yield OpenAI-style function schemas for the server's tools."""
        self._ensure_started()
        for tool in self._tools.values():
            # mcp SDK v1 exposes Tool.inputSchema; v2 renamed to input_schema.
            schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
            yield self._normalize_tool(
                tool.name, getattr(tool, "description", "") or "", schema
            )

    @staticmethod
    def _normalize_tool(name: str, description: str, input_schema: Dict[str, Any]) -> Dict[str, Any]:
        """Standalone schema normalization (unit-testable without an SDK)."""
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description or "",
                "parameters": input_schema or {"type": "object", "properties": {}},
            },
        }

    # ------------------------------------------------------------------- call
    def call_function(self, action_name: str, payload: Any = None) -> Dict[str, Any]:
        """Invoke an MCP tool.  Returns a JSON-serializable result dict.

        On success: ``{"ok": True, "connector", "action", "data": str}``.
        Failures never raise; they return a clean error dict.
        """
        payload = {} if payload is None else payload
        try:
            self._ensure_started()
        except Exception as exc:
            return self._error(action_name, str(exc))

        if action_name not in self._tools:
            return self._error(action_name, f"unknown MCP tool {action_name!r}")

        try:
            result = self._submit(self._call_tool(action_name, payload), 300).result()
        except Exception as exc:
            return self._error(action_name, f"tool call failed: {exc}")

        return {"ok": True, "connector": self.id, "action": action_name, "data": result}

    # -------------------------------------------------------------- internals
    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro, timeout: float) -> "asyncio.Future":
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("MCP event loop is not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _ensure_started(self):
        if self._session is None:
            self.start()

    async def _runner(self):
        """Single long-lived task owning the transport + session stack."""
        from contextlib import AsyncExitStack

        self._ready_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                await self._connect_into_stack(stack)
                self._ready_event.set()
                await self._stop_event.wait()
        except BaseException as exc:
            self._start_error = exc
            self._ready_event.set()
            raise
        finally:
            self._session = None
            self._tools = {}

    async def _wait_ready(self):
        if self._ready_event is None:  # pragma: no cover - defensive
            return
        await self._ready_event.wait()

    async def _request_stop(self):
        if self._stop_event is not None:
            self._stop_event.set()

    async def _connect_into_stack(self, stack):
        transport = str(self.config.get("transport", "stdio")).lower()

        if transport == "stdio":
            if StdioServerParameters is None:
                raise RuntimeError("stdio transport requires the 'mcp' SDK")
            command = self.config.get("command")
            if not command:
                raise RuntimeError("stdio transport requires a 'command'")
            env = dict(os.environ)
            env.update({str(k): str(v) for k, v in (self.config.get("env") or {}).items()})
            params = StdioServerParameters(
                command=command,
                args=self.config.get("args") or [],
                env=env,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        else:
            url = self.config.get("url")
            if not url:
                raise RuntimeError("http/sse transport requires a 'url'")
            headers = self.config.get("headers") or {}
            if streamable_http_client is not None:
                if _HTTP_HEADERS_KWARG:
                    streams = await stack.enter_async_context(
                        streamable_http_client(url, headers=headers)
                    )
                else:
                    http_client = None
                    if headers:  # v2: headers ride on an httpx.AsyncClient
                        import httpx

                        http_client = httpx.AsyncClient(headers=headers)
                        await stack.enter_async_context(http_client)
                    streams = await stack.enter_async_context(
                        streamable_http_client(url, http_client=http_client)
                    )
            elif sse_client is not None:
                streams = await stack.enter_async_context(sse_client(url, headers=headers))
            else:
                raise RuntimeError("no MCP HTTP transport available; install/upgrade 'mcp'")
            read, write = streams[0], streams[1]

        self._session = await stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

        listing = await self._session.list_tools()
        self._tools = {tool.name: tool for tool in listing.tools}
        logger.info("MCP connector %s discovered %d tools", self.id, len(self._tools))

    async def _call_tool(self, name: str, arguments: Any):
        result = await self._session.call_tool(name, arguments=arguments or None)

        if getattr(result, "structuredContent", None):
            return result.structuredContent

        parts = []
        for block in getattr(result, "content", []) or []:
            block_type = getattr(block, "type", None)
            text = getattr(block, "text", None)
            if block_type in ("text", None) and text is not None:
                parts.append(str(text))
            elif block_type == "image":
                data = getattr(block, "data", "") or ""
                mime = getattr(block, "mimeType", "image/png")
                parts.append(f"[image data:{mime} bytes:{len(data)}]")
            elif block_type == "resource":
                parts.append(f"[resource {getattr(block, 'uri', '')}]")

        if getattr(result, "isError", False):
            joined = "\n".join(parts) or "MCP tool reported an error"
            raise RuntimeError(joined[:2000])
        return "\n".join(parts)

    def _error(self, action_name, message):
        return {"ok": False, "connector": self.id, "action": action_name, "error": str(message)}