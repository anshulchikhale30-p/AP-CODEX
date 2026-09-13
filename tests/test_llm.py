"""Unit tests for the OpenAI-compatible LLM adapter (hermetic, no network)."""

from __future__ import annotations

import json
import sys
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.llm import LLMError, LLMOptions, OpenAICompatibleLLM  # noqa: E402


class _LLMHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _send(self, code: int, payload: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def do_POST(self):
        body = self._body()

        if body.get("model") == "reject-me":
            self._send(401, b"bad key")
            return

        stream = bool(body.get("stream"))
        if stream:
            lines = [
                {"choices": [{"delta": {"role": "assistant"}}]},
                {"choices": [{"delta": {"content": "Hello "}}]},
                {"choices": [{"delta": {"content": "world"}}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "id": "call_7",
                                     "function": {"name": "testapi.fetch", "arguments": "{\"id\":"}}
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0,
                                     "function": {"name": "_order", "arguments": "\"41\"}"}}
                                ]
                            }
                        }
                    ]
                },
            ]
            payload = (
                "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in lines)
                + "data: [DONE]\n\n"
            ).encode()
        else:
            payload = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_3",
                                        "type": "function",
                                        "function": {
                                            "name": "testapi.fetch_order",
                                            "arguments": json.dumps({"order_id": "42"}),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                }
            ).encode()

        self._send(200, payload)


class LLMTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _LLMHandler)
        import threading

        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _llm(self) -> OpenAICompatibleLLM:
        return OpenAICompatibleLLM(
            LLMOptions(api_base=f"http://127.0.0.1:{self.port}/v1", model="fake", timeout=10)
        )

    def test_complete_with_tool_calls(self):
        import asyncio

        response = asyncio.run(self._llm().complete(
            [{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {}}]
        ))
        self.assertEqual(len(response.choice.tool_calls), 1)
        tc = response.choice.tool_calls[0]
        self.assertEqual(tc.name, "testapi.fetch_order")
        self.assertEqual(tc.arguments, {"order_id": "42"})
        self.assertEqual(response.usage["total_tokens"], 8)
        self.assertEqual(response.choice.finish_reason, "tool_calls")

    def test_unauthorized_raises_clean_error(self):
        import asyncio

        llm = self._llm()
        llm.options.model = "reject-me"
        with self.assertRaises(LLMError) as ctx:
            asyncio.run(llm.complete([{"role": "user", "content": "x"}], []))
        self.assertIn("401", str(ctx.exception))

    def test_stream_assembles_text_and_tool_calls(self):
        import asyncio

        async def collect():
            events = []
            async for event in self._llm().stream(
                [{"role": "user", "content": "hi"}], []
            ):
                events.append(event)
            return events

        events = asyncio.run(collect())
        text = "".join(e.text for e in events if e.kind == "text")
        self.assertEqual(text, "Hello world")
        done = [e for e in events if e.kind == "done"]
        self.assertEqual(len(done), 1)
        tool_calls = done[0].response.choice.tool_calls
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].name, "testapi.fetch_order")
        self.assertEqual(tool_calls[0].arguments, {"id": "41"})
        self.assertEqual(tool_calls[0].id, "call_7")


if __name__ == "__main__":
    unittest.main()