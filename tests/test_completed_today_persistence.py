"""Restart checks using separate processes and synthetic, isolated SQLite stores."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
WORKER = '''
import json
import sys
from datetime import datetime, timezone
from dashboard.completed_jobs import CompletedJobsTracker

tracker = CompletedJobsTracker(sys.argv[1])
startup = dict(tracker.status)
now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
scan = json.loads(sys.argv[2])
if scan is not None:
    tracker.observe([dict(id=7, erp_routing=[
        dict(id=number, workstationName='LASER', job_tag={'name': 'Cut'}, status=status)
        for number, status in scan['steps']])], complete=scan['complete'], now=now)
summary, status = tracker.summary(now)
print(json.dumps(dict(startup=startup, summary=summary, status=status)))
'''


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=PROJECT / 'dashboard')
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'tracker.sqlite3'

    def process(self, steps=None, complete=True):
        scan = None if steps is None else dict(steps=steps, complete=complete)
        result = subprocess.run(
            [sys.executable, '-B', '-c', WORKER, str(self.path), json.dumps(scan)],
            cwd=PROJECT, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_baseline_survives_restart(self):
        first = self.process([(1, 'started')])['summary']['tracker']
        before = self.path.read_bytes()
        restarted = self.process()
        self.assertEqual(restarted['startup']['state'], 'persisted_awaiting_scan')
        self.assertTrue(restarted['summary']['tracker']['baseline_initialized'])
        self.assertEqual(restarted['summary']['tracker']['coverage_started_at'], first['coverage_started_at'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_tracked_steps_survive_restart(self):
        self.process([(1, 'paused'), (2, 'completed'), (3, 'blocked')])
        restarted = self.process()
        self.assertEqual(restarted['summary']['tracker']['tracked_steps'], 3)
        completed = self.process([(1, 'completed'), (2, 'completed'), (3, 'completed')])
        self.assertEqual(completed['summary']['total'], 2)

    def test_events_survive_restart(self):
        self.process([(1, 'started')])
        expected = self.process([(1, 'completed')])['summary']
        self.assertEqual(expected['total'], 1)
        before = self.path.read_bytes()
        self.assertEqual(self.process()['summary'], expected | {
            'tracker': expected['tracker'] | {'status': 'persisted_awaiting_scan'}})
        self.assertEqual(self.path.read_bytes(), before)

    def test_no_duplicate_events_across_restarts(self):
        self.process([(1, 'started')])
        for state in ('completed', 'completed', 'paused', 'completed'):
            self.assertEqual(self.process([(1, state)])['summary']['total'], 1)
        self.assertEqual(self.process()['summary']['tracker']['last_scan_new_events'], 0)

    def test_newly_discovered_completed_rows_do_not_create_events(self):
        self.process([(1, 'started')])
        self.assertEqual(self.process([(1, 'started'), (2, 'completed')])['summary']['total'], 0)
        restarted = self.process([(1, 'completed'), (2, 'completed'), (3, 'completed')])
        self.assertEqual(restarted['summary']['total'], 1)
        self.assertEqual(restarted['summary']['tracker']['tracked_steps'], 3)

    def test_incomplete_snapshot_after_restart_does_not_mutate(self):
        self.process([(1, 'started')])
        before = self.path.read_bytes()
        self.process([(1, 'completed'), (2, 'completed')], complete=False)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.process([(1, 'completed')])['summary']['total'], 1)

    def test_startup_and_incomplete_snapshot_do_not_create_missing_store(self):
        self.assertEqual(self.process()['startup']['state'], 'pending')
        self.process([(1, 'completed')], complete=False)
        self.assertFalse(self.path.exists())
