"""Non-root offline suites, exact counts, no skips, and source immutability.

Run from the checkout: python3 -I -B tests/run_bootstrap_verification.py
All subprocess environments are synthetic; fixtures remain in the checkout.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
NODE = (
    'test_browser_launch_sandbox.cjs', 'test_login_apparmor.cjs',
    'test_login_diagnostics.cjs', 'test_login_sandbox_probe.cjs',
    'test_login_socket.cjs', 'test_epoptia_login.cjs',
    'test_epoptia_browser_runtime.cjs', 'test_chromium_runtime_smoke.cjs',
    'test_epoptia_browser_access_check.cjs',
)


def fingerprints():
    manifest = json.loads((ROOT / 'admin_bootstrap/login_manifest.json').read_bytes())
    paths = {ROOT / name for name in manifest}
    paths.update((ROOT / 'admin_bootstrap').glob('*.py'))
    paths.update((ROOT / 'admin_bootstrap').glob('*.sh'))
    paths.update((ROOT / 'tests').glob('test_login*'))
    paths.update(ROOT / 'tests' / name for name in NODE)
    paths.update(ROOT / name for name in (
        'admin_bootstrap/login_manifest.json', 'admin_bootstrap/ermis-admin',
        'admin_bootstrap/ERMIS_ADMIN_WRAPPER_READY',
        'tests/run_bootstrap_verification.py', 'tests/login_test_loader.cjs',
        'tests/browser_policy_fixture.cjs', 'tests/test_browser_login_gateway.py'))
    result = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(paths) if p.is_file()}
    if any(result[name] != digest for name, digest in manifest.items()):
        raise AssertionError('source manifest mismatch')
    return result


def main():
    if os.getuid() == 0 or os.geteuid() == 0:
        raise SystemExit('Run as the unprivileged checkout owner')
    baseline = fingerprints()
    digest = hashlib.sha256(json.dumps(baseline, sort_keys=True).encode()).hexdigest()
    commands = [
        ('admin', ['/bin/sh', 'admin_bootstrap/verify.sh']),
        ('login-python', [sys.executable, '-B', '-m', 'unittest', 'discover',
                          '-s', 'tests', '-p', 'test_login_*.py']),
        ('gateway-python', [sys.executable, '-B', '-m', 'unittest',
                            'tests.test_browser_login_gateway']),
    ]
    # Direct file execution preserves node:test's individual test counts and
    # failure diagnostics even when tested modules isolate process state.
    commands.extend(('node/' + name, ['/usr/bin/node', 'tests/' + name]) for name in NODE)
    totals = []
    with tempfile.TemporaryDirectory(prefix='bootstrap-verification-', dir=ROOT / 'tests') as folder:
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'TMPDIR': folder,
               'PYTHONDONTWRITEBYTECODE': '1'}
        for iteration in (1, 2):
            total = 0
            for label, command in commands:
                result = subprocess.run(command, cwd=ROOT, env=env,
                                        capture_output=True, text=True, timeout=180)
                output = result.stdout + result.stderr
                counts = re.findall(r'^# tests (\d+)$' if label.startswith('node/') else
                                    r'^Ran (\d+) tests? in ', output, re.MULTILINE)
                skipped = re.search(r'^# (?:skipped|todo) [1-9]|skipped=|expected failures=|unexpected successes=',
                                    output, re.MULTILINE)
                if result.returncode or not counts or skipped:
                    print(output, flush=True)
                    raise AssertionError(f'{label}: failed, skipped, or missing test count')
                count = sum(map(int, counts))
                total += count
                print(f'run={iteration} suite={label} tests={count} failures=0 skips=0', flush=True)
            if fingerprints() != baseline:
                raise AssertionError('test run changed source/test/marker fingerprints')
            totals.append(total)
            print(f'run={iteration} total={total} manifest=verified fingerprints=unchanged', flush=True)
    if totals[0] != totals[1]:
        raise AssertionError('inconsistent test counts')
    print(f'fingerprinted-files={len(baseline)} sha256={digest}', flush=True)


if __name__ == '__main__':
    main()
