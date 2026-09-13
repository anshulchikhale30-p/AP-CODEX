"""End-to-end verification for the AP-CODEX connector pipeline.

Run:  python -m unittest discover -s tests -v

Covers:
* API client: GET/POST, path + query templating, body merging
* Auth: bearer, custom header, OAuth2 client-credentials (with caching)
* Retries on 5xx, clean non-retryable errors, connection failures
* Security: secrets from env vars only, never leaked in returned errors
* Manager: registration, discovery dedupe+prefixing, routing, config loading
* Router entry point ``execute_connector_action``
* MCP schema normalization (server-free) and graceful no-SDK handling
"""

import json
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from connectors import (  # noqa: E402
    ConnectorManager,
    APIClient,
    MCPClient,
    execute_connector_action,
)


# ---------------------------------------------------------------------------
# A controllable local HTTP server
# ---------------------------------------------------------------------------
STATE = {"flaky_calls": 0, "token_calls": 0, "seen_auth": []}


class Router(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence test output
        pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record_auth(self):
        STATE["seen_auth"].append(self.headers.get("Authorization", ""))

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/orders":
            self._record_auth()
            return self._send_json(200, {"orders": [{"id": 1, "total": 10.0}]})
        if path.startswith("/orders/"):
            order_id = path.rsplit("/", 1)[-1]
            return self._send_json(200, {"id": order_id, "total": 42.0})
        if path == "/flaky":
            STATE["flaky_calls"] += 1
            if STATE["flaky_calls"] <= 2:
                return self._send_json(500, {"error": "not yet"})
            return self._send_json(200, {"ok": True, "attempts": STATE["flaky_calls"]})
        if path == "/missing":
            return self._send_json(404, {"error": "nope"})
        if path == "/header-echo":
            return self._send_json(200, {"X-API-Key": self.headers.get("X-API-Key", "")})
        return self._send_json(200, {"echo": True})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/token":
            STATE["token_calls"] += 1
            body = self._read_body()
            assert b"client_credentials" in body
            return self._send_json(
                200, {"access_token": "oauth-test-token", "expires_in": 3600}
            )
        if path == "/orders":
            self._record_auth()
            data = json.loads(self._read_body() or b"{}")
            return self._send_json(201, {"created": True, "payload": data})
        return self._send_json(405, {"error": "bad route"})


class ConnectorTestServer:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Router)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def url(self, path=""):
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class ConnectorPipelineTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ConnectorTestServer()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()


