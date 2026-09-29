"""Synthetic local-only completion tracker and shared scan integration checks."""
import asyncio
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

import epoptia_read
from dashboard.completed_jobs import CompletedJobsTracker
from dashboard.provider import DirectReader, LocalEpoptiaProvider
from dashboard.adapter import map_snapshot

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)


def step(status='started', **extra):
    return dict(workstationName='LASER', job_tag={'name': 'Cut'}, status=status, **extra)


def rows(*steps):
    return [dict(id=7, production_status='production', erp_routing=list(steps))]


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir='dashboard')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'tracker.sqlite3'
        self.tracker = CompletedJobsTracker(self.path)

    def observe(self, *steps, complete=True, now=NOW):
        self.tracker.observe(rows(*steps), complete=complete, now=now)

    def total(self, now=NOW):
        return self.tracker.summary(now)[0]['total']

    def test_baseline_historical_completed_and_new_steps(self):
        self.observe(step('completed', id=1))
        self.observe(step('completed', id=1), step('completed', id=2))
        self.assertEqual(self.total(), 0)
        self.assertEqual(self.tracker.summary(NOW)[0]['breakdown_by_workstation'], {})

    def test_shared_parser_drives_baseline_transition_dedup_and_restart(self):
        from test_routing_completion_candidates import status_tool

        parse = epoptia_read.parse_routing_step
        def parsed_step(raw):
            item = parse(raw)
            # Prove both consumers use parsed values, not parallel raw parsing.
            item.update(status=raw['synthetic_state'], workstation='ASSEMBLY', job='Fit')
            return item

        population = rows(step('unknown', id=1, synthetic_state='started'),
                          step('unknown', id=2, synthetic_state='completed'))
        population.append(dict(id=8, production_status='completed'))
        with patch('epoptia_read.parse_routing_step', side_effect=parsed_step) as parser:
            self.tracker.observe(population, complete=True, now=NOW)
            self.assertEqual(parser.call_count, 2)
            self.assertEqual(self.total(), 0)
            self.tracker = CompletedJobsTracker(self.path)
            population[0]['erp_routing'][0]['synthetic_state'] = 'completed'
            self.tracker.observe(population, complete=True, now=NOW)
            summary = self.tracker.summary(NOW)[0]
            self.assertEqual(summary['breakdown_by_workstation'], {'ASSEMBLY': 1})
            self.assertTrue(summary['tracker']['baseline_exists'])
            self.assertEqual(summary['tracker']['tracked_step_count'], 2)
            self.assertEqual(summary['tracker']['last_refresh_new_events'], 1)
            self.assertEqual(summary['tracker']['source'], 'shared_routing_parser_rest_scan')
            self.tracker = CompletedJobsTracker(self.path)
            self.tracker.observe(population, complete=True, now=NOW)
            self.assertEqual(self.total(), 1)
            self.assertEqual(self.tracker.summary(NOW)[0]['tracker']['last_refresh_new_events'], 0)
            parser.reset_mock()
            result = status_tool(population[0])
            self.assertEqual(parser.call_count, 2)
            self.assertEqual(len(result['completed']), 2)
            self.assertEqual(result['completed'][0]['workstation'], 'ASSEMBLY')
            self.assertEqual(result['completed'][0]['job'], 'Fit')

    def test_full_scan_with_unrouted_terminal_rows_persists_first_baseline(self):
        population = rows(step('started', id=1), step('completed', id=2)) + [
            dict(id=8, production_status='archived', erp_routing=None),
            dict(id=9, production_status='completed'),
            dict(id=10, status='cancelled', erp_routing=[]),
            dict(id=11, production_status='completed', erp_routing=[
                dict(step('completed', id=3), workstationName='ASSEMBLY')]),
        ]
        reader = DirectReader(tracker=self.tracker, clock=lambda: NOW)
        reader._observe_routing(population, complete=True, cancelled=Event())
        self.assertEqual(self.tracker.status['state'], 'ready')
        self.tracker = CompletedJobsTracker(self.path)
        summary = self.tracker.summary(NOW)[0]
        self.assertTrue(summary['tracker']['baseline_initialized'])
        self.assertEqual(summary['tracker']['tracked_steps'], 3)
        self.assertEqual(summary['tracker']['last_scan_steps'], 3)
        self.assertEqual(summary['total'], 0)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(sorted(status for status, in db.execute('SELECT status FROM steps')),
                             ['completed', 'completed', 'started'])
        coverage = summary['tracker']['coverage_started_at']
        population[0]['erp_routing'][0]['status'] = 'completed'
        for _ in range(2):
            self.tracker.observe(population, complete=True, now=NOW)
            self.tracker = CompletedJobsTracker(self.path)
        summary = self.tracker.summary(NOW)[0]
        self.assertEqual(summary['total'], 1)
        self.assertEqual(summary['breakdown_by_workstation'], {'LASER': 1})
        self.assertEqual(summary['tracker']['coverage_started_at'], coverage)

    def test_missing_active_or_malformed_terminal_routing_cannot_mutate_baseline(self):
        for row in (dict(id=8, production_status='production'),
                    dict(id=8, production_status='production', erp_routing=None),
                    dict(id=8, production_status='archived', erp_routing={})):
            with self.subTest(row=row):
                population = rows(step('completed', id=1)) + [row]
                self.tracker.observe(population, complete=True, now=NOW)
                self.assertFalse(self.path.exists())
        self.observe(step('started', id=1))
        before = self.path.read_bytes()
        self.tracker.observe(population, complete=True, now=NOW)
        self.assertEqual(self.path.read_bytes(), before)

    def test_unrouted_terminal_parent_preserves_prior_step_until_observed(self):
        self.observe(step('paused', id=1))
        self.tracker.observe([dict(id=7, production_status='completed', erp_routing=None)],
                             complete=True, now=NOW)
        self.tracker = CompletedJobsTracker(self.path)
        self.assertEqual(self.total(), 0)
        self.assertEqual(self.tracker.summary(NOW)[0]['tracker']['tracked_steps'], 1)
        self.observe(step('completed', id=1))
        self.assertEqual(self.total(), 1)

    def test_new_completed_step_persists_without_event_until_observed_transition(self):
        self.observe(step(id=1))
        self.observe(step(id=1), step('completed', id=2))
        self.tracker = CompletedJobsTracker(self.path)
        self.assertEqual(self.total(), 0)
        self.assertEqual(self.tracker.summary(NOW)[0]['tracker']['tracked_steps'], 2)
        self.observe(step(id=1), step('paused', id=2))
        self.tracker = CompletedJobsTracker(self.path)
        self.observe(step(id=1), step('completed', id=2))
        self.assertEqual(self.total(), 1)

    def test_incomplete_mixed_population_never_mutates_store(self):
        population = rows(step('completed', id=1)) + [
            dict(id=8, production_status='archived', erp_routing=None)]
        self.tracker.observe(population, complete=False, now=NOW)
        self.assertFalse(self.path.exists())
        self.observe(step(id=1))
        before = self.path.read_bytes()
        self.tracker.observe(population, complete=False, now=NOW)
        self.assertEqual(self.path.read_bytes(), before)

    def test_one_transition_event_fields_utc_and_local_date(self):
        self.observe(step(id=1))
        self.observe(step('completed', id=1))
        self.assertEqual(self.total(), 1)
        with sqlite3.connect(self.path) as db:
            event = db.execute('SELECT * FROM events').fetchone()
        self.assertEqual(len(event[0]), 64)
        self.assertEqual(event[1:], ('7', 'LASER', 'Cut', NOW.isoformat(), '2026-09-14'))

    def test_repeated_completed_and_reopened_deduplicated(self):
        self.observe(step(id=1))
        for status in ('completed', 'completed', 'paused', 'completed'):
            self.observe(step(status, id=1))
        self.assertEqual(self.total(), 1)

    def test_restart_persists_baseline_and_event(self):
        self.observe(step(id=1))
        self.tracker = CompletedJobsTracker(self.path)
        self.observe(step('completed', id=1))
        self.tracker = CompletedJobsTracker(self.path)
        self.assertEqual(self.total(), 1)
        self.observe(step('completed', id=1))
        self.assertEqual(self.total(), 1)

    def test_concurrent_store_instances_insert_once(self):
        self.observe(step(id=1))
        trackers = [CompletedJobsTracker(self.path), CompletedJobsTracker(self.path)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(tracker.observe, rows(step('completed', id=1)),
                                   complete=True, now=NOW) for tracker in trackers]
            for future in futures:
                future.result()
        self.assertEqual(self.total(), 1)
        self.assertTrue(all(tracker.status['state'] == 'ready' for tracker in trackers))

    def test_all_recognized_active_statuses_transition(self):
        for index, status in enumerate(('not_started', 'in_progress', 'started', 'paused', 'waiting')):
            self.observe(step(status, id=index))
            self.observe(step('Completed', id=index))
        self.assertEqual(self.total(), 5)

    def test_duplicate_names_without_ids_are_skipped(self):
        self.observe(step(), step())
        self.observe(step('completed'), step('completed'))
        self.assertEqual(self.total(), 0)
        self.assertEqual(self.tracker.summary(NOW)[0]['tracker']['recovery_status'],
                         'awaiting_baseline')

    def test_explicit_identity_survives_route_reordering(self):
        self.observe(step(id=1), step(id=2))
        self.observe(step('completed', id=2), step(id=1))
        self.assertEqual(self.total(), 1)

    def test_incomplete_scan_no_write_including_baseline(self):
        self.observe(step(), complete=False)
        self.assertFalse(self.path.exists())
        self.observe(step('completed'))
        self.assertEqual(self.total(), 0)
        self.observe(step())
        before = self.path.read_bytes()
        self.observe(step('completed'), complete=False)
        self.assertEqual(self.path.read_bytes(), before)
        self.observe(step('completed'))
        self.assertEqual(self.total(), 1)

    def test_invalid_scan_is_atomic_and_unknown_can_complete(self):
        self.observe(step(id=1), step('unknown', id=2))
        before = self.path.read_bytes()
        self.observe(step('completed', id=1), step('completed', id=1))
        self.assertEqual(self.path.read_bytes(), before)
        self.observe(step('completed', id=1), step('completed', id=2))
        self.assertEqual(self.total(), 2)

    def test_any_prior_non_completed_state_can_complete(self):
        statuses = ('queued', 'blocked', 'cancelled', 'unknown')
        self.observe(*(step(status, id=index) for index, status in enumerate(statuses)))
        self.observe(*(step('completed', id=index) for index in range(len(statuses))))
        self.assertEqual(self.total(), len(statuses))

    def test_derived_identity_ignores_status_and_mutable_quantities(self):
        self.observe(step('blocked', position=1, qty=10, completed_qty=0),
                     dict(step(position=2, qty=10), job_tag={'name': 'Other'}))
        self.observe(dict(step(position=2, qty=5), job_tag={'name': 'Other'}),
                     step('completed', position=1, qty=20, completed_qty=20))
        self.assertEqual(self.total(), 1)

    def test_upstream_identity_preferred_over_labels_and_position(self):
        self.observe(step('blocked', job_id=9, position=1))
        self.observe(dict(step('completed', job_id=9, position=2),
                          workstationName='ASSEMBLY', job_tag={'name': 'Renamed'}))
        self.assertEqual(self.total(), 1)

    def test_routing_identifiers_persist_across_restart_and_label_changes(self):
        for field in ('routing_id', 'routingId'):
            with self.subTest(field=field):
                self.path = Path(self.directory.name) / (field + '.sqlite3')
                self.tracker = CompletedJobsTracker(self.path)
                self.observe(step(**{field: 1}), step(**{field: 2}))
                self.tracker = CompletedJobsTracker(self.path)
                population = rows(
                    dict(step('completed', **{field: 2}), workstationName='ASSEMBLY'),
                    dict(step('completed', **{field: 1}), job_tag={'name': 'Renamed'}))
                self.tracker.observe(population, complete=True, now=NOW)
                self.tracker.observe(population, complete=True, now=NOW)
                self.assertEqual(self.total(), 2)
                self.assertEqual(self.tracker.summary(NOW)[0]['tracker']['last_scan_new_events'], 0)

    def test_added_routing_identifier_preserves_existing_upstream_baseline(self):
        self.observe(step(job_id=9))
        self.observe(step('completed', routing_id=1, job_id=9))
        self.tracker = CompletedJobsTracker(self.path)
        self.observe(step('completed', routing_id=1))
        self.assertEqual(self.total(), 1)
        self.assertEqual(self.tracker.summary(NOW)[0]['tracker']['tracked_steps'], 1)

    def test_unique_label_fallback_survives_restart_and_reordering(self):
        other = dict(step(), workstationName='ASSEMBLY')
        self.observe(step(), other)
        self.tracker = CompletedJobsTracker(self.path)
        self.observe(dict(other, status='completed'), step('completed'))
        self.assertEqual(self.tracker.summary(NOW)[0]['breakdown_by_workstation'],
                         {'ASSEMBLY': 1, 'LASER': 1})

    def test_api_serializes_persisted_counts_and_separate_compact_diagnostics(self):
        from dashboard.server import create_app
        self.observe(step(id=1), dict(step(id=2), workstationName='ASSEMBLY'))
        self.tracker = CompletedJobsTracker(self.path)
        self.observe(step('completed', id=1),
                     dict(step('completed', id=2), workstationName='ASSEMBLY'))
        self.tracker = CompletedJobsTracker(self.path)
        provider = LocalEpoptiaProvider(read=lambda: None, tracker=self.tracker, clock=lambda: NOW)
        # Exercise Flask serialization without scheduling any reads or workers.
        with patch('dashboard.server.Thread.start'):
            app = create_app(provider=provider, clock=lambda: NOW)
        self.addCleanup(app.extensions['dashboard_stop'].set)
        model = app.test_client().get('/api/dashboard').get_json()
        self.assertEqual(model['completed_today'],
                         dict(total=2, breakdown_by_workstation={'ASSEMBLY': 1, 'LASER': 1}))
        diagnostic = model['tracker_diagnostics']
        self.assertTrue(diagnostic['baseline_exists'])
        self.assertEqual(diagnostic['tracked_step_count'], 2)
        self.assertEqual(diagnostic['last_refresh_new_events'], 2)
        self.assertEqual(diagnostic['status'], 'persisted_awaiting_scan')
        self.assertEqual(diagnostic['source'], 'shared_routing_parser_rest_scan')
        self.assertEqual(len(diagnostic), 7)

    def test_api_fields_present_without_baseline_and_on_store_failure(self):
        from dashboard.server import create_app
        with patch('dashboard.server.Thread.start'):
            app = create_app(provider=lambda: None, clock=lambda: NOW)
        self.addCleanup(app.extensions['dashboard_stop'].set)
        model = app.test_client().get('/api/dashboard').get_json()
        self.assertEqual(model['completed_today'], dict(total=0, breakdown_by_workstation={}))
        self.assertFalse(model['tracker_diagnostics']['baseline_exists'])
        self.path.mkdir()
        self.observe(step())
        provider = LocalEpoptiaProvider(read=lambda: None, tracker=self.tracker, clock=lambda: NOW)
        model = map_snapshot(provider.core_snapshot(), NOW)
        self.assertEqual(model['tracker_diagnostics']['status'], 'unavailable')
        self.assertEqual(model['completed_today'], dict(total=0, breakdown_by_workstation={}))

    def test_active_disappearance_never_infers_completion_or_removes_baseline(self):
        self.observe(step('blocked', id=1), step(id=2))
        for complete in (False, True):
            self.observe(step(id=2), complete=complete)
            self.assertEqual(self.total(), 0)
        self.tracker.observe([], complete=True, now=NOW)
        self.tracker = CompletedJobsTracker(self.path)
        self.observe(step('completed', id=1), step('completed', id=2))
        self.assertEqual(self.total(), 2)

    def test_existing_database_baseline_and_events_are_not_reset(self):
        # Seed the pre-fix on-disk format independently of observe().
        from hashlib import sha256
        import json
        identity = lambda number: sha256(json.dumps(
            ['7', 'upstream', ['id', str(number)]], ensure_ascii=False).encode()).hexdigest()
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE steps (identity TEXT PRIMARY KEY, status TEXT NOT NULL)')
            db.execute('''CREATE TABLE events (step_identity TEXT PRIMARY KEY, wol_id TEXT NOT NULL,
                workstation TEXT NOT NULL, job_name TEXT NOT NULL,
                observed_completed_at TEXT NOT NULL, local_date TEXT NOT NULL)''')
            db.executemany('INSERT INTO steps VALUES (?, ?)',
                           [(identity(1), 'blocked'), (identity(2), 'completed')])
            event = (identity(2), '7', 'LASER', 'Cut', NOW.isoformat(), '2026-09-14')
            db.execute('INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)', event)
        self.observe(step('completed', id=1), step('completed', id=2), step('completed', id=3))
        self.tracker = CompletedJobsTracker(self.path)
        self.assertEqual(self.total(), 2)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT * FROM events WHERE step_identity=?',
                                        (identity(2),)).fetchone(), event)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM steps').fetchone()[0], 3)

    def test_migrate_legacy_identifier_to_stronger_id_and_restart(self):
        from dashboard.completed_jobs import identity_hash
        old = identity_hash(['7', 'upstream', ['job_id', '9']])
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE steps (identity TEXT PRIMARY KEY, status TEXT NOT NULL)')
            db.execute('INSERT INTO steps VALUES (?, ?)', (old, 'blocked'))
        self.observe(step('completed', id=1, job_id=9))
        self.assertEqual(self.total(), 1)
        self.tracker = CompletedJobsTracker(self.path)
        self.observe(step('completed', id=1))
        self.assertEqual(self.total(), 1)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM steps').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT value FROM tracker_metadata WHERE key='deterministic_recovery_events_count'").fetchone()[0], '1')

    def test_legacy_positional_hash_not_used_as_transition_evidence(self):
        from dashboard.completed_jobs import identity_hash
        old = identity_hash(['7', 'derived', 'LASER', 'Cut', ['index', '0']])
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE steps (identity TEXT PRIMARY KEY, status TEXT NOT NULL)')
            db.execute('INSERT INTO steps VALUES (?, ?)', (old, 'started'))
        self.observe(step('completed'))
        self.assertEqual(self.total(), 0)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT status FROM steps WHERE identity=?', (old,)).fetchone()[0], 'started')

    def test_conflicting_legacy_identifiers_never_merge(self):
        self.observe(step(id=1), step(job_id=9))
        self.observe(step('completed', id=1, job_id=9))
        self.assertEqual(self.total(), 0)
        self.assertEqual(self.tracker.status['state'], 'degraded')

    def test_diagnostics_persist_and_duplicate_refresh_reports_zero(self):
        self.observe(step(id=1))
        coverage = self.tracker.summary(NOW)[0]['tracker']['coverage_started_at']
        self.observe(step('completed', id=1))
        self.assertEqual(self.tracker.summary(NOW)[0]['tracker']['last_scan_new_events'], 1)
        self.tracker = CompletedJobsTracker(self.path)
        diagnostic = self.tracker.summary(NOW)[0]['tracker']
        self.assertTrue(diagnostic['baseline_initialized'])
        self.assertEqual(diagnostic['status'], 'persisted_awaiting_scan')
        self.observe(step('completed', id=1), now=NOW + timedelta(minutes=5))
        diagnostic = self.tracker.summary(NOW)[0]['tracker']
        self.assertEqual(diagnostic['last_scan_new_events'], 0)
        self.assertEqual(diagnostic['coverage_started_at'], coverage)
        self.assertEqual(diagnostic['tracked_steps'], 1)
        self.assertEqual(diagnostic['last_scan_steps'], 1)
        self.assertEqual(diagnostic['identity_version'], 2)

    def test_reused_job_id_with_changed_step_id_does_not_merge(self):
        self.observe(step(id=1, job_id=9))
        self.observe(step('completed', id=2, job_id=9))
        self.assertEqual(self.total(), 0)
        self.assertEqual(self.tracker.status['state'], 'degraded')

    def test_failed_transaction_keeps_state_events_and_metadata(self):
        self.observe(step(id=1), step(id=2))
        with sqlite3.connect(self.path) as db:
            before = list(db.iterdump())
            db.execute("CREATE TRIGGER reject_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        self.observe(step('completed', id=1), step('completed', id=2))
        self.assertEqual(self.tracker.status['state'], 'unavailable')
        with sqlite3.connect(self.path) as db:
            after = [line for line in db.iterdump() if not line.startswith('CREATE TRIGGER')]
        self.assertEqual(before, after)

    def runtime_refresh(self, population, *, complete=True):
        provider = LocalEpoptiaProvider(tracker=self.tracker, clock=lambda: NOW,
                                       cache_dir=self.directory.name)
        pages = [population[:1], population[1:]] if len(population) > 1 else [population]
        responses = [Mock(status_code=200, json=Mock(return_value=dict(
            numberOfPages=len(pages), workorderLines=page))) for page in pages]
        if not complete:
            responses[-1].json.side_effect = ValueError('synthetic incomplete page')
        with patch('dashboard.provider.epoptia_queries.application_settings',
                   return_value=dict(base_url='https://synthetic.invalid', headers={})), \
             patch('epoptia_read.requests.get', side_effect=responses) as get:
            asyncio.run(provider.refresh_core('workstation_wip'))
        self.assertEqual(get.call_count, len(pages))
        self.assertTrue(all(call.args[0] ==
            'https://synthetic.invalid/api/3.03/workorderlines' for call in get.call_args_list))
        return provider

    def test_runtime_baseline_transition_dedup_restart_and_api(self):
        population = rows(step('blocked', id=1), step('completed', id=2), step(id=3)) + [
            dict(id=8, production_status='archived'),
            dict(id=9, status='completed', erp_routing=None),
            dict(id=10, status='completed', erp_routing=[step('completed', id=4)])]
        for row in population:
            row['workorder'] = dict(id=row['id'], progress=50)
        provider = self.runtime_refresh(population)
        diagnostic = map_snapshot(provider.core_snapshot(), NOW)['tracker_diagnostics']
        self.assertTrue(diagnostic['baseline_exists'])
        self.assertEqual(diagnostic['tracked_step_count'], 4)
        self.assertEqual(self.total(), 0)
        self.tracker = CompletedJobsTracker(self.path)
        population[0]['erp_routing'][0]['status'] = 'completed'
        before = self.path.read_bytes()
        self.runtime_refresh(population, complete=False)
        self.assertEqual(self.path.read_bytes(), before)
        provider = self.runtime_refresh(population)
        self.assertEqual(self.total(), 1)
        self.tracker = CompletedJobsTracker(self.path)
        provider = self.runtime_refresh(population)
        self.assertEqual(self.total(), 1)
        self.assertEqual(self.tracker.summary(NOW)[0]['tracker']['last_scan_new_events'], 0)
        from dashboard.server import create_app
        with patch('dashboard.server.Thread'):
            app = create_app(provider=provider, clock=lambda: NOW)
        response = app.test_client().get('/api/dashboard')
        self.assertEqual(response.status_code, 200)
        model = response.get_json()
        self.assertEqual(model['completed_today'], dict(total=1, breakdown_by_workstation={'LASER': 1}))
        self.assertEqual(model['tracker_diagnostics']['tracked_step_count'], 4)
        self.assertTrue(model['tracker_diagnostics']['baseline_exists'])

    def test_runtime_incomplete_or_missing_routing_never_creates_baseline(self):
        for population, complete in ((rows(step(id=1)) + [dict(id=8)], False),
                                     ([dict(id=7)], True),
                                     ([dict(id=7, status='completed')], True),
                                     (rows(dict(workstationName='LASER', id=1)), True)):
            with self.subTest(complete=complete, population=population):
                self.runtime_refresh(population, complete=complete)
                self.assertFalse(self.path.exists())
                self.assertEqual(self.total(), 0)
                self.assertFalse(self.tracker.summary(NOW)[0]['tracker']['baseline_initialized'])

    def test_runtime_parent_progress_increase_requires_observed_step_transition(self):
        population = rows(step('started', id=1, qty_done=10))
        population[0]['workorder'] = dict(id=701, progress=78.43)
        self.runtime_refresh(population)
        population[0]['workorder']['progress'] = 79.28
        population[0]['erp_routing'][0]['qty_done'] = 11
        self.tracker = CompletedJobsTracker(self.path)
        self.runtime_refresh(population)
        self.assertEqual(self.total(), 0)
        self.assertEqual(self.tracker.summary(NOW)[0]['tracker']['tracked_steps'], 1)

    def test_runtime_parent_progress_transition_with_reordered_normalized_routing(self):
        population = rows(step(' started ', id=1), step('completed', id=2))
        population[0]['workorder'] = dict(id=701, progress=78.43)
        self.runtime_refresh(population)
        coverage = self.tracker.summary(NOW)[0]['tracker']['coverage_started_at']
        population[0]['workorder']['progress'] = 79.28
        population[0]['erp_routing'] = [step('completed', id=2),
                                       step(' COMPLETED ', id=1),
                                       step('completed', id=3)]
        # An incomplete scan must preserve the baseline, even with a real
        # transition and an already-completed newly discovered step present.
        before = self.path.read_bytes()
        self.runtime_refresh(population + [dict(id=8)], complete=False)
        self.assertEqual(self.path.read_bytes(), before)
        for _ in range(2):
            self.tracker = CompletedJobsTracker(self.path)
            self.runtime_refresh(population)
            summary = self.tracker.summary(NOW)[0]
            self.assertEqual(summary['total'], 1)
            self.assertEqual(summary['breakdown_by_workstation'], {'LASER': 1})
            self.assertEqual(summary['tracker']['tracked_steps'], 3)
            self.assertEqual(summary['tracker']['coverage_started_at'], coverage)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT wol_id, local_date FROM events').fetchall(),
                             [('7', '2026-09-14')])

    def test_completed_rest_scan_persists_before_station_projection_failure(self):
        population = rows(step('started', id=1), step('completed', id=2))
        parse = epoptia_read.parse_routing_step

        def station_failure(*args):
            # The REST population is complete even if a downstream consumer fails.
            self.assertTrue(self.path.exists())
            raise ValueError('synthetic station projection failure')

        with patch('dashboard.stations.collect_stations', side_effect=station_failure), \
             patch.object(self.tracker, 'observe', wraps=self.tracker.observe) as observe, \
             patch('epoptia_read.parse_routing_step', wraps=parse) as parser:
            provider = self.runtime_refresh(population)
        observe.assert_called_once_with(population, complete=True, now=NOW)
        self.assertEqual(parser.call_count, 2)
        self.assertIs(provider.read.tracker, provider.tracker)
        self.assertEqual(provider.tracker.path, self.path.absolute())
        self.assertEqual(provider.sources['workstation_wip']['state'], 'unavailable')
        # Reopen the updater's store in the API provider, without another scan.
        restored = LocalEpoptiaProvider(read=lambda: None,
            tracker=CompletedJobsTracker(self.path), clock=lambda: NOW)
        from dashboard.server import create_app
        with patch('dashboard.server.Thread'):
            app = create_app(provider=restored, clock=lambda: NOW)
        model = app.test_client().get('/api/dashboard').get_json()
        self.assertTrue(model['tracker_diagnostics']['baseline_exists'])
        self.assertEqual(model['tracker_diagnostics']['tracked_step_count'], 2)
        self.assertEqual(model['completed_today']['total'], 0)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)

    def test_athens_day_rollover_summer_and_winter(self):
        for month, hour in ((9, 21), (1, 22)):
            midnight = datetime(2026, month, 15, hour, tzinfo=timezone.utc)
            self.observe(step(id=month), now=midnight-timedelta(seconds=2))
            self.observe(step('completed', id=month), now=midnight-timedelta(seconds=1))
            self.assertEqual(self.total(midnight-timedelta(seconds=1)), 1)
            self.assertEqual(self.total(midnight), 0)

    def test_workstation_totals_and_terminal_parent(self):
        other = dict(step(), workstationName='ASSEMBLY')
        self.observe(step(id=1), step(id=2), other)
        population = rows(step('completed', id=1), step('completed', id=2), dict(other, status='completed'))
        population[0]['production_status'] = 'completed'
        self.tracker.observe(population, complete=True, now=NOW)
        self.assertEqual(self.tracker.summary(NOW)[0]['total'], 3)
        self.assertEqual(self.tracker.summary(NOW)[0]['breakdown_by_workstation'],
                         {'ASSEMBLY': 1, 'LASER': 2})

    def test_store_failure_keeps_dashboard_available(self):
        self.path.mkdir()
        self.observe(step())
        provider = LocalEpoptiaProvider(read=lambda: None, tracker=self.tracker, clock=lambda: NOW)
        model = map_snapshot(provider.core_snapshot(), NOW)
        self.assertEqual(model['completion_tracker']['reason'], 'tracker_store_unavailable')
        self.assertEqual(model['completed_today']['total'], 0)
        self.assertIn('workstations', model)

    def test_shared_rest_scan_observed_once_and_cancelled_scan_ignored(self):
        reader = DirectReader(tracker=self.tracker, clock=lambda: NOW)
        settings = dict(base_url='synthetic', headers={})
        response = Mock(status_code=200, json=Mock(return_value=dict(
            numberOfPages=1, workorderLines=rows(step()))))
        with patch('epoptia_read.requests.get', return_value=response) as scan, \
             patch.object(self.tracker, 'observe', wraps=self.tracker.observe) as observe:
            reader._shared_wols('production_overview', settings, Event())
            reader._shared_wols('workstation_wip', settings, Event())
            self.assertEqual(scan.call_count, 1)
            self.assertTrue(self.path.exists())
            self.assertEqual(observe.call_count, 1)
        before = self.path.read_bytes()
        cancelled = Event()
        cancelled.set()
        reader._observe_routing(rows(step('completed')), complete=True, cancelled=cancelled)
        self.assertEqual(self.path.read_bytes(), before)

    def test_runtime_collectors_share_one_32_page_rest_scan(self):
        reader = DirectReader(tracker=self.tracker, clock=lambda: NOW)
        responses = [Mock(status_code=200, json=Mock(return_value=dict(
            numberOfPages=32, workorderLines=[dict(workorderline_id=i,
                production_status='production', erp_routing=[step('not_started', id=1)])])))
            for i in range(32)]

        def overview(base_url, *, username, password, headers, wol_snapshot):
            # The capacity-planning result need not contain routing. Only the
            # shared REST snapshot is allowed to feed the completion tracker.
            wol_snapshot()
            return dict(ok=True)

        async def refresh():
            reader.begin_snapshot()
            await asyncio.gather(reader('workstation_wip'), reader('production_overview'))

        with patch('dashboard.provider.epoptia_queries.application_settings', return_value=dict(
                base_url='https://synthetic.invalid', headers={}, username=None, password=None)), \
             patch('dashboard.provider.epoptia_queries.production_overview', side_effect=overview), \
             patch('epoptia_read.requests.get', side_effect=responses) as get, \
             patch.object(self.tracker, 'observe', wraps=self.tracker.observe) as observe:
            asyncio.run(refresh())
        self.assertEqual(get.call_count, 32)
        self.assertEqual(observe.call_count, 1)
        summary = self.tracker.summary(NOW)[0]
        self.assertEqual(summary['tracker']['tracked_steps'], 32)
        self.assertEqual(summary['total'], 0)

    def test_shared_incomplete_and_cancelled_rest_scans_do_not_mutate_store(self):
        self.observe(step('not_started', id=1))
        before = self.path.read_bytes()
        reader = DirectReader(tracker=self.tracker, clock=lambda: NOW)
        cancelled = Event()
        with patch('epoptia_read.fetch_wols', side_effect=epoptia_read.ReadError('synthetic')), \
             patch.object(self.tracker, 'observe', wraps=self.tracker.observe) as observe:
            reader._shared_wols('workstation_wip', dict(base_url='synthetic', headers={}), cancelled)
        observe.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

        def cancel_scan(*args, scan):
            scan.update(complete=True)
            cancelled.set()
            return rows(step('completed', id=1))

        with patch('epoptia_read.fetch_wols', side_effect=cancel_scan):
            with self.assertRaises(ValueError):
                reader._shared_wols('workstation_wip', dict(base_url='synthetic', headers={}), cancelled)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
