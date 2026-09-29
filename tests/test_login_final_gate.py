"""Project-local fixtures and mocked probes; no host policy or services."""
from contextlib import ExitStack
import fcntl
import os
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from admin_bootstrap import login_diagnose as d, login_apparmor as aa, login_bootstrap as login
from tests import test_login_first_install as first


class FinalGate(unittest.TestCase):
    setUp = first.FirstInstall.setUp

    def probes(self):
        self.source_probe = self.stack.enter_context(patch.object(d, 'source', return_value=({}, 'fingerprint')))
        self.path = Mock()
        self.path.lstat.return_value = 'identity'
        self.tree = {'noreplace_support': lambda: True, 'source': lambda root: (self.path, 1001),
            'signature': lambda value: value, 'tree': Mock(return_value=self.record),
            'destination_state': Mock(return_value=None)}
        self.stack.enter_context(patch.object(d, 'source_modules', return_value=(self.tree, {'profile': aa.profile})))
        self.validation = self.stack.enter_context(patch.object(d, 'validate_candidate', return_value='ok'))
        self.history = self.stack.enter_context(patch.object(d, 'transaction_observation', return_value=('idle', None)))
        self.conflicts = self.stack.enter_context(patch.object(d, 'conflicts', return_value='clear'))
        self.state = {'baseline': 'clean-absent', 'installed-tree': 'absent', 'profile-disk': 'absent', 'profile-kernel': 'absent',
            'kernel-probe': 'absent', 'receipt-classification': 'missing'}
        self.current = self.stack.enter_context(patch.object(d, 'current_state', side_effect=lambda *a:
            (dict(self.state), 'clear', (self.record, 'snapshot'))))
        self.baseline = self.stack.enter_context(patch.object(d, 'baseline', return_value={
            'disk': {str(p): None for p in d.DISK_ROOTS}, 'kernel': []}))
        self.activity = Mock(return_value={'login-activity': 'inactive', 'browser-activity': 'inactive'})
        self.destinations = Mock()
        self.expected = d.atomic_install_recheck(self.activity, self.destinations)
        self.state.update({'installed-tree': 'valid', 'profile-disk': 'candidate',
            'receipt-classification': 'ambiguous', 'receipt-reason': 'incomplete-receipt-set'})

    def run_install(self, inject=lambda: None, after_gate=lambda: None):
        # Two independent descriptors contend on the same project-local inode.
        with ExitStack() as stack:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            other = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            stack.callback(os.close, fd)
            stack.callback(os.close, other)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with aa.policy_transaction(self.api, 'install', self.record) as publish:
                def final(prepared):
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    inject()
                    d.atomic_install_recheck(self.activity, self.destinations,
                        phase='prepared', expected=self.expected, prepared=prepared)
                    self.events.append('final-gate')
                    after_gate()
                publish.final_recheck = final
                with publish():
                    pass

    def test_shared_classifier_fresh_final_gate_and_success_order(self):
        with patch.object(d, 'classify_install_baseline', wraps=d.classify_install_baseline) as classifier:
            self.probes()
            initial_reads = self.source_probe.call_count
            self.run_install()
        self.assertEqual([c.kwargs['phase'] for c in classifier.call_args_list],
            ['initial', 'initial', 'prepared', 'prepared'])
        self.assertEqual(self.source_probe.call_count, initial_reads + 2)
        self.assertEqual(self.events.count('load'), 1)
        self.assertEqual(self.events[self.events.index('load') - 1], 'final-gate')
        self.assertGreater(self.events.index('verify-kernel'), self.events.index('load'))
        self.assertTrue(self.receipt.exists())

    def test_every_state_dimension_unknown_blocks_load(self):
        self.stack.close()
        # Each fixture owns separate files so retained failures never reset state.
        for key in ('installed-tree', 'profile-disk', 'profile-kernel', 'kernel-probe',
                    'receipt-classification', 'receipt-reason'):
            with self.subTest(key=key):
                case = FinalGate()
                try:
                    case.setUp()
                    case.probes()
                    with case.assertRaises(ValueError):
                        case.run_install(lambda: case.state.update({key: 'unknown'}))
                    case.assertNotIn('load', case.events)
                finally:
                    case.doCleanups()

    def test_every_fresh_probe_error_and_drift_blocks_load(self):
        self.stack.close()
        for key in ('source', 'tree', 'validation', 'transaction', 'conflict', 'activity',
                    'destinations', 'baseline', 'binding', 'kernel-appears', 'source-drift'):
            with self.subTest(key=key):
                case = FinalGate()
                try:
                    case.setUp()
                    case.probes()
                    def inject():
                        probe = {'source': case.source_probe, 'tree': case.tree['tree'],
                            'validation': case.validation, 'transaction': case.history,
                            'conflict': case.conflicts, 'activity': case.activity,
                            'destinations': case.destinations, 'baseline': case.baseline}.get(key)
                        if probe is not None:
                            probe.side_effect = OSError('fixture')
                        elif key == 'binding':
                            case.profile.write_bytes(b'drift')
                        elif key == 'kernel-appears':
                            case.state['kernel-probe'] = 'present-exact'
                        else:
                            case.source_probe.return_value = ({}, 'changed')
                    with case.assertRaises((ValueError, OSError)):
                        case.run_install(inject)
                    case.assertNotIn('load', case.events)
                finally:
                    case.doCleanups()

    def test_final_observation_drift_blocks_load(self):
        self.stack.close()
        for key in ('identity', 'record', 'destination', 'transaction', 'snapshot', 'baseline'):
            with self.subTest(key=key):
                case = FinalGate()
                try:
                    case.setUp()
                    case.probes()
                    def inject():
                        if key == 'identity':
                            case.path.lstat.return_value = 'replacement-inode'
                        elif key == 'record':
                            case.tree['tree'].return_value = dict(case.record, tree_sha256='a' * 64)
                        elif key == 'destination':
                            case.tree['destination_state'].side_effect = ['before', 'after']
                        elif key == 'transaction':
                            case.history.return_value = ('idle', {'changed': True})
                        elif key == 'snapshot':
                            case.current.side_effect = [(dict(case.state), 'clear', (case.record, v))
                                for v in ('before', 'after')]
                        else:
                            before = case.baseline.return_value
                            after = dict(before, kernel=[['other', 'enforce', '/other', 'a' * 40]])
                            case.baseline.side_effect = [before, after]
                    with case.assertRaises(ValueError):
                        case.run_install(inject)
                    case.assertNotIn('load', case.events)
                finally:
                    case.doCleanups()

    def test_every_classifier_dimension_blocks_at_final_load_edge(self):
        self.stack.close()
        captured = {}
        real = d.classify_install_baseline
        # Discover the observed dimensions from the actual successful gate,
        # keeping the production classifier's policy as the only authority.
        case = FinalGate()
        try:
            case.setUp()
            case.probes()
            def capture(details, conflict, **kwargs):
                captured.update(details)
                return real(details, conflict, **kwargs)
            with patch.object(d, 'classify_install_baseline', side_effect=capture):
                case.run_install()
        finally:
            case.doCleanups()
        for key in captured:
            with self.subTest(key=key):
                case = FinalGate()
                try:
                    case.setUp()
                    case.probes()
                    def unknown(details, conflict, **kwargs):
                        return real(dict(details, **{key: 'unknown'}), conflict, **kwargs)
                    with patch.object(d, 'classify_install_baseline', side_effect=unknown), \
                            case.assertRaises(ValueError):
                        case.run_install()
                    case.assertNotIn('load', case.events)
                finally:
                    case.doCleanups()

    def test_race_after_final_probe_is_add_only_failure_without_unload(self):
        self.probes()
        original = aa.parser
        def parser(action, path):
            if action == 'load':
                self.events.append('load-rejected')
                raise ValueError('kernel-add-unproven')
            return original(action, path)
        def race():
            self.kernel.write_bytes(b'foreign')
        with patch.object(aa, 'parser', parser), self.assertRaisesRegex(ValueError, 'after-kernel-boundary'):
            self.run_install(after_gate=race)
        self.assertEqual(self.kernel.read_bytes(), b'foreign')
        self.assertFalse(self.receipt.exists())
        self.assertNotIn('remove', self.events)
        self.assertEqual(self.events.count('load-rejected'), 1)

    def test_missing_final_gate_fails_closed(self):
        with aa.policy_transaction(self.api, 'install', self.record) as publish:
            with self.assertRaisesRegex(ValueError, 'before-kernel-boundary'):
                with publish():
                    pass
        self.assertNotIn('load', self.events)

    def test_second_installer_cannot_reach_preflight(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        other = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(login, 'validate_source'), patch.object(login, 'artifacts', return_value={}), \
                    patch.object(login.os, 'geteuid', return_value=0), patch('pwd.getpwnam'), \
                    patch.object(login, 'prepare_parent'), patch.object(login.os, 'open', return_value=other), \
                    patch.object(login.os, 'close'), patch.object(login, 'atomic_preflight') as preflight:
                with self.assertRaises(ValueError):
                    login.install('install')
                preflight.assert_not_called()
        finally:
            os.close(other)
            os.close(fd)


class Classifier(unittest.TestCase):
    def test_all_dimensions_fail_closed_and_receipt_exception_is_exact(self):
        good = {'baseline': 'clean-absent', 'source-manifest': 'ok', 'browser-source-tree': 'ok',
            'source-candidate-validation': 'ok', 'candidate-validation': 'ok',
            'transaction': 'idle', 'noreplace-support': 'symbol-present',
            'login-activity': 'inactive', 'browser-activity': 'inactive',
            'kernel-probe': 'absent', 'profile-kernel': 'absent', 'installed-tree': 'absent',
            'profile-disk': 'absent', 'candidate-artifact': 'not-applicable',
            'staged-tree': 'not-applicable', 'receipt-classification': 'missing'}
        self.assertTrue(d.classify_install_baseline(good, 'clear'))
        for key in good:
            for value in ('unknown', 'active', 'existing', 'conflict', None):
                self.assertFalse(d.classify_install_baseline(dict(good, **{key: value}), 'clear'))
        for value in ('valid-precommit-failure-rollback', 'stale-recognized-source-binding',
                      'valid-committed-success', 'ambiguous'):
            self.assertEqual(d.classify_install_baseline(dict(good, **{'receipt-classification': value}),
                'clear'), value == 'valid-precommit-failure-rollback')
        self.assertFalse(d.classify_install_baseline(good, 'unknown'))
        self.assertFalse(d.classify_install_baseline(good, 'clear', phase='other'))