class TestApiClient(ConnectorPipelineTestCase):
    def _api(self, auth=None, base=None, endpoints=None, secrets=None, **extra):
        cfg = {
            "id": "testapi",
            "base_url": base or self.srv.url(),
            "timeout": 5,
            "retries": 0,
            **extra,
        }
        if auth:
            cfg["auth"] = auth
        cfg["endpoints"] = endpoints or {}
        return APIClient(cfg, secrets=secrets)

    def test_tool_discovery_schema(self):
        api = self._api(
            endpoints={
                "fetch_order": {
                    "description": "Fetch one order",
                    "method": "GET",
                    "path": "/orders/{order_id}",
                    "params": ["order_id"],
                }
            }
        )
        tools = list(api.discover_tools())
        self.assertEqual(tools[0]["function"]["name"], "testapi.fetch_order")
        self.assertEqual(tools[0]["function"]["description"], "Fetch one order")
        self.assertIn("order_id", tools[0]["function"]["parameters"]["properties"])

    def test_get_with_path_and_query(self):
        api = self._api(
            base=self.srv.url(),
            endpoints={
                "fetch_order": {
                    "method": "GET",
                    "path": "/orders/{order_id}",
                    "params": ["order_id"],
                }
            },
        )
        result = api.call_function("fetch_order", {"order_id": 77})
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["data"]["id"], "77")

    def test_post_body_merge(self):
        api = self._api(
            endpoints={
                "create_order": {
                    "method": "POST",
                    "path": "/orders",
                    "body": {"source": "ap-codex"},
                }
            }
        )
        result = api.call_function("create_order", {"total": 5.5})
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 201)
        self.assertEqual(result["data"]["payload"]["source"], "ap-codex")
        self.assertEqual(result["data"]["payload"]["total"], 5.5)

    def test_bearer_token_from_env(self):
        os.environ["TEST_ORDERS_TOKEN"] = "env-bearer-token"
        self.addCleanup(os.environ.pop, "TEST_ORDERS_TOKEN", None)
        api = self._api(
            auth={"type": "bearer", "token_env": "TEST_ORDERS_TOKEN"},
            endpoints={"list": {"method": "GET", "path": "/orders"}},
        )
        result = api.call_function("list", {})
        self.assertTrue(result["ok"])
        self.assertEqual(STATE["seen_auth"][-1], "Bearer env-bearer-token")

    def test_custom_header_from_secrets_map(self):
        api = self._api(
            secrets={"CUSTOM_KEY": "custom-secret-value"},
            auth={"type": "header", "header_name": "X-API-Key", "value_env": "CUSTOM_KEY"},
            endpoints={"echo": {"method": "GET", "path": "/header-echo"}},
        )
        result = api.call_function("echo", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["X-API-Key"], "custom-secret-value")

    def test_oauth2_client_credentials_cached(self):
        os.environ["TEST_CLIENT_ID"] = "cid"
        os.environ["TEST_CLIENT_SECRET"] = "csecret"
        self.addCleanup(os.environ.pop, "TEST_CLIENT_ID", None)
        self.addCleanup(os.environ.pop, "TEST_CLIENT_SECRET", None)

        api = self._api(
            auth={
                "type": "oauth2_client_credentials",
                "token_url": self.srv.url("/token"),
                "client_id_env": "TEST_CLIENT_ID",
                "client_secret_env": "TEST_CLIENT_SECRET",
            },
            endpoints={"list": {"method": "GET", "path": "/orders"}},
        )
        first = api.call_function("list", {})
        second = api.call_function("list", {})
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(STATE["seen_auth"][-1], "Bearer oauth-test-token")
        self.assertEqual(STATE["token_calls"], 1, "token must be cached across calls")

    def test_retries_on_server_error(self):
        api = self._api(
            retries=2,
            backoff_base=0.05,
            endpoints={"flaky": {"method": "GET", "path": "/flaky"}},
        )
        result = api.call_function("flaky", {})
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(STATE["flaky_calls"], 3, "should have retried")

    def test_non_retryable_error_is_clean(self):
        os.environ["TEST_ORDERS_TOKEN"] = "x"
        self.addCleanup(os.environ.pop, "TEST_ORDERS_TOKEN", None)
        api = self._api(
            auth={"type": "bearer", "token_env": "TEST_ORDERS_TOKEN"},
            endpoints={"missing": {"method": "GET", "path": "/missing"}},
        )
        result = api.call_function("missing", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result.get("status"), 404)
        self.assertTrue(result["error"])
        self.assertIn("404", result["error"])

    def test_token_never_leaks_in_errors(self):
        os.environ["TEST_ORDERS_TOKEN"] = "SUPER-SECRET-TOKEN-XYZ"
        self.addCleanup(os.environ.pop, "TEST_ORDERS_TOKEN", None)
        api = self._api(
            auth={"type": "bearer", "token_env": "TEST_ORDERS_TOKEN"},
            endpoints={"missing": {"method": "GET", "path": "/missing"}},
        )
        result = api.call_function("missing", {})
        self.assertFalse(result["ok"])
        self.assertNotIn("SUPER-SECRET-TOKEN-XYZ", json.dumps(result))

    def test_connection_error_is_clean(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        api = self._api(
            base=f"http://127.0.0.1:{dead_port}",
            endpoints={"ping": {"method": "GET", "path": "/ping"}},
        )
        result = api.call_function("ping", {})
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_unknown_action(self):
        api = self._api(endpoints={})
        result = api.call_function("nope", {})
        self.assertFalse(result["ok"])
        self.assertIn("unknown action", result["error"])


class MockConnector:
    def __init__(self, connector_id="mock", tools=None):
        self.id = connector_id
        self.name = connector_id
        self._tools = tools or [("echo_task", "echo anything")]

    def discover_tools(self):
        for name, desc in self._tools:
            yield {
                "type": "function",
                "function": {"name": name, "description": desc, "parameters": {}},
            }

    def call_function(self, action_name, payload):
        if action_name != "echo_task":
            return {"ok": False, "error": "unknown mock action"}
        return {"ok": True, "data": {"echo": payload}}


class TestManager(ConnectorPipelineTestCase):
    def test_register_and_route_with_prefix_stripping(self):
        manager = ConnectorManager()
        manager.register(MockConnector("mock"))
        result = manager.execute_connector_action("mock", "mock.echo_task", {"hi": 1})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], {"echo": {"hi": 1}})
        result2 = manager.execute_connector_action("mock", "echo_task", {"hi": 2})
        self.assertTrue(result2["ok"])

    def test_unknown_connector_returns_clean_error(self):
        manager = ConnectorManager()
        result = manager.execute_connector_action("ghost", "anything", {})
        self.assertFalse(result["ok"])
        self.assertIn("not registered", result["error"])

    def test_discovery_dedupes_and_prefixes(self):
        manager = ConnectorManager()
        manager.register(MockConnector("mock_a", [("run", "a")]))
        manager.register(MockConnector("mock_b", [("run", "b")]))
        names = [t["function"]["name"] for t in manager.discover_tools()]
        self.assertIn("mock_a.run", names)
        self.assertIn("mock_b.run", names)

    def test_load_from_config_dict_and_file(self):
        manager = ConnectorManager()
        config = {
            "connectors": [
                {
                    "id": "loaded_api",
                    "base_url": self.srv.url(),
                    "endpoints": {
                        "list": {"method": "GET", "path": "/orders"}
                    },
                }
            ]
        }
        ids = manager.load_from_config(config)
        self.assertEqual(ids, ["loaded_api"])
        self.assertTrue(manager.execute_connector_action("loaded_api", "list", {})["ok"])

        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(config, fh)
            path = fh.name
        try:
            manager2 = ConnectorManager()
            ids2 = manager2.load_from_config(path)
            self.assertEqual(ids2, ["loaded_api"])
        finally:
            os.unlink(path)

    def test_module_router(self):
        manager = ConnectorManager()
        manager.register(MockConnector("module_mock"))
        # exercise the free-standing router with a temp global registration
        from connectors import manager as mgr_mod

        mgr_mod._default_manager.register(MockConnector("module_mock"))
        result = execute_connector_action("module_mock", "echo_task", {"via": "router"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["echo"]["via"], "router")


class TestMcpClient(unittest.TestCase):
    def test_schema_normalization(self):
        schema = MCPClient._normalize_tool(
            name="read_file",
            description="Read a file",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
        self.assertEqual(schema["function"]["name"], "read_file")
        self.assertEqual(schema["function"]["parameters"]["properties"]["path"]["type"], "string")

    def test_missing_sdk_gives_clean_error(self):
        # No command/url in config: start() raises regardless of SDK presence.
        # call_function must swallow it and return a clean error dict instead.
        client = MCPClient({"id": "broken_mcp", "transport": "stdio"})
        result = client.call_function("anything", {})
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()