"""Offline action-time confirmation contract for browser enrollment."""
import unittest
from unittest.mock import AsyncMock, patch
from ermis_gateway import ACTIONS, Gateway, validate


class LoginGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_fixed_actions(self):
        for verb in ('start', 'stop', 'finalize'):
            action = 'epoptia_browser_login_' + verb
            self.assertTrue(ACTIONS[action].write)
            invoke = AsyncMock(return_value={'ok': False, 'status': 'ready_not_enrolled'})
            gateway = Gateway(invoke=invoke)
            proposal = await gateway.request({'session_id': 'browser_login_test_0001',
                                              'action': action, 'arguments': {}})
            self.assertEqual(proposal['status'], 'confirmation_required')
            invoke.assert_not_called()
            await gateway.confirm({'session_id': 'browser_login_test_0001',
                                   'confirmation_id': proposal['confirmation_id'], 'approved': True})
            invoke.assert_awaited_once_with('Ermis_System', action, {})

    async def test_status_internal_dispatch_and_no_arbitrary_options(self):
        self.assertFalse(ACTIONS['epoptia_browser_login_status'].write)
        for verb in ('start', 'status', 'stop', 'finalize'):
            with self.assertRaises(ValueError):
                validate('epoptia_browser_login_' + verb, {'host': '0.0.0.0'})
        with patch('epoptia_browser.login_command', new_callable=AsyncMock,
                   return_value={'ok': False, 'status': 'ready_not_enrolled'}) as command:
            result = await Gateway().request({'session_id': 'browser_login_test_0001',
                                             'action': 'epoptia_browser_login_status', 'arguments': {}})
            self.assertEqual(result['result']['status'], 'ready_not_enrolled')
            command.assert_awaited_once_with('status')


class PublicLoginGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_cancel_replay_and_session_binding(self):
        for verb in ('start', 'finalize', 'stop'):
            for approved in (True, False):
                invoke = AsyncMock(return_value={'ok': False, 'status': 'login_not_ready'})
                gateway = Gateway(invoke=invoke)
                action = 'epoptia_login_' + verb
                args = {'ttl_minutes': 2} if verb == 'start' else {}
                proposal = await gateway.request(dict(session_id='login_public_test_0001', action=action, arguments=args))
                self.assertEqual(proposal['status'], 'confirmation_required')
                invoke.assert_not_called()
                confirm = dict(session_id='login_public_test_0001', confirmation_id=proposal['confirmation_id'], approved=approved)
                wrong = await gateway.confirm(dict(confirm, session_id='login_public_test_0002'))
                self.assertEqual(wrong['status'], 'invalid_confirmation')
                invoke.assert_not_called()
                await gateway.confirm(confirm)
                if approved:
                    invoke.assert_awaited_once_with('Ermis_System', action, args)
                else:
                    invoke.assert_not_called()
                self.assertEqual((await gateway.confirm(confirm))['status'], 'invalid_confirmation')

    async def test_expiry(self):
        invoke = AsyncMock()
        now = [0]
        gateway = Gateway(invoke=invoke, clock=lambda: now[0])
        proposal = await gateway.request(dict(session_id='login_public_test_0001', action='epoptia_login_start', arguments={}))
        now[0] = 121
        result = await gateway.confirm(dict(session_id='login_public_test_0001', confirmation_id=proposal['confirmation_id'], approved=True))
        self.assertEqual(result['status'], 'invalid_confirmation')
        invoke.assert_not_called()

    async def test_strict_contracts_and_no_io_when_blocked(self):
        from epoptia_browser import login_command, LOGIN_BLOCKERS
        for prefix in ('epoptia_login_', 'epoptia_browser_login_'):
            for verb in ('status', 'start', 'finalize', 'stop'):
                self.assertEqual(ACTIONS[prefix + verb].write, verb != 'status')
                validate(prefix + verb, {})
                for field in ('host', 'url', 'password', 'cookies', 'session', 'path', 'confirmed'):
                    with self.assertRaises(ValueError):
                        validate(prefix + verb, {field: 'SYNTHETIC'})
                if verb != 'start':
                    with self.assertRaises(ValueError):
                        validate(prefix + verb, {'ttl_minutes': 1})
            for ttl in (True, False, None, 0, 6, 1.5, '5'):
                with self.assertRaises(ValueError):
                    validate(prefix + 'start', {'ttl_minutes': ttl})
            for ttl in (1, 5):
                validate(prefix + 'start', {'ttl_minutes': ttl})
        with patch('admin_bootstrap.login_bootstrap.source_blockers', return_value=list(LOGIN_BLOCKERS)), patch('epoptia_browser.asyncio.create_subprocess_exec') as spawn, patch('builtins.open') as read:
            for verb in ('status', 'start', 'finalize', 'stop'):
                result = await login_command(verb)
                self.assertFalse(result['ok'])
                self.assertEqual(result['blockers'], list(LOGIN_BLOCKERS))
                self.assertFalse(result['operational_ready'])
                self.assertFalse(result['bootstrap_install_ready'])
                self.assertNotIn('url', result)
            spawn.assert_not_called()
            read.assert_not_called()

    async def test_system_gateway_status_is_local_and_readonly(self):
        # Exercise real dispatch without requiring the optional MCP transport or
        # registering a host server. Only its registration decorator is faked.
        import importlib.util
        from pathlib import Path
        import sys
        from types import ModuleType
        class FixtureMCP:
            def __init__(self, *args, **kwargs):
                pass
            def tool(self, **kwargs):
                return lambda function: function
            def run(self, *args, **kwargs):
                raise AssertionError('transport must not start')
        modules = {name: ModuleType(name) for name in ('mcp', 'mcp.server', 'mcp.server.mcpserver')}
        modules['mcp.server.mcpserver'].MCPServer = FixtureMCP
        spec = importlib.util.spec_from_file_location('fixture_system_server',
                Path(__file__).resolve().parents[1] / 'ermis_system_server.py')
        system = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(system)
        with patch('ermis_gateway._call_existing', new_callable=AsyncMock) as network, patch('epoptia_browser.login_command', new_callable=AsyncMock, return_value={'status': 'login_not_ready'}):
            result = await system.ermis_gateway_execute('request', dict(
                session_id='login_public_test_0001', action='epoptia_login_status', arguments={}))
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['result']['status'], 'login_not_ready')
            network.assert_not_called()
