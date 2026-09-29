"""Local-only tests: never read or execute the installed privileged wrapper."""
import asyncio
import subprocess
import unittest
from unittest.mock import AsyncMock, Mock, patch

import chromium_dependencies as deps
from ermis_gateway import Gateway, call_existing


class ChromiumGatewayTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gateway = Gateway()
        self.session = "chromium_test_0001"
        # The installer is fully mocked; keep it on this test loop to avoid
        # depending on executor shutdown in restricted test environments.
        async def inline(function):
            return function()
        worker = patch("ermis_gateway.asyncio.to_thread", side_effect=inline)
        worker.start()
        self.addCleanup(worker.stop)

    async def propose(self, arguments):
        return await self.gateway.request({"session_id": self.session,
            "action": "install_chromium_dependencies", "arguments": arguments})

    async def confirm(self, proposal, **overrides):
        return await self.gateway.confirm({"session_id": self.session,
            "confirmation_id": proposal["confirmation_id"], "approved": True, **overrides})

    async def test_confirmation_required_and_fixed_command_once(self):
        with patch.object(deps, "_installed_action_available", return_value=True) as probe, \
                patch.object(deps.subprocess, "run", return_value=Mock(returncode=0)) as run:
            proposal = await self.propose({})
            self.assertEqual(proposal["status"], "confirmation_required")
            self.assertEqual(proposal["arguments"], {})
            self.assertIn("fixed Playwright/Chromium Ubuntu runtime dependency set", proposal["prompt"])
            self.assertIn("no Epoptia data changes", proposal["prompt"])
            probe.assert_not_called()
            run.assert_not_called()
            for overrides in ({"approved": "true"}, {"session_id": "other_session_0001"},
                              {"arguments": {}}, {"confirmation_id": "unknown"}):
                self.assertEqual((await self.confirm(proposal, **overrides))["status"],
                                 "invalid_confirmation")
            run.assert_not_called()
            self.assertEqual(await self.confirm(proposal), {"ok": True, "status": "completed"})
            run.assert_called_once_with(
                ("/usr/bin/sudo", "-n", "/usr/local/sbin/ermis-admin",
                 "browser", "install-chromium-dependencies"),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                shell=False, check=False, timeout=930, cwd="/home/ermis/projects/epoptia-bridge",
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"})
            self.assertEqual((await self.confirm(proposal))["status"], "invalid_confirmation")
            run.assert_called_once()

    async def test_real_probe_path_requires_single_use_confirmation(self):
        for code in (0, 1):
            with patch.object(deps.subprocess, "run", return_value=Mock(returncode=code)) as run:
                proposal = await self.propose({})
                run.assert_not_called()
                result = await self.confirm(proposal)
                self.assertEqual(result["status"], "completed" if code == 0
                                 else "admin_wrapper_action_unavailable")
                self.assertEqual([c.args[0] for c in run.call_args_list],
                                 [deps.PROBE_COMMAND, deps.COMMAND] if code == 0
                                 else [deps.PROBE_COMMAND])
                self.assertEqual((await self.confirm(proposal))["status"], "invalid_confirmation")
                self.assertEqual(run.call_count, 2 if code == 0 else 1)

    async def test_extra_arguments_and_arbitrary_input_rejected(self):
        with patch.object(deps, "install") as install:
            for args in (None, [], "", {"packages": ["curl"]}, {"package": "curl"},
                         {"args": []}, {"command": "echo test; id"}, {"shell": "id"},
                         {"operation": "restart"}, {"argv": []}):
                self.assertEqual((await self.propose(args))["status"], "unsupported_request")
                with self.assertRaises(ValueError):
                    await call_existing("Ermis_System", "install_chromium_dependencies", args)
            self.assertEqual(self.gateway.pending, {})
            install.assert_not_called()
        with self.assertRaises(TypeError):
            deps.install("curl")

    async def test_cancel_expiry_and_concurrent_approval(self):
        with patch.object(deps, "install", return_value={"status": "completed"}) as install:
            proposal = await self.propose({})
            self.assertEqual((await self.confirm(proposal, approved=False))["status"], "cancelled")
            self.assertEqual((await self.confirm(proposal))["status"], "invalid_confirmation")
            proposal = await self.propose({})
            self.gateway.clock = lambda: float("inf")
            self.assertEqual((await self.confirm(proposal))["status"], "invalid_confirmation")
            install.assert_not_called()
            self.gateway = Gateway()
            proposal = await self.propose({})
            results = await asyncio.gather(*(self.confirm(proposal) for _ in range(4)))
            self.assertEqual(sum(r["ok"] for r in results), 1)
            install.assert_called_once_with()

    async def test_unavailable_wrapper_and_sanitized_results(self):
        with patch.object(deps, "_installed_action_available", return_value=False), \
                patch.object(deps.subprocess, "run") as run:
            self.assertEqual(await self.confirm(await self.propose({})),
                             {"ok": False, "status": "admin_wrapper_action_unavailable"})
            run.assert_not_called()
        for result in ({"status": "completed", "output": "synthetic private detail"},
                       {"status": "synthetic private detail"}, {"status": []}, None):
            with patch.object(deps, "install", return_value=result):
                response = await self.confirm(await self.propose({}))
                self.assertEqual(set(response), {"ok", "status"})
                self.assertNotIn("synthetic private detail", str(response))
        with patch.object(deps, "install", side_effect=RuntimeError("synthetic private detail")):
            self.assertEqual(await self.confirm(await self.propose({})),
                             {"ok": False, "status": "outcome_unknown"})

    async def test_existing_restart_dispatch_is_preserved(self):
        invoke = AsyncMock(return_value={"ok": True})
        self.gateway = Gateway(invoke=invoke)
        proposal = await self.gateway.request({"session_id": self.session,
            "action": "restart_service", "arguments": {"service": "ermis-system-mcp.service"}})
        self.assertEqual(proposal["prompt"], "Restart service ermis-system-mcp.service?")
        invoke.assert_not_called()
        self.assertEqual((await self.confirm(proposal))["status"], "completed")
        invoke.assert_awaited_once_with("Ermis_System", "ermis_service_control",
                                      {"service": "ermis-system-mcp.service", "operation": "restart"})


class ChromiumAdapterTest(unittest.TestCase):
    def test_capability_does_not_require_readable_wrapper_source(self):
        with patch("builtins.open", side_effect=PermissionError) as read, \
                patch("os.open", side_effect=PermissionError) as low_level_read, \
                patch.object(deps.subprocess, "run", return_value=Mock(returncode=0)) as run:
            self.assertTrue(deps._installed_action_available())
            read.assert_not_called()
            low_level_read.assert_not_called()
            run.assert_called_once_with(
                ("/usr/bin/sudo", "-n", "/usr/local/sbin/ermis-admin",
                 "browser", "check-chromium-dependencies"),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                shell=False, check=False, timeout=10, cwd="/home/ermis/projects/epoptia-bridge",
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"})

    def test_old_missing_or_failed_probe_never_installs(self):
        for outcome in (Mock(returncode=1), Mock(returncode=127), FileNotFoundError(),
                        PermissionError(), OSError(), subprocess.TimeoutExpired("probe", 10)):
            with patch.object(deps.subprocess, "run", **(
                    {"side_effect": outcome} if isinstance(outcome, Exception)
                    else {"return_value": outcome})) as run:
                self.assertEqual(deps.install(),
                                 {"ok": False, "status": "admin_wrapper_action_unavailable"})
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0], deps.PROBE_COMMAND)

    def test_execution_failures_withhold_all_output(self):
        for outcome, status in ((Mock(returncode=1, stdout="private", stderr="private"),
                                 "admin_wrapper_action_failed"),
                                (OSError("private"), "admin_wrapper_execution_unavailable"),
                                (subprocess.TimeoutExpired("private", 930, output="private"),
                                 "outcome_unknown")):
            with patch.object(deps, "_installed_action_available", return_value=True), \
                    patch.object(deps.subprocess, "run", **(
                        {"side_effect": outcome} if isinstance(outcome, Exception)
                        else {"return_value": outcome})) as run:
                self.assertEqual(deps.install(), {"ok": False, "status": status})
                run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
