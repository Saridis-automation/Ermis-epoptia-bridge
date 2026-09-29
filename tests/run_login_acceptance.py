"""Eight offline acceptance groups; synthetic fixtures, no deployment/browser.

Run: python3 tests/run_login_acceptance.py [group|all]
The legacy staging alias retains its original coverage.
"""
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
GROUPS = {
    'source': [('test_login_browser_stage.py', ('static_metadata', 'old_layout', 'source_swap'))],
    'permissions': [('test_login_browser_stage.py', ('complete_tree', 'python_node', 'reject_binary',
                                                   'symlink_ancestor', 'permissions_hardlinks'))],
    'atomic': [('test_login_browser_stage.py', ('atomic_publish', 'browser_publish')),
               'test_login_apparmor.py', 'test_login_install_sandbox.py'],
    'launcher': ['test_browser_launch_sandbox.cjs', 'test_login_apparmor.cjs', 'test_epoptia_browser_runtime.cjs',
                 'test_chromium_runtime_smoke.cjs', 'test_epoptia_browser_access_check.cjs'],
    'profile': ['test_login_apparmor_diagnose.py',
                ('test_login_apparmor.py', ('literal_profile', 'loaded_attachment'))],
    'offline-probe': ['test_login_sandbox_probe.cjs', 'test_login_sandbox_probe.py'],
    'login-confirmation': ['test_epoptia_login.cjs', 'test_login_diagnostics.cjs',
                           'test_login_managed_home.py', 'test_browser_login_gateway.py'],
    'manifest': [('test_login_apparmor.py', ('tampering', 'manifest_rejects')),
                 'test_login_readiness.py'],
}
ALIASES = {'staging': ('source', 'permissions', 'atomic')}


def main():
    selected = sys.argv[1] if len(sys.argv) == 2 else 'all'
    if selected not in (*GROUPS, *ALIASES, 'all') or len(sys.argv) > 2:
        raise SystemExit('expected ' + ', '.join((*GROUPS, *ALIASES, 'all')))
    selected_groups = tuple(GROUPS) if selected == 'all' else ALIASES.get(selected, (selected,))
    temporary = tempfile.TemporaryDirectory(prefix='login-acceptance-', dir=ROOT / 'tests')
    env = {'PATH': '/usr/bin:/bin', 'TMPDIR': temporary.name,
           'PYTHONDONTWRITEBYTECODE': '1'}
    failed = []
    for group in selected_groups:
        print(f'ACCEPTANCE GROUP: {group}', flush=True)
        group_failed = False
        for spec in GROUPS[group]:
            name, patterns = spec if isinstance(spec, tuple) else (spec, ())
            print(f'TEST FILE: tests/{name}' + (f' filters={patterns}' if patterns else ''), flush=True)
            args = (['node', str(ROOT / 'tests' / name)] if name.endswith('.cjs') else
                    [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', name, '-v'])
            for pattern in patterns:
                args.extend(['-k', pattern])
            if subprocess.run(args, cwd=ROOT, env=env).returncode:
                failed.append(group + '/' + name)
                group_failed = True
        print(('FAIL' if group_failed else 'PASS') + ': GROUP ' + group, flush=True)
    print('FAIL: ' + ', '.join(failed) if failed else 'PASS: ' + selected, flush=True)
    temporary.cleanup()
    return bool(failed)


if __name__ == '__main__':
    sys.exit(main())
