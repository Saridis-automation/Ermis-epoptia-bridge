"""Local-only tests: host commands are mocked and files stay in this directory."""
import importlib.machinery
import importlib.util
import io
import json
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

BASE = Path(__file__).resolve().parent


def load(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(BASE / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


admin = load('admin_wrapper', 'ermis-admin')
bootstrap = load('admin_installer', 'bootstrap.py')


class Validation(unittest.TestCase):
    def setUp(self):
        for name, value in [('PACKAGES', {'example-package'}), ('SERVICES', {'example.service'}),
                            ('PORTS', {'443/tcp'}), ('FILES', {'banner': '/etc/example/banner'})]:
            context = patch.object(admin, name, value)
            context.start()
            self.addCleanup(context.stop)

    def test_supported_verbs(self):
        requests = [ ['package', 'update'], ['nginx', 'test'], ['nginx', 'reload'],
                     ['certbot', 'renew'], ['certbot', 'dry-run'], ['ufw', 'status'],
                     ['journal', 'example.service', '200', '1440'] ]
        requests += [['package', op, 'example-package'] for op in ('install', 'remove')]
        requests += [['service', op, 'example.service'] for op in ('status', 'start', 'stop', 'restart', 'reload')]
        requests += [['ufw', op, '443/tcp'] for op in ('allow', 'delete')]
        for request in requests:
            with self.subTest(request=request):
                command = admin.plan(request)
                self.assertTrue(command[0].startswith('/usr/'))
                self.assertIsInstance(command, list)
        for operation in ('install', 'backup', 'restore'):
                self.assertIsNone(admin.plan(['file', operation, 'banner']))

    def test_chromium_packages_match_installed_playwright_manifest(self):
        core = BASE.parent / 'node_modules/playwright-core'
        self.assertEqual(json.loads((core / 'package.json').read_text())['version'], '1.63.0')
        source = (core / 'lib/coreBundle.js').read_text()
        match = re.search(r'"ubuntu24\.04-x64":\s*\{\s*tools:\s*(\[[^]]*\]),\s*chromium:\s*(\[[^]]*\])', source)
        self.assertIsNotNone(match)
        expected = tuple(json.loads(match[1]) + json.loads(match[2]))
        self.assertEqual(admin.CHROMIUM_PACKAGES, expected)
        command = admin.plan(['browser', 'install-chromium-dependencies'])
        self.assertEqual(command, ['/usr/bin/apt-get', '--assume-yes', '--no-remove',
                                  '--no-upgrade', '--no-install-recommends', 'install', *expected])

    def test_chromium_capability_is_exact_privileged_and_status_only(self):
        request = ['browser', 'check-chromium-dependencies']
        self.assertEqual(admin.plan(request), [])
        for args in (request + ['extra'], request + ['--help'],
                     ['browser', 'check'], ['capabilities']):
            with self.assertRaises(ValueError):
                admin.plan(args)
        for uid, expected in ((1000, 1), (0, 0)):
            with patch.object(admin.os, 'geteuid', return_value=uid), \
                    patch.object(admin, 'audit'), patch.object(admin, 'run') as run, \
                    patch.object(admin, 'file_action') as files, \
                    patch('builtins.open', side_effect=PermissionError) as read, \
                    patch('sys.stdout', io.StringIO()) as output, \
                    patch('sys.stderr', io.StringIO()):
                self.assertEqual(admin.main(request), expected)
                run.assert_not_called()
                files.assert_not_called()
                read.assert_not_called()
                self.assertEqual(output.getvalue(), '')

    def test_chromium_rejects_all_extra_arguments_and_individual_packages(self):
        for extra in ['libnspr4', '--help', ';id', '$(id)', 'install', '']:
            with self.assertRaises(ValueError):
                admin.plan(['browser', 'install-chromium-dependencies', extra])
        for package in admin.CHROMIUM_PACKAGES:
            for verb in ['install', 'remove']:
                with self.assertRaises(ValueError):
                    admin.plan(['package', verb, package])

    def test_chromium_privilege_platform_and_output_guards(self):
        request = ['browser', 'install-chromium-dependencies']
        for uid, distro, version, arch, expected in [
                (1000, 'ubuntu', '24.04', 'x86_64', 1),
                (0, 'debian', '24.04', 'x86_64', 1),
                (0, 'ubuntu', '22.04', 'x86_64', 1),
                (0, 'ubuntu', '24.04', 'aarch64', 1),
                (0, 'ubuntu', '24.04', 'x86_64', 0)]:
            with self.subTest(uid=uid, distro=distro, version=version, arch=arch), \
                    patch.object(admin.os, 'geteuid', return_value=uid), \
                    patch.object(admin.platform, 'freedesktop_os_release', return_value={
                        'ID': distro, 'VERSION_ID': version}) as release, \
                    patch.object(admin.platform, 'machine', return_value=arch), \
                    patch.object(admin, 'audit'), patch.object(admin.fcntl, 'flock'), \
                    patch.object(admin.subprocess, 'run', return_value=Mock(returncode=0)) as run, \
                    patch('sys.stderr', io.StringIO()):
                self.assertEqual(admin.main(request), expected)
                if expected:
                    run.assert_not_called()
                    if uid:
                        release.assert_not_called()
                else:
                    self.assertEqual(run.call_args.args[0], admin.plan(request))
                    self.assertEqual(run.call_args.kwargs['stdout'], subprocess.DEVNULL)
                    self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)

    def test_injection_unknown_and_traversal(self):
        bad = [';id', '$(id)', '`id`', 'a|b', 'a&b', 'a>b', 'a<b', 'a\nb',
               'a b', '*', '--help', '../banner', '/etc/passwd', 'a/../b',
               'a\\b', '%2e%2e', 'unknown', '', 'a\0b']
        for item in bad:
            for request in (['package', 'install', item], ['service', 'restart', item],
                            ['ufw', 'allow', item], ['file', 'install', item]):
                with self.subTest(request=request), self.assertRaises(ValueError):
                    admin.plan(request)
        for request in ([], ['shell', 'id'], ['package', 'upgrade'], ['nginx', 'test', 'extra'],
                        ['ufw', 'reset'], ['file', 'delete', 'banner']):
            with self.assertRaises(ValueError):
                admin.plan(request)

    def test_journal_limits(self):
        for lines, minutes in [('0', '1'), ('201', '1'), ('1', '1441'), ('01', '1'), ('1', '-1')]:
            with self.assertRaises(ValueError):
                admin.plan(['journal', 'example.service', lines, minutes])

    def test_rejections_do_not_execute_or_log_input(self):
        with patch.object(admin, 'run') as run, patch.object(admin, 'audit') as audit, patch('sys.stderr', io.StringIO()):
            self.assertEqual(admin.main(['shell', 'untrusted-input']), 1)
            run.assert_not_called()
            audit.assert_called_once_with('rejected-or-failed', None)

    def test_no_shell_clean_environment_and_time_bound(self):
        with patch.object(admin.subprocess, 'run', return_value=Mock(returncode=0)) as run, \
                patch.object(admin.tempfile, 'TemporaryFile', return_value=io.BytesIO()):
            admin.run(['/usr/sbin/nginx', '-t'])
            kwargs = run.call_args.kwargs
            self.assertFalse(kwargs['shell'])
            self.assertEqual(kwargs['env'], admin.ENV)
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            self.assertEqual(kwargs['timeout'], 900)
            self.assertIsNone(kwargs['preexec_fn'])
            self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)

    def test_journal_output_bound(self):
        with patch.object(admin.subprocess, 'run', return_value=Mock(returncode=0)) as run, \
                patch.object(admin.tempfile, 'TemporaryFile', return_value=io.BytesIO(b'x' * (admin.LIMIT + 100))), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            admin.run(admin.plan(['journal', 'example.service', '10', '5']), journal=True)
            self.assertTrue(callable(run.call_args.kwargs['preexec_fn']))
            self.assertLess(len(output.getvalue()), admin.LIMIT + 100)

    def test_nginx_reload_requires_successful_test(self):
        with patch.object(admin.os, 'geteuid', return_value=0), patch.object(admin, 'audit'), \
                patch.object(admin.fcntl, 'flock'), patch.object(admin, 'run', side_effect=RuntimeError) as run, \
                patch('sys.stderr', io.StringIO()):
            self.assertEqual(admin.main(['nginx', 'reload']), 1)
            run.assert_called_once_with(['/usr/sbin/nginx', '-t'])


