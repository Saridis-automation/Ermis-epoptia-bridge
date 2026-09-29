"""Local synthetic checks; no real browser, service, credentials or network."""
import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import chromium_runtime_smoke as smoke
import ermis_system_server as system
from ermis_gateway import ACTIONS, Gateway


class SmokeTest(unittest.IsolatedAsyncioTestCase):
    async def request(self, arguments):
        result = await system.mcp.call_tool('ermis_gateway_execute', {
            'operation': 'request', 'payload': {'session_id': 'smoke_test_00001',
                'action': 'chromium_runtime_smoke', 'arguments': arguments}})
        self.assertFalse(result.is_error)
        return result.structured_content

    async def test_service_dispatch_and_no_confirmation(self):
        self.assertFalse(ACTIONS['chromium_runtime_smoke'].write)
        with patch.object(smoke, 'run', return_value={'status': 'completed', 'private': 'PRIVATE'}) as run, \
                patch.object(system, 'call_cached_production') as other:
            self.assertEqual(await self.request({}), {'ok': True, 'status': 'completed'})
            run.assert_awaited_once_with()
            other.assert_not_called()
            self.assertEqual(await Gateway().execute('chromium_runtime_smoke', {}),
                             {'ok': False, 'status': 'service_context_required'})
            self.assertEqual(run.await_count, 1)

    async def test_invalid_arguments_and_errors(self):
        with patch.object(smoke, 'run', side_effect=RuntimeError('PRIVATE')) as run:
            for args in (None, [], {'url': 'about:blank'}, {'command': 'PRIVATE'}, {'timeout': 1}):
                self.assertEqual((await self.request(args))['status'], 'unsupported_request')
            run.assert_not_called()
            self.assertEqual(await self.request({}), {'ok': False, 'status': 'launch_failed'})

    async def test_status_allowlist(self):
        for value in (None, [], {'status': []}, {'status': 'PRIVATE'}, {'ok': True}):
            self.assertEqual(smoke.sanitize_result(value), {'ok': False, 'status': 'launch_failed'})
        for status in smoke.STATUSES:
            self.assertEqual(smoke.sanitize_result({'status': status, 'private': 'PRIVATE'}),
                             {'ok': status == 'completed', 'status': status})

    async def test_worker_output_deadline_and_cleanup(self):
        for output, code, expected in ((b'{"status":"completed","private":"PRIVATE"}', 0, 'completed'),
                (b'PRIVATE', 0, 'launch_failed'), (b'x' * 257, 0, 'launch_failed'),
                (b'{"status":"completed"}', 1, 'launch_failed'),
                (asyncio.TimeoutError(), None, 'timeout')):
            worker = AsyncMock(pid=12345, returncode=code)
            if isinstance(output, Exception):
                worker.stdout.read.side_effect = output
            else:
                worker.stdout.read.return_value = output
            with patch.object(smoke.asyncio, 'create_subprocess_exec', return_value=worker) as spawn, \
                    patch.object(smoke.os, 'killpg') as kill:
                self.assertEqual((await smoke.run())['status'], expected)
                worker.stdout.read.assert_awaited_once_with(257)
                kill.assert_called_once_with(12345, smoke.signal.SIGKILL)
                options = spawn.call_args.kwargs
                self.assertTrue(options['start_new_session'])
                self.assertEqual(spawn.call_args.args,
                    ('/usr/bin/node', str(smoke.ROOT / 'chromium_runtime_smoke.cjs')))
                self.assertFalse(Path(options['env']['TMPDIR']).exists())

    async def test_busy_and_cancellation_cleanup(self):
        async with smoke._lock:
            self.assertEqual((await smoke.run())['status'], 'busy')
        worker = AsyncMock(pid=12345, returncode=None)
        worker.stdout.read.side_effect = asyncio.CancelledError()
        with patch.object(smoke.asyncio, 'create_subprocess_exec', return_value=worker), \
                patch.object(smoke.os, 'killpg') as kill:
            with self.assertRaises(asyncio.CancelledError):
                await smoke.run()
            kill.assert_called_once()
            worker.wait.assert_awaited_once()

    async def test_elapsed_deadline_terminates_hung_worker(self):
        async def hang(*args):
            await asyncio.Event().wait()
        worker = AsyncMock(pid=12345, returncode=None)
        worker.stdout.read.side_effect = hang
        with patch.object(smoke, 'TIMEOUT', 0.01), \
                patch.object(smoke.asyncio, 'create_subprocess_exec', return_value=worker), \
                patch.object(smoke.os, 'killpg') as kill:
            self.assertEqual(await smoke.run(), {'ok': False, 'status': 'timeout'})
            kill.assert_called_once()
            worker.wait.assert_awaited_once()
