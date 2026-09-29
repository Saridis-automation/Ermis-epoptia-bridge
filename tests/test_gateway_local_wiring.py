"""Verify local MCP wiring without credentials, sockets, or production imports."""
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from httpx2 import ASGITransport, AsyncClient
from mcp.server.mcpserver import MCPServer

from ermis_gateway import ENDPOINTS, Gateway


class GatewayLocalWiringTest(unittest.IsolatedAsyncioTestCase):
    async def test_epoptia_action_uses_default_mcp_route_and_post(self):
        endpoint = urlsplit(ENDPOINTS["Epoptia_MES"])
        self.assertEqual((endpoint.scheme, endpoint.hostname, endpoint.port,
                          endpoint.path), ("http", "127.0.0.1", 8000, "/mcp"))
        server = MCPServer("Synthetic Epoptia")

        @server.tool()
        async def production_overview() -> dict:
            return {"ok": True, "synthetic": True}

        # Same transport options as mcp_server.py; use the SDK's default path.
        app = server.streamable_http_app(json_response=True, stateless_http=True,
                                         host="127.0.0.1")
        requests = []

        async def capture(request):
            if request.method == "POST":
                payload = json.loads(request.content)
                requests.append((request.method, request.url.path, payload["method"]))

        def local_client(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            kwargs.setdefault("event_hooks", {}).setdefault("request", []).append(capture)
            return AsyncClient(transport=ASGITransport(app=app), **kwargs)

        async with app.router.lifespan_context(app):
            with patch("httpx2.AsyncClient", side_effect=local_client):
                result = await Gateway().execute("production_overview", {})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"], {"ok": True, "synthetic": True})
        self.assertIn(("POST", "/mcp", "initialize"), requests)
        self.assertIn(("POST", "/mcp", "tools/call"), requests)
        self.assertTrue(all(path == "/mcp" for _, path, _ in requests))

    async def test_wrong_path_is_reported_as_upstream_unavailable(self):
        server = MCPServer("Synthetic Epoptia")
        app = server.streamable_http_app(json_response=True, stateless_http=True)

        def local_client(**kwargs):
            return AsyncClient(transport=ASGITransport(app=app), **kwargs)

        async with app.router.lifespan_context(app):
            with patch("httpx2.AsyncClient", side_effect=local_client), \
                    patch.dict(ENDPOINTS, {"Epoptia_MES": "http://127.0.0.1:8000/"}):
                result = await Gateway().execute("production_overview", {})
        self.assertEqual(result, {"ok": False, "status": "upstream_unavailable",
                                  "retry_safe": True, "failure_stage": "initialize_failed",
                                  "error_category": "http_status_404"})
