"""Source-only filesystem tests; fixtures stay inside this project."""
import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from admin_bootstrap import login_diagnose as d

ROOT = Path(__file__).resolve().parents[1]


class SourceManifest(unittest.TestCase):
    def test_allowlist_and_generator_are_canonical(self):
        self.assertEqual(tuple(sorted(set(d.SOURCE_PATHS))), d.SOURCE_PATHS)
        self.assertNotIn('admin_bootstrap/login_manifest.json', d.SOURCE_PATHS)
        self.assertEqual(d.generate_manifest(ROOT), d.generate_manifest(ROOT))
        self.assertEqual(d.generate_manifest(ROOT), (ROOT / 'admin_bootstrap/login_manifest.json').read_bytes())

    def test_source_reader_type_mode_and_no_follow(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as folder:
            root = Path(folder)
            leaf = root / 'fixture'
            leaf.write_bytes(b'fixture')
            # Executable owner mode is valid source, including root-like reads.
            leaf.chmod(0o755)
            with patch.object(d.os, 'geteuid', return_value=0):
                self.assertEqual(d.source_read(root, 'fixture'), b'fixture')
            leaf.chmod(0o666)
            with self.assertRaises(d.SourceError):
                d.source_read(root, 'fixture')
            leaf.chmod(0o600)
            (root / 'link').symlink_to(leaf)
            with self.assertRaises(OSError):
                d.source_read(root, 'link')
            (root / 'parent').symlink_to(root, target_is_directory=True)
            with self.assertRaises(OSError):
                d.source_read(root, 'parent/fixture')
            with self.assertRaises(d.SourceError):
                d.source_read(root, '.')

    def test_errno_is_preserved_without_message(self):
        for number in (errno.EACCES, errno.EPERM, errno.EIO, errno.ENOENT, errno.ELOOP):
            with self.subTest(number=number), patch.object(d, 'source_read', side_effect=OSError(number, 'private')):
                with self.assertRaises(d.SourceError) as caught:
                    d.verify_source(ROOT)
                self.assertEqual(caught.exception.errno, number)
                self.assertNotIn('private', str(caught.exception))
                self.assertEqual(caught.exception.status, 'invalid' if number in (errno.ENOENT, errno.ELOOP) else 'unreadable')

    def test_source_ok_without_root_or_installed_state(self):
        with patch.object(d.os, 'geteuid', return_value=1000), patch.object(d, 'optional', side_effect=AssertionError), patch.object(d, 'evidence', side_effect=AssertionError):
            result = d.diagnose()
        self.assertIn('source-manifest=ok', result)
        self.assertIn('source-manifest-reason=verified', result)
