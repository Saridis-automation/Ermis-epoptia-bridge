"""Hermetic privileged-path tests; no host parser, kernel policy or services."""
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

from admin_bootstrap import login_diagnose as d, login_apparmor as aa, bootstrap

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = 'private fixture information must never escape'


class Primitives(unittest.TestCase):
    def test_parser_commands_are_nonloading_and_noncaching(self):
        for returns, stderr, expected in (([0, 0], b'', None),
                ([1], b'unknown option', 'apparmor-parser-options-unknown'),
                ([1], b'parser environment failed', 'apparmor-parser-options-unknown'),
                ([0, 1], b'syntax error', 'apparmor-profile-syntax-invalid'),
                ([0, 1], b'environment failed', 'apparmor-profile-syntax-invalid'),
                ([0, -9], b'', 'apparmor-source-parse-unknown')):
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                self.assertNotIn('preexec_fn', kwargs)
                self.assertNotIn('timeout', kwargs)
                self.assertEqual(command, d.PARSER_COMMAND)
                self.assertNotIn('-', command)
                self.assertEqual(kwargs['input'], b'profile ermis-diagnose-probe { }\n' if len(calls) == 1 else b'fixture')
                return SimpleNamespace(returncode=returns[len(calls)-1], stdout=b'', stderr=stderr)
            with patch.object(d, 'opened', return_value=123), patch.object(d, 'no_capabilities'), patch.object(os, 'close'), patch.object(os, 'fstat', return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0)), patch.object(subprocess, 'run', side_effect=run):
                if expected:
                    with self.assertRaises(d.Failure) as failure:
                        d.parser(b'fixture')
                    self.assertEqual(failure.exception.result[0], expected)
                else:
                    d.parser(b'fixture')

    def test_help_version_and_stderr_are_not_option_contracts(self):
        for text in (b'', b'--unsupported', b'version 4', b'warning'):
            with patch.object(d, 'opened', return_value=123), patch.object(d, 'no_capabilities'), patch.object(os, 'close'), patch.object(os, 'fstat', return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0)), patch.object(subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=text, stderr=text)) as run:
                d.parser(b'candidate')
                self.assertEqual(run.call_count, 2)
                self.assertNotIn('--help', run.call_args.args[0])
                self.assertNotIn('--version', run.call_args.args[0])

    def test_parser_missing_timeout_and_exec_failure(self):
        with patch.object(d, 'opened', side_effect=FileNotFoundError(PRIVATE)):
            with self.assertRaises(d.Failure) as failure:
                d.parser(b'fixture')
            self.assertEqual(failure.exception.result[0], 'apparmor-parser-missing')
        for error in (subprocess.TimeoutExpired(PRIVATE, 20), subprocess.SubprocessError(PRIVATE)):
            with patch.object(d, 'opened', return_value=123), patch.object(d, 'no_capabilities'), patch.object(os, 'close'), patch.object(os, 'fstat', return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0)), patch.object(subprocess, 'run', side_effect=error):
                with self.assertRaises(d.Failure) as failure:
                    d.parser(b'fixture')
                self.assertEqual(failure.exception.result, ('apparmor-parser-options-unknown', 'apparmor-parser-options', 'unknown', 'retry-forbidden'))

    def test_unobservable_and_stale_kernel_attachment(self):
        record = {'revision': '1243', 'executable': str(d.TARGET / 'chrome-linux64/chrome')}
        name = aa.profile_name(record).encode()
        with patch.object(d, 'listing', return_value=['fixture']), patch.object(d, 'read', side_effect=[name + b' (unconfined)', name, b'wrong-attachment']):
            with self.assertRaises(d.Failure) as failure:
                d.loaded(record, vars(aa))
            self.assertEqual(failure.exception.result[1], 'apparmor-postload-verify')
        with patch.object(d, 'read', side_effect=PermissionError(PRIVATE)):
            with self.assertRaises(PermissionError):
                d.loaded(record, vars(aa))

    def test_parser_sandbox_denies_real_child_writes(self):
        # Runs unprivileged, inside a workspace fixture. No parser/kernel access.
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as name:
            target = Path(name) / 'fixture'
            target.write_bytes(b'original')
            script = """
import os, sys
from pathlib import Path
from admin_bootstrap.login_diagnose import parser_readonly
try:
    parser_readonly()
except Exception:
    sys.exit(77)
p = Path(sys.argv[1])
operations = [lambda: p.write_bytes(b'changed'), lambda: p.unlink(),
              lambda: p.rename(p.with_name('moved')),
              lambda: p.with_name('new').write_bytes(b'created'),
              lambda: p.chmod(0o777), lambda: os.utime(p, None)]
for operation in operations:
    try:
        operation()
    except PermissionError:
        continue
    sys.exit(1)
sys.exit(0)
"""
            result = subprocess.run(['/usr/bin/python3', '-B', '-c', script, str(target)],
                                    cwd=ROOT, env={'PATH': '/usr/bin:/bin', 'LANG': 'C'},
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            self.assertIn(result.returncode, (0, 77))
            self.assertEqual(target.read_bytes(), b'original')
            self.assertEqual(sorted(p.name for p in Path(name).iterdir()), ['fixture'])
            if result.returncode == 77:
                self.skipTest('Kernel sandbox unavailable; parser must fail closed')

    def test_sandbox_survives_exec(self):
        result = subprocess.run(['/usr/bin/python3', '-I', '-B', '-c', 'raise SystemExit(0)'],
                                preexec_fn=d.parser_readonly, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                cwd=ROOT, env={'PATH': '/usr/bin:/bin', 'LANG': 'C'}, timeout=10)
        self.assertEqual(result.returncode, 0)

    def test_duplicate_json_rejected(self):
        with self.assertRaises(ValueError):
            d.decode(b'{"source": "old", "source": "new"}')

    def test_kernel_conflicts(self):
        record = {'revision': '1243', 'executable': str(d.TARGET / 'chrome-linux64/chrome')}
        name = aa.profile_name(record)
        for lines in ((name + ' (enforce)\n'), (name + ' (unconfined)\n') * 2,
                      '/home/ermis/.cache/ms-playwright/chromium-1234/chrome (unconfined)\n'):
            with patch.object(d, 'read', return_value=lines.encode()):
                with self.assertRaises(d.Failure):
                    d.loaded(record, vars(aa))
        with patch.object(d, 'read', return_value=b''):
            self.assertFalse(d.loaded(record, vars(aa)))

    def test_residue_names_never_opened(self):
        for prefix in ('.stage-malicious', '.ermis-login-malicious'):
            with patch.object(d, 'listing', return_value=[prefix]), patch.object(d, 'read', side_effect=AssertionError()) as reader:
                self.assertTrue(d.residue())
                reader.assert_not_called()

    def test_noatime_and_symlink_special_rejection(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as name:
            root = Path(name)
            file = root / 'fixture'
            file.write_bytes(b'fixture')
            before = file.stat()
            with patch.object(d, 'opened', side_effect=lambda path, owner=0: os.open(path, d.FLAGS)):
                self.assertEqual(d.read(file, os.getuid()), b'fixture')
            self.assertEqual(before.st_atime_ns, file.stat().st_atime_ns)
            link = root / 'link'
            link.symlink_to(file)
            fifo = root / 'fifo'
            os.mkfifo(fifo)
            for path in (link, fifo):
                with self.assertRaises((OSError, ValueError)):
                    d.read(path, os.getuid())
            directory_link = root / 'directory-link'
            directory_link.symlink_to(root, target_is_directory=True)
            with self.assertRaises((OSError, ValueError)):
                d.read(directory_link / 'fixture', os.getuid())

    def test_source_manifest_and_fixed_entries(self):
        sources, fingerprint = d.source()
        self.assertEqual(tuple(sources), d.SOURCE_PATHS)
        self.assertEqual(len(d.source_modules(sources)), 2)
        self.assertRegex(fingerprint, '[0-9a-f]{64}')
        real = d.source_read
        def forged(root, relative):
            data = real(root, relative)
            if relative == 'admin_bootstrap/login_manifest.json':
                manifest = json.loads(data)
                manifest['../../forbidden'] = 'a' * 64
                return json.dumps(manifest).encode()
            return data
        with patch.object(d, 'source_read', side_effect=forged):
            with self.assertRaises(d.SourceError):
                d.source()

    def test_scoped_cli_keeps_bounded_compatibility_prefix(self):
        module = b"def diagnose(scoped=False):\n return ('apparmor-indeterminate', 'other', 'INDETERMINATE', 'retry=blocked:insufficient-transaction-evidence') + (('phase-load-INDETERMINATE',) if scoped else ())\n"
        with patch.object(os, 'geteuid', return_value=0), patch.object(bootstrap, 'diagnostic_read', return_value=module), patch.object(bootstrap, 'DIAGNOSE_SOURCE_SHA256', hashlib.sha256(module).hexdigest()), patch.object(bootstrap, 'login_operation', side_effect=AssertionError()) as install, patch('sys.stdout', io.StringIO()) as out:
            self.assertEqual(bootstrap.cli(['diagnose', 'apparmor']), 1)
        install.assert_not_called()
        self.assertEqual(out.getvalue().splitlines(), ['unknown', 'unknown', 'apparmor-indeterminate', 'manual-review-required', 'substage-other', 'state-INDETERMINATE', d.INSUFFICIENT, 'phase-load-INDETERMINATE'])

    def test_cli_does_not_enter_legacy_inspection_or_install(self):
        module = b"def diagnose():\n return ('apparmor-ready', 'other', 'clean-new-version', 'retry-forbidden')\n"
        with patch.object(os, 'geteuid', return_value=0), patch.object(bootstrap, 'diagnostic_read', return_value=module), patch.object(bootstrap, 'DIAGNOSE_SOURCE_SHA256', hashlib.sha256(module).hexdigest()), patch.object(bootstrap, 'diagnose', side_effect=AssertionError()) as legacy, patch.object(bootstrap, 'login_operation', side_effect=AssertionError()) as install, patch('sys.stdout', io.StringIO()) as out:
            self.assertEqual(bootstrap.cli(['diagnose']), 0)
            legacy.assert_not_called()
            install.assert_not_called()
            self.assertEqual(len(out.getvalue().splitlines()), 7)


class TransactionEvidence(unittest.TestCase):
    def setUp(self):
        self.snapshot = {'disk': {str(p): None for p in d.DISK_ROOTS}, 'kernel': []}
        self.snapshot['disk'][str(d.PROFILE)] = [[1, 2, stat.S_IFREG | 0o644, 0, 0, 3, 4, 5, 1, 6], 'a' * 64]
        self.snapshot['kernel'] = [['fixture', 'unconfined', '/fixture', 'b' * 40]]
        self.ledger = dict(schema=1, source='c' * 64, status='idle',
                           phases={p: 'ok' for p in d.PHASES}, prestate=self.snapshot, committed=None)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.reader = self.stack.enter_context(patch.object(d, 'read', side_effect=lambda *a, **k: json.dumps(self.ledger).encode()))
        self.stack.enter_context(patch.object(d, 'source', return_value=(None, 'c' * 64)))
        self.current = self.stack.enter_context(patch.object(d, 'baseline', return_value=self.snapshot))

    def test_full_baseline_equality_and_all_phase_classifications(self):
        state, phases = d.evidence()
        self.assertEqual(state, 'exact-rollback-baseline')
        self.assertEqual(phases, tuple('phase-' + p + '-ok' for p in d.PHASES))
        self.ledger['phases']['original-trigger'] = 'failed'
        self.ledger['phases']['load'] = 'failed'
        self.ledger['phases']['post-load-verify'] = 'not-run'
        self.assertEqual(d.evidence()[0], 'exact-rollback-baseline')
        self.assertIn('phase-original-trigger-failed', d.evidence()[1])

    def test_rc_success_without_equality_is_not_rollback_success(self):
        import copy
        variants = []
        for index in range(10):
            changed = copy.deepcopy(self.snapshot)
            changed['disk'][str(d.PROFILE)][0][index] += 1
            variants.append(changed)
        changed = copy.deepcopy(self.snapshot)
        changed['disk'][str(d.PROFILE)][1] = 'd' * 64
        variants.append(changed)
        for index in range(4):
            changed = copy.deepcopy(self.snapshot)
            changed['kernel'][0][index] = ('other', 'complain', '/other', 'd' * 40)[index]
            variants.append(changed)
        for root in (d.PROFILE.parent / 'disable/fixture', d.PROFILE.parent / 'force-complain/fixture', Path('/var/cache/apparmor/fixture')):
            changed = copy.deepcopy(self.snapshot)
            changed['disk'][str(root)] = [[1] * 10, 'e' * 64]
            variants.append(changed)
        for changed in variants:
            with self.subTest(changed=variants.index(changed)):
                self.current.return_value = changed
                self.assertEqual(d.evidence()[0], 'baseline-mismatch')

    def test_missing_incomplete_foreign_or_malicious_ledger(self):
        for error in (FileNotFoundError(), PermissionError(PRIVATE), ValueError(PRIVATE), OSError(PRIVATE)):
            self.reader.side_effect = error
            self.assertEqual(d.evidence()[0], 'INDETERMINATE')
        self.reader.side_effect = lambda *a, **k: json.dumps(self.ledger).encode()
        for key in tuple(self.ledger):
            value = self.ledger.pop(key)
            self.assertEqual(d.evidence()[0], 'INDETERMINATE')
            self.ledger[key] = value
        self.ledger['prestate'] = {'disk': {}, 'kernel': []}
        self.assertEqual(d.evidence()[0], 'INDETERMINATE')
        self.ledger['prestate'] = self.snapshot
        self.ledger['phases']['load'] = PRIVATE
        state, phases = d.evidence()
        self.assertEqual(state, 'INDETERMINATE')
        self.assertNotIn(PRIVATE, str(phases))

    def test_committed_active_raced_and_unobservable(self):
        self.ledger['prestate'] = {**self.snapshot, 'kernel': []}
        self.ledger['committed'] = self.snapshot
        self.assertEqual(d.evidence()[0], 'exact-committed-candidate')
        self.ledger['status'] = 'active'
        self.assertEqual(d.evidence()[0], 'transaction-active')
        self.current.side_effect = [self.snapshot, {**self.snapshot, 'kernel': []}]
        self.assertEqual(d.evidence()[0], 'INDETERMINATE')
        self.current.side_effect = ValueError(PRIVATE)
        self.assertEqual(d.evidence()[0], 'INDETERMINATE')

    def test_no_unconditional_retry_even_with_equal_baseline(self):
        details = dict(d.MODEL_FIELDS, baseline='clean-absent', transaction='idle')
        details.update({k: 'absent' for k in ('installed-tree', 'profile-disk', 'profile-kernel', 'receipt')})
        details.update({'source-manifest': 'ok', 'browser-source-tree': 'ok',
                        'candidate-validation': 'ok', 'source-candidate-validation': 'ok',
                        'candidate-artifact': 'not-applicable', 'staged-tree': 'not-applicable',
                        'login-activity': 'inactive', 'browser-activity': 'inactive',
                        'install-atomic-recheck': 'verified', 'kernel-probe': 'absent',
                        'receipt-classification': 'missing'})
        with patch.object(d, 'model_observation', return_value=(details, {}, (), None, 'clear')):
            result = d.diagnose()
        self.assertEqual(result[3], 'retry=blocked')
        details['noreplace-support'] = 'symbol-present'
        with patch.object(d, 'model_observation', return_value=(details, {}, (), None, 'clear')):
            self.assertEqual(d.diagnose()[3], 'retry=eligible_after_atomic_recheck')


class ConflictScan(unittest.TestCase):
    def setUp(self):
        self.record = {'revision': '1243', 'executable': str(d.TARGET / 'chrome-linux64/chrome')}
        self.name = aa.profile_name(self.record)
        self.row = (self.name, 'unconfined', self.record['executable'])

    def scan(self, rows, disk):
        with patch.object(d, 'kernel_inventory', return_value=rows), patch.object(d, 'listing', return_value=list(disk)), patch.object(d, 'read', side_effect=lambda p: disk[p.name]):
            return d.conflicts(self.record, vars(aa))

    def test_exact_kernel_duplicates_and_foreign_attachment(self):
        self.assertEqual(self.scan([self.row, self.row], {}), 'definite-conflict')
        self.assertEqual(self.scan([self.row, ('foreign', 'enforce', self.row[2])], {}), 'definite-conflict')
        self.assertEqual(self.scan([self.row, (self.name, 'enforce', '/foreign')], {}), 'definite-conflict')

    def test_disk_duplicates_quoted_unquoted_and_implicit(self):
        for definition in (b'profile "foreign" "/foreign" {\n}', b'profile foreign /foreign {\n}', b'/foreign {\n}'):
            self.assertEqual(self.scan([], {'one': definition, 'two': definition}), 'definite-conflict')
        definition = ('profile "foreign" "' + self.row[2] + '" {\n}').encode()
        self.assertEqual(self.scan([], {'foreign': definition}), 'definite-conflict')

    def test_canonical_headers_and_compact_duplicate_names(self):
        for name in (self.name, '"' + self.name + '"'):
            for suffix in ('{}', ' flags=(unconfined){}', ' "' + self.row[2] + '"{}'):
                definition = ('profile ' + name + suffix).encode()
                self.assertEqual(self.scan([], {d.PROFILE.name: definition}), 'clear')
                self.assertEqual(self.scan([], {d.PROFILE.name: definition * 2}), 'definite-conflict')
        # Name-only flags must never become a shared executable attachment.
        self.assertEqual(self.scan([], {d.PROFILE.name: b'profile a flags=(enforce){}\nprofile b flags=(enforce){}'}), 'clear')
        self.assertEqual(self.scan([], {d.PROFILE.name: b'profile a{}\nprofile "broken'}), 'possible-conflict')
        self.assertEqual(self.scan([], {d.PROFILE.name: b'profile a{'}), 'possible-conflict')
        commented = ('# profile ' + self.name + '{}\nprofile foreign{}').encode()
        self.assertEqual(self.scan([], {'commented': commented}), 'clear')

    def test_scan_rejects_symlink_and_special_entries_without_following(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as name:
            root = Path(name)
            target = root / 'target'
            target.write_bytes(b'profile foreign{}')
            (root / 'link').symlink_to(target)
            os.mkfifo(root / 'fifo')
            for entry in ('link', 'fifo'):
                with patch.object(d, 'PROFILE', root / 'expected'), patch.object(d, 'kernel_inventory', return_value=[]), patch.object(d, 'listing', return_value=[entry]), patch.object(d, 'opened', side_effect=lambda p, owner=0: os.open(p, d.FLAGS)), patch.object(os, 'read', side_effect=AssertionError(PRIVATE)) as raw_read:
                    with self.assertRaises((OSError, ValueError)):
                        d.conflicts(self.record, vars(aa))
                    raw_read.assert_not_called()

    def test_possible_overlap_is_blocking(self):
        for attach in ('/**', '/opt/{ermis,other}/**', '@{BROWSER}', '/opt/ermis/epoptia-browser/chromium-????/**'):
            self.assertEqual(self.scan([('foreign', 'enforce', attach)], {}), 'possible-conflict')
            self.assertEqual(self.scan([], {'foreign': ('profile foreign "' + attach + '" {\n}').encode()}), 'possible-conflict')
        self.assertEqual(self.scan([], {'foreign': b'unknown syntax'}), 'possible-conflict')
        self.assertEqual(self.scan([self.row], {}), 'clear')

    def test_kernel_scan_not_limited_to_managed_names(self):
        directory = d.PROFILES.parent / 'policy/profiles'
        files = {d.PROFILES: b'foreign (enforce)\n', directory / 'fixture/name': b'foreign',
                 directory / 'fixture/mode': b'enforce', directory / 'fixture/attach': self.row[2].encode()}
        with patch.object(d, 'listing', return_value=['fixture']), patch.object(d, 'read', side_effect=lambda p, **kw: files[p]):
            self.assertEqual(d.kernel_inventory(False), [('foreign', 'enforce', self.row[2])])

    def test_foreign_special_and_symlink_baselines_fail_closed(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as name:
            root = Path(name)
            file = root / 'fixture'
            file.write_bytes(b'fixture')
            link = root / 'link'
            link.symlink_to(file)
            fifo = root / 'fifo'
            os.mkfifo(fifo)
            for path in (link, fifo):
                with patch.object(d, 'DISK_ROOTS', (path,)), patch.object(d, 'opened', side_effect=lambda p: os.open(p, d.FLAGS)):
                    with self.assertRaises((OSError, ValueError)):
                        d.baseline()
            with patch.object(d, 'opened', return_value=123), patch.object(d, 'no_capabilities'), patch.object(os, 'close'), patch.object(os, 'fstat', return_value=SimpleNamespace(st_uid=123, st_mode=stat.S_IFREG | 0o644)):
                with self.assertRaises(ValueError):
                    d.baseline()


if __name__ == '__main__':
    unittest.main()
