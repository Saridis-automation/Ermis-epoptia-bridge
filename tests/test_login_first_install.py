"""Non-root, repository-local race fixtures. No parser/kernel/service execution."""
from contextlib import contextmanager
from pathlib import Path
import os
import stat
import unittest
from unittest.mock import patch

from admin_bootstrap import login_apparmor as aa, login_bootstrap as login
from admin_bootstrap import login_browser_stage as fs
from tests import test_login_browser_stage as browser

REAL_LOADED = aa.loaded
REAL_KERNEL_ENTRIES = aa.kernel_entries


class FirstInstall(unittest.TestCase):
    def setUp(self):
        browser.Staging.setUp(self)
        browser.Staging.fake_root(self)
        self.target = self.root / ('chromium-' + fs.REVISION)
        self.source.rename(self.target)
        self.source = self.target
        self.stack.enter_context(patch.object(fs, 'BASE', self.root))
        self.record = fs.tree(self.target, 0)
        self.profile = self.root / 'profile'
        self.receipt = self.root / 'receipt'
        self.parser_path = self.root / 'parser'
        self.parser_path.write_bytes(b'fixture')
        self.parser_path.chmod(0o755)
        self.kernel = self.root / 'kernel'
        self.kernel.write_bytes(b'')
        for name, value in [('PROFILE', self.profile), ('RECEIPT', self.receipt),
                            ('PARSER', str(self.parser_path)), ('PROFILES', self.kernel)]:
            self.stack.enter_context(patch.object(aa, name, value))
        self.stack.enter_context(patch.object(aa, 'staging', lambda: vars(fs)))
        self.stack.enter_context(patch.object(aa, 'diagnostic_parents'))
        self.stack.enter_context(patch.object(aa, 'kernel_entries', return_value=[]))
        self.stack.enter_context(patch.object(login, 'transaction_fs', lambda: vars(fs)))
        self.stack.enter_context(patch.object(login.os, 'fchown'))
        self.stack.enter_context(patch.object(login, 'owned_read', lambda p, mode: p.read_bytes()))
        self.api = dict(vars(login), prepare_parent=lambda *a, **k: None)
        self.events = []
        self.hook = lambda event: None
        def parser(action, path):
            self.events.append(action)
            self.hook(action)
            if action == 'load':
                self.assertTrue(self.profile.exists())
                self.assertFalse(self.receipt.exists() and self.receipt.read_bytes() != b'stale')
                self.kernel.write_bytes(b'loaded')
            self.assertNotEqual(action, 'remove')
            return 0
        self.stack.enter_context(patch.object(aa, 'parser', parser))
        def loaded(record):
            self.events.append('verify-kernel')
            self.hook('verify-kernel')
            if self.kernel.read_bytes() != b'loaded':
                raise ValueError('kernel-unproven')
        self.stack.enter_context(patch.object(aa, 'loaded', loaded))
        self.stack.enter_context(patch.object(aa.subprocess, 'run', side_effect=AssertionError('host command')))
        self.stack.enter_context(patch.object(os, 'replace', side_effect=AssertionError('overwrite')))
        self.stack.enter_context(patch.object(os, 'unlink', side_effect=AssertionError('delete')))
        self.stack.enter_context(patch.object(fs.shutil, 'rmtree', side_effect=AssertionError('delete-tree')))

    def install(self, body=None):
        with aa.policy_transaction(self.api, 'install', self.record) as publish:
            publish.final_recheck = lambda prepared: prepared['verify']()
            with publish() as verify:
                if body:
                    body()
                verify()

    def replace_foreign(self, path):
        if path.exists():
            path.rename(path.with_name(path.name + '.saved'))
        path.write_bytes(b'foreign')
        return fs.signature(path.lstat()), path.read_bytes()

    def test_all_files_and_archive_precede_add(self):
        self.receipt.write_bytes(b'stale')
        def check(event):
            if event == 'load':
                self.assertTrue(self.target.exists())
                self.assertEqual(self.profile.read_bytes(), aa.profile(self.record))
                self.assertFalse(self.receipt.exists())
                archive, = self.root.glob('.ermis-login-archive-*')
                self.assertEqual(archive.read_bytes(), b'stale')
        self.hook = check
        self.install()

    def test_full_kernel_inventory_gates_receipt_and_retains_artifacts(self):
        name = aa.profile_name(self.record)
        executable = self.record['executable']
        expected = (name, 'unconfined', executable)
        cases = {
            'zero': [],
            'one': [expected],
            'two-identical': [expected, expected],
            'related-identity': [expected, (name + '-other', 'unconfined', executable)],
            'alternate-binding': [expected, ('other', 'unconfined', executable)],
            'legacy-cache-binding': [expected, ('/home/ermis/.cache/ms-playwright/chromium-1234/chrome',
                                               'unconfined', '/home/ermis/.cache/ms-playwright/chromium-1234/chrome')],
            'wrong-name': [('other', 'unconfined', executable)],
            'wrong-mode': [(name, 'enforce', executable)],
            'wrong-attachment': [(name, 'unconfined', executable + '-other')],
            'substring': [(name + '-other', 'unconfined', executable)],
            'conflicting-mode': [expected, (name, 'complain', executable)],
            'conflicting-attachment': [expected, (name, 'unconfined', '/other')],
            'pattern-binding': [expected, ('other', 'unconfined', '/**')],
            'partial': [(name, 'unconfined', '')],
            'malformed': [(name + '\ntrailing', 'unconfined', executable)],
            'padded': [(' ' + name, 'unconfined', executable)],
            'unknown-mode': [(name, 'unknown', executable)],
            'flat-disagreement': [expected],
            'malformed-flat': [expected],
            'query-failure': [expected],
            'missing-attachment': [expected],
        }
        # Exercise the real inventory reader with repository-local attributes.
        directory = self.root / 'policy/profiles'
        directory.mkdir(parents=True)
        original_parser = aa.parser
        for edge in ('postload', 'final'):
            for case, rows in cases.items():
                with self.subTest(edge=edge, case=case):
                    corrupted = False
                    def inventory(values):
                        for index, row in enumerate(values):
                            entry = directory / str(index)
                            entry.mkdir()
                            for key, value in zip(('name', 'mode', 'attach'), row):
                                (entry / key).write_text(value + '\n')
                        self.kernel.write_text(''.join(n + ' (' + m + ')\n' for n, m, _ in values))
                    def clear_inventory():
                        for entry in directory.iterdir():
                            entry.rename(self.root / ('saved-' + edge + '-' + case + '-' + entry.name))
                    def corrupt():
                        nonlocal corrupted
                        corrupted = True
                        clear_inventory()
                        inventory(rows)
                        if case == 'flat-disagreement':
                            self.kernel.write_text('')
                        elif case == 'malformed-flat':
                            self.kernel.write_text(name + ' (unconfined) trailing\n')
                        elif case == 'missing-attachment':
                            (directory / '0/attach').rename(directory / '0/saved-attach')
                    def parser(action, path):
                        result = original_parser(action, path)
                        if action == 'load':
                            inventory([expected] if edge == 'final' else rows)
                            if edge == 'postload':
                                corrupt()
                        return result
                    def read(path, *args, **kwargs):
                        if case == 'query-failure' and path == self.kernel and corrupted:
                            raise OSError('fixture-query-failure')
                        return path.read_bytes()
                    with patch.object(aa, 'loaded', REAL_LOADED), \
                         patch.object(aa, 'kernel_entries', REAL_KERNEL_ENTRIES), \
                         patch.object(aa, 'diagnostic_read', read), \
                         patch.object(aa, 'parser', parser):
                        if case == 'one':
                            self.install()
                            self.assertTrue(self.receipt.exists())
                            self.receipt.rename(self.root / ('success-' + edge))
                        else:
                            with self.assertRaisesRegex(ValueError, 'manual-recovery:after-kernel-boundary'):
                                self.install(corrupt if edge == 'final' else None)
                            self.assertFalse(self.receipt.exists())
                        self.assertEqual(self.profile.read_bytes(), aa.profile(self.record))
                        self.assertTrue(self.target.is_dir())
                        self.assertNotIn('remove', self.events)
                    self.profile.rename(self.root / ('retained-' + edge + '-' + case))
                    # Archive the synthetic inventory; no removal/rollback.
                    directory.rename(self.root / ('inventory-' + edge + '-' + case))
                    directory.mkdir()
                    self.kernel.write_bytes(b'')

    def test_receipt_rename_has_no_subsequent_fallible_checks(self):
        rename = fs.rename_absent
        def commit(src, dst):
            rename(src, dst)
            if dst == self.receipt:
                self.hook = lambda event: (_ for _ in ()).throw(AssertionError('late check'))
                self.stack.enter_context(patch.object(fs, 'check_binding', side_effect=AssertionError('late file check')))
                self.stack.enter_context(patch.object(login, 'sync_parent', side_effect=AssertionError('late sync')))
        with patch.object(fs, 'rename_absent', commit):
            self.install()
        import json
        self.assertEqual(json.loads(self.receipt.read_bytes()), aa.receipt_record(self.record))

    def test_add_nonzero_and_unknown_outcomes_block_receipt(self):
        original = aa.parser
        for outcome in (1, None, False):
            with self.subTest(outcome=outcome):
                def command(action, path):
                    result = original(action, path)
                    return outcome if action == 'load' else result
                with patch.object(aa, 'parser', command):
                    with self.assertRaisesRegex(ValueError, 'after-kernel-boundary:kernel-add'):
                        self.install()
                self.assertFalse(self.receipt.exists())
                self.assertEqual(self.kernel.read_bytes(), b'loaded')
                self.profile.rename(self.root / ('retained-' + str(outcome)))
                self.kernel.write_bytes(b'')

    def test_success_receipt_last_and_stale_archive(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                if stale:
                    self.receipt.write_bytes(b'stale')
                    before = fs.object_binding(self.receipt, b'stale')
                commits = []
                rename = fs.rename_absent
                def commit(src, dst):
                    commits.append(dst)
                    if dst == self.receipt:
                        self.assertIn('verify-kernel', self.events)
                    rename(src, dst)
                with patch.object(fs, 'rename_absent', commit):
                    self.install()
                self.assertEqual(commits[-1], self.receipt)
                self.assertEqual(commits[0], self.profile)
                self.assertEqual(self.events[0], 'validate')
                if stale:
                    archive, = self.root.glob('.ermis-login-archive-*')
                    self.assertEqual(fs.object_binding(archive, b'stale'), before)
                self.profile.rename(self.root / ('done-profile-' + str(stale)))
                self.receipt.rename(self.root / ('done-receipt-' + str(stale)))
                self.kernel.write_bytes(b'')
                self.events.clear()

    def test_profile_and_receipt_commit_races_preserve_foreign(self):
        for path in (self.profile, self.receipt):
            for edge in ('before', 'after'):
                with self.subTest(path=path.name, edge=edge):
                    original = fs.rename_absent
                    foreign = []
                    def race(src, dst):
                        if dst == path and edge == 'before':
                            foreign.append(self.replace_foreign(path))
                        original(src, dst)
                        if dst == path and edge == 'after':
                            foreign.append(self.replace_foreign(path))
                    with patch.object(fs, 'rename_absent', race):
                        if path == self.receipt and edge == 'after':
                            self.install()  # atomic success has no post-commit failure path
                        else:
                            with self.assertRaisesRegex(ValueError, 'manual-recovery'):
                                self.install()
                    self.assertEqual((fs.signature(path.lstat()), path.read_bytes()), foreign[0])
                    if path == self.profile:
                        self.assertNotIn('load', self.events)
                    for item in (self.profile, self.receipt):
                        if item.exists():
                            item.rename(self.root / (item.name + '-' + edge + '-' + path.name))
                    self.kernel.write_bytes(b'')
                    self.events.clear()

    def test_kernel_ambiguity_and_postload_failure_retain_disk_without_receipt(self):
        for event in ('load', 'verify-kernel'):
            with self.subTest(event=event):
                def fail(current):
                    if current == event:
                        self.kernel.write_bytes(b'loaded')
                        raise ValueError('ambiguous')
                self.hook = fail
                with self.assertRaisesRegex(ValueError, 'after-kernel-boundary'):
                    self.install()
                self.assertTrue(self.profile.exists())
                self.assertFalse(self.receipt.exists())
                self.assertEqual(self.kernel.read_bytes(), b'loaded')
                self.profile.rename(self.root / ('retained-' + event))
                self.kernel.write_bytes(b'')

    def test_foreign_postload_profile_tree_and_receipt_are_untouched(self):
        for name in ('profile', 'tree', 'receipt'):
            with self.subTest(name=name):
                path = {'profile': self.profile, 'tree': self.target, 'receipt': self.receipt}[name]
                foreign = []
                def body():
                    foreign.append(self.replace_foreign(path))
                with self.assertRaisesRegex(ValueError, 'manual-recovery'):
                    self.install(body)
                self.assertEqual((fs.signature(path.lstat()), path.read_bytes()), foreign[0])
                if name != 'receipt':
                    self.assertFalse(self.receipt.exists())
                for item in (self.profile, self.receipt):
                    if item.exists():
                        item.rename(self.root / (item.name + '-post-' + name))
                if name == 'tree':
                    self.target.rename(self.root / 'foreign-tree')
                    self.target.with_name(self.target.name + '.saved').rename(self.target)
                self.kernel.write_bytes(b'')

    def test_foreign_receipt_after_archival_is_not_touched(self):
        self.receipt.write_bytes(b'stale')
        foreign = []
        def body():
            foreign.append(self.replace_foreign(self.receipt))
        with self.assertRaisesRegex(ValueError, 'manual-recovery'):
            self.install(body)
        self.assertEqual((fs.signature(self.receipt.lstat()), self.receipt.read_bytes()), foreign[0])
        archive, = self.root.glob('.ermis-login-archive-*')
        self.assertEqual(archive.read_bytes(), b'stale')

    def test_stale_identity_race_before_archival_blocks_kernel(self):
        self.receipt.write_bytes(b'stale')
        rename = fs.rename_absent
        foreign = []
        def race(src, dst):
            rename(src, dst)
            if dst == self.profile:
                foreign.append(self.replace_foreign(self.receipt))
        with patch.object(fs, 'rename_absent', race):
            with self.assertRaisesRegex(ValueError, 'before-kernel-boundary'):
                self.install()
        self.assertEqual((fs.signature(self.receipt.lstat()), self.receipt.read_bytes()), foreign[0])
        self.assertEqual(list(self.root.glob('.ermis-login-archive-*')), [])
        self.assertNotIn('load', self.events)

    def test_archive_noreplace_conflict_preserves_both(self):
        self.receipt.write_bytes(b'stale')
        rename = fs.rename_absent
        archives = []
        def race(src, dst):
            if dst.name.startswith('.ermis-login-archive-'):
                dst.write_bytes(b'foreign')
                archives.append(dst)
            rename(src, dst)
        with patch.object(fs, 'rename_absent', race):
            with self.assertRaisesRegex(ValueError, 'manual-recovery'):
                self.install()
        self.assertEqual(self.receipt.read_bytes(), b'stale')
        self.assertEqual(archives[0].read_bytes(), b'foreign')

    def test_prior_candidate_kernel_conflict_and_unsupported_deny(self):
        self.profile.write_bytes(aa.profile(self.record))
        with self.assertRaisesRegex(ValueError, 'first-install-only'):
            self.install()
        self.assertEqual(self.events, [])
        self.profile.rename(self.root / 'prior')
        self.kernel.write_text(aa.profile_name(self.record) + ' (unconfined)')
        with self.assertRaisesRegex(ValueError, 'kernel-conflict'):
            self.install()
        self.assertEqual(self.events, [])
        self.kernel.write_bytes(b'')
        with patch.object(fs, 'noreplace_support', return_value=False):
            with self.assertRaisesRegex(ValueError, 'noreplace-unsupported'):
                self.install()
        self.assertEqual(self.events, [])

    def test_temporary_same_bytes_replacement_rejected(self):
        with login.candidate(self.profile, b'candidate', 0o644) as pending:
            pending.rename(pending.with_name('saved-temp'))
            pending.write_bytes(b'candidate')
            before = fs.signature(pending.lstat())
            with self.assertRaisesRegex(ValueError, 'binding-changed'):
                login.validate_candidate(pending, b'candidate', 0o644)
        self.assertEqual(fs.signature(pending.lstat()), before)

    def test_kernel_conflict_at_preboundary_never_adds(self):
        rename = fs.rename_absent
        def conflict(src, dst):
            rename(src, dst)
            if dst == self.profile:
                self.kernel.write_text(aa.profile_name(self.record) + ' (unconfined)')
        with patch.object(fs, 'rename_absent', conflict):
            with self.assertRaisesRegex(ValueError, 'before-kernel-boundary'):
                self.install()
        self.assertNotIn('load', self.events)
        self.assertTrue(self.profile.exists())
        self.assertFalse(self.receipt.exists())

    def test_filesystem_noreplace_unsupported_never_adds(self):
        import errno
        with patch.object(fs, 'rename_absent', side_effect=OSError(errno.ENOSYS, 'unsupported')):
            with self.assertRaisesRegex(ValueError, 'before-kernel-boundary'):
                self.install()
        self.assertNotIn('load', self.events)
        self.assertFalse(self.profile.exists())
        self.assertFalse(self.receipt.exists())

    def test_foreign_receipt_after_archive_retains_archive_and_candidate(self):
        self.receipt.write_bytes(b'stale')
        rename = fs.rename_absent
        foreign = []
        def race(src, dst):
            rename(src, dst)
            if dst.name.startswith('.ermis-login-archive-'):
                foreign.append(self.replace_foreign(self.receipt))
        with patch.object(fs, 'rename_absent', race):
            with self.assertRaisesRegex(ValueError, 'before-kernel-boundary'):
                self.install()
        self.assertEqual((fs.signature(self.receipt.lstat()), self.receipt.read_bytes()), foreign[0])
        archive, = self.root.glob('.ermis-login-archive-*')
        self.assertEqual(archive.read_bytes(), b'stale')
        self.assertFalse(self.kernel.read_bytes())
        self.assertTrue(any(p.read_bytes().startswith(b'{') for p in self.root.glob('.ermis-login-*')
                            if p.is_file() and p != archive))

    def test_staged_profile_and_receipt_replacement_before_commit(self):
        for target in ('profile', 'receipt'):
            with self.subTest(target=target):
                def replace_at_validation(event):
                    if event != 'validate':
                        return
                    for path in self.root.glob('.ermis-login-*'):
                        if not path.is_file():
                            continue
                        data = path.read_bytes()
                        if (target == 'receipt') == data.startswith(b'{'):
                            path.rename(path.with_name('saved-' + target))
                            path.write_bytes(data)
                            break
                self.hook = replace_at_validation
                with self.assertRaisesRegex(ValueError, 'binding-changed'):
                    self.install()
                self.assertNotIn('load', self.events)
                self.assertFalse(self.receipt.exists())
                # Move retained fixtures out of the scan for the next subcase.
                for index, path in enumerate(self.root.glob('.ermis-login-*')):
                    path.rename(self.root / ('retained-' + target + '-' + str(index)))


if __name__ == '__main__':
    unittest.main()
