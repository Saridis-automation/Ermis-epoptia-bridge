"""Isolated runner regressions: no host parser or live state access."""
from contextlib import ExitStack
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from admin_bootstrap import login_diagnose as d, login_apparmor as aa

ROOT = Path(__file__).resolve().parents[1]
OPTIONS = ['--config-file=/dev/null', '-Q', '-K', '--abort-on-error']


class Plumbing(unittest.TestCase):
    def guard(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(d, 'opened', return_value=123))
        stack.enter_context(patch.object(d, 'no_capabilities'))
        stack.enter_context(patch.object(os, 'close'))
        stack.enter_context(patch.object(os, 'fstat', return_value=SimpleNamespace(
            st_mode=stat.S_IFREG | 0o755, st_uid=0)))
        for obj, names in ((Path, ('write_bytes', 'write_text', 'touch', 'mkdir')),
                           (tempfile, ('NamedTemporaryFile', 'mkstemp', 'mkdtemp')),
                           (os, ('write', 'chmod', 'chown', 'unlink', 'rename', 'mkdir'))):
            for name in names:
                stack.enter_context(patch.object(obj, name, side_effect=AssertionError('mutation')))
        return stack

    def test_exact_candidate_and_status_only_classification(self):
        candidate = aa.profile(dict(d.PINNED, sha256='a' * 64, tree_sha256='b' * 64))
        for rc, category in ((0, 'ok'), (1, 'compile-failure'), (23, 'compile-failure'),
                             (124, 'timeout'), (126, 'exec-error'), (127, 'exec-error'),
                             (137, 'signal-error'), (-9, 'signal-error')):
            with self.subTest(rc=rc), self.guard():
                calls = []
                def run(command, **kw):
                    self.assertEqual(command, d.PARSER_COMMAND)
                    self.assertEqual(kw['env'], {'PATH': '/usr/sbin:/usr/bin:/bin', 'LANG': 'C'})
                    self.assertEqual(kw['stdout'], subprocess.DEVNULL)
                    self.assertEqual(kw['stderr'], subprocess.DEVNULL)
                    for key in ('cwd', 'timeout', 'preexec_fn', 'user', 'pass_fds'):
                        self.assertNotIn(key, kw)
                    calls.append(kw['input'])
                    return SimpleNamespace(returncode=0 if len(calls) == 1 else rc,
                                           stderr=b'unknown option syntax error Permission denied')
                details = {}
                with patch.object(subprocess, 'run', side_effect=run):
                    if rc:
                        with self.assertRaises(d.Failure):
                            d.parser(candidate, details)
                    else:
                        d.parser(candidate, details)
                self.assertEqual(calls, [b'profile ermis-diagnose-probe { }\n', candidate])
                self.assertEqual(details['parser-options'], 'ok')
                self.assertEqual(details['parser-environment'], 'ok')
                self.assertEqual(details['candidate-compile'], category)

    def test_failed_environment_never_runs_candidate(self):
        for outcome, category in (
                (PermissionError('private'), 'exec-error'),
                (subprocess.TimeoutExpired('private', 20), 'timeout'),
                (subprocess.SubprocessError('private'), 'runtime-error'),
                (SimpleNamespace(returncode=1), 'environment-failure'),
                (SimpleNamespace(returncode=124), 'timeout'),
                (SimpleNamespace(returncode=127), 'exec-error'),
                (SimpleNamespace(returncode=141), 'signal-error')):
            with self.subTest(category=category), self.guard():
                details = {}
                with patch.object(subprocess, 'run', side_effect=
                                  outcome if isinstance(outcome, Exception) else lambda *a, **k: outcome) as run:
                    with self.assertRaises(d.Failure):
                        d.parser(b'candidate', details)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(details['parser-environment'], 'failed')
                self.assertEqual(details['parser-runtime'], category)
                self.assertEqual(details['candidate-compile'], 'not-reached')
                self.assertNotIn('private', repr(details))

    def shell(self, script, data=b'', errexit=True, pipefail=True):
        return subprocess.run(['/bin/bash', '--noprofile', '--norc',
                               '-e' if errexit else '+e', '-o' if pipefail else '+o',
                               'pipefail', '-c', script], input=data, cwd=ROOT,
                              env={'PATH': '/unusable', 'LANG': 'C', 'HOME': '/inaccessible',
                                   'TMPDIR': '/inaccessible', 'SUDO_USER': 'root', 'SUDO_UID': '0'},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)

    def test_exact_options_and_anonymous_stdin(self):
        candidate = aa.profile(dict(d.PINNED, sha256='a' * 64, tree_sha256='b' * 64))
        for payload in (b'profile ermis-diagnose-probe { }\n', candidate):
            code = ('import os,stat,sys; '
                    'assert stat.S_ISFIFO(os.fstat(0).st_mode); '
                    f'assert os.geteuid() == {os.geteuid()}; '
                    f'assert sys.argv[1:] == {OPTIONS!r}; '
                    f'assert sys.stdin.buffer.read() == {payload!r}')
            stub = '/usr/sbin/apparmor_parser() { ' + shlex.quote(sys.executable) + ' -I -B -c ' + shlex.quote(code) + ' "$@"; }\n'
            result = self.shell(stub + d.PARSER_PIPELINE, payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b'')

    def test_consumer_status_and_exact_shell_option_restoration(self):
        # A shell function substitutes only the absolute binary at the exec edge.
        # The production pipeline and immediate PIPESTATUS assignment are intact.
        for errexit in (False, True):
            for pipefail in (False, True):
                for producer in ('return 0', 'return 23', 'trap - PIPE; kill -s PIPE "$BASHPID"'):
                    for rc in (0, 1, 23, 124, 126, 127, 137):
                        with self.subTest(errexit=errexit, pipefail=pipefail, producer=producer, rc=rc):
                            prelude = f'''/bin/cat() {{ {producer}; }}
/usr/sbin/apparmor_parser() {{ return {rc}; }}
options_before=$-
set_before=$(set +o)
trap 'saved=$?; [[ $- == "$options_before" && $(set +o) == "$set_before" ]] || exit 99; exit "$saved"' EXIT
'''
                            result = self.shell(prelude + d.PARSER_PIPELINE,
                                                errexit=errexit, pipefail=pipefail)
                            self.assertEqual(result.returncode, rc, result.stderr)

    def test_success_ignores_real_cat_sigpipe(self):
        # More than pipe capacity: early parser success closes its read end.
        stub = '/usr/sbin/apparmor_parser() { return 0; }\n'
        result = self.shell(stub + d.PARSER_PIPELINE, b'x' * (1024 * 1024))
        self.assertEqual(result.returncode, 0, result.stderr)
