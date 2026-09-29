"""Public shell entrypoint with hermetic boundaries; no host diagnose/install."""
import builtins
from contextlib import ExitStack
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
BOOT = ROOT / 'admin_bootstrap/bootstrap.sh'


def child(case, args):
    case, _, stream_case = case.partition('+')
    assert args[:4] == ['/usr/bin/python3', '-I', '-B', str(BOOT.with_suffix('.py'))]
    sys.path.insert(0, str(ROOT))
    from admin_bootstrap import login_diagnose as d, login_apparmor as aa, login_bootstrap as lb
    diagnostic = (ROOT / 'admin_bootstrap/login_diagnose.py').read_bytes()
    code = compile(BOOT.with_suffix('.py').read_bytes(), str(BOOT.with_suffix('.py')), 'exec')
    sources = {name: (ROOT / name).read_bytes() for name in d.SOURCE_PATHS}
    sources['admin_bootstrap/login_manifest.json'] = (ROOT / 'admin_bootstrap/login_manifest.json').read_bytes()
    record = dict(d.PINNED, sha256='a' * 64, tree_sha256='b' * 64)
    fingerprint = hashlib.sha256(sources['admin_bootstrap/login_manifest.json']).hexdigest()
    ledger = dict(schema=2, source=fingerprint, status='idle', **{'tree-stage': 'ok'},
                  generation='reached', candidate='.ermis-login-fixture', staged=None, cleaned=True)
    files = {}
    rows = []
    installed = case.startswith('binding-') or case in ('candidate', 'prior', 'mixed', 'stale-receipt', 'race', 'kernel-unbound', 'kernel-foreign', 'installed-denied')
    prior = {**record, 'sha256': 'c' * 64} if case == 'prior' else record
    profile = aa.profile(prior)
    if installed:
        files[d.PROFILE] = profile
        policy = json.dumps(aa.receipt_record(prior)).encode()
        if case == 'kernel-unbound':
            value = json.loads(policy)
            value.pop('policy_sha256')
            policy = json.dumps(value).encode()
        artifacts = lb.artifacts()
        files.update({p: data for p, (data, mode) in artifacts.items()})
        login = json.dumps({'source': fingerprint, 'files': {str(p): hashlib.sha256(data).hexdigest() for p, (data, mode) in artifacts.items()}}).encode()
        if case == 'binding-files':
            login = json.dumps({'source': fingerprint, 'files': {'fixture': 'd' * 64}}).encode()
        if case == 'binding-source':
            login = login.replace(fingerprint.encode(), b'f' * 64)
        if case.startswith('binding-record-'):
            value = json.loads(policy)
            value.pop(case.removeprefix('binding-record-'))
            policy = json.dumps(value).encode()
        ready = json.dumps({'source': fingerprint, 'receipts': {
            str(d.POLICY): hashlib.sha256(policy).hexdigest(), str(d.RECEIPT): hashlib.sha256(login).hexdigest()}}).encode()
        files.update({d.POLICY: policy, d.RECEIPT: login, d.READY: ready})
        if case == 'binding-bytes':
            files[d.PROFILE] += b'changed'
        if case == 'binding-missing':
            files.pop(next(iter(d.INSTALLED_FILES)))
        rows = [(aa.profile_name(record), 'unconfined', record['executable'], 'e' * 40)]
        if case == 'kernel-foreign':
            rows[0] = (rows[0][0] + '-foreign', *rows[0][1:])
        if case == 'mixed':
            files.pop(d.PROFILE)
        if case == 'stale-receipt':
            files[d.READY] = b'{}'
    if case in ('conflict', 'conflict-parse'):
        rows = [('foreign', 'enforce', record['executable'], 'e' * 40)]
    if case in ('rollback', 'stage-failed', 'not-reached', 'active', 'candidate-denied', 'candidate-enoent', 'candidate-link', 'candidate-special'):
        if case in ('stage-failed', 'not-reached'):
            ledger.update({'tree-stage': 'failed' if case == 'stage-failed' else 'not-reached',
                           'generation': 'not-reached', 'candidate': None})
        if case == 'active':
            ledger['status'] = 'active'
        if case.startswith('candidate-'):
            ledger.update(status='active', cleaned=False)
        files[d.LEDGER] = json.dumps(ledger).encode()
    if stream_case == 'duplicate':
        rows = [('synthetic', 'enforce', 'none', 'e' * 40)] * 2
    if stream_case.startswith('large-'):
        rows = [(f'synthetic.snap.application.{i:06d}.worker', 'enforce', 'none', 'e' * 40)
                for i in range(24000)] + rows
        if stream_case == 'large-conflict':
            rows.append(('foreign', 'enforce', record['executable'], 'e' * 40))
    directory = d.PROFILES.parent / 'policy/profiles'
    files[d.PROFILES] = ''.join(n + ' (' + m + ')\n' for n, m, a, h in rows).encode()
    if stream_case.startswith('large-'):
        assert 1048576 < len(files[d.PROFILES]) < 16 * 1024 * 1024
        assert len(rows) < 131072
    if stream_case in ('byte-limit', 'line-limit', 'per-line-limit', 'read-error', 'invalid-line'):
        files[d.PROFILES] += b'synthetic (enforce)\n'
        if stream_case == 'invalid-line':
            files[d.PROFILES] += b'invalid\n'
    for index, row in enumerate(rows):
        files.update({directory / str(index) / key: value.encode()
                      for key, value in zip(('name', 'mode', 'attach', 'sha1'), row)})
    if case in ('legacy-stale', 'invalid-stale', 'committed-missing'):
        legacy = ('abi <abi/4.0>,\n# ermis-epoptia-login owned v1\n# playwright=1.63.0 chromium=153.0.8010.12 revision=1243 sha256=' + 'a' * 64 + '\nprofile "/home/ermis/.cache/ms-playwright/chromium-1243/chrome-linux/chrome" flags=(unconfined) {\n  userns,\n}\n').encode()
        value = dict(aa.legacy_profile(legacy), profile_sha256=hashlib.sha256(legacy).hexdigest())
        if case == 'invalid-stale':
            value = {}
        if case == 'committed-missing':
            value = aa.receipt_record(record)
        files[d.POLICY] = json.dumps(value).encode()
    before = files.copy()
    real_exec = builtins.exec
    checked = []
    with ExitStack() as stack:
        stack.enter_context(patch.object(os, 'geteuid', return_value=0))
        def execute(source, namespace, *rest):
            result = real_exec(source, namespace, *rest)
            if getattr(source, 'co_filename', None) != '<verified-diagnose>':
                return result
            checked.append(namespace)
            def source_read(root, name):
                assert root == ROOT
                assert name in sources
                if case.startswith('manifest-'):
                    raise OSError({'denied': errno.EACCES, 'missing': errno.ENOENT, 'link': errno.ELOOP, 'io': errno.EIO, 'notdir': errno.ENOTDIR, 'perm': errno.EPERM}[case.removeprefix('manifest-')], 'private')
                return sources[name]
            def read(path, **kwargs):
                if case.startswith('binding-metadata-') and path == d.PROFILE:
                    raise namespace['InvariantError'](case.removeprefix('binding-metadata-'))
                if case == 'installed-denied' and path == d.PROFILE:
                    raise OSError(errno.EACCES, 'private')
                if ledger['candidate'] and path == d.PROFILE.parent / ledger['candidate']:
                    if case == 'candidate-denied':
                        raise OSError(errno.EACCES, 'private')
                    if case == 'candidate-link':
                        raise namespace['InvariantError']('unsafe-link')
                    if case == 'candidate-special':
                        raise namespace['InvariantError']('special')
                if path not in files:
                    raise OSError(errno.ENOENT, 'private')
                return files[path]
            def listing(path):
                if path == directory:
                    return [str(i) for i in range(len(rows))]
                if path == d.PROFILE.parent:
                    return [d.PROFILE.name] if d.PROFILE in files else []
                assert path in (d.BASE, d.POLICY.parent)
                return ['.stage-unowned'] if case == 'residue' and path == d.BASE else []
            tree_calls = []
            def checked_tree(path, uid, tree):
                assert path in (d.TARGET, ROOT / 'fixture-tree')
                tree_calls.append(path)
                if path == d.TARGET:
                    if not installed:
                        raise OSError(errno.ENOENT, 'private')
                    return {**prior, 'sha256': 'f' * 64} if case == 'race' and tree_calls.count(path) > 1 else prior
                if case == 'source-identity':
                    raise OSError(errno.EACCES, 'private')
                if case in ('source-denied', 'source-enoent', 'source-io', 'source-link', 'source-perm', 'source-notdir'):
                    raise OSError({'source-denied': errno.EACCES, 'source-enoent': errno.ENOENT, 'source-io': errno.EIO, 'source-link': errno.ELOOP, 'source-perm': errno.EPERM, 'source-notdir': errno.ENOTDIR}[case], 'private')
                if case.startswith('source-invariant-'):
                    raise namespace['InvariantError'](case.removeprefix('source-invariant-'))
                return record
            original_modules = namespace['source_modules']
            def modules(s):
                tree, module = original_modules(s)
                tree['source'] = lambda root: (ROOT / 'fixture-tree', 1000)
                generator = module['profile']
                if case in ('identity', 'source-identity'):
                    module['profile'] = lambda record: generator(record).replace(b'profile "ermis-', b'profile "other-')
                if case == 'rules':
                    module['profile'] = lambda record: generator(record).replace(b'userns,', b'network,')
                return tree, module
            original_observation = namespace['model_observation']
            def observation():
                details, errors, phases, history, conflict = original_observation()
                if case.startswith('gate-'):
                    key = case.removeprefix('gate-')
                    if key == 'conflict':
                        conflict = 'possible-conflict'
                    else:
                        details[key] = 'unknown'
                return details, errors, phases, history, conflict
            namespace['model_observation'] = observation
            namespace['retry_observation'] = lambda: {
                'login-activity': 'active' if case == 'login-active' else 'unknown' if case == 'login-unknown' else 'inactive',
                'browser-activity': 'active' if case == 'browser-active' else 'unknown' if case == 'browser-unknown' else 'inactive',
                'install-atomic-recheck': 'unsupported' if case == 'recheck' else 'verified'}
            namespace['activity_probe'] = lambda: {k: v for k, v in namespace['retry_observation']().items() if k != 'install-atomic-recheck'}
            namespace.update(source_read=source_read, source_modules=modules, read=read, listing=listing,
                             checked_tree=checked_tree, target_absent=lambda: not installed)
            # Exercise actual parser validation, mocking only process/FD edges.
            namespace['opened'] = lambda path, **kwargs: 123
            def getxattr(fd, name):
                assert (fd, name) == (123, 'security.capability')
                raise OSError(errno.ENODATA, 'fixture has no capabilities')
            stack.enter_context(patch.object(os, 'getxattr', side_effect=getxattr))
            info = SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0,
                st_nlink=1, st_dev=1, st_ino=1, st_gid=0, st_size=0,
                st_mtime_ns=0, st_ctime_ns=0, st_atime_ns=0)
            stat_calls = 0
            def fstat(fd):
                nonlocal stat_calls
                stat_calls += 1
                if stream_case == 'identity-drift' and stat_calls % 2 == 0:
                    return SimpleNamespace(**{**vars(info), 'st_ino': 2})
                if stream_case == 'stat-error':
                    raise OSError(errno.EIO, 'private /synthetic/path')
                return info
            stack.enter_context(patch.object(os, 'fstat', side_effect=fstat))
            def lstat(path):
                assert path == d.PROFILES
                return info
            stack.enter_context(patch.object(Path, 'lstat', lstat))
            stream = None
            interrupted = False
            def opened(path, **kwargs):
                nonlocal stream, interrupted
                if stream_case == 'open-permission':
                    raise PermissionError(errno.EACCES, 'private /synthetic/path')
                stream = io.BytesIO(files[d.PROFILES])
                interrupted = False
                return 123
            namespace['opened'] = opened
            def direct_read(fd, size):
                nonlocal interrupted
                assert fd == 123 and size == namespace['KERNEL_CHUNK']
                if stream_case.startswith('errno-'):
                    raise OSError(getattr(errno, stream_case.removeprefix('errno-')), 'private /synthetic/path synthetic (enforce)')
                if stream_case == 'eintr-handled' and not interrupted:
                    interrupted = True
                    raise InterruptedError(errno.EINTR, 'private /synthetic/path')
                if stream_case == 'read-error' and stream.tell():
                    raise OSError('private fixture')
                return stream.read(size)
            stack.enter_context(patch.object(os, 'read', side_effect=direct_read))
            stack.enter_context(patch.object(os, 'fdopen', side_effect=AssertionError('buffering forbidden')))
            for kind, key, limit in (
                    ('byte-limit', 'KERNEL_BYTES', len(files[d.PROFILES]) - 1),
                    ('line-limit', 'KERNEL_LINES', len(rows)),
                    ('per-line-limit', 'KERNEL_LINE_BYTES', 8)):
                if stream_case == kind:
                    namespace[key] = limit
            class Entries:
                def __enter__(self):
                    return (SimpleNamespace(name=str(i)) for i in range(len(rows)))
                def __exit__(self, *args):
                    pass
            def scandir(path):
                assert path == directory
                return Entries()
            stack.enter_context(patch.object(os, 'scandir', side_effect=scandir))
            stack.enter_context(patch.object(os, 'close', side_effect=
                OSError(errno.EIO, 'private /synthetic/path') if stream_case == 'close-error' else None))
            if stream_case in ('helper-error', 'helper-unknown', 'helper-no-errno'):
                def summary():
                    if stream_case == 'helper-no-errno':
                        raise OSError('private /synthetic/path')
                    raise (OSError(errno.EBADF, 'private /synthetic/path') if stream_case == 'helper-error'
                           else ValueError('private /synthetic/path synthetic (enforce)'))
                namespace['kernel_summary'] = summary
            if stream_case in ('missing-detail', 'unrecognized-detail'):
                def probe():
                    if stream_case == 'missing-detail':
                        return 'unknown', 'read-error'
                    return namespace['KernelProbeResult']('unknown', 'read-error',
                        'private /synthetic/path synthetic (enforce)')
                namespace['kernel_probe'] = probe
            def run(command, **kwargs):
                assert command == namespace['PARSER_COMMAND']
                assert 'preexec_fn' not in kwargs and 'timeout' not in kwargs
                assert 'cwd' not in kwargs
                assert kwargs['stderr'] == subprocess.DEVNULL
                if case == 'parser-timeout':
                    raise subprocess.TimeoutExpired('private', 20)
                if case == 'parser-signal':
                    return SimpleNamespace(returncode=-9, stdout=b'', stderr=b'private')
                failed = case in ('parse', 'conflict-parse') and b'ermis-diagnose-probe' not in kwargs.get('input', b'')
                return SimpleNamespace(returncode=int(failed), stdout=b'--config-file --skip-kernel-load --skip-cache --skip-read-cache',
                                       stderr=b'syntax error private' if failed else b'')
            stack.enter_context(patch.object(subprocess, 'run', side_effect=run))
            return result
        stack.enter_context(patch.object(builtins, 'exec', side_effect=execute))
        for owner, methods in ((builtins, ('open',)), (io, ('open',)),
                               (os, ('open', 'write', 'writev', 'chmod', 'chown', 'fchmod', 'fchown', 'unlink', 'remove', 'rename', 'replace', 'mkdir', 'system', 'utime', 'link', 'symlink', 'truncate', 'ftruncate', 'rmdir', 'setxattr', 'removexattr')),
                               (socket, ('create_connection',)), (socket.socket, ('connect', 'connect_ex', 'bind', 'listen', 'sendto')),
                               (subprocess, ('Popen',))):
            for method in methods:
                guard = stack.enter_context(patch.object(owner, method, side_effect=AssertionError('forbidden action')))
                stack.callback(guard.assert_not_called)
        namespace = {'__name__': 'entrypoint_fixture', '__file__': str(BOOT.with_suffix('.py'))}
        real_exec(code, namespace)
        def diagnostic_read(path):
            assert path == BOOT.with_name('login_diagnose.py')
            if case.startswith('module-'):
                raise OSError({'missing': errno.ENOENT, 'denied': errno.EACCES, 'perm': errno.EPERM, 'link': errno.ELOOP, 'notdir': errno.ENOTDIR, 'io': errno.EIO}[case.removeprefix('module-')], 'private')
            return diagnostic
        namespace['diagnostic_read'] = diagnostic_read
        for method in ('login_operation', 'main', 'diagnose', 'apparmor_diagnose'):
            namespace[method] = Mock(side_effect=AssertionError('forbidden action'))
            stack.callback(namespace[method].assert_not_called)
        status = namespace['cli'](args[4:])
        assert (checked or case.startswith('module-')) and before == files
        return status


