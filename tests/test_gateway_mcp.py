"""Exercise the registered gateway tool with synthetic upstreams only."""
import unittest
from unittest.mock import AsyncMock, patch

from ermis_gateway import ACTIONS, Gateway
import ermis_system_server as system


class GatewayMCPTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.invoke = AsyncMock(return_value={"ok": True, "accepted": False})
        self.now = 10
        self.gateway = Gateway(self.invoke, lambda: self.now)
        replacement = patch.object(system, "gateway", self.gateway)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.session = "chat_session_0001"

    async def call(self, operation="request", **payload):
        result = await system.mcp.call_tool("ermis_gateway_execute", {
            "operation": operation, "payload": {"session_id": self.session, **payload}})
        self.assertFalse(result.is_error)
        return result.structured_content

    async def proposal(self):
        return await self.call(action="restart_service",
                               arguments={"service": "ermis-system-mcp.service"})

    async def test_discovery_schema(self):
        tools = await system.mcp.list_tools()
        gateway_tools = [tool for tool in tools if tool.name.startswith("ermis_gateway")]
        self.assertEqual([tool.name for tool in gateway_tools], ["ermis_gateway_execute"])
        schema = gateway_tools[0].input_schema
        self.assertEqual(set(schema["properties"]), {"operation", "payload"})
        self.assertEqual(set(schema["required"]), {"operation", "payload"})
        self.assertEqual(schema["properties"]["operation"]["enum"], ["request", "confirm"])

    async def test_all_reads_use_existing_allowlist(self):
        values = dict(wol_id=12, workorder_id=34, service="ermis-system-mcp.service",
                      job_id="a" * 32, workstation="Strantza")
        for name, action in ACTIONS.items():
            if action.write:
                continue
            args = {field: values[field] for field in action.fields}
            result = await self.call(action=name, arguments=args)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["result"], {"ok": True, "accepted": False})
            self.invoke.assert_awaited_with(action.server, action.tool, args)
        await self.call(text="show production overview")
        self.invoke.assert_awaited_with("Epoptia_MES", "production_overview", {})

    async def test_confirmation_binding_tampering_and_replay(self):
        proposal = await self.proposal()
        self.assertEqual(proposal["status"], "confirmation_required")
        self.invoke.assert_not_called()
        confirmation = dict(confirmation_id=proposal["confirmation_id"], approved=True)
        for extra in ({"session_id": "other_session_0001"}, {"approved": "true"},
                      {"arguments": {"service": "ermis-epoptia-mcp.service"}}):
            result = await self.call("confirm", **(confirmation | extra))
            self.assertEqual(result["status"], "invalid_confirmation")
        self.invoke.assert_not_called()
        self.assertEqual((await self.call("confirm", **confirmation))["status"], "completed")
        self.invoke.assert_awaited_once_with("Ermis_System", "ermis_service_control",
                                             {"service": "ermis-system-mcp.service", "operation": "restart"})
        self.assertEqual((await self.call("confirm", **confirmation))["status"], "invalid_confirmation")
        self.invoke.assert_awaited_once()

    async def test_cancel_and_expiry_do_not_dispatch(self):
        proposal = await self.proposal()
        self.assertEqual((await self.call("confirm", confirmation_id=proposal["confirmation_id"],
                                        approved=False))["status"], "cancelled")
        proposal = await self.proposal()
        self.now += 120
        self.assertEqual((await self.call("confirm", confirmation_id=proposal["confirmation_id"],
                                        approved=True))["status"], "invalid_confirmation")
        self.invoke.assert_not_called()

    async def test_unapproved_and_non_allowlisted_inputs_do_not_dispatch(self):
        for payload in [dict(action="ermis_gateway_execute", arguments={}),
                        dict(action="ermis_git_commit", arguments={"message": "x"}),
                        dict(action="restart_service", arguments={"service": "ssh.service"}),
                        dict(text="git status", approved=True),
                        dict(text="yes"), dict(session_id="short", text="git status")]:
            self.assertEqual((await self.call(**payload))["status"], "unsupported_request")
        self.assertEqual((await system.ermis_gateway_execute("execute", {}))["status"],
                         "unsupported_request")
        self.invoke.assert_not_called()

    async def test_upstream_write_failure_is_not_retried(self):
        self.invoke.side_effect = TimeoutError("synthetic private detail")
        self.assertEqual((await self.call(text="git status"))["status"], "upstream_unavailable")
        proposal = await self.proposal()
        confirmation = dict(confirmation_id=proposal["confirmation_id"], approved=True)
        self.assertEqual(await self.call("confirm", **confirmation),
                         {"ok": False, "status": "outcome_unknown", "retry_safe": False})
        await self.call("confirm", **confirmation)
        self.assertEqual(self.invoke.await_count, 2)