class Files(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=BASE)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'banner'

    def test_untrusted_parents_and_traversal(self):
        for path in (self.path.parent, Path('/etc/../etc'), Path('relative')):
            with self.assertRaises((ValueError, FileNotFoundError)):
                admin.trusted_directory(path)

    def test_symlink_rejected(self):
        target = self.path.with_name('target')
        target.write_bytes(b'fixture')
        self.path.symlink_to(target)
        with patch.object(admin, 'trusted_directory'), self.assertRaises(ValueError):
            admin.atomic_write(self.path, b'new')
        with self.assertRaises(OSError):
            admin.read_file(self.path)
        self.assertEqual(target.read_bytes(), b'fixture')

    def test_content_limits(self):
        for data in (b'x' * (admin.LIMIT + 1), b'null\0', b'\xff'):
            with self.assertRaises(ValueError):
                admin.validate_data(data)

    def test_atomic_write_and_failure_preserve_original(self):
        with patch.object(admin, 'trusted_directory'), patch.object(admin, 'read_file'):
            admin.atomic_write(self.path, b'original')
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            with patch.object(admin.os, 'replace', side_effect=OSError), self.assertRaises(OSError):
                admin.atomic_write(self.path, b'changed')
            self.assertEqual(self.path.read_bytes(), b'original')
            self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_backup_restore_and_required_backup(self):
        self.path.write_bytes(b'original')
        def local_read(path):
            return path.read_bytes()
        with patch.object(admin, 'FILES', {'banner': str(self.path)}), patch.object(admin, 'trusted_directory'), \
                patch.object(admin, 'read_file', side_effect=local_read), patch.object(admin.sys, 'stdin', Mock(buffer=io.BytesIO(b'new'))):
            with self.assertRaises(FileNotFoundError):
                admin.file_action('install', 'banner')
            admin.file_action('backup', 'banner')
            with self.assertRaises(ValueError):
                admin.file_action('backup', 'banner')
            with patch.object(admin.sys, 'stdin', Mock(buffer=io.BytesIO(b'new'))):
                admin.file_action('install', 'banner')
            self.assertEqual(self.path.read_bytes(), b'new')
            admin.file_action('restore', 'banner')
            self.assertEqual(self.path.read_bytes(), b'original')