class Entrypoint(unittest.TestCase):
    def invoke(self, case, scoped=True):
        shell = '''exec() { /usr/bin/python3 -I -B "$adapter" --child "$fixture_case" "$@"; exit "$?"; }
. "$0"
'''
        result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', shell, str(BOOT), 'diagnose',
                                 *(['apparmor'] if scoped else [])], cwd=ROOT,
                                env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'adapter': str(Path(__file__).resolve()),
                                     'fixture_case': case}, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.stderr, '')
        self.assertEqual(result.returncode, 0 if case in ('candidate', 'candidate+large-present') else 1)
        self.assertNotRegex(result.stdout, r'private|/synthetic/|synthetic \(enforce\)|/home/|/opt/|[a-f0-9]{40}')
        self.assertNotRegex(result.stdout, r'\[Errno|\b[0-9]+\b')
        self.assertLess(len(result.stdout), 2000)
        lines = result.stdout.splitlines()
        if not case.startswith('module-'):
            fields = dict(line.split('=', 1) for line in lines if '=' in line)
            self.assertIn(fields['kernel-probe'], (
                'absent', 'present-exact', 'present-conflict', 'ambiguous', 'unknown',
                'disabled', 'securityfs-unmounted', 'permission-denied', 'probe-error'))
            self.assertIn('kernel-probe-reason', fields)
            self.assertNotIn('malformed-partial', fields.values())
        return lines

    def test_kernel_read_diagnostics_public_entrypoint(self):
        cases = [
            ('open-permission', 'open', 'permission', 'open-permission-denied'),
            ('identity-drift', 'identity', 'identity', 'fd-identity-error'),
            ('close-error', 'close', 'io', 'fd-close-error-EIO'),
            ('stat-error', 'unknown', 'io', 'stat-error-EIO'),
            ('invalid-line', 'parse', 'malformed', 'invalid-line'),
            ('byte-limit', 'parse', 'limit', 'total-byte-limit'),
            ('line-limit', 'parse', 'limit', 'line-count-limit'),
            ('per-line-limit', 'parse', 'limit', 'per-line-limit'),
            ('helper-error', 'unknown', 'bad-fd', 'read-error'),
            ('helper-unknown', 'helper', 'invalid', 'read-error'),
            ('helper-no-errno', 'unknown', 'unknown', 'read-error'),
            ('missing-detail', 'unknown', 'unknown', 'read-error'),
            ('unrecognized-detail', 'unknown', 'unknown', 'read-error'),
        ]
        for symbol, category in (('EAGAIN', 'would-block'), ('EBADF', 'bad-fd'),
                ('EIO', 'io'), ('ENOMEM', 'memory'), ('EFAULT', 'fault')):
            cases.append(('errno-' + symbol, 'read', category, 'profiles-read-error-' + symbol))
        for suffix, stage, category, reason in cases:
            for scoped in (False, True):
                with self.subTest(suffix=suffix, scoped=scoped):
                    lines = self.invoke('idle+' + suffix, scoped)
                    for field, value in (('kernel-probe', 'unknown'),
                            ('kernel-probe-reason', reason), ('kernel-read-stage', stage),
                            ('kernel-read-category', category), ('profile-kernel', 'unknown'),
                            ('retry', 'blocked'), ('primary', 'INSUFFICIENT_EVIDENCE')):
                        self.assertIn(field + '=' + value, lines)
        # EINTR is retried by the existing reader; it has no exhaustion limit.
        for case, status in (('idle+eintr-handled', 'absent'), ('candidate', 'present-exact')):
            for scoped in (False, True):
                lines = self.invoke(case, scoped)
                self.assertIn('kernel-probe=' + status, lines)
                self.assertIn('kernel-read-stage=none', lines)
                self.assertIn('kernel-read-category=none', lines)

    def test_current_baselines_and_priority(self):
        cases = {
            'idle': ('clean-absent', 'NONE', True),
            'legacy-stale': ('mixed', 'INSTALLED_STATE_MIXED', False),
            'invalid-stale': ('mixed', 'INSTALLED_STATE_MIXED', False),
            'committed-missing': ('mixed', 'INSTALLED_STATE_MIXED', False),
            'rollback': ('clean-absent', 'NONE', True),
            'stage-failed': ('clean-absent', 'TREE_STAGE_FAILURE', True),
            'not-reached': ('clean-absent', 'NONE', True),
            'candidate': ('coherent-candidate', 'NONE', False),
            'prior': ('coherent-prior', 'NONE', False),
            'mixed': ('mixed', 'INSTALLED_STATE_MIXED', False),
            'stale-receipt': ('mixed', 'INSTALLED_STATE_MIXED', False),
            'conflict': ('mixed', 'INSTALLED_STATE_MIXED', False),
            'race': ('unknown', 'INSUFFICIENT_EVIDENCE', False),
            'active': ('unknown', 'INSUFFICIENT_EVIDENCE', False),
            'residue': ('unknown', 'INSUFFICIENT_EVIDENCE', False),
            'installed-denied': ('unknown', 'INSUFFICIENT_EVIDENCE', False),
            'kernel-unbound': ('mixed', 'INSTALLED_STATE_MIXED', False),
            'kernel-foreign': ('mixed', 'INSTALLED_STATE_MIXED', False),
            'source-identity': ('unknown', 'TREE_SOURCE_FAILURE', False),
            'conflict-parse': ('mixed', 'PROFILE_PARSE_FAILURE', False),
            'identity': ('unknown', 'PROFILE_IDENTITY_FAILURE', False),
            'rules': ('unknown', 'PROFILE_PARSE_FAILURE', False),
            'parse': ('unknown', 'PROFILE_PARSE_FAILURE', False),
        }
        for case, (baseline, primary, eligible) in cases.items():
            for scoped in (False, True):
                with self.subTest(case=case, scoped=scoped):
                    lines = self.invoke(case, scoped)
                    self.assertIn('baseline=' + baseline, lines)
                    self.assertIn('primary=' + primary, lines)
                    self.assertIn('retry=' + ('eligible_after_atomic_recheck' if eligible else 'blocked'), lines)
                    if case == 'committed-missing':
                        self.assertIn('receipt-classification=valid-committed-success', lines)
                        self.assertIn('baseline-blocker=committed-success-artifacts-missing', lines)
                    if case == 'legacy-stale':
                        self.assertIn('receipt-classification=stale-recognized-source-binding', lines)
                        self.assertIn('receipt-reason=legacy-non-success', lines)
                    if case == 'rollback':
                        self.assertIn('candidate-artifact=missing-after-cleanup', lines)
                    if case == 'idle':
                        self.assertIn('candidate-artifact=not-applicable', lines)
                        self.assertIn('candidate-generation=unknown', lines)
                    if case in ('idle', 'stage-failed'):
                        for field in ('parser-options', 'parser-environment',
                                      'candidate-compile', 'source-candidate-validation'):
                            self.assertIn(field + '=ok', lines)
                    if case == 'stage-failed' and scoped:
                        self.assertIn('phase-load-not-reached', lines)
                        self.assertIn('candidate-generation=not-reached', lines)

    def test_streaming_public_reason_and_baseline_contract(self):
        cases = [
            ('idle+large-absent', 'absent', 'complete-listing', 'clean-absent', True),
            ('legacy-stale+large-absent', 'absent', 'complete-listing', 'mixed', False),
            ('candidate+large-present', 'present-exact', 'verified-identity', 'coherent-candidate', False),
            ('idle+duplicate', 'ambiguous', 'duplicate-name', 'unknown', False),
            ('idle+large-conflict', 'present-conflict', 'related-identity-or-attachment', 'mixed', False),
        ]
        for base in ('idle', 'legacy-stale', 'candidate'):
            for suffix, reason in (('byte-limit', 'total-byte-limit'),
                    ('line-limit', 'line-count-limit'), ('per-line-limit', 'per-line-limit'),
                    ('read-error', 'profiles-read-error-OTHER'), ('invalid-line', 'invalid-line')):
                cases.append((base + '+' + suffix, 'unknown', reason, 'unknown', False))
        for case, status, reason, baseline, eligible in cases:
            for scoped in (False, True):
                with self.subTest(case=case, scoped=scoped):
                    lines = self.invoke(case, scoped)
                    for field, expected in (('kernel-probe', status), ('kernel-probe-reason', reason),
                            ('baseline', baseline), ('install-policy', 'first-install-only'),
                            ('retry', 'eligible_after_atomic_recheck' if eligible else 'blocked')):
                        self.assertIn(field + '=' + expected, lines)
                    if status in ('unknown', 'ambiguous'):
                        self.assertIn('profile-kernel=unknown', lines)
                        self.assertIn('baseline-blocker=profile-kernel-unproven', lines)
                        self.assertIn('primary=INSUFFICIENT_EVIDENCE', lines)
                    if case.startswith('legacy-stale'):
                        self.assertIn('receipt-classification=stale-recognized-source-binding', lines)
                        self.assertIn('receipt-reason=legacy-non-success', lines)

    def test_incomplete_bindings_and_retry_gates(self):
        cases = ['binding-files', 'binding-source', 'binding-bytes', 'binding-missing']
        cases += ['binding-metadata-' + key for key in ('owner', 'mode', 'unsafe-link', 'special')]
        cases += ['binding-record-' + key for key in
                  ('revision', 'layout', 'executable', 'tree_sha256', 'sha256',
                   'profile_sha256', 'policy_identity', 'policy_sha256', 'playwright', 'chromium', 'schema')]
        for case in cases:
            with self.subTest(case=case):
                lines = self.invoke(case)
                self.assertFalse(any(line.startswith('baseline=coherent') for line in lines))
                self.assertIn('retry=blocked', lines)
        for case, blocker in [('login-active', 'login-not-inactive'),
                              ('login-unknown', 'login-not-inactive'),
                              ('browser-active', 'browser-not-inactive'),
                              ('browser-unknown', 'browser-not-inactive'),
                              ('parser-timeout', 'candidate-unproven'),
                              ('parser-signal', 'candidate-unproven')]:
            with self.subTest(case=case):
                lines = self.invoke(case)
                self.assertIn('retry=blocked', lines)
                self.assertIn('retry-blocker=' + blocker, lines)
        # The legacy recheck label is advisory; installation must perform its
        # own fresh gate, regardless of the diagnostic label's value.
        for case in ('recheck', 'gate-install-atomic-recheck', 'gate-receipt'):
            with self.subTest(case=case):
                lines = self.invoke(case)
                self.assertIn('retry=eligible_after_atomic_recheck', lines)
                self.assertIn('irreversible-boundary=pending-fresh-verification', lines)
                self.assertIn('receipt-classification=missing', lines)

    def test_each_required_retry_gate_fails_closed(self):
        for key in ('source-manifest', 'browser-source-tree', 'candidate-validation',
                    'source-candidate-validation', 'transaction', 'candidate-artifact',
                    'staged-tree', 'baseline', 'conflict', 'login-activity',
                    'browser-activity', 'kernel-probe', 'installed-tree',
                    'profile-disk', 'profile-kernel', 'receipt-classification', 'noreplace-support'):
            with self.subTest(gate=key):
                lines = self.invoke('gate-' + key)
                self.assertIn('retry=blocked', lines)
                self.assertNotIn('retry-blocker=none', lines)

    def test_manifest_errno(self):
        for case, bucket in [('missing', 'missing'), ('denied', 'access-denied'),
                             ('perm', 'access-denied'), ('link', 'symlink'),
                             ('notdir', 'unsafe-path'), ('io', 'io-error')]:
            lines = self.invoke('manifest-' + case)
            module_lines = self.invoke('module-' + case)
            self.assertIn('diagnostic-module=' + bucket, module_lines)
            self.assertIn('source-manifest=' + bucket, lines)
            self.assertIn('primary=TREE_SOURCE_FAILURE', lines)
            self.assertIn('retry=blocked', lines)

    def test_artifact_errors_never_become_identity_failure(self):
        for case, bucket in (('denied', 'access-denied'), ('enoent', 'missing'), ('link', 'symlink'), ('special', 'nonregular')):
            lines = self.invoke('candidate-' + case)
            self.assertIn('candidate-artifact=' + bucket, lines)
            self.assertIn('candidate-validation=ok', lines)
            self.assertIn('primary=INSUFFICIENT_EVIDENCE', lines)
            self.assertIn('retry=blocked', lines)

    def test_tree_first_failure_is_separate_from_source_candidate(self):
        cases = {'source-denied': 'access-denied', 'source-enoent': 'missing', 'source-io': 'io-error', 'source-link': 'symlink', 'source-perm': 'access-denied', 'source-notdir': 'unsafe-path'}
        cases.update({'source-invariant-' + name: name for name in
                      ('unsafe-link', 'special', 'owner', 'mode', 'layout', 'elf', 'suid-sgid', 'caps', 'hash')})
        for case, bucket in cases.items():
            lines = self.invoke(case)
            self.assertIn('browser-source-tree=' + bucket, lines)
            self.assertIn('tree-first-invariant=' + bucket, lines)
            for field in ('parser-options', 'parser-environment', 'candidate-compile',
                          'source-candidate-validation'):
                self.assertIn(field + '=ok', lines)
            self.assertIn('candidate-validation=ok', lines)
            self.assertIn('primary=TREE_SOURCE_FAILURE', lines)
            self.assertIn('retry=blocked', lines)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--child']:
        sys.exit(child(sys.argv[2], sys.argv[3:]))
    unittest.main()
