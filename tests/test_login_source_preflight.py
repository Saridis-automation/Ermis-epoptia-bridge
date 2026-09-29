"""No host paths opened: synthetic FD graph and mocked install observations."""
from contextlib import ExitStack
import errno
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from admin_bootstrap import login_browser_stage as s, login_diagnose as d


class SourcePaths(unittest.TestCase):
    def setUp(self):
        self.path = Path('/home/ermis/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome')
        self.nodes = [SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0 if i < 2 else 1001,
                      st_gid=0 if i < 2 else 1001, st_dev=1, st_ino=i+1, st_size=1,
                      st_mtime_ns=1, st_ctime_ns=1, st_nlink=1) for i in range(len(self.path.parts))]
        self.nodes[-1].st_mode = stat.S_IFREG | 0o755
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(s.os, 'open', side_effect=lambda name, flags, dir_fd=None: 0 if name == '/' else dir_fd+1))
        self.stack.enter_context(patch.object(s.os, 'close'))
        self.stack.enter_context(patch.object(s.os, 'fstat', side_effect=lambda fd: self.nodes[fd]))
        self.stat = self.stack.enter_context(patch.object(s.os, 'stat', side_effect=lambda name, dir_fd, **kw: self.nodes[dir_fd+1]))
        self.caps = self.stack.enter_context(patch.object(s.os, 'getxattr', side_effect=OSError(errno.ENODATA, '')))

    def failure(self, category, ordinal):
        with self.assertRaises(s.PathInvariant) as caught:
            s.open_path(self.path, 1001)
        self.assertEqual((caught.exception.invariant, caught.exception.component), (category, str(ordinal)))

    def test_valid_mixed_ownership_and_root_policy(self):
        self.assertEqual(s.open_path(self.path, 1001), len(self.nodes)-1)
        with self.assertRaises(s.PathInvariant) as caught:
            s.open_path(self.path, 0)
        self.assertEqual(caught.exception.invariant, 'owner')

    def test_group_writable_mode_is_redacted_and_blocked(self):
        self.nodes[3].st_mode = stat.S_IFDIR | 0o775
        with self.assertRaises(s.PathInvariant) as caught:
            s.open_path(self.path, 1001)
        error = caught.exception
        self.assertEqual((error.invariant, error.component), ('mode', '3'))
        self.assertEqual(error.component_label, 'source-cache-parent')
        self.assertEqual(error.observed_mode, '0775')
        self.assertEqual(error.mode_category, 'group-writable')
        self.assertEqual(error.remediation, 'remove-group-write')
        details = dict(d.MODEL_FIELDS)
        details['browser-source-tree'] = 'mode'
        with patch.object(d, 'model_observation', return_value=(
                details, {'browser-source-tree': error}, (), None, 'clear')):
            result = d.diagnose()
        for token in ('tree-first-component=3', 'tree-first-component-label=source-cache-parent',
                      'tree-first-observed-mode=0775', 'tree-first-mode-category=group-writable',
                      'tree-first-remediation=remove-group-write'):
            self.assertIn(token, result)
        self.assertNotIn(str(self.path), repr(result))
        for mode in (0o755, 0o750, 0o700):
            self.nodes[3].st_mode = stat.S_IFDIR | mode
            self.assertEqual(s.open_path(self.path, 1001), len(self.nodes)-1)

    def test_each_component_failure(self):
        for mode, category in ((stat.S_IFLNK | 0o755, 'unsafe-link'),
                               (stat.S_IFIFO | 0o600, 'special'),
                               (stat.S_IFREG | 0o644, 'non-directory'),
                               (stat.S_IFDIR | 0o777, 'mode'),
                               (stat.S_IFDIR | 0o4755, 'suid-sgid'),
                               (stat.S_IFDIR | 0o2755, 'suid-sgid')):
            with self.subTest(category=category):
                self.nodes[3].st_mode = mode
                self.failure(category, 3)
        self.nodes[3].st_mode = stat.S_IFDIR | 0o755
        self.nodes[3].st_uid = 0
        self.failure('owner', 3)

    def test_missing_access_caps_escape_and_swap(self):
        for number, category in ((errno.ENOENT, 'missing'), (errno.EACCES, 'access-denied'), (errno.EIO, 'io-error')):
            self.stat.side_effect = OSError(number, 'private path')
            self.failure(category, 1)
        self.stat.side_effect = lambda name, dir_fd, **kw: self.nodes[dir_fd+1]
        self.caps.side_effect = None
        self.caps.return_value = b'caps'
        self.failure('caps', 0)
        self.caps.side_effect = OSError(errno.ENODATA, '')
        with self.assertRaises(s.PathInvariant) as caught:
            s.open_path(Path('/tmp/chrome'), 1001)
        self.assertEqual(caught.exception.invariant, 'prefix-escape')
        calls = 0
        def swapped(name, dir_fd, **kw):
            nonlocal calls
            calls += 1
            node = self.nodes[dir_fd+1]
            return SimpleNamespace(**{**vars(node), 'st_ino': 999}) if calls > len(self.nodes)-1 else node
        self.stat.side_effect = swapped
        self.failure('inode-device-swap', 1)


