"""Offline source/manifests only; never inspect runtime state or launch login."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from admin_bootstrap import login_bootstrap as bootstrap
import epoptia_browser

ROOT = Path(__file__).resolve().parents[1]

class ListenerTest(unittest.TestCase):
    def test_fixed_private_listener_probe(self):
        for output, expected in [
            ('', None),
            ('LISTEN 0 128 127.0.0.1:5991 0.0.0.0:*\nLISTEN 0 128 127.0.0.1:6091 0.0.0.0:*\n', None),
            ('LISTEN 0 128 0.0.0.0:5991 0.0.0.0:*\n', 'login-unsafe-listener'),
            ('LISTEN 0 128 [::]:6091 [::]:*\n', 'login-unsafe-listener'),
            ('LISTEN 0 128 192.0.2.1:6091 0.0.0.0:*\n', 'login-unsafe-listener'),
            ('SYNTHETIC', 'login-listener-check-unavailable'),
            ('x' * 4097, 'login-listener-check-unavailable'),
        ]:
            with self.subTest(expected=expected), patch('epoptia_browser.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=output)) as run:
                self.assertEqual(epoptia_browser.login_listener_blocker(), expected)
                self.assertEqual(run.call_args.args[0], ['/usr/bin/ss', '-H', '-ltn', '( sport = :5991 or sport = :6091 )'])
                self.assertFalse(run.call_args.kwargs['shell'])

    def test_probe_failures_are_sanitized(self):
        import subprocess
        for error in (OSError('SYNTHETIC'), UnicodeError('SYNTHETIC'), subprocess.TimeoutExpired('SYNTHETIC', 5)):
            with patch('epoptia_browser.subprocess.run', side_effect=error):
                self.assertEqual(epoptia_browser.login_listener_blocker(), 'login-listener-check-unavailable')
        with patch('epoptia_browser.subprocess.run', return_value=SimpleNamespace(returncode=1, stdout='SYNTHETIC')):
            self.assertEqual(epoptia_browser.login_listener_blocker(), 'login-listener-check-unavailable')


class ReadinessTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Probes below are mocks; keep them on this test loop without worker threads.
        async def inline(function, *args):
            return function(*args)
        context = patch('epoptia_browser.asyncio.to_thread', side_effect=inline)
        context.start()
        self.addCleanup(context.stop)
        context = patch.object(bootstrap, 'owned_read', side_effect=lambda p, mode: p.read_bytes())
        context.start()
        self.addCleanup(context.stop)
        # Publication tests use local fixtures; never perform privileged chown.
        context = patch.object(bootstrap.os, 'fchown')
        context.start()
        self.addCleanup(context.stop)
        for target, value in [
            ('admin_bootstrap.login_bootstrap.runtime_blockers', []),
            ('epoptia_browser.login_listener_blocker', None),
        ]:
            context = patch(target, return_value=value)
            mock = context.start()
            self.addCleanup(context.stop)
            if target.endswith('.runtime_blockers'):
                self.unit_status = mock
            else:
                self.listener_check = mock

    def test_valid_source_and_fixed_manifest(self):
        self.assertEqual(bootstrap.source_blockers(ROOT), [])
        bootstrap.validate_source(ROOT)
        artifacts = bootstrap.artifacts()
        self.assertEqual(len(artifacts), 8)
        assets = json.loads((ROOT / 'admin_bootstrap/login_assets.json').read_text())
        self.assertEqual(assets, {path.name: hashlib.sha256(content).hexdigest()
                                  for path, (content, _) in artifacts.items()})
        self.assertIn('User=ermis\n', bootstrap.UNIT)
        self.assertIn('KillMode=control-group', bootstrap.UNIT)
        self.assertIn('DirectoryMode=0700', bootstrap.SOCKET_UNIT)
        self.assertIn('StandardOutput=null', bootstrap.UNIT)
        self.assertNotIn('0.0.0.0', bootstrap.UNIT)
        for path, (content, mode) in artifacts.items():
            self.assertNotIn('sudo', str(path))
            if path.name == 'ermis-epoptia-login-runtime':
                self.assertTrue(content.startswith(b'#!/usr/bin/python3 -I\n'))
                self.assertNotIn(b'/home/ermis/projects', content)
                self.assertNotIn(b'subprocess', content)
            elif mode == 0o755:
                self.assertIn(b'[ "$#" -eq 0 ] || exit 2', content)
                self.assertTrue(b'login_bootstrap.py check' in content or b'login_bootstrap.py installed-check' in content)

    def test_all_four_blockers_and_source_mismatch(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            root = Path(directory)
            self.assertEqual(bootstrap.source_blockers(root), list(epoptia_browser.LOGIN_BLOCKERS))
            (root / 'admin_bootstrap').mkdir()
            manifest = json.loads((ROOT / 'admin_bootstrap/login_manifest.json').read_text())
            for name in manifest:
                (root / name).write_bytes((ROOT / name).read_bytes())
            (root / 'admin_bootstrap/login_manifest.json').write_text(json.dumps(manifest))
            self.assertEqual(bootstrap.source_blockers(root), [])
            for file, expected in [('epoptia_login_policy.cjs', ['approved_origin_missing', 'authenticated_landing_signal_missing']),
                                   ('epoptia_login_backend.cjs', ['persistent_loopback_backend_missing']),
                                   ('epoptia_browser.py', ['persistent_loopback_backend_missing']),
                                   ('admin_bootstrap/login_bootstrap.py', ['fixed_bootstrap_installer_missing'])]:
                original = (root / file).read_bytes()
                (root / file).write_bytes(original + b' ')
                self.assertEqual(bootstrap.source_blockers(root), expected)
                (root / file).write_bytes(original)
            with self.assertRaises(ValueError):
                bootstrap.validate_source(root)

    async def test_readiness_no_operational_claim_without_managed_runtime(self):
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b'{"ok":false,"status":"login_not_ready"}', b'')
        with patch.object(bootstrap, 'installed_blockers', return_value=['login-supervisor-missing']), patch('epoptia_browser.asyncio.create_subprocess_exec', return_value=process):
            value = await epoptia_browser.login_command('status')
        self.assertTrue(value['source_wiring_ready'])
        self.assertTrue(value['bootstrap_install_ready'])
        self.assertTrue(value['bootstrap_install_required'])
        self.assertFalse(value['operational_ready'])
        self.assertEqual(value['blockers'], ['login-supervisor-missing'])
        self.assertNotIn('SYNTHETIC', json.dumps(value))

    async def test_blocked_source_never_starts_client(self):
        with patch.object(bootstrap, 'source_blockers', return_value=list(epoptia_browser.LOGIN_BLOCKERS)), patch('epoptia_browser.asyncio.create_subprocess_exec') as spawn:
            value = await epoptia_browser.login_command('start')
            self.assertFalse(value['bootstrap_install_ready'])
            spawn.assert_not_called()

    def test_coherent_existing_artifact_and_rollback_are_refused(self):
        import os
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            artifact = Path(directory) / 'synthetic-wrapper.txt'
            files = {artifact: (b'fixed synthetic wrapper\n', 0o600)}
            artifact.write_bytes(files[artifact][0])
            artifact.chmod(0o600)
            before = artifact.stat()
            bootstrap.validate_artifacts(files, os.getuid())
            for action in ('install', 'rollback'):
                with self.assertRaisesRegex(ValueError, 'first-install-only'):
                    bootstrap.publish_artifacts(files, action)
                self.assertEqual(artifact.stat(), before)
                self.assertEqual(artifact.read_bytes(), files[artifact][0])
            artifact.write_bytes(b'SYNTHETIC mismatch')
            with self.assertRaises(ValueError):
                bootstrap.validate_artifacts(files, os.getuid())

    def test_public_bind_source_refused_even_with_updated_manifest(self):
        with patch.object(bootstrap, 'source_blockers', return_value=[]), patch.object(Path, 'read_text', return_value="listen('0.0.0.0')"):
            with self.assertRaisesRegex(ValueError, 'unsafe_bind'):
                bootstrap.validate_source(ROOT)

    async def test_matching_install_ipc_failure_does_not_require_reinstall(self):
        process = AsyncMock(returncode=0)
        process.communicate.return_value = (b'{"ok":false,"status":"login_not_ready"}', b'')
        with patch.object(bootstrap, 'installed_blockers', return_value=[]), patch.object(bootstrap, 'configuration_blockers', return_value=[]), patch.object(bootstrap, 'dependency_blockers', return_value=[]), patch('epoptia_browser.asyncio.create_subprocess_exec', return_value=process):
            value = await epoptia_browser.login_command('status')
        self.assertFalse(value['bootstrap_install_required'])
        self.assertFalse(value['operational_ready'])
        self.assertEqual(value['blockers'], ['login-backend-ipc-failure'])

    async def test_runtime_and_listener_states_block_without_ipc(self):
        for runtime, listener, blocker in [
            (['login-socket-unsafe-or-missing'], None, 'login-socket-unsafe-or-missing'),
            ([], 'login-unsafe-listener', 'login-unsafe-listener'),
        ]:
            with patch.object(bootstrap, 'installed_blockers', return_value=[]), patch.object(bootstrap, 'configuration_blockers', return_value=[]), patch.object(bootstrap, 'dependency_blockers', return_value=[]), patch('epoptia_browser.asyncio.create_subprocess_exec') as spawn:
                self.unit_status.return_value = runtime
                self.listener_check.return_value = listener
                value = await epoptia_browser.login_command('status')
                self.assertEqual(value['blockers'], [blocker])
                spawn.assert_not_called()

    async def test_runtime_must_match_current_manifest(self):
        for revision, ready in [('0' * 64, False), (bootstrap.fingerprint(), True)]:
            process = AsyncMock(returncode=0)
            process.communicate.return_value = (json.dumps({'ok': True, 'status': 'ready_not_enrolled', 'operational_ready': True, 'source_revision': revision}).encode(), b'')
            with patch.object(bootstrap, 'installed_blockers', return_value=[]), patch.object(bootstrap, 'configuration_blockers', return_value=[]), patch.object(bootstrap, 'dependency_blockers', return_value=[]), patch('epoptia_browser.asyncio.create_subprocess_exec', return_value=process):
                value = await epoptia_browser.login_command('status')
            self.assertEqual(value['operational_ready'], ready)
            self.assertFalse(value['bootstrap_install_required'])
            self.assertEqual(value['blockers'], [] if ready else ['login-supervisor-restart-required'])

    async def test_invalid_ipc_is_sanitized_and_cleanup_still_dispatches(self):
        for payload in (b'[]', b'null', b'SYNTHETIC', b'{"status":"SYNTHETIC"}', b'{"status":[]}', b'x' * 1025):
            process = AsyncMock(returncode=0)
            process.communicate.return_value = (payload, b'')
            with patch.object(bootstrap, 'installed_blockers', return_value=[]), patch.object(bootstrap, 'configuration_blockers', return_value=[]), patch.object(bootstrap, 'dependency_blockers', return_value=[]), patch('epoptia_browser.asyncio.create_subprocess_exec', return_value=process):
                value = await epoptia_browser.login_command('status')
                self.assertEqual(value['blockers'], ['login-backend-ipc-failure'])
                self.assertNotIn('SYNTHETIC', json.dumps(value))
        self.listener_check.reset_mock()
        self.listener_check.return_value = 'login-unsafe-listener'
        process.communicate.return_value = (json.dumps(dict(ok=True, status='stopped', operational_ready=True, source_revision=bootstrap.fingerprint())).encode(), b'')
        with patch.object(bootstrap, 'installed_blockers', return_value=[]), patch.object(bootstrap, 'configuration_blockers', return_value=[]), patch.object(bootstrap, 'dependency_blockers', return_value=[]), patch('epoptia_browser.asyncio.create_subprocess_exec', return_value=process):
            self.assertTrue((await epoptia_browser.login_command('stop'))['ok'])
        self.listener_check.assert_not_called()

    async def test_missing_dependency_blocks_client_without_reinstall(self):
        with patch.object(bootstrap, 'installed_blockers', return_value=[]), patch.object(bootstrap, 'configuration_blockers', return_value=[]), patch.object(bootstrap, 'dependency_blockers', return_value=['login-dependency-novnc']), patch('epoptia_browser.asyncio.create_subprocess_exec') as spawn:
            value = await epoptia_browser.login_command('status')
        spawn.assert_not_called()
        self.assertFalse(value['operational_ready'])
        self.assertFalse(value['bootstrap_install_required'])
        self.assertEqual(value['dependency_blockers'], ['login-dependency-novnc'])
        self.assertEqual(value['blockers'], ['login-dependency-novnc'])

    def test_owned_prior_upgrade_and_rollback_require_manual_review(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            target = Path(directory) / 'supervisor'
            receipt = Path(directory) / 'receipt'
            old = {target: (b'old approved source', 0o755)}
            new = {target: (b'new approved source', 0o755)}
            target.write_bytes(old[target][0])
            with patch.object(bootstrap, 'RECEIPT', receipt):
                receipt.write_bytes(bootstrap.receipt_bytes(old))
                before = (target.read_bytes(), receipt.read_bytes())
                for action in ('install', 'rollback'):
                    with self.assertRaisesRegex(ValueError, 'first-install-only'):
                        bootstrap.publish_artifacts(bootstrap.managed_files(new), action)
                    self.assertEqual((target.read_bytes(), receipt.read_bytes()), before)

    def test_existing_partial_install_refused_before_publication(self):
        import os
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            first, second = Path(directory) / 'first', Path(directory) / 'second'
            first.write_bytes(b'old')
            replace = os.replace
            def fail(source, destination):
                if destination == second:
                    raise OSError('synthetic')
                replace(source, destination)
            with patch.object(bootstrap.os, 'replace', side_effect=fail):
                with self.assertRaisesRegex(ValueError, "first-install-only"):
                    bootstrap.publish_artifacts({first: (b'new', 0o600), second: (b'new', 0o600)}, 'install')
            self.assertEqual(first.read_bytes(), b'old')
            self.assertFalse(second.exists())
            self.assertEqual(list(Path(directory).iterdir()), [first])

    def test_main_cli_installs_login_even_when_admin_is_current(self):
        from admin_bootstrap import bootstrap as main
        import io
        with patch.object(main, 'main') as admin, patch.object(main, 'login_operation') as login, patch('sys.stdout', io.StringIO()):
            self.assertEqual(main.cli(['install']), 0)
            admin.assert_called_once_with('install')
            login.assert_called_once_with('install')
        # Verified diagnose dispatch is isolated in test_login_diagnose_entrypoint.
        with patch.object(main, 'main') as admin, patch.object(main, 'login_operation', side_effect=ValueError('synthetic')), patch('sys.stderr', io.StringIO()):
            self.assertEqual(main.cli(['rollback']), 1)
            admin.assert_not_called()
