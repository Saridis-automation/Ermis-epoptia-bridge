"""Synthetic project-local trees only; no browser, policy, /opt or service calls."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from admin_bootstrap import login_browser_stage as stage

ROOT = Path(__file__).resolve().parents[1]


class Staging(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=ROOT / 'tests')))
        self.source = self.root / 'source'
        (self.source / 'chrome-linux64/resources').mkdir(parents=True)
        self.chrome = self.source / stage.LAYOUT
        self.chrome.write_bytes(b'\x7fELFfixture')
        self.chrome.chmod(0o755)
        (self.source / 'chrome-linux64/chrome_sandbox').write_bytes(b'\x7fELFhelper')
        (self.source / 'chrome-linux64/chrome_sandbox').chmod(0o755)
        (self.source / 'chrome-linux64/resources/data.pak').write_bytes(b'resources')
        self.uid = os.getuid()
        # The checkout's deployment permissions are unrelated to these synthetic
        # trees. Scope ancestor checks/opening to the fixture's filesystem root.
        def parents(path, uid=0):
            for parent in path.parents:
                if parent == self.root.parent:
                    break
                info = parent.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid) or info.st_mode & 0o022 or (uid == 0 and info.st_gid != 0):
                    raise ValueError()
        def open_path(path, uid=0, flags=stage.FLAGS, ancestry=None):
            parents(path, uid)
            return os.open(path, flags)
        self.stack.enter_context(patch.object(stage, 'parents', parents))
        self.stack.enter_context(patch.object(stage, 'open_path', open_path))

    def test_complete_tree_deterministic_copy_and_no_setuid(self):
        expected = stage.tree(self.source, self.uid)
        destination = self.root / 'copy'
        destination.mkdir()
        with patch.object(os, 'chown') as ownership:
            self.assertEqual(stage.tree(self.source, self.uid, destination), expected)
        self.assertTrue(ownership.called)
        for call in ownership.call_args_list:
            self.assertEqual(call.args[1:], (0, 0))
        self.assertEqual(stage.tree(destination, self.uid), expected)
        self.assertEqual((destination / 'chrome-linux64/chrome_sandbox').stat().st_mode & 0o7777, 0o755)
        self.assertEqual((destination / 'chrome-linux64/resources/data.pak').stat().st_mode & 0o7777, 0o644)
        (destination / 'chrome-linux64/resources/data.pak').write_bytes(b'drift')
        self.assertNotEqual(stage.tree(destination, self.uid)['tree_sha256'], expected['tree_sha256'])

    def test_python_node_tree_identity_and_profile_agree(self):
        from admin_bootstrap import login_apparmor as aa
        expected = stage.tree(self.source, self.uid)
        script = r"""
const fs = require('node:fs'), assert = require('node:assert/strict');
const aa = require('./tests/login_test_loader.cjs').load('epoptia_login_apparmor.cjs', {},
  {EPOPTIA_LOGIN_CHROMIUM_EXECUTABLE: '/opt/ermis/epoptia-browser/chromium-1243/chrome-linux64/chrome'});
const fixture = process.argv[1], expected = JSON.parse(process.argv[2]);
const map = p => fixture + p.slice(aa.ROOT.length);
const rootOwned = s => {s.uid = 0; s.gid = 0; return s;};
const io = {
  realpathSync: p => p,
  lstatSync: p => p.startsWith(aa.ROOT) ? rootOwned(fs.lstatSync(map(p))) :
    {uid: 0, gid: 0, mode: 0o40755, isDirectory: () => true, isSymbolicLink: () => false},
  openSync: (p, flags) => fs.openSync(map(p), flags), closeSync: fs.closeSync,
  fstatSync: fd => rootOwned(fs.fstatSync(fd)), readFileSync: fs.readFileSync,
  readdirSync: p => fs.readdirSync(map(p)),
};
const actual = aa.identity({executablePath: () => assert.fail()}, io, 1000,
  {executable: aa.EXECUTABLE, capabilities: () => {}});
