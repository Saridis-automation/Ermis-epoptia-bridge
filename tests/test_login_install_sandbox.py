"""Real login installer, synthetic ownership; all filesystem writes stay local."""
import io
from contextlib import contextmanager, nullcontext
import json
import hashlib
import ast
import sys
import os
from pathlib import Path
import pwd
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from admin_bootstrap import login_bootstrap as login
from admin_bootstrap import bootstrap as main

BASE = Path(__file__).resolve().parent
ATOMIC_PREFLIGHT = login.atomic_preflight


class LoginSandbox(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=BASE)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.real_lstat, self.real_fstat = Path.lstat, os.fstat
        self.owners = {}
        self.files = {self.local(p): value for p, value in login.artifacts().items()}
        self.receipt = self.local(login.RECEIPT)
        self.state = self.local('/home/ermis/.local/state/epoptia-browser')
        self.local('/home').mkdir()
        self.local('/run').mkdir()
        self.local('/usr/local').mkdir(parents=True)
        self.mock(login, 'atomic_preflight', lambda files: None)
        self.operations = []
        self.mock(login, 'socket_operation', self.operations.append)
        # Dedicated AppArmor fixture tests exercise its privileged transaction.
        self.mock(login, 'apparmor_transaction', lambda action, **kwargs: nullcontext())
        self.real_bindings = login.installed_bindings
        self.mock(login, 'installed_bindings', lambda: {})
        self.mock(login, 'socket_blockers', lambda: [])
        self.mock(login, 'verify_unit_origin', lambda kind: None)
        self.ancestors = set(self.root.parents)
        self.mock(login, 'Path', self.local)
        self.mock(login, 'RECEIPT', self.receipt)
        self.ready = self.local(login.READY)
        self.mock(login, 'READY', self.ready)
        policy_receipt = self.root / 'policy-receipt-fixture'
        policy_receipt.write_bytes(b'fixture policy receipt')
        self.mock(login, 'POLICY_RECEIPT', policy_receipt)
        self.mock(login, 'artifacts', lambda: self.files.copy())
        self.mock(login, 'validate_source', lambda: None)
        self.mock(login, 'fingerprint', lambda: 'a' * 64)
        self.mock(login.os, 'geteuid', lambda: 0)
        self.mock(pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=1001, pw_gid=1001))
        self.mock(Path, 'lstat', lambda path: self.lstat(path))
        self.mock(os, 'fstat', lambda fd: self.metadata(self.real_fstat(fd)))
        self.mock(os, 'fchown', self.fchown)
        self.mock(os, 'chown', self.chown)
        self.mock(login.subprocess, 'run', self.forbid)

    def mock(self, obj, name, value):
        context = patch.object(obj, name, value)
        context.start()
        self.addCleanup(context.stop)

    def forbid(self, *args, **kwargs):
        self.fail('Installer must not launch commands')

    def local(self, value):
        path = Path(value)
        if path == self.root or self.root in path.parents:
            return path
        return self.root / str(path).lstrip('/')

    def metadata(self, info):
        uid, gid = self.owners.get((info.st_dev, info.st_ino), (0, 0))
        return SimpleNamespace(st_mode=info.st_mode, st_uid=uid, st_gid=gid,
                               st_nlink=info.st_nlink, st_size=info.st_size,
                               st_dev=info.st_dev, st_ino=info.st_ino,
                               st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns, st_mtime=info.st_mtime)

    def lstat(self, path):
        if path in self.ancestors:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_gid=0,
                                   st_dev=1, st_ino=hash(path))
        self.assertTrue(path == self.root or self.root in path.parents)
        return self.metadata(self.real_lstat(path))

    def fchown(self, fd, uid, gid):
        self.assertEqual((uid, gid), (0, 0))
        info = self.real_fstat(fd)
        self.owners[info.st_dev, info.st_ino] = uid, gid

    def chown(self, path, uid, gid, **kwargs):
        self.assertTrue(self.root in path.parents)
        info = self.real_lstat(path)
        self.owners[info.st_dev, info.st_ino] = uid, gid


    def test_installed_runtime_helper_is_fixed_idempotent_and_refuses_unsafe_state(self):
        # Execute the generated helper with only its stdlib bindings replaced by
        # this local fixture. No installed helper or host path is used.
        tree = ast.parse(login.RUNTIME_HELPER)
        tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
        code = compile(tree, '<runtime-helper-fixture>', 'exec')
        bindings = dict(os=os, pwd=pwd, stat=stat, sys=sys, Path=self.local)
        with patch.object(sys, 'argv', ['runtime-helper']):
            exec(code, bindings)
            exec(code, bindings)
            runtime = self.local(login.RUNTIME)
            self.assertEqual(self.lstat(runtime).st_uid, 1001)
            runtime.chmod(0o755)
            with self.assertRaises(SystemExit) as error:
                exec(code, bindings)
            self.assertEqual(error.exception.code, 1)
        with patch.object(sys, 'argv', ['runtime-helper', 'unapproved']):
            with self.assertRaises(SystemExit):
                exec(code, bindings)


    def test_atomic_recheck_failure_precedes_all_mutations(self):
        import fcntl
        before = set(self.root.rglob('*'))
        events = []
        def recheck(files):
            self.assertEqual(events, ['locked'])
            raise ValueError('unproven')
        with patch.object(fcntl, 'flock', side_effect=lambda *args: events.append('locked')), patch.object(login, 'atomic_preflight', side_effect=recheck):
            with self.assertRaises(ValueError):
                login.install('install')
        self.assertEqual(set(self.root.rglob('*')), before)
        self.assertEqual(self.operations, [])


    def production_policy(self):
        """Use real tree, policy and installer transactions with local OS seams."""
        from admin_bootstrap import login_apparmor as aa, login_browser_stage as stage
        policy_transaction = aa.policy_transaction
        @contextmanager
        def fixture_policy(*args, **kwargs):
            with policy_transaction(*args, **kwargs) as publish:
                # This fixture bypasses atomic_preflight; retain the final file
                # checks. test_login_final_gate exercises the full fresh gate.
                publish.final_recheck = lambda prepared: prepared['verify']()
                yield publish
        self.mock(aa, 'policy_transaction', fixture_policy)
        self.mock(login, 'apparmor_transaction', lambda action, **kwargs: aa.transaction(vars(login), action, **kwargs))
        self.mock(aa, 'staging', lambda: vars(stage))
        self.mock(login, 'policy_module', lambda: vars(aa))
        self.mock(login, 'installed_bindings', self.real_bindings)
        real_stat = os.stat
        self.mock(os, 'stat', lambda *args, **kwargs: self.metadata(real_stat(*args, **kwargs)))
        self.mock(stage, 'BASE', self.root / 'opt/ermis/epoptia-browser')
        (self.root / 'opt').mkdir(exist_ok=True)
        self.source = self.root / 'browser-source'
        (self.source / 'chrome-linux64').mkdir(parents=True)
        chrome = self.source / stage.LAYOUT
        chrome.write_bytes(b'\x7fELFfixture')
        chrome.chmod(0o755)
        self.mock(stage, 'source', lambda root: (self.source, 0))
        self.mock(stage, 'parents', lambda path, uid=0: login.prepare_parent(path))
        self.mock(stage, 'open_path', lambda path, uid=0, flags=stage.FLAGS, ancestry=None: os.open(path, flags))
        self.profile = self.root / 'etc/apparmor.d/profile'
        self.policy_receipt = self.receipt.parent / 'policy-receipt'
        self.mock(aa, 'PROFILE', self.profile)
        self.mock(aa, 'RECEIPT', self.policy_receipt)
        self.mock(login, 'POLICY_RECEIPT', self.policy_receipt)
        self.kernel = self.root / 'kernel-profiles'
        self.kernel.write_text('')
        self.mock(aa, 'PROFILES', self.kernel)
        parser_path = self.root / 'parser'
        parser_path.write_text('fixture')
        parser_path.chmod(0o755)
        self.mock(aa, 'PARSER', str(parser_path))
        self.mock(aa, 'diagnostic_attachment', lambda record: True)
        self.mock(aa, 'kernel_entries', lambda: [
            (aa.profile_name(stage.tree(self.target, 0)), 'unconfined',
             stage.tree(self.target, 0)['executable'])] if self.kernel.read_text() else [])
        self.target = stage.BASE / ('chromium-' + stage.REVISION)
        self.policy_calls = []
        def parser(action, path):
            data = path.read_bytes()
            self.assertNotIn(b'/home/', data)
            self.policy_calls.append(action)
            if action == 'load':
                self.assertTrue(self.target.exists())
                self.assertIn('validate', self.policy_calls)
                self.kernel.write_text(aa.profile_name(stage.tree(self.target, 0)) + ' (unconfined)')
            self.assertNotEqual(action, 'remove')
            return 0
        self.mock(aa, 'parser', parser)
        return aa, stage

    def production_snapshot(self):
        paths = [*self.files, self.receipt, self.profile, self.policy_receipt, self.kernel]
        return ({p: (p.read_bytes(), stat.S_IMODE(p.lstat().st_mode)) if p.exists() else None
                 for p in paths},
                {str(p.relative_to(self.target)): (p.read_bytes() if p.is_file() else None,
                                                   stat.S_IMODE(p.lstat().st_mode))
                 for p in self.target.rglob('*')} if self.target.exists() else None)

    def assert_transaction_clean(self):
        self.assertEqual(list(self.root.rglob('.ermis-login-*')), [])
        self.assertEqual(list(self.root.rglob('.stage-*')), [])


    def test_production_source_gate_precedes_all_privileged_actions(self):
        with patch.object(login, 'validate_source', side_effect=ValueError('fixture')), \
             patch.object(main, 'main') as admin, \
             patch.object(main, 'login_operation') as operation, \
             patch('runpy.run_path', return_value=vars(login)), \
             patch('sys.stderr', io.StringIO()):
            self.assertEqual(main.cli(['install']), 1)
            admin.assert_not_called()
            operation.assert_not_called()
        with patch.object(login, 'validate_source', side_effect=ValueError('fixture')), \
             patch.object(login, 'prepare_parent') as prepare:
            with self.assertRaises(login.LoginInstallError):
                login.install('install')
            prepare.assert_not_called()


    def test_real_atomic_preflight_rejects_late_policy_and_target_failures_without_writes(self):
        from contextlib import ExitStack
        import runpy
        from admin_bootstrap import login_diagnose as d
        aa, stage = self.production_policy()
        self.mock(login, 'atomic_preflight', ATOMIC_PREFLIGHT)
        self.mock(runpy, 'run_path', lambda path: vars(d))
        self.mock(d, 'source', lambda: ({}, 'a' * 64))
        self.mock(d, 'source_modules', lambda sources: (vars(stage), vars(aa)))
        self.mock(d, 'validate_candidate', lambda *args: 'ok')
        self.mock(d, 'transaction_observation', lambda fp: ('idle', None))
        self.mock(d, 'conflicts', lambda *args: 'clear')
        self.mock(d, 'current_state', lambda *args: ({
            'baseline': 'clean-absent', 'installed-tree': 'absent',
            'profile-disk': 'absent', 'profile-kernel': 'absent',
            'kernel-probe': 'absent', 'receipt-classification': 'missing',
        }, 'clear', None))
        self.mock(d, 'baseline', lambda: {'disk': {str(p): None for p in d.DISK_ROOTS}, 'kernel': []})
        self.mock(login, 'install_activity', lambda: {'login-activity': 'inactive', 'browser-activity': 'inactive'})
        cases = ((stage, 'destination_state'), (login, 'managed_files'))
        for owner, name in cases:
            with self.subTest(gate=name), ExitStack() as stack:
                stack.enter_context(patch.object(owner, name, side_effect=ValueError('changed-after-diagnosis')))
                guards = [stack.enter_context(patch.object(obj, method, side_effect=AssertionError('mutation')))
                          for obj, method in ((Path, 'mkdir'), (tempfile, 'mkstemp'), (tempfile, 'mkdtemp'),
                                              (login, 'candidate'), (aa, 'parser'), (stage, 'rename_absent'))]
                with self.assertRaisesRegex(login.LoginInstallError, 'B_LOGIN_ATOMIC_RECHECK'):
                    login.install('install')
                for guard in guards:
                    guard.assert_not_called()
                self.assertEqual(self.operations, [])
                self.assertEqual(self.policy_calls, [])
        # A successful real recheck yields a bound plan before the first mkdir.
        checked = []
        def stop_after_preflight(files):
            plan = ATOMIC_PREFLIGHT(files)
            plan['check_bound']()
            checked.append(plan)
            raise ValueError('preflight-complete')
        with patch.object(login, 'atomic_preflight', stop_after_preflight), \
             patch.object(Path, 'mkdir', side_effect=AssertionError('early mkdir')), \
             patch.object(tempfile, 'mkstemp', side_effect=AssertionError('early temp')):
            with self.assertRaisesRegex(ValueError, 'preflight-complete'):
                login.install('install')
        self.assertEqual(len(checked), 1)
        self.assertEqual(checked[0]['profile'], aa.profile(checked[0]['record']))


    def test_first_install_files_before_add_and_receipts_last(self):
        aa, stage = self.production_policy()
        parser = aa.parser
        events = []
        rename = stage.rename_absent
        def commit(src, dst):
            events.append(dst)
            return rename(src, dst)
        def add(action, path):
            if action == 'load':
                self.assertTrue(all(p.exists() for p in self.files))
                self.assertTrue(self.profile.exists())
                self.assertFalse(self.policy_receipt.exists())
                self.assertTrue(self.ready.exists())
                events.append('kernel')
            self.assertNotEqual(action, 'remove')
            return parser(action, path)
        with patch.object(stage, 'rename_absent', commit), patch.object(aa, 'parser', add), \
             patch.object(os, 'replace', side_effect=AssertionError('overwrite')):
            login.install('install')
        self.assertLess(events.index(self.target), events.index('kernel'))
        self.assertLess(events.index(self.profile), events.index('kernel'))
        self.assertLess(events.index(self.ready), events.index('kernel'))
        self.assertEqual(events[-1], self.policy_receipt)
        self.assertEqual(login.ready_blockers(), [])

    def test_final_receipt_move_has_no_outer_verification(self):
        aa, stage = self.production_policy()
        rename = stage.rename_absent
        def commit(src, dst):
            rename(src, dst)
            if dst == self.policy_receipt:
                # Poison every outer verification seam after successful rename.
                self.mock(stage, 'check_binding', self.forbid)
                self.mock(aa, 'loaded', self.forbid)
                self.mock(login, 'sync_parent', self.forbid)
                self.mock(login, 'installed_bindings', self.forbid)
        with patch.object(stage, 'rename_absent', commit):
            login.install('install')
        self.assertTrue(self.policy_receipt.exists())

    def test_stale_receipt_archive_precedes_kernel_in_full_install(self):
        aa, stage = self.production_policy()
        self.policy_receipt.parent.mkdir(parents=True, exist_ok=True)
        self.policy_receipt.write_bytes(b'stale')
        parser = aa.parser
        def command(action, path):
            if action == 'load':
                self.assertTrue(self.target.exists())
                self.assertTrue(self.profile.exists())
                self.assertFalse(self.policy_receipt.exists())
                archive, = self.policy_receipt.parent.glob('.ermis-login-archive-*')
                self.assertEqual(archive.read_bytes(), b'stale')
            return parser(action, path)
        with patch.object(aa, 'parser', command):
            login.install('install')
        self.assertEqual(login.ready_blockers(), [])

    def test_coherent_candidate_and_prior_retry_denied_without_mutation(self):
        self.production_policy()
        login.install('install')
        before = self.production_snapshot()
        for changed in (False, True):
            if changed:
                (self.source / 'chrome-linux64/chrome').write_bytes(b'\x7fELFchanged')
            calls = list(self.policy_calls)
            with self.assertRaises(login.LoginInstallError):
                login.install('install')
            self.assertEqual(self.production_snapshot(), before)
            self.assertEqual(self.policy_calls, calls)

    def test_preboundary_parse_failure_retains_temps_without_publication(self):
        aa, stage = self.production_policy()
        before = self.production_snapshot()
        with patch.object(aa, 'parser', side_effect=ValueError('parse')):
            with self.assertRaises(login.LoginInstallError):
                login.install('install')
        self.assertEqual(self.production_snapshot(), before)
        self.assertFalse(self.ready.exists())
        self.assertTrue(list(self.root.rglob('.ermis-login-*')))

    def test_late_failure_never_removes_kernel_or_files(self):
        self.production_policy()
        with patch.object(login, 'socket_blockers', side_effect=ValueError('late')):
            with self.assertRaises(login.LoginInstallError):
                login.install('install')
        self.assertTrue(self.target.exists())
        self.assertTrue(self.profile.exists())
        self.assertTrue(all(p.exists() for p in self.files))
        self.assertTrue(self.kernel.read_bytes())
        self.assertFalse(self.policy_receipt.exists())
        self.assertTrue(self.ready.exists())
        self.assertEqual(login.ready_blockers(), ['login-not-ready'])
        self.assertNotIn('remove', self.policy_calls)

    def test_ready_race_preserves_foreign_without_kernel_rollback(self):
        aa, stage = self.production_policy()
        rename = stage.rename_absent
        def race(src, dst):
            if dst == self.ready:
                self.ready.write_bytes(b'foreign')
            return rename(src, dst)
        with patch.object(stage, 'rename_absent', race):
            with self.assertRaises(login.LoginInstallError):
                login.install('install')
        self.assertEqual(self.ready.read_bytes(), b'foreign')
        self.assertFalse(self.kernel.read_bytes())
        self.assertNotIn('remove', self.policy_calls)
        self.assertEqual(login.ready_blockers(), ['login-not-ready'])


if __name__ == '__main__':
    unittest.main()
