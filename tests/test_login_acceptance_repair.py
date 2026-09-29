"""Synthetic capability and kernel proofs; no host policy or command execution."""
import errno
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from admin_bootstrap import login_apparmor as aa, login_browser_stage as fs, login_diagnose as d

ROOT = Path(__file__).resolve().parents[1]


class CapabilityProof(unittest.TestCase):
    def test_fd_query_absence_present_missing_error_and_unsupported(self):
        for query in (fs.caps, d.no_capabilities):
            with patch.object(os, 'getxattr', side_effect=OSError(errno.ENODATA, '')) as call:
                query(123)
                call.assert_called_once_with(123, 'security.capability')
            for value in (b'capability', b'', None):
                with patch.object(os, 'getxattr', return_value=value), self.assertRaisesRegex(ValueError, 'caps'):
                    query(123)
            for error in (AttributeError(), NotImplementedError(), OSError(errno.EACCES, ''),
                          OSError(errno.EIO, ''), OSError(errno.ENOTSUP, '')):
                with patch.object(os, 'getxattr', side_effect=error), self.assertRaisesRegex(ValueError, 'caps-unknown'):
                    query(123)

    def test_source_file_checked_without_following_links(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as name:
            root = Path(name)
            (root / 'candidate').write_bytes(b'source')
            with patch.object(os, 'getxattr', side_effect=OSError(errno.ENODATA, '')) as call:
                self.assertEqual(d.source_read(root, 'candidate'), b'source')
                self.assertIsInstance(call.call_args.args[0], int)
            for error, reason in ((None, 'caps'), (OSError(errno.EPERM, ''), 'caps-unknown')):
                with patch.object(os, 'getxattr', return_value=b'caps', side_effect=error):
                    with self.assertRaises(d.SourceError) as caught:
                        d.source_read(root, 'candidate')
                    self.assertEqual(caught.exception.reason, reason)
            (root / 'link').symlink_to(root / 'candidate')
            with self.assertRaises(OSError):
                d.source_read(root, 'link')


class KernelProof(unittest.TestCase):
    record = {**d.PINNED, 'sha256': '1' * 64, 'tree_sha256': '2' * 64}

    def test_canonical_digest_covers_exact_policy(self):
        name = aa.profile_name(self.record)
        self.assertTrue(name.endswith(hashlib.sha256(aa.canonical_policy(self.record)).hexdigest()))
        for replacement in ('flags=(complain)', '  deny userns,', 'abi <abi/3.0>'):
            raw = aa.canonical_policy(self.record).replace(
                b'flags=(unconfined)' if replacement.startswith('flags') else
                b'  userns,' if 'userns' in replacement else b'abi <abi/4.0>', replacement.encode())
            self.assertFalse(name.endswith(hashlib.sha256(raw).hexdigest()))
        self.assertNotEqual(name, aa.profile_name({**self.record, 'executable': '/different'}))
        self.assertEqual(aa.receipt_record(self.record)['profile_sha256'], hashlib.sha256(aa.profile(self.record)).hexdigest())

    def test_loaded_exact_name_mode_duplicate_and_attachment(self):
        name = aa.profile_name(self.record)
        good = (name, 'unconfined', self.record['executable'])
        for rows, okay in (([good], True),
                ([(name, 'unconfined', '/wrong')], False),
                ([(name, 'complain', good[2])], False),
                ([('ermis-epoptia-login-chromium-1243-wrong', *good[1:])], False),
                ([good, good], False), ([], False)):
            with self.subTest(rows=rows), patch.object(aa, 'kernel_entries', return_value=rows), \
                    patch.object(os, 'open', side_effect=AssertionError('host read forbidden')):
                if okay:
                    aa.loaded(self.record)
                else:
                    with self.assertRaises(ValueError):
                        aa.loaded(self.record)
        with patch.object(aa, 'kernel_entries', side_effect=OSError('unknown')):
            with self.assertRaisesRegex(ValueError, 'kernel-loaded-unproven'):
                aa.loaded(self.record)

    def test_parser_add_only_explicit_success_and_unknown(self):
        from types import SimpleNamespace
        for code in (0, 1, None):
            with patch.object(aa.subprocess, 'run', return_value=SimpleNamespace(returncode=code)) as command:
                if code == 0:
                    self.assertEqual(aa.parser('load', Path('/synthetic/candidate')), 0)
                else:
                    with self.assertRaises(ValueError):
                        aa.parser('load', Path('/synthetic/candidate'))
                self.assertIn('--add', command.call_args.args[0])
                self.assertNotIn('--replace', command.call_args.args[0])
