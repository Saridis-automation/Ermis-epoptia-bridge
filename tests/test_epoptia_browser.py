import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import epoptia_browser


class BrowserToolTest(unittest.TestCase):
    def test_invalid_input_never_spawns(self):
        with patch.object(epoptia_browser.asyncio, 'create_subprocess_exec') as spawn:
            for value in ['https://example.invalid', '../workorders', '--help', {}, None]:
                self.assertEqual(asyncio.run(epoptia_browser.inspect(value))['status'], 'invalid_page')
            spawn.assert_not_called()

    def test_missing_setup_safe_local_diagnostic(self):
        self.assertEqual(asyncio.run(epoptia_browser.inspect()),
                         {'ok': False, 'status': 'auth_material_missing'})

    def test_errors_never_expose_worker_output(self):
        worker = AsyncMock(returncode=1)
        worker.communicate.return_value = (b'PRIVATE', None)
        with patch.object(epoptia_browser.asyncio, 'create_subprocess_exec', return_value=worker) as spawn:
            self.assertEqual(asyncio.run(epoptia_browser.inspect())['status'], 'launch_failed')
            self.assertEqual(spawn.call_args.args[0], '/usr/bin/node')
            self.assertEqual(set(spawn.call_args.kwargs['env']), {'PATH', 'LANG', 'HOME'})

    def test_mcp_schema_and_delegation(self):
        import ermis_system_server as server
        tool = next(t for t in asyncio.run(server.mcp.list_tools())
                    if t.name == 'ermis_epoptia_browser_inspect')
        self.assertEqual(set(tool.input_schema['properties']), {'page'})
        with patch.object(epoptia_browser, 'inspect', return_value={'ok': False}) as inspect:
            self.assertEqual(asyncio.run(server.ermis_epoptia_browser_inspect()), {'ok': False})
            inspect.assert_awaited_once_with('production_report')

    def test_timeout_kills_only_dedicated_worker_group(self):
        worker = AsyncMock(returncode=None, pid=12345)
        worker.communicate.side_effect = asyncio.TimeoutError
        with patch.object(epoptia_browser.asyncio, 'create_subprocess_exec', return_value=worker), \
                patch('os.killpg') as kill:
            self.assertEqual(asyncio.run(epoptia_browser.inspect())['status'], 'launch_failed')
            kill.assert_called_once()
            self.assertEqual(kill.call_args.args[0], 12345)
            worker.wait.assert_awaited_once()

    def test_worker_permission_failure_is_sanitized(self):
        with patch.object(epoptia_browser.asyncio, 'create_subprocess_exec',
                          side_effect=PermissionError('PRIVATE')):
            self.assertEqual(asyncio.run(epoptia_browser.inspect_scope('smoke')),
                             {'ok': False, 'status': 'runtime_permission_denied'})
