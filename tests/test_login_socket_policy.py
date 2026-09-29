"""Offline socket policy tests. All host probes and mutations are mocked."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from admin_bootstrap import login_bootstrap as login
import epoptia_browser


class SocketPolicy(unittest.TestCase):
    def test_literal_mutation_allowlist_and_sanitized_failures(self):
        expected = {
            'reload': ('/usr/bin/systemctl', 'daemon-reload'),
            'enable': ('/usr/bin/systemctl', 'enable', '--now', 'ermis-epoptia-login.socket'),
            'disable': ('/usr/bin/systemctl', 'disable', '--now', 'ermis-epoptia-login.socket'),
            'stop': ('/usr/bin/systemctl', 'disable', '--now', 'ermis-epoptia-login.service'),
        }
        with patch.object(login.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            for action, command in expected.items():
                login.socket_operation(action)
                self.assertEqual(run.call_args.args[0], command)
                self.assertFalse(run.call_args.kwargs['shell'])
            run.reset_mock()
            for action in ('restart', 'ssh.service', 'enable --now ssh.service', 'start'):
                with self.assertRaises(ValueError):
                    login.socket_operation(action)
            run.assert_not_called()
            run.side_effect = OSError('PRIVATE FIXTURE')
            with self.assertRaisesRegex(login.LoginInstallError, '^B_LOGIN_SOCKET_ENABLE$'):
                login.socket_operation('enable')

    def test_units_have_one_private_stream_and_no_service_enablement(self):
        unit = login.SOCKET_UNIT
        self.assertEqual([line for line in unit.splitlines() if line.startswith('Listen')],
                         ['ListenStream=/run/ermis-epoptia-login/control.sock'])
        for line in ('Accept=no', 'SocketUser=ermis', 'SocketGroup=ermis',
                     'SocketMode=0600', 'DirectoryMode=0700', 'RemoveOnStop=yes'):
            self.assertIn(line, unit.splitlines())
        self.assertNotIn('[Install]', login.UNIT)
        self.assertIn('RuntimeDirectory=ermis-epoptia-login/enrollment\n', login.UNIT)
        self.assertNotIn('RuntimeDirectory=ermis-epoptia-login\n', login.UNIT)
        self.assertIn('Requires=ermis-epoptia-login.socket', login.UNIT)
        self.assertIn('User=ermis', login.UNIT)
        self.assertNotIn('sudo', unit + login.UNIT)

    def test_diagnose_listening_and_tcp_scope(self):
        status = ('LoadState=loaded\nActiveState=active\nSubState=listening\n'
                  'UnitFileState=enabled\nListen=' + login.SOCKET_PATH + ' (Stream)\n')
        unix = 'header\n0: 00000002 00000000 00010000 0001 01 42 ' + login.SOCKET_PATH + '\n'
        cases = [
            (status, unix, '', []),
            (status.replace('SubState=listening', 'SubState=running'), unix, '', []),
            (status.replace('enabled', 'disabled'), unix, '', ['login-socket-not-enabled-listening']),
            (status.replace(login.SOCKET_PATH, '0.0.0.0:1234'), unix, '', ['login-socket-not-enabled-listening']),
            (status, unix.replace('00010000', '00000000'), '', ['login-socket-not-listening']),
            (status, unix.replace('0001 01', '0002 01'), '', ['login-socket-not-listening']),
            (status, unix, 'LISTEN 0 128 127.0.0.1:6091 0.0.0.0:*\n', []),
            (status, unix, 'LISTEN 0 128 [::]:6091 [::]:*\n', ['login-unsafe-listener']),
            (status, unix, 'PRIVATE FIXTURE', ['login-socket-inspection-unavailable']),
        ]
        for output, proc, tcp, expected in cases:
            with self.subTest(expected=expected), patch.object(login, 'verify_unit_origin'), patch.object(login, 'runtime_blockers', return_value=[]), patch.object(Path, 'read_text', return_value=proc), patch.object(login.subprocess, 'run', side_effect=[SimpleNamespace(returncode=0, stdout=output), SimpleNamespace(returncode=0, stdout=tcp)]):
                self.assertEqual(login.socket_blockers(), expected)

    def test_unit_origin_rejects_dropins_and_unrelated_fragments(self):
        for kind, name in [('socket', login.SOCKET_NAME), ('service', login.UNIT_NAME)]:
            valid = 'FragmentPath=/etc/systemd/system/' + name + '\nDropInPaths=\n'
            for output in (valid, valid.replace('/etc/systemd/system/', '/other/'), valid.replace('DropInPaths=', 'DropInPaths=/fixture')):
                with patch.object(login.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=output)) as run:
                    if output == valid:
                        login.verify_unit_origin(kind)
                    else:
                        with self.assertRaises(ValueError):
                            login.verify_unit_origin(kind)
                    self.assertEqual(run.call_args.args[0][2], name)

    def test_runtime_metadata_rejects_wrong_owner_mode_and_type(self):
        import stat
        import pwd
        directory = dict(st_mode=stat.S_IFDIR | 0o700, st_uid=1001, st_gid=1001)
        socket = dict(st_mode=stat.S_IFSOCK | 0o600, st_uid=1001, st_gid=1001)
        with patch.object(pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1001, pw_gid=1001)):
            for override in ({}, {'st_uid': 0}, {'st_gid': 0}, {'st_mode': stat.S_IFREG | 0o600}, {'st_mode': stat.S_IFSOCK | 0o666}):
                with patch.object(Path, 'lstat', side_effect=[SimpleNamespace(**directory), SimpleNamespace(**(socket | override))]):
                    self.assertEqual(login.runtime_blockers(), ['login-socket-unsafe-or-missing'] if override else [])


class GatewaySocket(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        async def inline(function, *args):
            return function(*args)
        context = patch.object(epoptia_browser.asyncio, 'to_thread', side_effect=inline)
        context.start()
        self.addCleanup(context.stop)

    async def test_confirmed_dispatch_never_uses_service_control(self):
        process = AsyncMock(returncode=0)
        process.communicate.return_value = (json.dumps(dict(ok=False, status='awaiting_login',
            operational_ready=True, source_revision=login.fingerprint())).encode(), b'')
        with patch.object(login, 'installed_blockers', return_value=[]), patch.object(login, 'configuration_blockers', return_value=[]), patch.object(login, 'dependency_blockers', return_value=[]), patch.object(login, 'runtime_blockers', return_value=[]), patch.object(epoptia_browser, 'login_listener_blocker', return_value=None), patch('service_control.control', side_effect=AssertionError('service control forbidden')), patch.object(epoptia_browser.asyncio, 'create_subprocess_exec', return_value=process) as spawn:
            value = await epoptia_browser.login_command('start', ttl_minutes=2)
        self.assertEqual(value['local_url'], 'http://127.0.0.1:6091/vnc.html')
        self.assertEqual(value['ssh_forward'], 'ssh -N -L 127.0.0.1:6091:127.0.0.1:6091 ermis@<server>')
        self.assertEqual(spawn.call_args.args[2:], ('start', '{"ttl_minutes": 2}'))
        self.assertNotIn('sudo', str(spawn.call_args))

    async def test_timeout_reaps_client_and_returns_no_url(self):
        process = AsyncMock(returncode=None)
        process.kill = unittest.mock.Mock()
        process.communicate.side_effect = asyncio.TimeoutError
        with patch.object(login, 'installed_blockers', return_value=[]), patch.object(login, 'configuration_blockers', return_value=[]), patch.object(login, 'dependency_blockers', return_value=[]), patch.object(login, 'runtime_blockers', return_value=[]), patch.object(epoptia_browser, 'login_listener_blocker', return_value=None), patch.object(epoptia_browser.asyncio, 'create_subprocess_exec', return_value=process):
            value = await epoptia_browser.login_command('start')
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
        self.assertNotIn('local_url', value)
        self.assertEqual(value['blockers'], ['login-backend-ipc-failure'])
