"""Offline only: all privileged subprocess calls and availability checks mocked."""
import asyncio
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

import epoptia_vnc_dependencies as deps
from ermis_gateway import Gateway, call_existing
import ermis_system_server as system

loader = importlib.machinery.SourceFileLoader(
    'vnc_admin', str(Path(__file__).resolve().parents[1] / 'admin_bootstrap/ermis-admin'))
spec = importlib.util.spec_from_loader(loader.name, loader)
admin = importlib.util.module_from_spec(spec)
loader.exec_module(admin)
ACTION = 'install_epoptia_vnc_dependencies'
REQUEST = ['browser', 'install-epoptia-vnc-dependencies']
PROMPT = ('Install x11vnc, noVNC, websockify, and xauth if needed for a loopback-only '
          'SSH-tunneled Epoptia enrollment window? This makes no Epoptia data changes.')


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0
        self.gateway = Gateway(invoke=system._gateway_invoke, clock=lambda: self.now)
        self.session = 'vnc_test_session_0001'
        async def inline(fn):
            return fn()
        worker = patch('ermis_gateway.asyncio.to_thread', side_effect=inline)
        worker.start()
        self.addCleanup(worker.stop)

    async def propose(self, arguments=None):
        return await self.gateway.request(dict(session_id=self.session, action=ACTION,
                                               arguments={} if arguments is None else arguments))

    async def confirm(self, proposal, **extra):
        return await self.gateway.confirm(dict(session_id=self.session,
            confirmation_id=proposal['confirmation_id'], approved=True) | extra)

    async def test_confirmed_system_mapping_and_single_use(self):
        with patch.object(system, 'gateway', self.gateway), patch.object(
                deps.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            proposal = await system.ermis_gateway_execute('request', dict(
                session_id=self.session, action=ACTION, arguments={}))
            self.assertEqual(proposal['status'], 'confirmation_required')
            self.assertEqual(proposal['prompt'], PROMPT)
            self.assertEqual(proposal['expires_in_seconds'], 120)
            run.assert_not_called()
            for extra in ({'approved': 'true'}, {'session_id': 'other_session_0001'},
                          {'arguments': {'packages': ['curl']}}):
                self.assertEqual((await self.confirm(proposal, **extra))['status'],
                                 'invalid_confirmation')
            run.assert_not_called()
            results = await asyncio.gather(*(self.confirm(proposal) for _ in range(3)))
            self.assertEqual(sum(r['status'] == 'completed' for r in results), 1)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0], deps.COMMAND)
            self.assertEqual(deps.COMMAND[-2:], tuple(REQUEST))

    async def test_injected_arguments_rejected(self):
        with patch.object(deps, 'install') as install:
            for args in ([], 'curl', {'packages': ['curl']}, {'options': ['--upgrade']},
                         {'command': 'id'}, {'argv': []}, {'shell': True}):
                self.assertEqual((await self.propose(args))['status'], 'unsupported_request')
                with self.assertRaises(ValueError):
                    await call_existing('Ermis_System', ACTION, args)
            with self.assertRaises(ValueError):
                await call_existing('Ermis_System', ACTION, None)
            self.assertFalse(self.gateway.pending)
            install.assert_not_called()

    async def test_cancel_and_exact_expiry(self):
        with patch.object(deps, 'install') as install:
            self.assertEqual((await self.confirm(await self.propose(), approved=False))['status'],
                             'cancelled')
            proposal = await self.propose()
            self.now = 120
            self.assertEqual((await self.confirm(proposal))['status'], 'invalid_confirmation')
            install.assert_not_called()

    async def test_gateway_bounds_results_and_errors(self):
        for value in ({'status': 'completed', 'output': 'private' * 10000},
                      {'status': 'already_installed'}, {'status': []}, None):
            with patch.object(deps, 'install', return_value=value):
                result = await self.confirm(await self.propose())
                self.assertEqual(set(result), {'ok', 'status'})
                self.assertIn(result['status'], deps.STATUSES)
                self.assertLess(len(str(result)), 60)
        with patch.object(deps, 'install', side_effect=RuntimeError('private')):
            self.assertEqual(await self.confirm(await self.propose()),
                             {'ok': False, 'status': 'failed'})


