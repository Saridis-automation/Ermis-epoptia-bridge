"""Synthetic MCP timeout checks; advance loop time without waiting minutes."""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx2

from ermis_gateway import Gateway


class GatewayTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Virtual clock jumps are intentional, not slow event-loop callbacks.
        asyncio.get_running_loop().slow_callback_duration = 300

    async def exchange(self, action, elapsed=0, read_failure=False):
        loop = asyncio.get_running_loop()
        real_time = loop.time
        offset = 0
        calls = []
        client = httpx2.AsyncClient
        epoptia = action in ("production_overview", "workstation_wip")
        expected_overall = 120 if epoptia else 30

        async def handler(request):
            nonlocal offset
            self.assertEqual(request.extensions["timeout"], {
                "connect": 30, "read": 120 if epoptia else 30,
                "write": 30, "pool": 30})
            body = json.loads(request.content)
            method = body["method"]
            if method in ("notifications/initialized", "notifications/cancelled"):
                return httpx2.Response(202)
            if method == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "synthetic", "version": "1"}}
            elif method == "tools/call":
                calls.append(body["params"]["name"])
                if read_failure:
                    raise httpx2.ReadTimeout("synthetic private detail", request=request)
                # Exercise the actual asyncio deadline using a virtual 66s/121s delay.
                offset += elapsed
                await asyncio.sleep(0.001)
                result = {"content": [], "structuredContent": {"ok": True}}
            else:
                self.assertEqual(method, "tools/list")
                result = {"tools": [{"name": calls[0], "inputSchema": {"type": "object"}}]}
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": result})

        def factory(**kwargs):
            return client(transport=httpx2.MockTransport(handler), **kwargs)

        with patch.object(loop, "time", side_effect=lambda: real_time() + offset), \
                patch("httpx2.AsyncClient", side_effect=factory), \
                patch("ermis_gateway.asyncio.timeout", wraps=asyncio.timeout) as deadline:
            result = await Gateway().execute(action, {})
            self.assertEqual(deadline.call_args_list[0].args, (expected_overall,))
        self.assertEqual(len(calls), 1)  # No automatic retry is introduced.
        return result

    async def test_epoptia_long_calls_complete_beyond_30_seconds(self):
        for action in ("production_overview", "workstation_wip"):
            with self.subTest(action=action):
                self.assertEqual(await self.exchange(action, elapsed=66), {
                    "ok": True, "status": "completed", "action": action,
                    "result": {"ok": True}})

    async def test_system_timeouts_remain_30_seconds(self):
        self.assertTrue((await self.exchange("health"))["ok"])
        self.assertEqual(await self.exchange("health", elapsed=31), {
            "ok": False, "status": "upstream_unavailable", "retry_safe": True})

    async def test_epoptia_overall_deadline_expires_safely(self):
        # The SDK wraps cancellation in BrokenResourceError; preserve its existing
        # generic diagnostic while ensuring the bounded, retry-safe failure.
        self.assertEqual(await self.exchange("production_overview", elapsed=121), {
            "ok": False, "status": "upstream_unavailable", "retry_safe": True,
            "failure_stage": "protocol_error", "error_category": "unexpected_error"})

    async def test_epoptia_read_timeout_preserves_safe_error(self):
        self.assertEqual(await self.exchange("workstation_wip", read_failure=True), {
            "ok": False, "status": "upstream_unavailable", "retry_safe": True,
            "failure_stage": "tools_call_failed", "error_category": "read_timeout"})


if __name__ == "__main__":
    unittest.main()
