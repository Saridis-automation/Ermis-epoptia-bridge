"""Output-shaped fixtures only: no host units, processes, or AppArmor reads."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from admin_bootstrap import login_diagnose as d
from test_login_kernel_stream import kernel_fixture


class OutputCompatibility(unittest.TestCase):
    def kernel(self, raw, records):
        return kernel_fixture(raw, records)

    def test_complete_modes_names_and_whitespace(self):
        for mode in ('enforce', 'complain', 'kill', 'unconfined', 'prompt', 'user', 'audit'):
            row = ('unrelated profile (helper)//hat', mode, 'none')
            for ending in ('\n', '  \t\r\n', '\t\n'):
                raw = (row[0] + ' (' + mode + ')' + ending).encode()
                self.assertEqual(self.kernel(raw, [row]), ('absent', 'complete-listing'))
        self.assertEqual(self.kernel(b'', []), ('absent', 'complete-listing'))

    def test_invalid_never_absent(self):
        for raw in (b'other (enforce)', b'other\n', b'other (future)\n',
                    b'other (enforce) junk\n', b'other\x00 (enforce)\n', b'\xff (enforce)\n',
                    b'other (enforce)\r', b'\n'):
            self.assertEqual(self.kernel(raw, [])[0], 'unknown')
        self.assertEqual(self.kernel(b'other (enforce)\n', [])[0], 'unknown')
        self.assertEqual(kernel_fixture(b'', [], close_error=OSError())[0], 'unknown')

    def test_exact_legacy_and_duplicate_after_validation(self):
        exact = (d.expected_profile(), 'unconfined', d.PINNED['executable'])
        for rows, status in (([exact], 'present-exact'), ([exact, exact], 'ambiguous'),
                             ([(str(d.BASE) + '/old', 'prompt', 'none')], 'present-conflict')):
            raw = ''.join(name + ' (' + mode + ') \r\n' for name, mode, _ in rows).encode()
            self.assertEqual(self.kernel(raw, rows)[0], status)

    def activity(self, service, socket=None, processes=None, rc=0):
        if socket is None:
            socket = b'LoadState=loaded\nActiveState=active\nSubState=listening\n'
        if processes is None:
            processes = dict(login=('inactive', 'complete-scan'), browser=('inactive', 'complete-scan'))
        with patch.object(d.subprocess, 'run', side_effect=[
                SimpleNamespace(returncode=rc, stdout=service), SimpleNamespace(returncode=0, stdout=socket)]), \
             patch.object(d, 'process_probe', return_value=processes) as scan:
            result = d.activity_probe()
            scan.assert_called_once()
            return result

    def test_minimal_inactive_and_not_found_with_socket_infrastructure(self):
        for load in ('loaded', 'not-found'):
            raw = f'LoadState={load}\nActiveState=inactive\nSubState=dead\nMainPID=0\n'.encode()
            self.assertEqual(self.activity(raw)['login-activity'], 'inactive')
            socket = b'LoadState=not-found\nActiveState=inactive\nSubState=dead\n'
            self.assertEqual(self.activity(raw, socket)['login-activity'], 'inactive')

    def test_missing_core_contradictions_errors_and_partial_scan(self):
        raw = b'LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\n'
        cases = [raw.replace(line, b'') for line in raw.splitlines(keepends=True)]
        cases += [raw[:-1], raw + b'MainPID=0\n', raw.replace(b'dead', b'running'),
                  raw.replace(b'MainPID=0', b'MainPID=17'), raw.replace(b'inactive', b'activating'),
                  raw + b'ControlGroup=/\n', raw.replace(b'dead', b'de\x00ad')]
        for case in cases:
            self.assertEqual(self.activity(case)['login-activity'], 'unknown')
        self.assertEqual(self.activity(raw, rc=1)['login-activity'], 'unknown')
        partial = dict(login=('unknown', 'unknown'), browser=('inactive', 'complete-scan'))
        self.assertEqual(self.activity(raw, processes=partial)['login-activity'], 'unknown')

    def test_active_requires_verified_main_pid(self):
        raw = b'LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=17\n'
        self.assertEqual(self.activity(raw)['login-activity'], 'unknown')
        processes = dict(login=('active', 'matching-process'), browser=('inactive', 'complete-scan'))
        processes['main-pid'] = ('active', 'verified-main-pid')
        self.assertEqual(self.activity(raw, processes=processes)['login-activity'], 'active')
        self.assertEqual(self.activity(raw.replace(b'loaded', b'not-found'), processes=processes)['login-activity'], 'unknown')

    def test_pid_and_partial_proc_verification(self):
        class Entries:
            def __enter__(self):
                return iter([SimpleNamespace(name='17'), SimpleNamespace(name='18')])
            def __exit__(self, *args):
                pass
        owned = ('0::/system.slice/' + d.SERVICE + '\n').encode()
        for records, expected in (([owned, b'0::/other\n'], 'active'),
                                  ([b'0::/other\n'] * 2, 'unknown'),
                                  ([owned, b'partial'], 'unknown'),
                                  ([owned, b'0::/other\x00\n'], 'unknown')):
            with patch.object(d.os, 'scandir', return_value=Entries()), \
                 patch.object(d.os, 'getpid', return_value=99), \
                 patch.object(d, 'proc_bytes', side_effect=records), \
                 patch.object(d.os, 'readlink', return_value='/usr/bin/node'):
                self.assertEqual(d.process_probe(main_pid=17)['main-pid'][0], expected)


if __name__ == '__main__':
    unittest.main()
