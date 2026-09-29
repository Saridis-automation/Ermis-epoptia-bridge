"""Exercise the real installer with synthetic root metadata and fixed local targets.

No real chown, sudoers validator, audit logger, or maintenance command may run.
The production installer has no fake-root option or injectable command policy.
"""
import hashlib
from contextlib import ExitStack
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# Compatible with isolated-mode execution, without modifying the import path.
import runpy
helpers = runpy.run_path(str(Path(__file__).with_name('test_admin.py')))
bootstrap = helpers['bootstrap']
load = helpers['load']
BASE = Path(__file__).resolve().parent
subprocess_run = subprocess.run


class Sandbox(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='bootstrap-sandbox-', dir=BASE)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.owners = {}
        self.calls = []
        self.fail_validation = 0
        self.bad_parent = False
        self.real_lstat = Path.lstat
        self.real_fstat = os.fstat
        # Source verification has its own fixture suite. This admin-only fixture
        # must not run it through the synthetic installed-file metadata below.
        def login_module(path):
            self.assertEqual(Path(path).name, 'login_bootstrap.py')
            return {'validate_source': lambda: None}
        self.mock(runpy, 'run_path', login_module)
        for name in ('WRAPPER', 'RULE', 'RECEIPT'):
            target = self.root / str(getattr(bootstrap, name)).lstrip('/')
            target.parent.mkdir(parents=True, exist_ok=True)
            self.mock(bootstrap, name, target)
        self.directories = set(bootstrap.WRAPPER.parents) | set(bootstrap.RULE.parents)
        self.mock(bootstrap, 'login_operation', lambda action: None)
        self.mock(bootstrap, 'apparmor_diagnose', lambda: ('apparmor-ready', 'no-retry'))
        self.mock(bootstrap.os, 'geteuid', lambda: 0)
        self.mock(bootstrap.os, 'fchown', self.chown)
        self.mock(bootstrap.os, 'fstat', self.fstat)
        self.mock(Path, 'lstat', lambda path: self.lstat(path))
        self.mock(bootstrap.subprocess, 'run', self.validate)

    def mock(self, owner, name, value):
        context = patch.object(owner, name, value)
        context.start()
        self.addCleanup(context.stop)

    def metadata(self, info):
        uid, gid = self.owners.get((info.st_dev, info.st_ino), (info.st_uid, info.st_gid))
        return SimpleNamespace(st_mode=info.st_mode, st_uid=uid, st_gid=gid,
                               st_nlink=info.st_nlink)

    def lstat(self, path):
        if path in self.directories:
            # Ancestors outside the fake root are synthetic; never inspect them.
            mode = stat.S_IFDIR | (0o777 if self.bad_parent else 0o755)
            return SimpleNamespace(st_mode=mode, st_uid=0, st_gid=0, st_nlink=1)
        self.assertTrue(path == self.root or self.root in path.parents)
        return self.metadata(self.real_lstat(path))

    def fstat(self, fd):
        return self.metadata(self.real_fstat(fd))

    def chown(self, fd, uid, gid):
        self.assertEqual((uid, gid), (0, 0))
        info = self.real_fstat(fd)
        self.owners[(info.st_dev, info.st_ino)] = (uid, gid)

    def validate(self, command, **kwargs):
        self.assertEqual(command[:2], ['/usr/sbin/visudo', '-c'])
        self.assertEqual(kwargs, dict(check=True, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     env=bootstrap.ENV, cwd='/', timeout=30))
        if len(command) > 2:
            self.assertEqual(command[2], '-f')
            self.assertEqual(len(command), 4)
            candidate = Path(command[3])
            self.assertEqual(candidate.parent, bootstrap.RULE.parent)
            self.assertEqual(bootstrap.read_owned(candidate), bootstrap.RULE_BYTES)
            self.assertEqual(candidate.stat().st_mode & 0o777, 0o440)
        else:
            self.assertEqual(len(command), 2)
        self.calls.append(command)
        if len(self.calls) == self.fail_validation:
            raise RuntimeError('PRIVATE fixture details must never appear')
        return SimpleNamespace(returncode=0)

    def invoke(self, action='install'):
        with patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()) as error:
            result = bootstrap.cli([action])
        return result, error.getvalue()

    def assert_failure(self, identifier):
        self.assertEqual(self.invoke(), (1,
            'Bootstrap failed; review installation state before retrying. ' + identifier + '\n'))

    def test_exact_install_action_self_test_and_verified_rollback(self):
        self.assertEqual(self.invoke(), (0, ''))
        self.assertEqual(len(self.calls), 3)
        for path, mode in ((bootstrap.WRAPPER, 0o755), (bootstrap.RULE, 0o440),
                           (bootstrap.RECEIPT, 0o600)):
            info = self.lstat(path)
            self.assertEqual((info.st_uid, info.st_gid, info.st_mode & 0o777,
                              info.st_nlink), (0, 0, mode, 1))
            bootstrap.read_owned(path)
        data = bootstrap.read_owned(bootstrap.WRAPPER)
        self.assertEqual(data, (BASE / 'ermis-admin').read_bytes())
        self.assertEqual(bootstrap.read_owned(bootstrap.RECEIPT),
                         hashlib.sha256(data).hexdigest().encode('ascii'))
        installed = load('sandbox_installed_admin', bootstrap.WRAPPER)
        with patch.object(installed, 'audit') as audit, patch.object(installed, 'run') as run:
            self.assertEqual(installed.main(['browser', 'check-chromium-dependencies']), 0)
            audit.assert_not_called()
            run.assert_not_called()
            command = installed.plan(['browser', 'install-chromium-dependencies'])
            self.assertEqual(command, ['/usr/bin/apt-get', '--assume-yes', '--no-remove',
                '--no-upgrade', '--no-install-recommends', 'install', *installed.CHROMIUM_PACKAGES])
            with self.assertRaises(ValueError):
                installed.plan(['browser', 'install-chromium-dependencies', 'extra'])
        self.assertEqual(self.invoke('rollback'), (0, ''))
        self.assertFalse(any(p.is_file() for p in self.root.rglob('*')))

    def test_repeat_install_succeeds_without_changing_files(self):
        self.assertEqual(self.invoke(), (0, ''))
        paths = (bootstrap.WRAPPER, bootstrap.RULE, bootstrap.RECEIPT)
        before = [p.read_bytes() for p in paths]
        metadata = [self.real_lstat(p) for p in paths]
        self.assertEqual(self.invoke(), (0, ''))
        self.assertEqual(before, [p.read_bytes() for p in paths])
        self.assertEqual(metadata, [self.real_lstat(p) for p in paths])
        self.assertEqual(len(self.calls), 4)

    def assert_vnc_contract(self):
        # Load only the deployed sandbox copy. No privileged action is executed.
        installed = load('sandbox_vnc_wrapper', bootstrap.WRAPPER)
        request = ['browser', 'install-epoptia-vnc-dependencies']
        self.assertEqual(installed.plan(request), [
            '/usr/bin/apt-get', '--assume-yes', '--no-remove', '--no-upgrade',
            '--no-install-recommends', '-o', 'DPkg::Lock::Timeout=60', 'install',
            'x11vnc', 'novnc', 'websockify', 'xauth'])
        self.assertFalse(installed.PACKAGES)
        for extra in ('curl', '--upgrade', ';id'):
            with self.assertRaises(ValueError):
                installed.plan(request + [extra])
        for package in installed.VNC_PACKAGES:
            with self.assertRaises(ValueError):
                installed.plan(['package', 'install', package])

    def test_vnc_contract_fresh_upgrade_repeat_and_rollback(self):
        self.assertEqual(self.invoke(), (0, ''))
        self.assert_vnc_contract()
        # Model a trusted older wrapper that cannot dispatch the VNC action.
        older = bootstrap.WRAPPER.read_bytes().replace(
            b'install-epoptia-vnc-dependencies', b'prior-epoptia-vnc-dependencies')
        bootstrap.WRAPPER.write_bytes(older)
        bootstrap.RECEIPT.write_bytes(hashlib.sha256(older).hexdigest().encode('ascii'))
        rule_info = self.real_lstat(bootstrap.RULE)
        self.calls.clear()
        self.assertEqual(self.invoke(), (0, ''))
        self.assertEqual(bootstrap.WRAPPER.read_bytes(), (BASE / 'ermis-admin').read_bytes())
        self.assertEqual(self.real_lstat(bootstrap.RULE), rule_info)
        self.assert_vnc_contract()
        self.assertEqual(self.invoke(), (0, ''))
        self.assertEqual(self.invoke('rollback'), (0, ''))
        self.assertFalse(any(p.is_file() for p in self.root.rglob('*')))

    def older_installation(self):
        # A distinct, valid prior revision with its own trusted receipt.
        self.assertEqual(self.invoke(), (0, ''))
        older = (BASE / 'ermis-admin').read_bytes().replace(
            b'check-chromium-dependencies', b'prior-chromium-dependencies')
        self.assertNotEqual(hashlib.sha256(older).hexdigest(),
                            bootstrap.DIAGNOSE_WRAPPER_SHA256)
        bootstrap.WRAPPER.write_bytes(older)
        bootstrap.RECEIPT.write_bytes(hashlib.sha256(older).hexdigest().encode('ascii'))
        self.calls.clear()
        return bootstrap.installed_contents()

    def assert_no_staging_files(self):
        self.assertEqual(set(p for p in self.root.rglob('*') if p.is_file()),
                         {bootstrap.WRAPPER, bootstrap.RECEIPT, bootstrap.RULE})

    def test_upgrade_valid_older_wrapper_and_diagnose(self):
        previous = self.older_installation()
        self.diagnostic('source-installed-mismatch')
        rule_info = self.real_lstat(bootstrap.RULE)
        self.assertEqual(self.invoke(), (0, ''))
        self.assertNotEqual(bootstrap.installed_contents(), previous)
        self.assertEqual(bootstrap.WRAPPER.read_bytes(), (BASE / 'ermis-admin').read_bytes())
        self.assertEqual(self.real_lstat(bootstrap.RULE), rule_info)
        self.diagnostic('installed-valid', 'installed-valid')
        self.assert_no_staging_files()

    def test_upgrade_post_install_failures_restore_original(self):
        previous = self.older_installation()
        for failure in ('sudoers', 'installed'):
            with self.subTest(failure=failure):
                self.calls.clear()
                if failure == 'sudoers':
                    self.fail_validation = 2
                    self.assert_failure('B_SUDOERS_FINAL')
                    self.fail_validation = 0
                else:
                    with patch.object(bootstrap, 'installed_contents',
                                      side_effect=[previous, ValueError('PRIVATE')]):
                        self.assert_failure('B_INSTALLED_VERIFY')
                self.assertEqual(bootstrap.installed_contents(), previous)
                self.assert_no_staging_files()
                self.diagnostic('source-installed-mismatch')

    def test_upgrade_staging_and_replacement_failures_preserve_original(self):
        previous = self.older_installation()
        with patch.object(bootstrap.os, 'fsync', side_effect=OSError('PRIVATE')):
            self.assert_failure('B_UPGRADE_STAGE')
        self.assertEqual(bootstrap.installed_contents(), previous)
        replace = os.replace
        for target in (bootstrap.WRAPPER, bootstrap.RECEIPT):
            failed = False
            def fail_once(source, destination):
                nonlocal failed
                if destination == target and not failed:
                    failed = True
                    raise OSError('PRIVATE')
                return replace(source, destination)
            with self.subTest(target=target.name), patch.object(bootstrap.os, 'replace', fail_once):
                self.assert_failure('B_UPGRADE_REPLACE')
            self.assertEqual(bootstrap.installed_contents(), previous)
            self.assert_no_staging_files()

    def test_upgrade_rejects_invalid_unreviewed_and_linked_source_before_writes(self):
        previous = self.older_installation()
        source_dir = self.root / 'source'
        source_dir.mkdir()
        source = source_dir / 'ermis-admin'
        reviewed = (BASE / 'ermis-admin').read_bytes()
        with patch.object(bootstrap, '__file__', str(source_dir / 'bootstrap.py')):
            for content in (b'invalid syntax !', reviewed + b'\n# unreviewed change\n'):
                source.write_bytes(content)
                with patch.object(bootstrap, 'staged_file') as staging:
                    self.assert_failure('B_SOURCE')
                    staging.assert_not_called()
            source.write_bytes(reviewed)
            alias = source_dir / 'alias'
            source.rename(alias)
            source.symlink_to(alias)
            self.assert_failure('B_SOURCE')
            source.unlink()
            os.link(alias, source)
            self.assert_failure('B_SOURCE')
        self.assertEqual(bootstrap.installed_contents(), previous)
        self.assertEqual(self.calls, [])

    def test_upgrade_rejects_untrusted_existing_artifacts(self):
        previous = self.older_installation()
        for path, mode in ((bootstrap.WRAPPER, 0o755), (bootstrap.RECEIPT, 0o600),
                           (bootstrap.RULE, 0o440)):
            with self.subTest(path=path.name):
                path.chmod(mode ^ 0o040)
                self.assert_failure('B_EXISTING_INSTALL')
                path.chmod(mode)
                info = self.real_lstat(path)
                self.owners[(info.st_dev, info.st_ino)] = (0, 1234)
                self.assert_failure('B_EXISTING_INSTALL')
                self.owners[(info.st_dev, info.st_ino)] = (0, 0)
        bootstrap.RECEIPT.write_bytes(b'not-a-valid-receipt')
        self.assert_failure('B_EXISTING_INSTALL')
        bootstrap.RECEIPT.write_bytes(previous[1])
        bootstrap.RULE.chmod(0o640)
        bootstrap.RULE.write_bytes(b'not-the-fixed-rule')
        bootstrap.RULE.chmod(0o440)
        self.assert_failure('B_EXISTING_INSTALL')
        bootstrap.RULE.chmod(0o640)
        bootstrap.RULE.write_bytes(previous[2])
        bootstrap.RULE.chmod(0o440)
        self.assertEqual(self.calls, [])
        self.assertEqual(bootstrap.installed_contents(), previous)

    def test_sudoers_failure_stages_clean_up(self):
        for count, identifier in ((1, 'B_SUDOERS_INITIAL'), (2, 'B_SUDOERS_CANDIDATE'),
                                  (3, 'B_SUDOERS_FINAL')):
            with self.subTest(stage=identifier):
                self.calls.clear()
                self.fail_validation = count
                self.assert_failure(identifier)
                self.assertFalse(any(p.is_file() for p in self.root.rglob('*')))

    def test_parent_and_ownership_checks_remain_active(self):
        self.bad_parent = True
        self.assert_failure('B_PARENT_TRUST')
        self.assertEqual(self.calls, [])
        self.bad_parent = False
        self.assertEqual(self.invoke(), (0, ''))
        info = self.real_lstat(bootstrap.WRAPPER)
        self.owners[(info.st_dev, info.st_ino)] = (1234, 1234)
        with self.assertRaises(ValueError):
            bootstrap.read_owned(bootstrap.WRAPPER)
        self.assertEqual(self.invoke('rollback')[0], 1)
        self.assertTrue(bootstrap.RULE.exists())

    def test_publication_failure_stages(self):
        publish = bootstrap.publish
        for target, identifier in ((bootstrap.WRAPPER, 'B_PUBLISH_WRAPPER'),
                                  (bootstrap.RECEIPT, 'B_PUBLISH_RECEIPT'),
                                  (bootstrap.RULE, 'B_PUBLISH_RULE')):
            def fail(path, data, mode):
                if path == target:
                    raise OSError('PRIVATE fixture details')
                publish(path, data, mode)
            with self.subTest(stage=identifier), patch.object(bootstrap, 'publish', fail):
                self.assert_failure(identifier)
                self.assertFalse(any(p.is_file() for p in self.root.rglob('*')))

    def test_privilege_lock_source_and_unknown_diagnostics(self):
        import fcntl
        cases = (
            (bootstrap.os, 'geteuid', dict(return_value=1234), 'B_PRIVILEGE'),
            (fcntl, 'flock', dict(side_effect=OSError('PRIVATE')), 'B_LOCK'),
            (bootstrap, 'diagnostic_read', dict(side_effect=OSError('PRIVATE')), 'B_SOURCE'),
            (bootstrap, 'main', dict(side_effect=RuntimeError('PRIVATE')), 'B_UNKNOWN'),
        )
        for owner, name, options, identifier in cases:
            with self.subTest(stage=identifier), patch.object(owner, name, **options):
                self.assert_failure(identifier)
        with patch('sys.stderr', io.StringIO()) as error:
            self.assertEqual(bootstrap.cli(['PRIVATE']), 1)
            self.assertEqual(error.getvalue(),
                'Bootstrap failed; review installation state before retrying. B_ARGUMENT\n')

    def test_cleanup_failure_is_reported_without_private_details(self):
        self.fail_validation = 3
        unlink = Path.unlink
        def fail(path, *args, **kwargs):
            if path == bootstrap.RULE:
                raise OSError('PRIVATE')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail):
            self.assert_failure('B_CLEANUP')
        self.assertTrue(bootstrap.RULE.exists())

    def test_installed_write_bits_links_and_symlinks_are_rejected(self):
        self.assertEqual(self.invoke(), (0, ''))
        bootstrap.WRAPPER.chmod(0o777)
        with self.assertRaises(ValueError):
            bootstrap.read_owned(bootstrap.WRAPPER)
        bootstrap.WRAPPER.chmod(0o755)
        alias = bootstrap.WRAPPER.with_name('fixture-alias')
        os.link(bootstrap.WRAPPER, alias)
        with self.assertRaises(ValueError):
            bootstrap.read_owned(bootstrap.WRAPPER)
        alias.unlink()
        alias.symlink_to(bootstrap.WRAPPER)
        with self.assertRaises(OSError):
            bootstrap.read_owned(alias)

    def diagnostic(self, code, state='unknown'):
        """Forbid mutation and any subprocess except the fixed read-only check."""
        read = bootstrap.diagnostic_read
        paths = (bootstrap.WRAPPER, bootstrap.RECEIPT, bootstrap.RULE)
        def snapshot():
            result = []
            for path in paths:
                try:
                    info = self.real_lstat(path)
                except FileNotFoundError:
                    result.append(None)
                    continue
                data = read(path) if stat.S_ISREG(info.st_mode) else None
                result.append((info, data))
            return result
        before = snapshot()
        real_open = os.open
        def open_readonly(path, flags, *args, **kwargs):
            self.assertIn(Path(path), (*paths, BASE / 'ermis-admin'))
            self.assertEqual(flags, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOATIME)
            return real_open(path, flags, *args, **kwargs)
        def validate_readonly(command, **kwargs):
            self.assertEqual(command, ['/usr/sbin/visudo', '-c', '-f', str(bootstrap.RULE)])
            self.assertEqual(kwargs, dict(check=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=bootstrap.ENV, cwd='/', timeout=30))
            self.calls.append(command)
            return SimpleNamespace(returncode=0)
        with ExitStack() as stack:
            if bootstrap.subprocess.run == self.validate:
                stack.enter_context(patch.object(bootstrap.subprocess, 'run', validate_readonly))
            for owner, names in (
                (os, ('chmod', 'chown', 'fchmod', 'fchown', 'unlink', 'remove',
                      'rename', 'replace', 'link', 'symlink', 'mkdir', 'rmdir', 'write')),
                (Path, ('write_bytes', 'write_text', 'touch', 'unlink', 'chmod', 'mkdir')),
                (bootstrap, ('main', 'publish')),
                (tempfile, ('mkstemp', 'NamedTemporaryFile')),
            ):
                for name in names:
                    mock = stack.enter_context(patch.object(owner, name,
                        side_effect=AssertionError('PRIVATE mutation attempted')))
                    stack.callback(mock.assert_not_called)
            stack.enter_context(patch.object(os, 'open', open_readonly))
            out = stack.enter_context(patch('sys.stdout', io.StringIO()))
            err = stack.enter_context(patch('sys.stderr', io.StringIO()))
            # This legacy admin observer is no longer the public diagnose route.
            # The shell/CLI route has its own zero-side-effect fixture suite.
            result = bootstrap.diagnose()
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(result, (code, state))
        self.assertEqual(snapshot(), before)

    def test_diagnose_valid_and_missing_artifacts_are_readonly(self):
        self.diagnostic('wrapper-missing')
        self.assertEqual(self.invoke(), (0, ''))
        self.calls.clear()
        self.diagnostic('installed-valid', 'installed-valid')
        self.assertEqual(self.calls, [['/usr/sbin/visudo', '-c', '-f', str(bootstrap.RULE)]])
        for path, code in ((bootstrap.WRAPPER, 'wrapper-missing'),
                           (bootstrap.RECEIPT, 'helper-missing'),
                           (bootstrap.RULE, 'sudoers-missing')):
            saved = path.with_name('saved-fixture')
            path.rename(saved)
            self.diagnostic(code, 'partial-install')
            saved.rename(path)

    def test_diagnose_extra_arguments_never_reach_python(self):
        # Bypass the sandbox's subprocess mock only for the local shell guard.
        with patch.object(bootstrap.subprocess, 'run', subprocess_run):
            result = subprocess.run(['/bin/sh', str(BASE / 'bootstrap.sh'),
                                     'diagnose', 'PRIVATE'], capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, 'unknown\nunknown\n', ''))

    def test_diagnose_rejects_types_owners_and_modes(self):
        self.assertEqual(self.invoke(), (0, ''))
        for path, code in ((bootstrap.WRAPPER, 'wrapper-owner-mode'),
                           (bootstrap.RECEIPT, 'helper-owner-mode'),
                           (bootstrap.RULE, 'sudoers-invalid')):
            mode = stat.S_IMODE(self.real_lstat(path).st_mode)
            path.chmod(0o777)
            self.diagnostic(code)
            path.chmod(mode)
            info = self.real_lstat(path)
            for owner in ((1234, 0), (0, 1234)):
                self.owners[(info.st_dev, info.st_ino)] = owner
                self.diagnostic(code)
            self.owners[(info.st_dev, info.st_ino)] = (0, 0)
        saved = bootstrap.WRAPPER.with_name('saved-fixture')
        bootstrap.WRAPPER.rename(saved)
        bootstrap.WRAPPER.symlink_to(saved)
        self.diagnostic('wrapper-type')
        bootstrap.WRAPPER.unlink()
        os.mkfifo(bootstrap.WRAPPER)
        self.diagnostic('wrapper-type')

    def test_diagnose_fingerprints_rules_actions_and_redacted_failures(self):
        self.assertEqual(self.invoke(), (0, ''))
        read = bootstrap.diagnostic_read
        source_path = BASE / 'ermis-admin'
        source = read(source_path)
        def substituted(replacements):
            return patch.object(bootstrap, 'diagnostic_read',
                side_effect=lambda path, mode=None: replacements.get(path, read(path, mode)))
        for path, data, code in (
            (source_path, b'PRIVATE syntax !', 'source-invalid'),
            (source_path, source + b'\n# changed', 'source-installed-mismatch'),
            (bootstrap.RECEIPT, b'PRIVATE', 'source-installed-mismatch'),
            (bootstrap.RULE, b'PRIVATE', 'sudo-rule-mismatch'),
        ):
            with substituted({path: data}):
                self.diagnostic(code)
        for data, code in ((b'pass\n', 'action-missing'),
                           (b'if "PRIVATE" is "PRIVATE":\n    pass\n', 'action-missing'),
                           (source + b'\n# unreviewed revision', 'unknown')):
            with substituted({source_path: data, bootstrap.WRAPPER: data,
                              bootstrap.RECEIPT: hashlib.sha256(data).hexdigest().encode('ascii')}):
                self.diagnostic(code)
        for error, code in (
            (subprocess.CalledProcessError(1, 'PRIVATE', output='PRIVATE'), 'sudoers-invalid'),
            (FileNotFoundError('PRIVATE'), 'unknown'),
            (subprocess.TimeoutExpired('PRIVATE', 30, output='PRIVATE'), 'unknown'),
        ):
            with patch.object(bootstrap.subprocess, 'run', side_effect=error):
                self.diagnostic(code)
        with patch.object(bootstrap.os, 'geteuid', return_value=1234):
            self.diagnostic('unknown')
        self.bad_parent = True
        self.diagnostic('unknown')


if __name__ == '__main__':
    unittest.main(verbosity=2)