class AdapterTests(unittest.TestCase):
    def test_exit_mapping_and_suppressed_output(self):
        for code, status in ((0, 'completed'), (10, 'already_installed'), (1, 'failed'),
                             (127, 'failed')):
            with patch.object(deps.subprocess, 'run', return_value=Mock(
                    returncode=code, stdout='private', stderr='private')) as run:
                self.assertEqual(deps.install(), {'ok': code in (0, 10), 'status': status})
                self.assertEqual(run.call_args.args, (deps.COMMAND,))
                kw = run.call_args.kwargs
                for stream in ('stdin', 'stdout', 'stderr'):
                    self.assertEqual(kw[stream], subprocess.DEVNULL)
                self.assertFalse(kw['shell'])
                self.assertEqual(kw['timeout'], 950)
        for error in (OSError('private'), subprocess.TimeoutExpired('private', 950)):
            with patch.object(deps.subprocess, 'run', side_effect=error) as run:
                self.assertEqual(deps.install(), {'ok': False, 'status': 'failed'})
                run.assert_called_once()
        with self.assertRaises(TypeError):
            deps.install('curl')


class AdminTests(unittest.TestCase):
    def test_exact_allowlist_and_no_package_options(self):
        self.assertEqual(admin.VNC_PACKAGES, ('x11vnc', 'novnc', 'websockify', 'xauth'))
        self.assertEqual(admin.plan(REQUEST), ['/usr/bin/apt-get', '--assume-yes',
            '--no-remove', '--no-upgrade', '--no-install-recommends', '-o',
            'DPkg::Lock::Timeout=60', 'install', *admin.VNC_PACKAGES])
        for extra in ('curl', '--upgrade', '--allow-remove-essential', ';id', 'autoremove'):
            with self.assertRaises(ValueError):
                admin.plan(REQUEST + [extra])
        for package in admin.VNC_PACKAGES:
            for verb in ('install', 'remove', 'upgrade', 'autoremove'):
                with self.assertRaises(ValueError):
                    admin.plan(['package', verb, package])

    def test_missing_only_and_already_installed(self):
        for available, code, missing in (([True] * 4, 10, []),
                ([False] * 4, 0, list(admin.VNC_PACKAGES)),
                ([False, False, False, True], 0, list(admin.VNC_PACKAGES[:3]))):
            with patch.object(admin.os, 'geteuid', return_value=0), \
                    patch.object(admin, 'audit'), patch.object(admin.fcntl, 'flock'), \
                    patch.object(admin, 'vnc_package_available', side_effect=available), \
                    patch.object(admin.subprocess, 'run', return_value=Mock(returncode=0)) as run:
                self.assertEqual(admin.main(REQUEST), code)
                if not missing:
                    run.assert_not_called()
                else:
                    run.assert_called_once()
                    self.assertEqual(run.call_args.args[0], admin.plan(REQUEST)[:-4] + missing)
                    kw = run.call_args.kwargs
                    self.assertEqual(kw['timeout'], 900)
                    self.assertEqual(kw['env']['DEBIAN_FRONTEND'], 'noninteractive')
                    self.assertEqual(kw['stdout'], subprocess.DEVNULL)
                    self.assertEqual(kw['stderr'], subprocess.DEVNULL)
                    self.assertFalse(kw['shell'])

    def test_availability_query_is_fixed_and_bounded(self):
        for data, code, expected in ((b'installed', 0, True), (b'config-files', 0, False),
                                     (b'installed', 1, False)):
            output = io.BytesIO(data)
            with patch.object(admin.tempfile, 'TemporaryFile', return_value=output), \
                    patch.object(admin.subprocess, 'run', return_value=Mock(returncode=code)) as run:
                self.assertEqual(admin.vnc_package_available('novnc'), expected)
                self.assertEqual(run.call_args.args[0],
                    ['/usr/bin/dpkg-query', '-W', '-f=${db:Status-Status}', 'novnc'])
                self.assertEqual(run.call_args.kwargs['timeout'], 5)
        with patch.object(admin.shutil, 'which', return_value='/usr/bin/xauth'), \
                patch.object(admin.subprocess, 'run') as run:
            self.assertTrue(admin.vnc_package_available('xauth'))
            run.assert_not_called()

    def test_nonroot_lock_and_apt_failure_are_closed(self):
        for uid, lock_error, child in ((1000, None, Mock(returncode=0)),
                (0, BlockingIOError(), Mock(returncode=0)),
                (0, None, Mock(returncode=1)),
                (0, None, subprocess.TimeoutExpired('private', 900))):
            with patch.object(admin.os, 'geteuid', return_value=uid), \
                    patch.object(admin, 'audit'), \
                    patch.object(admin.fcntl, 'flock', side_effect=lock_error), \
                    patch.object(admin, 'vnc_package_available', return_value=False), \
                    patch.object(admin.subprocess, 'run', **({'side_effect': child}
                        if isinstance(child, Exception) else {'return_value': child})) as run, \
                    patch('sys.stderr', io.StringIO()) as output:
                self.assertEqual(admin.main(REQUEST), 1)
                self.assertEqual(output.getvalue(), 'Maintenance request rejected or failed.\n')
                if uid or lock_error:
                    run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
