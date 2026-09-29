"""Local metadata fixtures for the diagnostic-only observer."""
import errno
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from admin_bootstrap import login_diagnose as d, login_browser_stage as tree

ROOT = Path(__file__).resolve().parents[1]


class Boundaries(unittest.TestCase):
    def test_exact_errno_remains_private_and_distinct(self):
        for number, expected in ((errno.ENOENT, 'missing'), (errno.EACCES, 'access-denied'),
                                 (errno.EPERM, 'access-denied'), (errno.EIO, 'io-error'),
                                 (errno.ELOOP, 'symlink')):
            error = OSError(number, 'private')
            errors = {}
            with patch.object(d, 'read', side_effect=error):
                state, data = d.artifact(ROOT / 'unused', errors, 'candidate-artifact')
            self.assertEqual(state, expected)
            self.assertIsNone(data)
            self.assertIs(errors['candidate-artifact'], error)
            self.assertEqual(errors['candidate-artifact'].errno, number)

    def test_real_denied_and_missing_reads(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as folder:
            path = Path(folder) / 'fixture'
            path.write_bytes(b'fixture')
            path.chmod(0)
            try:
                if os.geteuid() != 0:
                    with patch.object(d, 'opened', side_effect=lambda p, owner=0: os.open(p, d.FLAGS)):
                        errors = {}
                        self.assertEqual(d.artifact(path, errors, 'candidate')[0], 'access-denied')
                        self.assertEqual(errors['candidate'].errno, errno.EACCES)
            finally:
                path.chmod(0o600)
            errors = {}
            with patch.object(d, 'opened', side_effect=lambda p, owner=0: os.open(p, d.FLAGS)):
                self.assertEqual(d.artifact(Path(folder) / 'absent', errors, 'candidate')[0], 'missing')
            self.assertEqual(errors['candidate'].errno, errno.ENOENT)

    def test_real_tree_invariants_and_same_record_as_install(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as folder:
            root = Path(folder)
            (root / 'chrome-linux64').mkdir()
            binary = root / 'chrome-linux64/chrome'
            binary.write_bytes(b'\x7fELFfixture')
            binary.chmod(0o755)
            uid = os.getuid()
            root.chmod(0o755)
            original_lstat = Path.lstat
            ancestor = SimpleNamespace(st_mode=0o40755, st_uid=0, st_gid=0)
            def lstat(path):
                return original_lstat(path) if path == root or root in path.parents else ancestor
            self.addCleanup(patch.stopall)
            patch.object(Path, 'lstat', lstat).start()
            patch.object(tree, 'parents').start()
            patch.object(tree, 'open_path', side_effect=lambda path, uid, flags, ancestry=None: os.open(path, flags)).start()
            # Source fixtures use the current unprivileged owner; no host tree.
            self.assertEqual(d.checked_tree(root, uid, vars(tree)), tree.tree(root, uid))
            for mode, invariant in ((0o777, 'mode'), (0o4755, 'suid-sgid')):
                binary.chmod(mode)
                with self.assertRaises(tree.PathInvariant) as caught:
                    d.checked_tree(root, uid, vars(tree))
                self.assertEqual(caught.exception.invariant, invariant)
            binary.chmod(0o755)
            binary.write_bytes(b'not ELF')
            with self.assertRaises(ValueError) as caught:
                d.checked_tree(root, uid, vars(tree))
            self.assertEqual(d.error_bucket(caught.exception), 'elf')
            binary.write_bytes(b'\x7fELFfixture')
            link = root / 'link'
            link.symlink_to(binary)
            with self.assertRaises(tree.PathInvariant) as caught:
                d.checked_tree(root, uid, vars(tree))
            self.assertEqual(caught.exception.invariant, 'unsafe-link')
            link.unlink()
            fifo = root / 'fifo'
            os.mkfifo(fifo)
            with self.assertRaises(tree.PathInvariant) as caught:
                d.checked_tree(root, uid, vars(tree))
            self.assertEqual(caught.exception.invariant, 'special')

    def test_unsafe_artifact_type_and_mode_remain_distinct(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as folder:
            root = Path(folder)
            file = root / 'fixture'
            file.write_bytes(b'fixture')
            file.chmod(0o666)
            with patch.object(d, 'opened', side_effect=lambda p, owner=0: os.open(p, d.FLAGS)):
                with self.assertRaises(d.InvariantError) as caught:
                    d.read(file, owner=os.getuid())
                self.assertEqual(caught.exception.invariant, 'mode')
            os.mkfifo(root / 'fifo')
            (root / 'link').symlink_to(file)
            for name, expected in (('fifo', 'nonregular'), ('link', 'symlink')):
                errors = {}
                with patch.object(d, 'opened', side_effect=lambda p, owner=0: os.open(p, d.FLAGS)):
                    self.assertEqual(d.artifact(root / name, errors, 'candidate')[0], expected)


if __name__ == '__main__':
    unittest.main()
