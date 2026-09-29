"""Hermetic read-only evidence tests. No host processes, units or kernel reads."""
import errno
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import patch
from admin_bootstrap import login_diagnose as d, login_apparmor as aa
from test_login_kernel_stream import kernel_fixture


class Evidence(unittest.TestCase):
    def test_kernel_truth_table(self):
        exact = (d.expected_profile(), 'unconfined', d.PINNED['executable'])
        self.assertEqual(exact[0], aa.profile_name(d.PINNED))
        for rows, expected in [([], 'absent'), ([exact], 'present-exact'),
                ([exact, exact], 'ambiguous'), ([('old', 'enforce', exact[2])], 'present-conflict'),
                ([('/home/ermis/.cache/ms-playwright/chromium-1243/chrome-linux/chrome', 'enforce', 'none')], 'present-conflict'),
                ([('other', 'enforce', '/**')], 'ambiguous'),
                ([('other', 'bad-mode', 'none')], 'unknown')]:
            raw = ''.join(name + ' (' + mode + ')\n' for name, mode, _ in rows).encode()
            with self.subTest(expected=expected):
                self.assertEqual(kernel_fixture(raw, rows)[0], expected)
        for error in (PermissionError(), ValueError(), OSError()):
            with patch.object(d, 'kernel_summary', side_effect=error):
                self.assertEqual(d.kernel_probe()[0], 'unknown')

    def test_kernel_fallback_never_proves_absence(self):
        for rc, expected in [(0, 'probe-error'), (1, 'disabled'), (2, 'probe-error'), (3, 'securityfs-unmounted'), (4, 'permission-denied'), (42, 'probe-error')]:
            with patch.object(d, 'kernel_summary', side_effect=FileNotFoundError()), patch.object(d.subprocess, 'run', return_value=SimpleNamespace(returncode=rc)) as run:
                self.assertEqual(d.kernel_probe()[0], expected)
                self.assertEqual(run.call_args.args[0], ['/usr/sbin/aa-status', '--enabled'])
                self.assertEqual(run.call_args.kwargs['timeout'], 5)
        with patch.object(d, 'kernel_summary', side_effect=FileNotFoundError()), patch.object(d.subprocess, 'run', side_effect=FileNotFoundError()):
            self.assertEqual(d.kernel_probe()[0], 'probe-error')

    def test_partial_securityfs(self):
        for raw in [b'other (enforce)', b'bad\n', b'\xff\n']:
            self.assertEqual(kernel_fixture(raw, [])[0], 'unknown')

    def unit(self, state='inactive', pid='0', load='loaded'):
        return dict(LoadState=load, ActiveState=state, SubState='listening' if state == 'active' else 'dead', MainPID=pid, ControlGroup='')

    def test_system_units_and_bad_output(self):
        raw = b'LoadState=not-found\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlGroup=\n'
        with patch.object(d.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=raw)) as run:
            self.assertEqual(d.unit_probe(d.SERVICE)['LoadState'], 'not-found')
            self.assertIn('--system', run.call_args.args[0])
            self.assertNotIn('--user', run.call_args.args[0])
        for raw in [b'', b'LoadState=loaded\nLoadState=loaded', b'private']:
            with patch.object(d.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=raw)):
                with self.assertRaises(ValueError):
                    d.unit_probe(d.SERVICE)

    def test_socket_idle_missing_units_query_error_and_session(self):
        inactive = dict(login=('inactive', 'complete-scan'), browser=('inactive', 'complete-scan'))
        for units, expected in [([self.unit(), self.unit('active')], 'inactive'),
                ([self.unit(load='not-found'), self.unit(load='not-found')], 'inactive'),
                ([self.unit('active', '123'), self.unit()], 'unknown'),
                ([PermissionError(), self.unit()], 'unknown')]:
            with patch.object(d, 'unit_probe', side_effect=units), patch.object(d, 'process_probe', return_value=inactive) as processes:
                self.assertEqual(d.activity_probe()['login-activity'], expected)
                processes.assert_called_once()

    def test_proc_exact_matching_partial_and_cgroup(self):
        class Entries:
            def __enter__(self):
                return iter([SimpleNamespace(name='123456')])
            def __exit__(self, *args):
                pass
        for exe, group, browser, login in [('/usr/bin/grep', b'0::/other\n', 'inactive', 'inactive'),
                (d.PINNED['executable'], b'0::/other\n', 'active', 'inactive'),
                ('/usr/bin/node', ('0::/system.slice/' + d.SERVICE + '\n').encode(), 'inactive', 'active'),
                ('/usr/bin/node', b'partial', 'unknown', 'unknown')]:
            with patch.object(d.os, 'scandir', return_value=Entries()), patch.object(d, 'proc_bytes', return_value=group), patch.object(d.os, 'readlink', return_value=exe):
                result = d.process_probe()
                self.assertEqual(result['browser'][0], browser)
                self.assertEqual(result['login'][0], login)
        with patch.object(d.os, 'scandir', return_value=Entries()), patch.object(d, 'proc_bytes', side_effect=PermissionError()):
            self.assertEqual(d.process_probe()['browser'], ('unknown', 'permission-denied'))

    def receipt(self, values):
        def read(path, **kwargs):
            value = values[(d.POLICY, d.RECEIPT, d.READY).index(path)]
            if isinstance(value, Exception):
                raise value
            if value is None:
                raise FileNotFoundError()
            return json.dumps(value).encode()
        with patch.object(d, 'read', side_effect=read):
            return d.receipt_probe('a' * 64)

    def test_receipt_truth_table(self):
        record = dict(d.PINNED, sha256='a' * 64, tree_sha256='b' * 64)
        committed = aa.receipt_record(record)
        legacy_data = ('abi <abi/4.0>,\n# ermis-epoptia-login owned v1\n# playwright=1.63.0 chromium=153.0.8010.12 revision=1243 sha256=' + 'a' * 64 + '\nprofile "/home/ermis/.cache/ms-playwright/chromium-1243/chrome-linux/chrome" flags=(unconfined) {\n  userns,\n}\n').encode()
        legacy = dict(aa.legacy_profile(legacy_data), profile_sha256=hashlib.sha256(legacy_data).hexdigest())
        login = {'source': 'a' * 64, 'files': {str(p): 'b' * 64 for p in d.INSTALLED_FILES}}
        for values, expected in [([None]*3, 'missing'), ([legacy, None, None], 'stale-recognized-source-binding'),
                ([committed, None, None], 'valid-committed-success'), ([{}, None, None], 'invalid-schema'),
                ([dict(legacy, profile_sha256='0'*64), None, None], 'invalid-schema'),
                ([None, login, None], 'valid-precommit-failure-rollback'),
                ([None, {}, None], 'ambiguous'), ([legacy, login, None], 'ambiguous'),
                ([PermissionError(), None, None], 'unreadable'),
                ([d.InvariantError('owner'), None, None], 'unsafe-type-owner-link'),
                ([ValueError(), None, None], 'ambiguous')]:
            with self.subTest(expected=expected):
                self.assertEqual(self.receipt(values)[0], expected)

    def test_committed_binding_and_invalid_schema(self):
        record = dict(d.PINNED, sha256='a' * 64, tree_sha256='b' * 64)
        policy = aa.receipt_record(record)
        for source, reason in [('a' * 64, 'source-current'), ('c' * 64, 'source-stale-success')]:
            login = {'source': source, 'files': {str(p): 'b' * 64 for p in d.INSTALLED_FILES}}
            ready = {'source': source, 'receipts': {
                str(d.POLICY): hashlib.sha256(json.dumps(policy).encode()).hexdigest(),
                str(d.RECEIPT): hashlib.sha256(json.dumps(login).encode()).hexdigest()}}
            self.assertEqual(self.receipt([policy, login, ready]), ('valid-committed-success', reason))
        for value in [dict(policy, profile_sha256='0' * 64), dict(policy, schema=2.0),
                      dict(policy, kernel_sha1='bad')]:
            self.assertEqual(self.receipt([value, None, None])[0], 'invalid-schema')
        self.assertEqual(self.receipt([dict(policy, kernel_sha1='c' * 40), None, None])[0], 'valid-committed-success')

    def test_proc_read_flags_and_bounds(self):
        with patch.object(d.os, 'open', return_value=123) as opened, patch.object(d.os, 'read', return_value=b'x' * 65537), patch.object(d.os, 'close') as close:
            with self.assertRaises(ValueError):
                d.proc_bytes(Path('/fixture/cgroup'))
            flags = opened.call_args.args[1]
            self.assertTrue(flags & os.O_NOFOLLOW)
            self.assertTrue(flags & os.O_NOATIME)
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
            close.assert_called_once_with(123)

    def test_independent_probes_even_source_failure_zero_effects(self):
        with patch.object(d, 'source', side_effect=d.SourceError('invalid', 'digest-mismatch')), \
             patch.object(d, 'activity_probe', return_value={'login-activity': 'inactive', 'browser-activity': 'inactive'}) as activity, \
             patch.object(d, 'kernel_probe', return_value=('absent', 'complete-listing')) as kernel, \
             patch.object(d, 'receipt_probe', return_value=('stale-recognized-source-binding', 'legacy-non-success')) as receipt, \
             patch.object(d.os, 'open', side_effect=AssertionError('unexpected open')), \
             patch.object(d.subprocess, 'run', side_effect=AssertionError('unexpected command')):
            result = d.diagnose()
            activity.assert_called_once()
            kernel.assert_called_once()
            receipt.assert_called_once()
            self.assertIn('primary=TREE_SOURCE_FAILURE', result)
            self.assertIn('baseline-blocker=source-manifest-unproven', result)
            self.assertIn('retry=blocked', result)


if __name__ == '__main__':
    unittest.main()
