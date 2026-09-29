"""Exercise the real Gateway MCP client over synthetic HTTP; no live services."""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx2

from ermis_gateway import Gateway


class OpenSSE(httpx2.AsyncByteStream):
    async def __aiter__(self):
        yield b": connected\n\n"
        await asyncio.Event().wait()


class GatewayProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def overview(self, handler):
        client = httpx2.AsyncClient

        def factory(**kwargs):
            self.assertEqual({k: v for k, v in kwargs.items() if k != "event_hooks"}, {
                "trust_env": False, "follow_redirects": False,
                "timeout": httpx2.Timeout(30, read=120)})
            return client(transport=httpx2.MockTransport(handler), **kwargs)

        with patch("httpx2.AsyncClient", side_effect=factory):
            return await asyncio.wait_for(Gateway().execute("production_overview", {}), 3)

    async def valid_transport(self, *, sessionful=False, sse=False, text=False):
        methods, ids = [], []
        expected = {"ok": True, "total_wols": 2}

        async def handler(request):
            self.assertEqual(str(request.url), "http://127.0.0.1:8000/mcp")
            self.assertEqual(set(request.headers["accept"].split(", ")),
                             {"application/json", "text/event-stream"})
            self.assertEqual(request.extensions["timeout"]["connect"], 30)
            self.assertEqual(request.extensions["timeout"]["read"], 120)
            if request.method in ("GET", "DELETE"):
                self.assertTrue(sessionful)
                self.assertEqual(request.headers["mcp-session-id"], "synthetic-session")
                if request.method == "GET":
                    return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                                           stream=OpenSSE())
                return httpx2.Response(200)
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.headers["content-type"], "application/json")
            body = json.loads(request.content)
            self.assertEqual(body["jsonrpc"], "2.0")
            method = body["method"]
            methods.append(method)
            headers = {}
            if method == "initialize":
                self.assertEqual(methods, ["initialize"])
                self.assertNotIn("mcp-session-id", request.headers)
                self.assertIn("clientInfo", body["params"])
                self.assertIn("capabilities", body["params"])
                result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "synthetic", "version": "1"}}
                if sessionful:
                    headers["mcp-session-id"] = "synthetic-session"
            else:
                self.assertEqual(request.headers.get("mcp-session-id"),
                                 "synthetic-session" if sessionful else None)
                self.assertEqual(request.headers["mcp-protocol-version"], "2025-03-26")
                if method == "notifications/initialized":
                    self.assertEqual(methods, ["initialize", "notifications/initialized"])
                    self.assertNotIn("id", body)
                    return httpx2.Response(202)
                if method == "tools/list":
                    # The SDK discovers output schemas to validate tool results.
                    self.assertEqual(methods, ["initialize", "notifications/initialized",
                                               "tools/call", "tools/list"])
                    result = {"tools": [{"name": "production_overview",
                                         "inputSchema": {"type": "object"}}]}
                else:
                    self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/call"])
                    self.assertEqual(body["params"]["name"], "production_overview")
                    self.assertEqual(body["params"]["arguments"], {})
                    result = {"isError": False, "content": []}
                    if text:
                        result["content"] = [{"type": "text", "text": json.dumps(expected)}]
                    else:
                        result["structuredContent"] = expected
            self.assertNotIn(body["id"], ids)
            ids.append(body["id"])
            response = {"jsonrpc": "2.0", "id": body["id"], "result": result}
            if sse:
                headers["content-type"] = "text/event-stream"
                return httpx2.Response(200, headers=headers,
                                       content="event: message\ndata: " + json.dumps(response) + "\n\n")
            return httpx2.Response(200, headers=headers, json=response)

        self.assertEqual(await self.overview(handler), {
            "ok": True, "status": "completed", "action": "production_overview", "result": expected})
        self.assertGreaterEqual(len(ids), 2)

    async def test_stateless_json_matches_server_configuration(self):
        await self.valid_transport()

    async def test_stateless_json_text_tool_result(self):
        await self.valid_transport(text=True)

    async def test_sessionful_sse_with_open_get(self):
        await self.valid_transport(sessionful=True, sse=True)

    async def test_stateless_sse(self):
        await self.valid_transport(sse=True)

    async def test_real_server_transport_with_synthetic_tool(self):
        from mcp.server.mcpserver import MCPServer

        server = MCPServer("synthetic")

        @server.tool()
        async def production_overview(dashboard: bool = False) -> dict:
            return {"ok": True, "total_wols": 2}

        # Match mcp_server.py without importing its credential-loading entrypoint.
        app = server.streamable_http_app(json_response=True, stateless_http=True)
        transport = httpx2.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            result = await self.overview(transport.handle_async_request)
        self.assertEqual(result, {"ok": True, "status": "completed",
                                  "action": "production_overview",
                                  "result": {"ok": True, "total_wols": 2}})

    async def test_connection_and_timeout_failures_are_safe(self):
        for error in (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.ReadTimeout):
            with self.subTest(error=error.__name__):
                async def handler(request):
                    raise error("synthetic private failure detail", request=request)

                self.assertEqual(await self.overview(handler), {
                    "ok": False, "status": "upstream_unavailable", "retry_safe": True,
                    "failure_stage": "initialize_failed",
                    "error_category": {httpx2.ConnectError: "connect_error",
                                       httpx2.ConnectTimeout: "connect_timeout",
                                       httpx2.ReadTimeout: "read_timeout"}[error]})


    async def failure_transport(self, target, failure):
        async def handler(request):
            body = json.loads(request.content)
            method = body["method"]
            if method == target:
                return failure(body)
            if method == "notifications/initialized":
                return httpx2.Response(202)
            result = {
                "initialize": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                               "serverInfo": {"name": "synthetic", "version": "1"}},
                "tools/call": {"isError": False, "content": [], "structuredContent": {"ok": True}},
                "tools/list": {"tools": []},
            }[method]
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
        return await self.overview(handler)

    async def test_http_failure_stages(self):
        for method, stage in (("initialize", "initialize_failed"),
                              ("tools/list", "tools_list_failed"),
                              ("tools/call", "tools_call_failed")):
            for code in (401, 404, 503):
                with self.subTest(method=method, code=code):
                    result = await self.failure_transport(method, lambda body: httpx2.Response(
                        code, text="synthetic private payload", headers={"set-cookie": "synthetic"}))
                    self.assertEqual(result, {"ok": False, "status": "upstream_unavailable",
                                              "retry_safe": True, "failure_stage": stage,
                                              "error_category": f"http_status_{code}"})

    async def test_ignored_notification_http_error_preserves_success(self):
        # The SDK ignores notification HTTP errors; diagnostics must not change success.
        result = await self.failure_transport("notifications/initialized",
                                              lambda body: httpx2.Response(503))
        self.assertEqual(result, {"ok": True, "status": "completed",
                                  "action": "production_overview", "result": {"ok": True}})

    async def test_rpc_failure_stages(self):
        for method, stage in (("initialize", "initialize_failed"),
                              ("tools/list", "tools_list_failed"),
                              ("tools/call", "tools_call_failed")):
            with self.subTest(method=method):
                result = await self.failure_transport(method, lambda body: httpx2.Response(200, json={
                    "jsonrpc": "2.0", "id": body["id"],
                    "error": {"code": -32603, "message": "synthetic private detail"}}))
                self.assertEqual(result, {"ok": False, "status": "upstream_unavailable",
                                          "retry_safe": True, "failure_stage": stage,
                                          "error_category": stage})

    async def test_tool_error_and_invalid_content(self):
        for payload, category in (({"isError": True, "content": []}, "tools_call_failed"),
                                  ({"content": [{"type": "text", "text": "synthetic invalid JSON"}]},
                                   "protocol_error")):
            with self.subTest(category=category):
                result = await self.failure_transport("tools/call", lambda body: httpx2.Response(200, json={
                    "jsonrpc": "2.0", "id": body["id"], "result": payload}))
                self.assertEqual(result, {"ok": False, "status": "upstream_unavailable",
                                          "retry_safe": True, "failure_stage": category,
                                          "error_category": category})

    async def test_nested_transport_and_protocol_failures(self):
        for error, category in ((httpx2.ConnectError("synthetic"), "connect_error"),
                                (httpx2.RemoteProtocolError("synthetic"), "protocol_error"),
                                (TimeoutError("synthetic"), "read_timeout")):
            with self.subTest(category=category):
                async def fail(*args):
                    raise ExceptionGroup("synthetic", [ExceptionGroup("synthetic", [error])])
                result = await Gateway(fail).execute("production_overview", {})
                self.assertEqual(result, {"ok": False, "status": "upstream_unavailable",
                                          "retry_safe": True, "failure_stage": "unexpected_error",
                                          "error_category": category})

    async def test_unexpected_failure_and_system_behavior(self):
        async def fail(*args):
            raise RuntimeError("synthetic private exception detail")
        self.assertEqual(await Gateway(fail).execute("production_overview", {}), {
            "ok": False, "status": "upstream_unavailable", "retry_safe": True,
            "failure_stage": "unexpected_error", "error_category": "unexpected_error"})
        self.assertEqual(await Gateway(fail).execute("health", {}), {
            "ok": False, "status": "upstream_unavailable", "retry_safe": True})


if __name__ == "__main__":
    unittest.main()
