"""Synthetic read-only diagnostics: no host policy/parser/browser access."""
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from admin_bootstrap import login_apparmor as aa, bootstrap

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = 'PRIVATE /secret/path digest=abcdef owner=123 mode=777'


class Diagnose(unittest.TestCase):
    def setUp(self):
        self.record = dict(schema=2, executable='/opt/ermis/epoptia-browser/chromium-1243/chrome-linux64/chrome',
                           revision='1243', layout='chrome-linux64/chrome', playwright='1.63.0', chromium='153.0.8010.12', sha256='a' * 64, tree_sha256='b' * 64)
        self.data = aa.profile(self.record)
        self.receipt = json.dumps(aa.receipt_record(self.record)).encode()
        self.files = {aa.ENABLED: b'Y\n', aa.PROFILE: self.data, aa.RECEIPT: self.receipt,
                      aa.PROFILES: (aa.profile_name(self.record) + ' (unconfined)\n').encode()}
        manifest = json.loads((ROOT / 'admin_bootstrap/login_manifest.json').read_bytes())
        self.files[ROOT / 'admin_bootstrap/login_manifest.json'] = json.dumps(manifest).encode()
        for relative in ('admin_bootstrap/bootstrap.py', 'admin_bootstrap/login_apparmor.py',
                         'epoptia_login_apparmor.cjs', 'epoptia_browser_runtime.cjs', 'admin_bootstrap/login_browser_stage.py'):
            self.files[ROOT / relative] = (ROOT / relative).read_bytes()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.mock(aa.os, 'geteuid', return_value=0)
        self.mock(aa, 'diagnostic_read', side_effect=self.read)
        self.mock(aa, 'diagnostic_parents')
        self.identity = self.mock(aa, 'diagnostic_identity', return_value=self.record)
        self.parser = self.mock(aa, 'diagnostic_parser', return_value=True)
        self.attachment = self.mock(aa, 'diagnostic_attachment', return_value=True)
        self.info = self.mock(Path, 'lstat', return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0))
        # All mutation entry points are forbidden, including installer helpers.
        for obj, names in ((os, ('open', 'write', 'chmod', 'chown', 'fchmod', 'fchown', 'unlink', 'remove', 'rename', 'replace', 'mkdir', 'rmdir', 'link', 'symlink')),
                           (Path, ('write_bytes', 'write_text', 'touch', 'unlink', 'mkdir', 'chmod')),
                           (tempfile, ('mkstemp', 'mkdtemp', 'NamedTemporaryFile')),
                           (aa, ('collect', 'parser', 'transaction')),
                           (subprocess, ('run', 'Popen'))):
            for name in names:
                mock = self.mock(obj, name, side_effect=AssertionError(PRIVATE))
                self.stack.callback(mock.assert_not_called)

    def mock(self, obj, name, **kwargs):
        return self.stack.enter_context(patch.object(obj, name, **kwargs))

    def read(self, path, *args, **kwargs):
        if path not in self.files:
            raise FileNotFoundError(PRIVATE)
        value = self.files[path]
        if isinstance(value, Exception):
            raise value
        return value

    def check(self, stage, recovery):
        before = self.files.copy()
        with patch('sys.stdout', io.StringIO()) as out, patch('sys.stderr', io.StringIO()) as err:
            actual = aa.diagnose(ROOT)
        self.assertEqual(actual, (stage, recovery))
        self.assertIn(actual[0], aa.STAGES)
        self.assertIn(actual[1], aa.RECOVERIES)
        for token in actual:
            self.assertRegex(token, r'^[a-z]+(?:-[a-z]+)*$')
        self.assertEqual(out.getvalue() + err.getvalue(), '')
        self.assertEqual(before, self.files)

    def test_all_primary_and_recovery_tokens_and_partial_states(self):
        cases = [
            ({}, 'apparmor-ready', 'no-retry'),
            ({aa.ENABLED: b'N'}, 'apparmor-disabled', 'no-retry'),
            ({aa.ENABLED: b'PRIVATE'}, 'apparmor-unknown', 'manual-review-required'),
            ({aa.PROFILE: None, aa.RECEIPT: None, aa.PROFILES: b''}, 'apparmor-profile-not-installed', 'manual-review-required'),
            ({aa.PROFILE: b'PRIVATE'}, 'apparmor-profile-content-mismatch', 'manual-review-required'),
            ({aa.PROFILES: b'', aa.RECEIPT: None}, 'apparmor-profile-installed-not-loaded', 'manual-review-required'),
            ({aa.PROFILES: b''}, 'apparmor-profile-installed-not-loaded', 'manual-review-required'),
            ({aa.PROFILES: (aa.profile_name(self.record) + ' (enforce)').encode()}, 'apparmor-profile-loaded-identity-mismatch', 'manual-review-required'),
            ({aa.RECEIPT: None}, 'apparmor-receipt-missing', 'manual-review-required'),
            ({aa.RECEIPT: b'PRIVATE'}, 'apparmor-receipt-mismatch', 'manual-review-required'),
            ({aa.PROFILE: None, aa.RECEIPT: None}, 'apparmor-partial-install-recoverable', 'manual-review-required'),
            ({aa.PROFILE: None, aa.PROFILES: b''}, 'apparmor-partial-install-recoverable', 'manual-review-required'),
        ]
        original = self.files.copy()
        for changes, stage, recovery in cases:
            with self.subTest(stage=stage, changes=list(changes)):
                self.files = original.copy()
                for path, value in changes.items():
                    if value is None:
                        self.files.pop(path)
                    else:
                        self.files[path] = value
                self.check(stage, recovery)
        self.files = original.copy()
        self.info.side_effect = FileNotFoundError(PRIVATE)
        self.check('apparmor-parser-missing', 'source-fix-required')
        self.info.side_effect = None
        self.identity.side_effect = ValueError(PRIVATE)
        self.check('apparmor-staging-invalid', 'manual-review-required')
        self.identity.side_effect = None
        self.parser.return_value = False
        self.check('apparmor-profile-syntax-invalid', 'source-fix-required')
        self.parser.return_value = True
        old = {**self.record, 'sha256': 'b' * 64}
        data = aa.profile(old)
        self.files[aa.PROFILE] = data
        self.files[aa.RECEIPT] = json.dumps(aa.receipt_record(old)).encode()
        self.check('apparmor-profile-content-mismatch', 'rollback-required')

    def test_source_and_staging_missing_are_bounded(self):
        self.identity.side_effect = FileNotFoundError()
        with patch.object(aa, 'staging', return_value={'source': lambda root: ('fixture', 1000), 'tree': lambda *args: None}):
            self.check('apparmor-staging-missing', 'manual-review-required')
        with patch.object(aa, 'staging', side_effect=ValueError(PRIVATE)):
            self.check('apparmor-source-invalid', 'source-fix-required')

    def test_observation_failures_redacted(self):
        for path in (aa.ENABLED, aa.PROFILE, aa.RECEIPT, aa.PROFILES):
            original = self.files[path]
            for error in (PermissionError(PRIVATE), ValueError(PRIVATE), OSError(PRIVATE)):
                self.files[path] = error
                self.check('apparmor-unknown', 'manual-review-required')
            self.files[path] = original
        self.parser.side_effect = subprocess.TimeoutExpired(PRIVATE, 20, stderr=PRIVATE)
        self.check('apparmor-unknown', 'manual-review-required')

    def test_nonroot_and_source_mismatch_fail_closed(self):
        with patch.object(os, 'geteuid', return_value=1000):
            self.check('apparmor-unknown', 'manual-review-required')
        self.files[ROOT / 'admin_bootstrap/login_apparmor.py'] = b'PRIVATE'
        self.check('apparmor-unknown', 'source-fix-required')

    def test_attachment_mismatch_and_unobservable_fail_closed(self):
        self.attachment.return_value = False
        self.check('apparmor-profile-loaded-identity-mismatch', 'manual-review-required')
        self.attachment.side_effect = PermissionError(PRIVATE)
        self.check('apparmor-unknown', 'manual-review-required')

    def test_receipt_fields_and_duplicate_kernel_identity(self):
        receipt = json.loads(self.receipt)
        for key in receipt:
            self.files[aa.RECEIPT] = json.dumps({**receipt, key: PRIVATE}).encode()
            self.check('apparmor-receipt-mismatch', 'manual-review-required')
        self.files[aa.RECEIPT] = self.receipt
        self.files[aa.PROFILES] *= 2
        self.check('apparmor-profile-loaded-identity-mismatch', 'manual-review-required')


