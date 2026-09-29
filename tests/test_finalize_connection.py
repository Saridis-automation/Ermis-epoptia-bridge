"""Local-only activation tests: every service command and live validator is mocked."""
import contextlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard import finalize_connection as launcher
from dashboard import verify_live


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_reviewed_sources_present(self):
        launcher.checked_sources()

    def test_false_report_stops(self):
        with patch.object(Path, 'read_text', return_value='{"fix_implemented": false}'), patch.object(launcher.subprocess, 'run') as run:
            self.assertEqual(launcher.main([]), 1)
            run.assert_not_called()

    def test_missing_or_changed_source_stops(self):
        for replacement in (b'not the reviewed fix',):
            with patch.object(Path, 'read_bytes', return_value=replacement), patch.object(launcher.subprocess, 'run') as run:
                self.assertEqual(launcher.main([]), 1)
                run.assert_not_called()

    def test_preflight_failure_never_activates(self):
        with patch.object(launcher, 'preflight', side_effect=launcher.Stop('failed')), patch.object(launcher, 'activate') as activate, patch.object(launcher.subprocess, 'run') as run:
            self.assertEqual(launcher.main([]), 1)
            activate.assert_not_called()
            run.assert_not_called()

    def test_suite_counts_fail_closed(self):
        good = dict(tests=15, failures=0, errors=0, skips=0, expected_failures=0, unexpected_successes=0, warnings=0)
        for key in good:
            counts = dict(good, **{key: 0 if key == 'tests' else 1})
            with self.subTest(key=key), patch.object(launcher, 'checked_sources'), patch.object(launcher.subprocess, 'run', return_value=Mock(returncode=0, stderr='', stdout=json.dumps(counts))) as run:
                self.assertEqual(launcher.main([]), 1)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0][1], '-c')

    def test_noninteractive_or_declined_never_runs_commands(self):
        for interactive, answer in ((False, 'RESTART'), (True, 'no')):
            with patch.object(launcher.sys.stdin, 'isatty', return_value=interactive), patch.object(launcher.sys.stdout, 'isatty', return_value=interactive), patch('builtins.input', return_value=answer), patch.object(launcher.subprocess, 'run') as run:
                with self.assertRaises(launcher.Stop):
                    launcher.activate()
                run.assert_not_called()

    def activate_context(self, outcomes):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(launcher.sys.stdin, 'isatty', return_value=True))
        stack.enter_context(patch.object(launcher.sys.stdout, 'isatty', return_value=True))
        stack.enter_context(patch('builtins.input', return_value='RESTART'))
        stack.enter_context(patch.object(launcher, 'checked_sources'))
        return stack.enter_context(patch.object(launcher.subprocess, 'run', side_effect=outcomes))

    @staticmethod
    def state(stamp=1, active='active'):
        return Mock(returncode=0, stderr='', stdout=f'ActiveState={active}\nSubState=running\nExecMainStartTimestampMonotonic={stamp}\n')

    def test_exact_service_order_and_start_evidence(self):
        self.assertEqual(launcher.SERVICES, ('ermis-dashboard.service',))
        run = self.activate_context([self.state(), Mock(returncode=0), self.state(2)])
        launcher.activate()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([c for c in commands if c[0] == 'sudo'], [
            ['sudo', 'systemctl', 'restart', name] for name in launcher.SERVICES])
        self.assertEqual([c[2] if c[0] == 'systemctl' else c[3] for c in commands],
                         [launcher.SERVICES[i] for i in (0, 0, 0)])
        self.assertTrue(all(not c.kwargs.get('shell', False) for c in run.call_args_list))

    def test_restart_failure_or_uncertainty_stops(self):
        for outcome in (Mock(returncode=1), subprocess.TimeoutExpired('mock', 1)):
            run = self.activate_context([self.state(), outcome])
            with self.assertRaises((launcher.Stop, subprocess.TimeoutExpired)):
                launcher.activate()
            self.assertEqual(run.call_count, 2)

    def test_unchanged_start_stops_before_second_restart(self):
        run = self.activate_context([self.state(), Mock(returncode=0), self.state()])
        with self.assertRaises(launcher.Stop):
            launcher.activate()
        self.assertEqual(run.call_count, 3)

    def test_bad_initial_state_prevents_restart(self):
        run = self.activate_context([self.state(active='inactive')])
        with self.assertRaises(launcher.Stop):
            launcher.activate()
        self.assertEqual(run.call_count, 1)

    def test_verify_only_calls_existing_validator_only(self):
        with patch.object(launcher, 'preflight', return_value=launcher.ROOT / 'venv/bin/python'), patch.object(launcher, 'activate') as activate, patch.object(launcher.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            self.assertEqual(launcher.main(['--verify-only']), 0)
            activate.assert_not_called()
            self.assertEqual(run.call_args.args[0], [str(launcher.ROOT / 'venv/bin/python'), '-m', 'dashboard.verify_live', '--wait', '600'])
            self.assertEqual(run.call_count, 1)

    def test_activation_failure_prevents_live_validation(self):
        with patch.object(launcher, 'preflight'), patch.object(launcher, 'activate', side_effect=launcher.Stop('uncertain')), patch.object(launcher.subprocess, 'run') as run:
            self.assertEqual(launcher.main([]), 1)
            run.assert_not_called()

    def test_live_failure_no_retry(self):
        with patch.object(launcher, 'preflight', return_value=Path('venv/bin/python')), patch.object(launcher.subprocess, 'run', return_value=Mock(returncode=1)) as run:
            self.assertEqual(launcher.main(['--verify-only']), 1)
            self.assertEqual(run.call_count, 1)

    def test_validator_http_is_bounded_get_and_safe(self):
        with patch.object(verify_live, 'build_opener') as opener, patch.object(verify_live, 'monotonic', side_effect=[0, 0, 0, 2, 2]):
            opener.return_value.open.side_effect = OSError('private payload')
            self.assertEqual(verify_live.main(['--wait', '1']), 1)
            call = opener.return_value.open.call_args
            self.assertEqual(call.args[0].method, 'GET')
            self.assertEqual(call.args[0].full_url, 'http://127.0.0.1:8010/api/dashboard')
            self.assertLessEqual(call.kwargs['timeout'], 5)

    def test_validator_discloses_limits_and_reset(self):
        observer = verify_live.Observer()
        with patch.object(verify_live, 'consistency', return_value=True):
            for generation in (1, 2, 3, 1):
                model = {'sources': {name: dict(generation=generation, last_success=f'2026-09-09T00:00:0{generation}+00:00', state='available', stale=False) for name in verify_live.SOURCES}}
                report = observer.observe(model, 1)
            self.assertFalse(report['passed'])
            self.assertFalse(report['upstream_read_verified'])
            self.assertEqual(report['coverage']['capacity'], 'unverified')
            self.assertEqual(list(observer.successes.values()), [0, 0])
