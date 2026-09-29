import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch
from ermis_gateway import ACTIONS, Gateway, validate
from epoptia_browser import login_command, sanitize_login_diagnostic


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_gateway_and_closed_response(self):
        for prefix in ('epoptia_login_', 'epoptia_browser_login_'):
            action = prefix + 'diagnose'
            self.assertFalse(ACTIONS[action].write)
            with self.assertRaises(ValueError):
                validate(action, {'path': 'PRIVATE'})
            raw = dict(ok=True, status='ready_not_enrolled', operational_ready=True,
                       source_wiring_ready=True, socket_activatable=True, ipc_phase='response',
                       installed_source_match='match', runtime_generation_match='match',
                       source_revision='PRIVATE', stderr='PRIVATE',
                       diagnostic=dict(command_accepted=True, child_launch_attempted=True,
                                       failure_stage='browser', failure_class='sandbox_denied',
                                       child_exit_class='nonzero', token='PRIVATE'))
            with patch('epoptia_browser._login_command', new_callable=AsyncMock, return_value=raw):
                result = await Gateway().request(dict(session_id='diagnostic_fixture_0001',
                                                     action=action, arguments={}))
            diagnostic = result['result']
            self.assertEqual(diagnostic['failure_class'], 'sandbox_denied')
            self.assertNotIn('PRIVATE', str(diagnostic))
            self.assertTrue(all(type(v) in (str, bool, int) for v in diagnostic.values()))

    async def test_preflight_and_invalid_schema(self):
        with patch('epoptia_browser._login_command', new_callable=AsyncMock,
                   return_value=dict(ok=False, status='login_not_ready')):
            result = await login_command('diagnose')
        self.assertEqual(result['ipc_phase'], 'preflight')
        self.assertEqual(result['runtime_generation_match'], 'unknown')
        self.assertFalse(result['socket_activatable'])
        for value in (None, [], {'failure_class': ['PRIVATE'], 'stderr': 'PRIVATE'}):
            self.assertNotIn('PRIVATE', str(sanitize_login_diagnostic(value)))

    async def test_socket_activated_start_and_later_diagnose(self):
        from unittest.mock import Mock
        revision = 'a' * 64
        process = Mock(returncode=0)
        process.communicate = AsyncMock()
        async def inline(function, *args):
            return function(*args)
        with ExitStack() as stack:
            stack.enter_context(patch('epoptia_browser.asyncio.to_thread', side_effect=inline))
            for name in ('source_blockers', 'installed_blockers', 'dependency_blockers',
                         'configuration_blockers', 'runtime_blockers'):
                stack.enter_context(patch('admin_bootstrap.login_bootstrap.' + name, return_value=[]))
            stack.enter_context(patch('admin_bootstrap.login_bootstrap.fingerprint', return_value=revision))
            stack.enter_context(patch('epoptia_browser.login_listener_blocker', return_value=None))
            spawn = stack.enter_context(patch('epoptia_browser.asyncio.create_subprocess_exec',
                                              new_callable=AsyncMock, return_value=process))
            for action, status in (('status', 'ready_not_enrolled'), ('start', 'login_unavailable'),
                                   ('diagnose', 'ready_not_enrolled')):
                process.communicate.return_value = (json.dumps(dict(
                    ok=action != 'start', status=status, operational_ready=True,
                    source_revision=revision, ipc_phase='response',
                    diagnostic=dict(command_accepted=True, child_launch_attempted=True,
                                    failure_stage='display', failure_class='executable_missing',
                                    child_exit_class='spawn_error', stderr='PRIVATE'))).encode(), b'')
                result = await login_command(action)
                self.assertEqual(result['status'], status)
                if action == 'diagnose':
                    self.assertTrue(result['command_accepted'])
                    self.assertEqual(result['failure_class'], 'executable_missing')
                    self.assertEqual(result['runtime_generation_match'], 'match')
                    self.assertNotIn('PRIVATE', str(result))
                    self.assertNotIn(revision, str(result))
            self.assertEqual(spawn.await_count, 3)

    async def test_all_browser_classes_and_lifecycle_survive_gateway_closed_schema(self):
        classes = ('executable_missing', 'executable_not_executable', 'shared_library_missing',
                   'sandbox_denied', 'display_unavailable', 'profile_locked', 'profile_permission',
                   'cache_permission', 'spawn_error', 'immediate_nonzero_exit', 'signal_exit',
                   'readiness_timeout', 'bind_conflict', 'browser_crash', 'protocol_error', 'unknown')
        lifecycle = ('spawn_returned', 'error_event', 'exit_event', 'close_event',
                     'readiness_timeout', 'cleanup_started')
        for category in classes:
            raw = dict(ok=False, status='login_unavailable', diagnostic=dict(
                failure_stage='browser', failure_class=category, child_exit_class='nonzero',
                **dict.fromkeys(lifecycle, True), stderr='PRIVATE', stdout='PRIVATE',
                path='PRIVATE', command='PRIVATE', env='PRIVATE', pid=123, port=123,
                url='PRIVATE', page='PRIVATE', token='PRIVATE', cookies='PRIVATE', credentials='PRIVATE'))
            with patch('epoptia_browser._login_command', new_callable=AsyncMock, return_value=raw):
                result = await login_command('diagnose')
            self.assertEqual(result['failure_class'], category)
            self.assertTrue(all(result[key] is True for key in lifecycle))
            self.assertNotIn('PRIVATE', str(result))
            for key in lifecycle:
                for value in ('PRIVATE', 1, [], {}, None):
                    self.assertIs(sanitize_login_diagnostic({key: value})[key], False)

    async def test_signal_allowlist_through_gateway(self):
        allowed = ('sigabrt', 'sigbus', 'sigill', 'sigkill', 'sigsegv', 'sigsys',
                   'sigtrap', 'sighup', 'sigterm', 'other', 'none', 'unknown')
        for signal in (*allowed, 'PRIVATE', 'SIGTERM', 9, None, [], {}):
            expected = signal if type(signal) is str and signal in allowed else 'unknown'
            raw = dict(ok=False, status='login_unavailable', diagnostic=dict(
                failure_stage='browser', failure_class='signal_exit', child_exit_class='signal',
                signal_class=signal, stderr='PRIVATE', pid=999, cookies='PRIVATE'))
            for prefix in ('epoptia_login_', 'epoptia_browser_login_'):
                with patch('epoptia_browser._login_command', new_callable=AsyncMock, return_value=raw):
                    result = (await Gateway().request(dict(session_id='diagnostic_fixture_0001',
                        action=prefix + 'diagnose', arguments={})))['result']
                self.assertEqual(result['signal_class'], expected)
                self.assertEqual(result['failure_class'], 'signal_exit')
                self.assertEqual(result['child_exit_class'], 'signal')
                self.assertNotIn('PRIVATE', str(result))