class AtomicRecheck(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.path = Mock()
        self.path.lstat.return_value = 'identity'
        self.source = self.stack.enter_context(patch.object(d, 'source', return_value=({}, 'manifest')))
        self.tree = {'noreplace_support': lambda: True, 'destination_state': lambda record: None, 'source': lambda root: (self.path, 1001), 'signature': lambda x: x,
                     'tree': lambda *a, **kw: dict(d.PINNED)}
        self.profile = Mock(return_value=b'candidate')
        self.stack.enter_context(patch.object(d, 'source_modules', return_value=(self.tree, {'profile': self.profile})))
        self.validation = self.stack.enter_context(patch.object(d, 'validate_candidate', return_value='ok'))
        self.transaction = self.stack.enter_context(patch.object(d, 'transaction_observation', return_value=('idle', None)))
        self.conflicts = self.stack.enter_context(patch.object(d, 'conflicts', return_value='clear'))
        self.current = self.stack.enter_context(patch.object(d, 'current_state', return_value=({'baseline': 'clean-absent', 'installed-tree': 'absent', 'profile-disk': 'absent', 'profile-kernel': 'absent', 'kernel-probe': 'absent', 'receipt-classification': 'missing'}, 'clear', 'snapshot')))
        self.baseline = self.stack.enter_context(patch.object(d, 'baseline', return_value={'disk': {str(p): None for p in d.DISK_ROOTS}, 'kernel': []}))
        self.activity = Mock(return_value={'login-activity': 'inactive', 'browser-activity': 'inactive'})
        self.destinations = Mock()

    def test_two_equal_observations_and_every_gate(self):
        d.atomic_install_recheck(self.activity, self.destinations)
        for mock in (self.source, self.validation, self.transaction, self.conflicts, self.current, self.baseline, self.activity, self.destinations):
            self.assertEqual(mock.call_count, 2)

    def test_unknown_and_changed_gates_deny(self):
        for mock, bad in ((self.validation, 'unknown'), (self.transaction, ('unknown', None)),
                          (self.conflicts, 'unknown'), (self.conflicts, 'possible-conflict'),
                          (self.conflicts, 'definite-conflict'),
                          (self.current, ({'baseline': 'unknown'}, 'clear', 'snapshot')),
                          (self.current, ({'baseline': 'coherent-prior'}, 'clear', 'snapshot')),
                          (self.current, ({'baseline': 'coherent-candidate'}, 'clear', 'snapshot')),
                          (self.current, ({'baseline': 'clean-absent'}, 'unknown', 'snapshot')),
                          (self.activity, {'login-activity': 'unknown', 'browser-activity': 'inactive'})):
            previous = mock.return_value
            mock.return_value = bad
            with self.subTest(gate=bad), self.assertRaises(ValueError):
                d.atomic_install_recheck(self.activity, self.destinations)
            mock.return_value = previous
        for mock, values in ((self.source, [({}, 'before'), ({}, 'after')]),
                             (self.path.lstat, ['before', 'after']),
                             (self.baseline, ['before', 'after'])):
            mock.side_effect = values
            with self.assertRaises(ValueError):
                d.atomic_install_recheck(self.activity, self.destinations)
            mock.side_effect = None
        self.destinations.side_effect = ValueError('unsafe-destination')
        with self.assertRaises(ValueError):
            d.atomic_install_recheck(self.activity, self.destinations)

    def test_stale_receipt_and_unknown_kernel_cannot_inherit_coherent_label(self):
        details = dict(d.MODEL_FIELDS, baseline='clean-absent', transaction='idle')
        details.update({'source-manifest': 'ok', 'browser-source-tree': 'ok',
                        'candidate-validation': 'ok', 'source-candidate-validation': 'ok',
                        'candidate-artifact': 'not-applicable', 'staged-tree': 'not-applicable',
                        'login-activity': 'inactive', 'browser-activity': 'inactive',
                        'install-atomic-recheck': 'supported-pending', 'receipt': 'stale'})
        with patch.object(d, 'model_observation', return_value=(details, {}, (), None, 'clear')):
            self.assertEqual(d.diagnose()[3], 'retry=blocked')


class InstallGateOrdering(unittest.TestCase):
    setUp = AtomicRecheck.setUp

    def test_each_failed_or_changed_observation_precedes_every_mutation(self):
        # Real install/atomic recheck control flow; all host observations are seams.
        import fcntl
        import pwd
        from admin_bootstrap import login_bootstrap as login
        from admin_bootstrap import login_apparmor as aa
        self.tree['tree'] = Mock(return_value=dict(d.PINNED))
        self.tree['destination_state'] = Mock(return_value=None)
        cases = [(self.profile, ValueError('generation')), (self.source, OSError('manifest')), (self.path.lstat, OSError('source')),
                 (self.tree['tree'], ValueError('source-safety')),
                 (self.tree['destination_state'], ValueError('target-safety')),
                 (self.validation, 'unknown'), (self.transaction, ('unknown', None)),
                 (self.conflicts, 'unknown'), (self.conflicts, 'possible-conflict'),
                 (self.conflicts, 'definite-conflict'), (self.conflicts, OSError('unreadable')),
                 (self.current, ({'baseline': 'unknown'}, 'clear', None)),
                 (self.current, ({'baseline': 'clean-absent'}, 'unknown', None)),
                 (self.baseline, None), (self.destinations, ValueError('destination')),
                 (self.activity, {'login-activity': 'unknown', 'browser-activity': 'inactive'}),
                 (self.activity, {'login-activity': 'inactive', 'browser-activity': 'unknown'})]
        changes = [(self.profile, b'changed'), (self.source, ({}, 'changed')), (self.path.lstat, 'replacement'),
                   (self.tree['tree'], {**d.PINNED, 'sha256': 'changed'}),
                   (self.tree['destination_state'], 'inserted'),
                   (self.transaction, ('idle', {'changed': True})),
                   (self.current, ({'baseline': 'clean-absent'}, 'clear', 'changed')),
                   (self.destinations, 'changed'),
                   (self.baseline, {'disk': {str(p): None for p in d.DISK_ROOTS},
                                    'kernel': [['other', 'enforce', '/other', 'a' * 40]]})]
        for observation, bad, changed in [(*c, False) for c in cases] + [(*c, True) for c in changes]:
            with self.subTest(observation=observation, changed=changed), ExitStack() as stack:
                events = []
                stack.enter_context(patch.object(login, 'validate_source'))
                stack.enter_context(patch.object(login, 'artifacts', return_value={}))
                stack.enter_context(patch.object(login.os, 'geteuid', return_value=0))
                stack.enter_context(patch.object(pwd, 'getpwnam'))
                stack.enter_context(patch.object(login.os, 'open', return_value=123))
                stack.enter_context(patch.object(login.os, 'close', side_effect=lambda fd: events.append('unlock')))
                stack.enter_context(patch.object(fcntl, 'flock', side_effect=lambda *a: events.append('lock')))
                stack.enter_context(patch.object(login, 'prepare_parent', side_effect=lambda p, create=False:
                    self.assertFalse(create)))
                def recheck(files):
                    self.assertEqual(events, ['lock'])
                    return d.atomic_install_recheck(self.activity, self.destinations)
                stack.enter_context(patch.object(login, 'atomic_preflight', side_effect=recheck))
                guards = []
                for owner, names in ((Path, ('mkdir', 'write_bytes', 'unlink', 'rename')),
                                     (s.tempfile, ('mkdtemp', 'mkstemp')),
                                     (login, ('candidate', 'managed_files', 'apparmor_transaction', 'socket_operation')),
                                     (aa, ('parser',)), (s, ('staged', 'rename_absent'))):
                    for name in names:
                        guards.append(stack.enter_context(patch.object(owner, name, side_effect=AssertionError('mutation'))))
                observation.side_effect = [observation.return_value, bad] if changed else bad if isinstance(bad, Exception) else None
                original = observation.return_value
                if not changed and not isinstance(bad, Exception):
                    observation.return_value = bad
                try:
                    with self.assertRaises((OSError, ValueError)):
                        login.install('install')
                    self.assertEqual(events, ['lock', 'unlock'])
                    for guard in guards:
                        guard.assert_not_called()
                finally:
                    observation.side_effect = None
                    observation.return_value = original
