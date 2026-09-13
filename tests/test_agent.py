"""Unit tests for the AgentLoop orchestration (deterministic FakeLLM)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from connectors import ConnectorManager, APIClient  # noqa: E402
from core.agent import AgentLoop  # noqa: E402

from helpers import EchoServer, FakeLLM, api_connector  # noqa: E402


class AgentLoopTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.echo = EchoServer()

    @classmethod
    def tearDownClass(cls):
        cls.echo.stop()

    def _manager(self):
        manager = ConnectorManager()
        manager.register(APIClient(api_connector(self.echo)))
        return manager

    def test_tool_discovery_to_execution_to_reply(self):
        import asyncio

        llm = FakeLLM()
        llm.enqueue(llm.respond_tools([llm.tool_call("testapi.echo", {"q": "42"})]))
        llm.enqueue(llm.respond_text("I called echo and got an answer."))
        agent = AgentLoop(manager=self._manager(), llm=llm, max_iterations=4)

        result = asyncio.run(agent.run([{"role": "user", "content": "echo 42"}]))
        self.assertTrue(result.ok)
        self.assertEqual(result.reply, "I called echo and got an answer.")
        self.assertEqual(result.iteration_count, 2)
        self.assertEqual(len(result.tool_calls), 1)
        self.assertTrue(result.tool_calls[0]["result"]["ok"])
        self.assertEqual(result.tool_calls[0]["connector"], "testapi")
        self.assertEqual(result.tool_calls[0]["action"], "echo")
        # the LLM actually saw the tool result (tool message with echo data)
        second_call_messages = llm.calls[1][0]
        roles = [m["role"] for m in second_call_messages]
        self.assertIn("tool", roles)
        tool_msg = next(m for m in second_call_messages if m["role"] == "tool")
        self.assertIn('"ok": true', tool_msg["content"])

    def test_stream_yields_status_tool_delta_done(self):
        import asyncio

        llm = FakeLLM()
        llm.enqueue(llm.respond_tools([llm.tool_call("testapi.echo", {"q": "7"})]))
        llm.enqueue(llm.respond_text("Done streaming."))

        async def collect():
            kinds, tool_statuses = [], []
            async for event in AgentLoop(self._manager(), llm, max_iterations=4).stream(
                [{"role": "user", "content": "go"}]
            ):
                kinds.append(event["kind"])
                if event["kind"] == "tool":
                    tool_statuses.append(event["status"])
            return kinds, tool_statuses

        kinds, tool_statuses = asyncio.run(collect())
        self.assertIn("status", kinds)
        self.assertEqual(tool_statuses[0], "started")
        self.assertEqual(tool_statuses[1], "ok")
        self.assertIn("delta", kinds)
        self.assertIn("done", kinds)

    def test_failed_tool_does_not_crash_loop(self):
        import asyncio

        llm = FakeLLM()
        llm.enqueue(llm.respond_tools([llm.tool_call("ghost.ping", {})]))
        llm.enqueue(llm.respond_text("Recovered despite the failure."))
        agent = AgentLoop(manager=self._manager(), llm=llm, max_iterations=4)

        result = asyncio.run(agent.run([{"role": "user", "content": "ping the ghost"}]))
        self.assertTrue(result.ok)
        self.assertFalse(result.tool_calls[0]["result"]["ok"])
        self.assertIn("not registered", result.tool_calls[0]["result"]["error"])
        self.assertEqual(result.reply, "Recovered despite the failure.")

    def test_max_iterations_guard(self):
        import asyncio

        llm = FakeLLM()
        tool_response = llm.respond_tools([llm.tool_call("testapi.echo", {"q": "1"})])
        llm.enqueue(tool_response, tool_response, tool_response)
        agent = AgentLoop(manager=self._manager(), llm=llm, max_iterations=2)

        result = asyncio.run(agent.run([{"role": "user", "content": "loop forever"}]))
        self.assertIn("iteration limit", result.reply)
        self.assertEqual(result.iteration_count, 3)

    def test_connector_error_surfaces_cleanly(self):
        import asyncio

        llm = FakeLLM()
        llm.enqueue(llm.respond_tools([llm.tool_call("testapi.fails", {})]))
        llm.enqueue(llm.respond_text("The endpoint is down."))
        agent = AgentLoop(manager=self._manager(), llm=llm, max_iterations=4)

        result = asyncio.run(agent.run([{"role": "user", "content": "do the failing thing"}]))
        self.assertTrue(result.ok)
        self.assertFalse(result.tool_calls[0]["result"]["ok"])
        self.assertEqual(result.tool_calls[0]["result"]["status"], 500)


if __name__ == "__main__":
    unittest.main()