assert.deepEqual(actual, expected);
assert.equal(aa.profile(actual), process.argv[3]);
"""
        result = subprocess.run(['node', '-e', script, str(self.source), json.dumps(expected), aa.profile(expected).decode()],
                                cwd=ROOT, env={}, capture_output=True)
        self.assertEqual(result.returncode, 0, 'cross-language identity mismatch')

    def test_reject_binary_and_tree_hazards(self):
        for mode in (0o644, 0o777, 0o4755, 0o2755):
            self.chrome.chmod(mode)
            with self.assertRaises(ValueError):
                stage.tree(self.source, self.uid)
        self.chrome.chmod(0o755)
        self.chrome.write_bytes(b'not ELF')
        with self.assertRaises(ValueError):
            stage.tree(self.source, self.uid)
        self.chrome.write_bytes(b'\x7fELFfixture')
        for name in ('bad\nname', 'bad"name', 'bad*name'):
            bad = self.source / name
            bad.write_bytes(b'fixture')
            with self.assertRaises(ValueError):
                stage.tree(self.source, self.uid)
            bad.unlink()
        link = self.source / 'escape'
        link.symlink_to(self.root)
        with self.assertRaises(ValueError):
            stage.tree(self.source, self.uid)
        link.unlink()
        link.symlink_to(self.chrome)
        with self.assertRaises(ValueError):
            stage.tree(self.source, self.uid)
        link.unlink()
        os.mkfifo(link)
        with self.assertRaises(ValueError):
            stage.tree(self.source, self.uid)
        link.unlink()
        with patch.object(os, 'getxattr', return_value=b'capability'):
            with self.assertRaises(ValueError):
                stage.tree(self.source, self.uid)
        with self.assertRaises(ValueError):
            stage.tree(self.source, self.uid + 1)
        self.chrome.rename(self.chrome.with_name('original'))
        self.chrome.symlink_to('original')
        with self.assertRaises(ValueError):
            stage.tree(self.source, self.uid)

    def test_symlink_ancestor_and_writable_ancestor(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            stage.tree(alias / 'chrome-linux64', self.uid)
        self.source.chmod(0o777)
        with self.assertRaises(ValueError):
            stage.tree(self.source, self.uid)
        self.source.chmod(0o755)
        with self.assertRaises(ValueError):
            stage.parents(self.chrome)  # Service-owned staged ancestors forbidden.

    def test_source_swap_during_copy_rejected(self):
        original = os.open
        swapped = False
        def race(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == 'chrome' and kwargs.get('dir_fd') is not None and not swapped:
                swapped = True
                self.chrome.rename(self.chrome.with_name('saved'))
                self.chrome.symlink_to('saved')
            return original(path, flags, *args, **kwargs)
        with patch.object(os, 'open', side_effect=race):
            with self.assertRaises((ValueError, OSError)):
                stage.tree(self.source, self.uid)

    def test_private_candidate_and_preflight_binding(self):
        base = self.root / 'opt/ermis/epoptia-browser'
        base.parent.parent.mkdir()
        self.fake_root()
        self.stack.enter_context(patch.object(stage, 'BASE', base))
        self.stack.enter_context(patch.object(stage, 'source', return_value=(self.source, 0)))
        nodes = []
        record = stage.tree(self.source, 0, identities=nodes)
        verified = {'identity': stage.signature(self.source.lstat()), 'nodes': nodes, 'record': record}
        def prepare(record):
            containers = list(base.glob('.stage-*'))
            self.assertEqual(len(containers), 1)
            self.assertEqual(stat.S_IMODE(containers[0].stat().st_mode), 0o700)
            self.assertEqual((containers[0] / 'tree').stat().st_dev, base.stat().st_dev)
        with stage.staged(self.root, prepare=prepare, verified_source=verified):
            self.assertTrue((base / ('chromium-' + stage.REVISION)).is_dir())
        self.assertTrue(list(base.glob('.stage-*')))
        verified['nodes'] = []
        with self.assertRaises(stage.PathInvariant):
            with stage.staged(self.root, verified_source=verified):
                self.fail('changed source admitted')

    def test_same_bytes_new_inode_between_verification_and_copy(self):
        base = self.root / 'opt/ermis/epoptia-browser'
        base.parent.parent.mkdir()
        self.fake_root()
        self.stack.enter_context(patch.object(stage, 'BASE', base))
        self.stack.enter_context(patch.object(stage, 'source', return_value=(self.source, 0)))
        original = tempfile.mkdtemp
        def swap(**kwargs):
            temporary = original(**kwargs)
            replacement = self.chrome.with_name('replacement')
            replacement.write_bytes(self.chrome.read_bytes())
            replacement.chmod(0o755)
            os.replace(replacement, self.chrome)
            return temporary
        with patch.object(tempfile, 'mkdtemp', side_effect=swap):
            with self.assertRaises(ValueError):
                with stage.staged(self.root):
                    self.fail('inode swap admitted')
        self.assertTrue(list(base.glob('.stage-*')))

    def test_old_layout_rejected(self):
        (self.source / 'chrome-linux64').rename(self.source / 'chrome-linux')
        with self.assertRaises(ValueError):
            stage.tree(self.source, self.uid)

    def test_permissions_hardlinks_and_capability_errors_fail_closed(self):
        helper = self.chrome.with_name('chrome_sandbox')
        for path in (self.source, self.source / 'chrome-linux64', helper):
            original = stat.S_IMODE(path.stat().st_mode)
            for mode in (0o777, 0o4755, 0o2755, 0o1755):
                with self.subTest(path=path.name, mode=oct(mode)):
                    path.chmod(mode)
                    with self.assertRaises(ValueError):
                        stage.tree(self.source, self.uid)
            path.chmod(original)
        link = self.source / 'hardlink'
        os.link(helper, link)
        with self.assertRaises(ValueError):
            stage.tree(self.source, self.uid)
        link.unlink()
        import errno
        for error in (errno.EACCES, errno.ENOTSUP, errno.EIO):
            with patch.object(os, 'getxattr', side_effect=OSError(error, 'fixture')):
                with self.assertRaises(stage.PathInvariant):
                    stage.tree(self.source, self.uid)

    def test_browser_publish_failures_retain_recovery_artifacts(self):
        base = self.root / 'opt/ermis/epoptia-browser'
        base.parent.parent.mkdir()
        self.fake_root()
        self.stack.enter_context(patch.object(stage, 'BASE', base))
        self.stack.enter_context(patch.object(stage, 'source', return_value=(self.source, 0)))
        target = base / ('chromium-' + stage.REVISION)
        rename, fsync = stage.rename_absent, os.fsync
        for boundary in ('rename', 'sync', 'outer'):
            fired = False
            def fail_rename(src, dst):
                nonlocal fired
                if boundary == 'rename' and dst == target:
                    fired = True
                    raise OSError('fixture')
                return rename(src, dst)
            def fail_sync(fd):
                nonlocal fired
                if boundary == 'sync' and target.exists() and not fired:
                    fired = True
                    raise OSError('fixture')
                return fsync(fd)
            with self.subTest(boundary=boundary), patch.object(stage, 'rename_absent', fail_rename), patch.object(os, 'fsync', fail_sync):
                with self.assertRaisesRegex(ValueError, "manual-recovery"):
                    with stage.staged(self.root):
                        self.assertTrue(target.is_dir())
                        fired = True
                        raise OSError('fixture')
                self.assertTrue(fired)
                self.assertEqual(target.exists(), boundary != 'rename')
                if target.exists():
                    target.rename(base / ('retained-' + boundary))
                self.assertTrue(list(base.glob('.stage-*')))

    def test_destination_races_preserve_insertions_and_prior_tree(self):
        base = self.root / 'opt/ermis/epoptia-browser'
        base.parent.mkdir(parents=True)
        self.fake_root()
        self.stack.enter_context(patch.object(stage, 'BASE', base))
        self.stack.enter_context(patch.object(stage, 'source', return_value=(self.source, 0)))
        target = base / ('chromium-' + stage.REVISION)
        for kind in ('directory', 'file', 'symlink'):
            for edge in ('callback', 'syscall'):
                with self.subTest(kind=kind, edge=edge):
                    def insert(*args):
                        if kind == 'directory':
                            target.mkdir()
                        elif kind == 'file':
                            target.write_bytes(b'foreign')
                        else:
                            target.symlink_to(self.source, target_is_directory=True)
                    commit = stage.rename_absent
                    def racing_commit(src, dst):
                        insert()
                        return commit(src, dst)
                    with patch.object(stage, 'rename_absent', racing_commit if edge == 'syscall' else commit):
                        with self.assertRaises((ValueError, OSError)):
                            with stage.staged(self.root, before_publish=insert if edge == 'callback' else None):
                                self.fail('destination race admitted')
                    self.assertTrue(target.is_symlink() if kind == 'symlink' else target.exists())
                    self.assertTrue(list(base.glob('.stage-*')))
                    if kind == 'directory':
                        target.rmdir()
                    else:
                        target.unlink()
        with stage.staged(self.root):
            pass
        prior = base / 'prior'
        def replace_prior():
            target.rename(prior)
            target.symlink_to(prior, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            with stage.staged(self.root, before_publish=replace_prior):
                self.fail('replacement admitted')
        self.assertFalse(target.is_symlink())
        self.assertFalse(prior.exists())
        self.assertEqual((target / stage.LAYOUT).read_bytes(), self.chrome.read_bytes())
        self.assertTrue(list(base.glob('.stage-*')))

    def test_target_recheck_conflict_precedes_mkdir_and_temp(self):
        base = self.root / 'opt/ermis/epoptia-browser'
        base.mkdir(parents=True)
        target = base / ('chromium-' + stage.REVISION)
        target.symlink_to(self.source)
        self.fake_root()
        self.stack.enter_context(patch.object(stage, 'BASE', base))
        self.stack.enter_context(patch.object(stage, 'source', return_value=(self.source, 0)))
        with patch.object(Path, 'mkdir', side_effect=AssertionError('mkdir')) as mkdir, \
             patch.object(stage.tempfile, 'mkdtemp', side_effect=AssertionError('temp')) as temp:
            with self.assertRaises((OSError, ValueError)):
                with stage.staged(self.root):
                    self.fail('unsafe destination admitted')
            mkdir.assert_not_called()
            temp.assert_not_called()

    def test_postcommit_foreign_tree_and_staged_same_content_swap(self):
        base = self.root / 'opt/ermis/epoptia-browser'
        base.parent.mkdir(parents=True)
        self.fake_root()
        self.stack.enter_context(patch.object(stage, 'BASE', base))
        self.stack.enter_context(patch.object(stage, 'source', return_value=(self.source, 0)))
        target = base / ('chromium-' + stage.REVISION)
        def swap_staged():
            container, = base.glob('.stage-*')
            temporary = container / 'tree'
            temporary.rename(container / 'saved')
            temporary.mkdir()
            stage.tree(self.source, 0, temporary)
        with self.assertRaisesRegex(ValueError, 'binding-changed'):
            with stage.staged(self.root, before_publish=swap_staged):
                self.fail('same-content replacement accepted')
        self.assertFalse(target.exists())
        with self.assertRaises(ValueError):
            with stage.staged(self.root):
                target.rename(base / 'saved-committed')
                target.write_bytes(b'foreign')
                before = stage.signature(target.lstat())
        self.assertEqual(stage.signature(target.lstat()), before)
        self.assertEqual(target.read_bytes(), b'foreign')

    def fake_root(self):
        # Simulate ownership, never chown the host or require root privileges.
        def root_stat(s):
            values = list(s)
            values[4:6] = [0, 0]
            # Preserve nanosecond fields used by race validation.
            return SimpleNamespace(**{k: (0 if k in ('st_uid', 'st_gid') else getattr(s, k))
                                      for k in dir(s) if k.startswith('st_')})
        original_stat, original_fstat = os.stat, os.fstat
        self.stack.enter_context(patch.object(os, 'stat', side_effect=lambda *a, **k: root_stat(original_stat(*a, **k))))
        self.stack.enter_context(patch.object(os, 'fstat', side_effect=lambda *a, **k: root_stat(original_fstat(*a, **k))))
        self.stack.enter_context(patch.object(os, 'chown'))

    def test_atomic_publish_failure_existing_conflict_and_rollback(self):
        base = self.root / 'opt/ermis/epoptia-browser'
        base.parent.parent.mkdir()
        self.fake_root()
        self.stack.enter_context(patch.object(stage, 'BASE', base))
        self.stack.enter_context(patch.object(stage, 'source', return_value=(self.source, 0)))
        target = base / ('chromium-' + stage.REVISION)
        original_tree = stage.tree
        def fail_copy(root, uid, destination=None, **kwargs):
            if destination:
                raise ValueError('synthetic copy failure')
            return original_tree(root, uid, **kwargs)
        with patch.object(stage, 'tree', side_effect=fail_copy):
            with self.assertRaises(ValueError):
                with stage.staged(self.root):
                    self.fail('incomplete copy published')
        self.assertFalse(target.exists())
        self.assertTrue(list(base.glob('.stage-*')))
        with self.assertRaisesRegex(ValueError, 'manual-recovery'):
            with stage.staged(self.root) as expected:
                self.assertTrue(target.is_dir())
                raise RuntimeError('synthetic failure')
        self.assertTrue(target.exists())
        with self.assertRaisesRegex(ValueError, 'first-install-only'):
            with stage.staged(self.root):
                self.fail('coherent candidate admitted')
        self.assertEqual(stage.identity(), expected)
        (target / 'chrome-linux64/resources/data.pak').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            with stage.staged(self.root):
                self.fail('invalid existing tree must not be replaced')
        self.assertEqual((target / 'chrome-linux64/resources/data.pak').read_bytes(), b'changed')
        self.assertTrue(list(base.glob('.stage-*')))

    def test_static_metadata_and_passwd_home_only(self):
        project = self.root / 'project'
        metadata = {
            'node_modules/playwright/package.json': {'version': stage.VERSION},
            'node_modules/playwright-core/package.json': {'version': stage.VERSION},
            'package-lock.json': {'packages': {'node_modules/' + p: {'version': stage.VERSION} for p in ('playwright', 'playwright-core')}},
            'node_modules/playwright-core/browsers.json': {'browsers': [{'name': 'chromium', 'revision': stage.REVISION, 'browserVersion': stage.CHROMIUM}]},
        }
        for relative, value in metadata.items():
            file = project / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps(value))
        account = SimpleNamespace(pw_uid=self.uid, pw_dir='/home/fixture-unused')
        candidate = project / '.cache/ms-playwright' / ('chromium-' + stage.REVISION)
        candidate.mkdir(parents=True)
        # Simulate the second cache's absence, without reading outside the project.
        original = Path.lstat
        def lstat(p, *args, **kwargs):
            if str(p).startswith('/home/fixture-unused'):
                raise FileNotFoundError()
            return original(p, *args, **kwargs)
        with patch.object(stage.pwd, 'getpwnam', return_value=account), patch.object(Path, 'lstat', lstat):
            self.assertEqual(stage.source(project), (candidate, self.uid))
            package = project / 'node_modules/playwright/package.json'
            hardlink = project / 'metadata-link'
            os.link(package, hardlink)
            with self.assertRaises(ValueError):
                stage.source(project)
            hardlink.unlink()
            with patch.object(os, 'getxattr', return_value=b'fixture-capability'):
                with self.assertRaises(ValueError):
                    stage.source(project)
            saved = package.with_name('saved.json')
            original_caps = stage.caps
            swapped = False
            def swap_metadata(fd):
                nonlocal swapped
                original_caps(fd)
                if not swapped:
                    swapped = True
                    package.rename(saved)
                    package.write_bytes(saved.read_bytes())
            with patch.object(stage, 'caps', side_effect=swap_metadata):
                with self.assertRaises(ValueError):
                    stage.source(project)
            self.assertTrue(swapped)
            package.unlink()
            saved.rename(package)
            def duplicate(p, *args, **kwargs):
                if str(p).startswith('/home/fixture-unused'):
                    return original(candidate)
                return original(p, *args, **kwargs)
            with patch.object(Path, 'lstat', duplicate):
                with self.assertRaises(ValueError):
                    stage.source(project)
            account.pw_dir = '/root'
            with self.assertRaises(ValueError):
                stage.source(project)
            account.pw_dir = '/home/fixture-unused'
            for bad in ('1242', '1243\n', '../1243', '1243"'):
                data = metadata['node_modules/playwright-core/browsers.json']
                data['browsers'][0]['revision'] = bad
                (project / 'node_modules/playwright-core/browsers.json').write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    stage.source(project)
        # Tree acceptance additionally requires the modern full Chromium layout.
        self.chrome.rename(self.chrome.with_name('headless_shell'))
        with self.assertRaises(ValueError):
            stage.tree(self.source, self.uid)


if __name__ == '__main__':
    unittest.main()
