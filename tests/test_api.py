"""Integration tests for the FastAPI layer (chat, streaming, tools, conns)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import unittest  # noqa: E402

from connectors import ConnectorManager, APIClient  # noqa: E402
from core.agent import AgentLoop  # noqa: E402
from api.app import create_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from helpers import EchoServer, FakeLLM, api_connector  # noqa: E402


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.echo = EchoServer()
        manager = ConnectorManager()
        manager.register(APIClient(api_connector(cls.echo)))
        cls.app = create_app(llm=FakeLLM(), manager=manager)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.echo.stop()

    def test_health(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")
        self.assertGreaterEqual(resp.json()["connectors"], 1)
        self.assertGreaterEqual(resp.json()["tools"], 1)

    def test_tools_endpoint(self):
        resp = self.client.get("/tools")
        body = resp.json()
        names = [t["function"]["name"] for t in body["tools"]]
        self.assertIn("testapi.echo", names)
        self.assertGreaterEqual(body["count"], 2)

    def test_connectors_endpoint(self):
        resp = self.client.get("/connectors")
        infos = {c["id"]: c for c in resp.json()}
        self.assertEqual(infos["testapi"]["kind"], "api")
        self.assertGreaterEqual(infos["testapi"]["tool_count"], 1)

    def test_chat_returns_reply_and_tool_calls(self):
        llm = self.app.state.llm
        llm.enqueue(llm.respond_tools([llm.tool_call("testapi.echo", {"q": "hi"})]))
        llm.enqueue(llm.respond_text("Echo complete."))
        resp = self.client.post("/chat", json={"messages": [{"role": "user", "content": "echo hi"}]})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["reply"], "Echo complete.")
        self.assertEqual(len(body["tool_calls"]), 1)
        self.assertTrue(body["tool_calls"][0]["ok"])
        self.assertEqual(body["tool_calls"][0]["connector"], "testapi")
        self.assertEqual(body["iteration_count"], 2)

    def test_chat_stream_sse(self):
        llm = self.app.state.llm
        llm.enqueue(llm.respond_text("Streamed reply."))
        with self.client.stream(
            "POST", "/chat/stream",
            json={"messages": [{"role": "user", "content": "say something"}]},
        ) as resp:
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/event-stream", resp.headers["content-type"])
            text = "".join(resp.iter_text())
        self.assertIn("event: delta", text)
        self.assertIn("Streamed reply.", text)
        self.assertIn("event: done", text)

    def test_chat_stream_tool_events(self):
        llm = self.app.state.llm
        llm.enqueue(llm.respond_tools([llm.tool_call("testapi.echo", {"q": "9"})]))
        llm.enqueue(llm.respond_text("After tool."))
        with self.client.stream(
            "POST", "/chat/stream",
            json={"messages": [{"role": "user", "content": "run c9"}]},
        ) as resp:
            text = "".join(resp.iter_text())
        self.assertIn("event: tool", text)
        self.assertIn('"status": "started"', text)
        self.assertIn('"status": "ok"', text)

    def test_register_connector(self):
        resp = self.client.post(
            "/connectors/register", json={"config": api_connector(self.echo, "extra_api")}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(resp.json()["id"], "extra_api")

    def test_register_connector_bad_config(self):
        resp = self.client.post("/connectors/register", json={"config": {"foo": "bar"}})
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertIn("error", body)

    def test_chat_rejects_extra_fields(self):
        resp = self.client.post(
            "/chat", json={"messages": [{"role": "user", "content": "x"}], "extra": True}
        )
        self.assertEqual(resp.status_code, 422)

    def test_chat_empty_messages_rejected(self):
        resp = self.client.post("/chat", json={"messages": []})
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()