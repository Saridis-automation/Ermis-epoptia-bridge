"""Synthetic gateway and source integrity checks; no installed runtime access."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from epoptia_browser import login_command, sanitize_sandbox_probe, SANDBOX_ENUMS
from ermis_gateway import ACTIONS, Gateway, validate
from admin_bootstrap import login_bootstrap

ROOT = Path(__file__).resolve().parents[1]


class SandboxProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_gateway_no_confirmation_and_closed_schema(self):
        for prefix in ('epoptia_login_', 'epoptia_browser_login_'):
            action = prefix + 'sandbox_probe'
            self.assertFalse(ACTIONS[action].write)
            for field in ('url', 'target', 'path', 'command', 'env', 'timeout', 'ttl_minutes'):
                with self.assertRaises(ValueError):
                    validate(action, {field: 'PRIVATE'})
            raw = dict(ok=True, status='sandbox_probe_complete', stderr='PRIVATE',
                       sandbox_probe=dict(ready=True, clean_close=True, sandbox_class='none',
                                          user_namespace='permission_denied', stderr='PRIVATE'))
            with patch('epoptia_browser._login_command', AsyncMock(return_value=raw)) as command:
                result = await Gateway().request(dict(session_id='sandbox_probe_test_0001',
                                                     action=action, arguments={}))
                command.assert_awaited_once_with('sandbox_probe')
            self.assertNotEqual(result['status'], 'confirmation_required')
            self.assertEqual(result['result']['sandbox_class'], 'none')
            self.assertNotIn('PRIVATE', str(result))
            self.assertTrue(all(type(v) in (str, bool) for v in result['result'].values()))

    def test_enum_allowlists_and_cross_language_schema(self):
        import subprocess
        actual = json.loads(subprocess.check_output([
            'node', '-e', "process.stdout.write(JSON.stringify(require('./epoptia_login_sandbox.cjs').ENUMS))"
        ], cwd=ROOT, env={}))
        self.assertEqual(actual, SANDBOX_ENUMS)
        for key, allowed in SANDBOX_ENUMS.items():
            for value in allowed:
                self.assertEqual(sanitize_sandbox_probe({key: value})[key], value)
            for value in ('PRIVATE', {}, [], None, 123):
                self.assertEqual(sanitize_sandbox_probe({key: value})[key], 'none' if key == 'cleanup_class' else 'unknown')

    def test_owned_digests_and_fail_closed_tamper(self):
        self.assertEqual(login_bootstrap.source_blockers(ROOT), [])
        manifest = json.loads((ROOT / 'admin_bootstrap/login_manifest.json').read_text())
        for name in login_bootstrap.SOURCE_FILES:
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), manifest[name])
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as folder:
            copied = Path(folder)
            for name in manifest:
                target = copied / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
                target.chmod(0o600)
            target = copied / 'admin_bootstrap/login_manifest.json'
            target.write_text(json.dumps(manifest))
            target.chmod(0o600)
            self.assertEqual(login_bootstrap.source_blockers(copied), [])
            for name in ('epoptia_login_sandbox.cjs', 'epoptia_login_sandbox_worker.cjs'):
                original = (copied / name).read_bytes()
                (copied / name).write_bytes(original + b'\n// tamper\n')
                self.assertIn('persistent_loopback_backend_missing', login_bootstrap.source_blockers(copied))
                (copied / name).write_bytes(original)

    def test_worker_does_not_import_auth_or_disable_sandbox(self):
        source = (ROOT / 'epoptia_login_sandbox_worker.cjs').read_text()
        self.assertNotIn('epoptia_browser_session', source)
        self.assertNotIn('epoptia_browser_scheduler', source)
        self.assertIn('DWELL_MS', source)
        for name in ('epoptia_login_sandbox_worker.cjs', 'epoptia_login_sandbox.cjs'):
            source = (ROOT / name).read_text()
            for forbidden in ('--no-sandbox', '--disable-setuid-sandbox', 'sudo ', 'systemctl ', 'writeFile'):
                self.assertNotIn(forbidden, source)

    def test_canonical_generated_hardening_unchanged(self):
        # Freeze the canonical pre-cleanup service/socket bytes, not installed state.
        artifacts = login_bootstrap.artifacts()
        expected = {'ermis-epoptia-login.socket': '9a0776c7d0cb046f9ce148c2e868979bf0ffa5272f99460ef954bbbc9a3b3949', 'ermis-epoptia-login.service': 'ac378d9a8de600a5234972adc99ad579e14a953ac8ddcfbba6541b1c4aad1742'}
        for name, digest in expected.items():
            data = next(data for path, (data, _) in artifacts.items() if path.name == name)
            # Removing the sole new executable selection must reproduce every old hardening byte.
            data = data.replace(b'Environment=EPOPTIA_LOGIN_CHROMIUM_EXECUTABLE=/opt/ermis/epoptia-browser/chromium-1243/chrome-linux64/chrome\n', b'')
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
        self.assertIn('NoNewPrivileges=true\n', login_bootstrap.UNIT)
        listeners = [line for line in login_bootstrap.SOCKET_UNIT.splitlines()
                     if line.startswith('Listen')]
        self.assertEqual(listeners, ['ListenStream=/run/ermis-epoptia-login/control.sock'])
        self.assertIn('SocketMode=0600\n', login_bootstrap.SOCKET_UNIT)
        for text in (login_bootstrap.UNIT, login_bootstrap.SOCKET_UNIT,
                     (ROOT / 'epoptia_login_sandbox.cjs').read_text()):
            self.assertNotIn('--no-sandbox', text)
            self.assertNotIn('--disable-setuid-sandbox', text)
