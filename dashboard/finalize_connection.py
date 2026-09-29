"""Owner-only activation: python3 dashboard/finalize_connection.py [--verify-only].

No report-provided command is executed. Source fingerprints deliberately fail
closed after edits: re-review the connection and update them before activation.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ('ermis-dashboard.service',)
# Reviewed direct default, shared paginated readers, contracts and GET validator.
SOURCE_HASHES = {
    "mcp_server.py": "d85cd83875d63e8c059934fdcb81481ba18af0466ab4cfcc870a15bd091cebc6",
    "epoptia_read.py": "30e67272fc3dc0089031ab7baf6271f048229013d942d077825e6f99cbd18465",
    "dashboard/provider.py": "4ab380d2b1b636031e863f4ca576cd6023181c2ba25587dc40055c4895c20f82",
    "dashboard/server.py": "2701054621271eb76c1cadb70c9f47978dfa011e3421e0853a287500cfd7cc72",
    "dashboard/orders.py": "40e8be666b5af1e8483765fe8f1b25d81ab70e2ec9d60571a48c0db8aa7d3601",
    "dashboard/stations.py": "d387489851123c1156f95a6d3e590cffe4b723baaf4ee4c74959d2444d6322b4",
    "dashboard/adapter.py": "872e1d1f965af74c076357868b8cc03e4d3990e3cabf60cf4c904764dba1a8fd",
    "dashboard/verify_live.py": "4247eb0cb75f661dff10fb71a150a62b3b4a967ff15090431aebb4ee71703116",
    "tests/test_dashboard_connection.py": "d028b0ee0bb597521fffc88a604e898df7bd69bd5c66a755c387dbf96263f1b2",
    "epoptia_queries.py": "cccff1af0d5e1e03982599f74c04065b19fa3c6be8447bf48e1be8ece1e9c449",
    "dashboard/static/dashboard.js": "955462c70c41a51571e9af44de41ac791c63e23e720a5bbdb2962ca2529ff5ff",
    "tests/test_dashboard_browser.cjs": "68cf256ff52210e4705c1a1cb42604dcf7b706d04264c1430c1fcf1d2b15f8b5",
    "tests/test_dashboard.py": "a66b5654d58f9a5b271b8f28b9d21183a839b2bb7c8ddac11b4c11f65eb8547d",
    "tests/test_finalize_connection.py": "3e6845bc2b0e4e3cf97164969d11cb28995540bfa5f24b0fda17a81a404627fc"
}
TEST_RUNNER = r'''
import contextlib, io, json, unittest, warnings
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter('always')
    suite = unittest.defaultTestLoader.discover('tests', pattern='test_dashboard_connection.py')
    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
counts = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
              skips=len(result.skipped), expected_failures=len(result.expectedFailures),
              unexpected_successes=len(result.unexpectedSuccesses), warnings=len(caught))
print(json.dumps(counts))
'''


class Stop(RuntimeError):
    """Contains only a fixed, safe operator explanation."""


def checked_sources():
    try:
        report = json.loads((ROOT / 'dashboard/collection_fix_verification.json').read_text())
        if report.get('fix_implemented') is not True:
            raise Stop('Preflight stopped: report does not confirm an implemented fix.')
        for path, digest in SOURCE_HASHES.items():
            if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != digest:
                raise Stop('Preflight stopped: reviewed application/test source changed; review required.')
        if not SOURCE_HASHES or not (ROOT / 'tests/test_dashboard_connection.py').is_file():
            raise Stop('Preflight stopped: required connection implementation/tests missing.')
    except (OSError, ValueError, AttributeError):
        raise Stop('Preflight stopped: implementation evidence missing or invalid.') from None


def preflight():
    checked_sources()
    python = ROOT / 'venv/bin/python'
    if not python.is_file():
        raise Stop('Preflight stopped: project venv/bin/python missing.')
    try:
        result = subprocess.run([str(python), '-c', TEST_RUNNER], cwd=ROOT,
                                capture_output=True, text=True, timeout=180, check=False)
        counts = json.loads(result.stdout)
        keys = ('tests', 'failures', 'errors', 'skips', 'expected_failures',
                'unexpected_successes', 'warnings')
        if (result.returncode or result.stderr or set(counts) != set(keys)
                or any(type(counts[key]) is not int or counts[key] < 0 for key in keys)):
            raise ValueError()
        print('Connection preflight: ' + json.dumps(counts))
        if counts['tests'] < 22 or any(counts[key] for key in keys if key != 'tests'):
            raise Stop('Preflight stopped: insufficient tests, failures, errors, skips or warnings.')
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        raise Stop('Preflight stopped: connection suite execution/result unavailable.') from None
    print('Verified: default direct Python reads; complete orders and station census; independent collection generations.')
    return python


def service_state(service):
    if service not in SERVICES:
        raise Stop('Service is not allowed.')
    result = subprocess.run(['systemctl', 'show', service, '--no-pager',
        '--property=ActiveState,SubState,ExecMainStartTimestampMonotonic'],
        cwd=ROOT, capture_output=True, text=True, timeout=15, check=False)
    try:
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines())
        stamp = int(fields['ExecMainStartTimestampMonotonic'])
        if (result.returncode or result.stderr or fields['ActiveState'] != 'active'
                or fields['SubState'] != 'running' or stamp <= 0):
            raise ValueError()
    except (KeyError, ValueError):
        raise Stop('Service state/start time unavailable or service is not active/running.') from None
    print(f'{service}: active/running, start_monotonic_us={stamp}')
    return stamp


def activate():
    print('Required services, in order: ' + ', '.join(SERVICES))
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise Stop('No interactive Terminal: no restart performed.')
    try:
        answer = input('Type RESTART to restart exactly these services: ')
    except EOFError:
        answer = ''
    if answer != 'RESTART':
        raise Stop('Confirmation declined: no restart performed.')
    # Check every service before touching the first one; no retries or rollback.
    before = {service: service_state(service) for service in SERVICES}
    checked_sources()
    for service in SERVICES:
        result = subprocess.run(['sudo', 'systemctl', 'restart', service],
                                cwd=ROOT, timeout=120, check=False)
        if result.returncode:
            raise Stop('Restart failed or uncertain; stopped without retry or rollback.')
        after = service_state(service)
        if after <= before[service]:
            raise Stop('New service start not proven; stopped without retry or rollback.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        python = preflight()
        if not args.verify_only:
            activate()
        else:
            print('Verify-only: observing existing dashboard; no service operations.')
        print('Bounded local GET validation: 3 new direct reads per source; upstream audit and optional coverage remain unverified.')
        result = subprocess.run([str(python), '-m', 'dashboard.verify_live', '--wait', '600'],
                                cwd=ROOT, timeout=620, check=False)
        if result.returncode:
            raise Stop('Live validation failed or timed out; no retry or rollback.')
        return 0
    except Stop as exc:
        print(str(exc))
    except (OSError, subprocess.SubprocessError, KeyboardInterrupt):
        print('Operation failed, interrupted or uncertain; stopped without retry or rollback.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