class Primitives(unittest.TestCase):
    def test_kernel_attachment_exact_identity_without_mutation(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            root = Path(directory)
            entry = root / 'policy/profiles/owned'
            entry.mkdir(parents=True)
            executable = '/home/ermis/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome'
            for name, data in (('name', aa.profile_name({'executable': executable, 'revision': '1243'})), ('attach', executable), ('mode', 'unconfined')):
                (entry / name).write_text(data + '\n')
            paths = (entry.parent, entry, *(entry / n for n in ('name', 'attach', 'mode')))
            def snapshot():
                return [(s.st_atime_ns, s.st_mtime_ns, s.st_ctime_ns) for s in (p.stat() for p in paths)]
            before = snapshot()
            with patch.object(aa, 'PROFILES', root / 'profiles'):
                self.assertTrue(aa.diagnostic_attachment({'executable': executable, 'revision': '1243'}))
                self.assertEqual(before, snapshot())
                (entry / 'attach').write_text('/PRIVATE')
                self.assertFalse(aa.diagnostic_attachment({'executable': executable, 'revision': '1243'}))
                (entry / 'attach').unlink()
                with self.assertRaises(FileNotFoundError):
                    aa.diagnostic_attachment({'executable': executable, 'revision': '1243'})

    def test_parser_delegates_to_confined_source_validator(self):
        code = b'def parser(data):\n    assert data == b"candidate"\nclass Failure(Exception): pass\n'
        manifest = json.dumps({'admin_bootstrap/login_diagnose.py': hashlib.sha256(code).hexdigest()}).encode()
        with patch.object(aa, 'diagnostic_parents'), patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0)), patch.object(aa, 'diagnostic_read', side_effect=[code, manifest]):
            self.assertTrue(aa.diagnostic_parser(b'candidate'))

    def test_read_preserves_metadata_and_rejects_links_and_large_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as directory:
            path = Path(directory) / 'artifact'
            path.write_bytes(b'fixture')
            before = path.stat()
            self.assertEqual(aa.diagnostic_read(path), b'fixture')
            after = path.stat()
            self.assertEqual((before.st_atime_ns, before.st_mtime_ns, before.st_ctime_ns),
                             (after.st_atime_ns, after.st_mtime_ns, after.st_ctime_ns))
            with self.assertRaises(ValueError):
                aa.diagnostic_read(path, limit=2)
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                aa.diagnostic_read(link)

    def test_cli_rejects_unpinned_diagnostic_before_execution(self):
        with patch.object(os, 'geteuid', return_value=0), patch.object(bootstrap, 'diagnostic_read', return_value=b'raise AssertionError()'), patch.object(bootstrap, 'login_operation', side_effect=AssertionError()) as login, patch('sys.stdout', io.StringIO()) as out:
            self.assertEqual(bootstrap.cli(['diagnose']), 1)
        login.assert_not_called()
        self.assertEqual(out.getvalue(), 'unknown\nunknown\napparmor-source-invalid\nsource-fix-required\nsubstage-other\nstate-unknown\nretry-source-fix-required\ndiagnostic-module=invalid\nexpected-binding=indeterminate\nprimary=DIAGNOSTIC_MODULE_FAILURE\nretry=blocked:diagnostic-module-invalid\n')


if __name__ == '__main__':
    unittest.main()
