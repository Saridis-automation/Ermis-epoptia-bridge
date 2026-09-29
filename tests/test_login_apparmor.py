"""Local synthetic AppArmor transaction tests. Never execute the parser/browser."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from contextlib import contextmanager
from unittest.mock import patch
from admin_bootstrap import login_apparmor as aa, login_bootstrap as login

ROOT = Path(__file__).resolve().parents[1]


class AppArmorTransaction(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = self.root / 'profile'
        self.receipt = self.root / 'receipt'
        self.parser_path = self.root / 'parser-fixture'
        self.parser_path.write_text('fixture only')
        self.profiles = self.root / 'profiles-fixture'
        self.profiles.write_text('')
        self.current = dict(schema=2, executable='/opt/ermis/epoptia-browser/chromium-1243/chrome-linux64/chrome',
                            revision='1243', layout='chrome-linux64/chrome', playwright='1.63.0', chromium='153.0.8010.12', sha256='a' * 64, tree_sha256='b' * 64)
        self.calls = []
        self.failure_action = None
        self.api = dict(vars(login), ROOT=self.root, prepare_parent=lambda *a, **k: None,
                        owned_read=lambda p, mode: p.read_bytes())
        self.patch(aa, 'PROFILE', self.profile)
        self.patch(aa, 'RECEIPT', self.receipt)
        self.patch(aa, 'PROFILES', self.profiles)
        self.patch(aa, 'PARSER', str(self.parser_path))
        self.patch(aa, 'collect', lambda root: self.current.copy())
        self.patch(aa, 'parser', self.parser)
        @contextmanager
        def staged(root, prepare, before_publish):
            prepare(self.current.copy())
            before_publish()
            yield self.current.copy()
        self.patch(aa, 'staging', lambda: {'staged': staged})
        self.patch(aa, 'diagnostic_attachment', lambda record: True)
        self.patch(os, 'fchown', lambda *a: None)
        self.patch(login, 'owned_read', lambda p, mode: p.read_bytes())
        self.api['invalidate_ready'] = lambda: None
        self.api['READY'] = self.root / 'ready'
        self.patch(aa, 'diagnostic_parents', lambda *args: None)
        real_stat = Path.lstat
        parser_path = self.parser_path
        self.patch(Path, 'lstat', lambda p, *a, **k: SimpleNamespace(st_mode=0o100755, st_uid=0)
                   if p == parser_path else real_stat(p, *a, **k))
        self.patch(aa.subprocess, 'run', lambda *a, **k: self.fail_test())

    def fail_test(self):
        self.fail('host commands forbidden')

    def patch(self, obj, name, value):
        context = patch.object(obj, name, value)
        context.start()
        self.addCleanup(context.stop)

    def parser(self, action, source):
        self.assertEqual(source.parent, self.root)
        self.calls.append(action)
        if self.failure_action == action:
            self.failure_action = None
            raise ValueError('private fixture detail')
        text = source.read_text()
        self.assertNotIn('/home/', text)
        executable = text.split('profile "')[1].split('"')[0]
        entries = self.profiles.read_text().splitlines()
        entry = executable + ' (unconfined)'
        if action == 'load':
            entries = [line for line in entries if line != entry] + [entry]
        elif action == 'remove':
            entries = [line for line in entries if line != entry]
        self.profiles.write_text('\n'.join(entries))

    def install(self):
        with aa.transaction(self.api, 'install'):
            pass

    def snapshot(self):
        return tuple(p.read_bytes() if p.exists() else None for p in (self.profile, self.receipt, self.profiles))


    def test_literal_profile_and_hostile_paths(self):
        data = aa.profile(self.current).decode()
        self.assertEqual(data.split('{\n')[1], '  userns,\n}\n')
        self.assertIn('profile "' + aa.profile_name(self.current) + '" "' + self.current['executable'] + '" flags=(unconfined)', data)
        for bad in ('/tmp/chrome', self.current['executable'] + '\n', self.current['executable'] + '"',
                    self.current['executable'].replace('1243', '*')):
            with self.assertRaises(ValueError):
                aa.profile({**self.current, 'executable': bad})


    def test_manifest_rejects_changed_owned_sources(self):
        manifest = json.loads((ROOT / 'admin_bootstrap/login_manifest.json').read_text())
        for name in manifest:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
        target = self.root / 'admin_bootstrap/login_manifest.json'
        target.write_text(json.dumps(manifest))
        self.assertEqual(login.source_blockers(self.root), [])
        for name in ('epoptia_login_apparmor.cjs', 'epoptia_login_backend.cjs',
                     'epoptia_login_sandbox.cjs', 'epoptia_login_sandbox_worker.cjs',
                     'epoptia_browser_runtime.cjs', 'admin_bootstrap/login_apparmor.py',
                     'admin_bootstrap/login_browser_stage.py', 'admin_bootstrap/bootstrap.py'):
            source = self.root / name
            original = source.read_bytes()
            source.write_bytes(original + b' ')
            self.assertTrue(login.source_blockers(self.root))
            source.write_bytes(original)
        manifest['epoptia_login_apparmor.cjs'] = '0' * 64
        target.write_text(json.dumps(manifest))
        self.assertTrue(login.source_blockers(self.root))
