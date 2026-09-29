"""Parse generated artifacts and exercise the real JS resolver without host probes."""
import configparser
import json
from pathlib import Path
import shlex
import subprocess
import unittest
from admin_bootstrap import login_bootstrap as login

ROOT = Path(__file__).resolve().parents[1]

class ManagedHome(unittest.TestCase):
    def test_generated_unit_effective_environment_and_resolver(self):
        data = next(data for path, (data, _) in login.artifacts().items()
                    if path.name == login.UNIT_NAME).decode()
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        parser.read_string(data)
        service = parser['Service']
        root = '/run/' + service['RuntimeDirectory']
        assignments = [assignment for line in data.splitlines() if line.startswith('Environment=')
                       for assignment in shlex.split(line.split('=', 1)[1])]
        env = dict(item.split('=', 1) for item in assignments)
        keys = ['EPOPTIA_LOGIN_BROWSER_HOME', 'HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
                'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'TMPDIR']
        self.assertEqual(len(assignments), len(keys) + 1)
        self.assertEqual(env, {**dict.fromkeys(keys, root), 'EPOPTIA_LOGIN_CHROMIUM_EXECUTABLE': '/opt/ermis/epoptia-browser/chromium-1243/chrome-linux64/chrome'})
        result = subprocess.run(['node', '-e', '''
const assert = require('node:assert/strict');
const {resolveManagedHome, homeEnvironment} = require('./epoptia_login_backend.cjs');
const env = JSON.parse(process.argv[1]);
const root = resolveManagedHome(env.EPOPTIA_LOGIN_BROWSER_HOME);
for (const [key, value] of Object.entries(homeEnvironment(root))) assert.equal(env[key], value);
''', json.dumps(env)], cwd=ROOT, env={}, capture_output=True)
        self.assertEqual(result.returncode, 0)
        expected = {'User': 'ermis', 'Group': 'ermis', 'RuntimeDirectoryMode': '0700',
                    'UMask': '0077', 'NoNewPrivileges': 'true', 'PrivateTmp': 'true',
                    'ProtectSystem': 'strict', 'ProtectHome': 'read-only',
                    'KillMode': 'control-group', 'RestrictAddressFamilies': 'AF_UNIX AF_INET',
                    'ReadWritePaths': '/home/ermis/.local/state/epoptia-browser ' + root}
        for key, value in expected.items():
            self.assertEqual(service[key], value)
        self.assertNotIn('--no-sandbox', data)
        self.assertNotIn('[Install]', data)
        socket = configparser.ConfigParser(interpolation=None)
        socket.read_string(login.SOCKET_UNIT)
        self.assertEqual(socket['Socket']['ListenStream'], '/run/ermis-epoptia-login/control.sock')
        self.assertEqual(socket['Socket']['SocketMode'], '0600')
        self.assertEqual(socket['Socket']['Accept'], 'no')

    def test_default_launcher_reads_required_hook_and_refuses_before_io(self):
        for env in ({}, {'EPOPTIA_LOGIN_BROWSER_HOME': '/tmp'},
                    {'EPOPTIA_LOGIN_BROWSER_HOME': '/run/ermis-epoptia-login/enrollment/'}):
            result = subprocess.run(['node', '-e', """
const assert = require('node:assert/strict');
const {Backend} = require('./epoptia_login_backend.cjs');
const backend = new Backend({launch: () => assert.fail('launch forbidden')});
(async () => {
  await assert.rejects(backend.start({host: '127.0.0.1'}), /backend_unavailable/);
  assert.equal(backend.diagnose().failure_class, 'managed_home_invalid');
  assert.equal(backend.diagnose().child_launch_attempted, false);
})().catch(() => { process.exitCode = 1; });
"""], cwd=ROOT, env=env, capture_output=True)
            self.assertEqual(result.returncode, 0)
