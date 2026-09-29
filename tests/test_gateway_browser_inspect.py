"""Exercise the existing MCP runtime path without network or real session data."""
import unittest
from unittest.mock import AsyncMock, patch

import epoptia_browser
import ermis_system_server as system
from ermis_gateway import ACTIONS, Gateway, call_existing
from ermis_gateway_voice import tool_schema


class BrowserGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def call(self, arguments):
        result = await system.mcp.call_tool('ermis_gateway_execute', {
            'operation': 'request', 'payload': {
                'session_id': 'browser_test_0001',
                'action': 'epoptia_browser_inspect', 'arguments': arguments}})
        self.assertFalse(result.is_error)
        return result.structured_content

    async def test_real_runtime_dispatches_all_scopes_without_mcp_http(self):
        self.assertFalse(ACTIONS['epoptia_browser_inspect'].write)
        for args, expected in [({}, 'dates'), ({'scope': 'dates'}, 'dates'),
                               ({'scope': 'session'}, 'session'), ({'scope': 'smoke'}, 'smoke')]:
            worker = AsyncMock(returncode=0)
            worker.communicate.return_value = (b'{"ok":false,"status":"session_unavailable"}', None)
            with patch.object(epoptia_browser.asyncio, 'create_subprocess_exec',
                              return_value=worker) as spawn, \
                    patch('ermis_gateway._call_existing') as http:
                result = await self.call(args)
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(spawn.call_args.args[2], expected)
                self.assertEqual(result['result']['status'], 'session_unavailable')
                if expected == 'session':
                    self.assertEqual(set(result['result']), {'ok', 'status', 'usable'})
                    self.assertFalse(result['result']['usable'])
                http.assert_not_called()

    async def test_invalid_arguments_never_dispatch(self):
        with patch.object(epoptia_browser, 'inspect_scope') as inspect:
            for args in [None, [], {'scope': None}, {'scope': True}, {'scope': []},
                         {'scope': 'DATES'}, {'scope': 'login'}, {'page': 'workorders'},
                         {'scope': 'dates', 'url': 'https://example.invalid'},
                         {'scope': 'smoke', 'script': 'PRIVATE'}]:
                self.assertEqual((await self.call(args))['status'], 'unsupported_request')
            inspect.assert_not_called()
        with self.assertRaises(ValueError):
            await call_existing('Ermis_System', 'epoptia_browser_inspect', {'url': 'PRIVATE'})

    async def test_default_gateway_also_uses_local_dispatch(self):
        with patch.object(epoptia_browser, 'inspect_scope', return_value={'ok': True}) as inspect:
            result = await Gateway().execute('epoptia_browser_inspect', {'scope': 'smoke'})
            self.assertEqual(result['result'], {'ok': True})
            inspect.assert_awaited_once_with(scope='smoke')

    async def test_session_errors_and_output_are_bounded(self):
        for response in [{'ok': False, 'status': 'browser_unavailable'},
                         {'ok': True, 'status': 'PRIVATE', 'cookies': 'PRIVATE'}]:
            with patch.object(epoptia_browser, '_run', return_value=response):
                result = await epoptia_browser.inspect_scope('session')
                self.assertEqual(set(result), {'ok', 'status', 'usable'})
                self.assertFalse(result['usable'])
                self.assertNotIn('PRIVATE', str(result))
        with patch.object(epoptia_browser.asyncio, 'create_subprocess_exec') as spawn:
            for scope in [None, {}, True, 'login']:
                self.assertEqual((await epoptia_browser.inspect_scope(scope))['status'], 'invalid_scope')
            spawn.assert_not_called()

    async def test_precise_blocker_reasons_survive_session_gateway(self):
        for status in ('auth_material_missing', 'session_policy_invalid',
                       'session_state_invalid', 'interactive_login_required',
                       'playwright_missing', 'chromium_missing',
                       'chromium_dependencies_missing', 'runtime_permission_denied',
                       'launch_failed'):
            with patch.object(epoptia_browser, '_run', return_value={
                    'ok': False, 'status': status, 'private': 'PRIVATE'}):
                self.assertEqual(await epoptia_browser.inspect_scope('session'),
                                 {'ok': False, 'usable': False, 'status': status})

    def test_voice_allowlist_has_optional_enum(self):
        schema = next(t['parameters'] for t in tool_schema() if t['name'] == 'epoptia_browser_inspect')
        self.assertEqual(schema['properties']['scope']['enum'], ['session', 'dates', 'smoke'])
        self.assertEqual(schema['required'], [])
        self.assertFalse(schema['additionalProperties'])