class Bootstrap(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=BASE)
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        for name in ('WRAPPER', 'RULE', 'RECEIPT'):
            context = patch.object(bootstrap, name, self.directory / name)
            context.start()
            self.addCleanup(context.stop)
        for target, value in [('trusted_parent', None), ('check_sudoers', None)]:
            context = patch.object(bootstrap, target, return_value=value)
            context.start()
            self.addCleanup(context.stop)
        for target in ('geteuid', 'fchown'):
            context = patch.object(bootstrap.os, target, return_value=0)
            context.start()
            self.addCleanup(context.stop)
        # Detailed ownership/type checks are exercised by the fake-root harness.
        context = patch.object(bootstrap, 'diagnostic_read',
                               side_effect=lambda path, mode=None: path.read_bytes())
        context.start()
        self.addCleanup(context.stop)

    def test_install_modes_and_rollback(self):
        bootstrap.main('install')
        self.assertEqual(bootstrap.WRAPPER.stat().st_mode & 0o777, 0o755)
        self.assertEqual(bootstrap.RULE.stat().st_mode & 0o777, 0o440)
        self.assertEqual(bootstrap.RULE.read_bytes(), bootstrap.RULE_BYTES)
        with patch.object(bootstrap, 'read_owned', side_effect=lambda p: p.read_bytes()):
            bootstrap.main('rollback')
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_fresh_install_publishes_capability_and_fixed_action(self):
        bootstrap.main('install')
        self.assertEqual(bootstrap.WRAPPER.read_bytes(), (BASE / 'ermis-admin').read_bytes())
        installed = load('installed_admin_wrapper', bootstrap.WRAPPER)
        with patch.object(installed.os, 'geteuid', return_value=0), \
                patch.object(installed, 'run') as run, patch.object(installed, 'audit'), \
                patch('builtins.open', side_effect=PermissionError):
            self.assertEqual(installed.main(['browser', 'check-chromium-dependencies']), 0)
            run.assert_not_called()
        self.assertEqual(installed.plan(['browser', 'install-chromium-dependencies']),
                         admin.plan(['browser', 'install-chromium-dependencies']))

    def test_refuses_existing_and_symlinks(self):
        bootstrap.WRAPPER.symlink_to(self.directory / 'missing')
        with self.assertRaises(ValueError):
            bootstrap.main('install')
        self.assertTrue(bootstrap.WRAPPER.is_symlink())

    def test_validation_failure_rolls_back(self):
        with patch.object(bootstrap, 'check_sudoers', side_effect=[None, None, RuntimeError]), self.assertRaises(bootstrap.BootstrapError):
            bootstrap.main('install')
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_modified_installation_blocks_rollback(self):
        bootstrap.main('install')
        bootstrap.WRAPPER.write_bytes(b'changed fixture')
        with patch.object(bootstrap, 'read_owned', side_effect=lambda p: p.read_bytes()), self.assertRaises(ValueError):
            bootstrap.main('rollback')
        self.assertTrue(bootstrap.RULE.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
