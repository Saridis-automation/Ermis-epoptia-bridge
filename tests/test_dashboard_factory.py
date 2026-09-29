"""Factory dashboard semantics, synthetic complete snapshots only."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from unittest.mock import patch
from dashboard.stations import collect_stations
from dashboard.station_activity import StationActivity, STATION_CAPACITY_TARGETS, star_state
from dashboard.adapter import map_snapshot
from dashboard.provider import LocalEpoptiaProvider, empty_snapshot

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)


def rows(status='started'):
    return [dict(id=1, production_status='production', erp_routing=[
        dict(id=1, workstationName='LASER', status=status),
        dict(id=2, workstationName='LASER', status='paused'),
        dict(id=3, workstationName='LASER', status='not_started'),
        dict(id=4, workstationName='LASER', status='future'),
        dict(id=5, workstationName='LASER', status='completed')])]


class FactoryTests(unittest.TestCase):
    def test_pending_deduplicated_and_no_archive(self):
        raw = rows()
        raw[0]['erp_routing'].append(deepcopy(raw[0]['erp_routing'][0]))
        raw += [deepcopy(raw[0]), dict(rows()[0], id=2, production_status='archive')]
        result = collect_stations(raw, True)
        self.assertTrue(result['complete'])
        self.assertEqual(result['workstations'][0]['pending_steps'], 4)
        self.assertEqual(result['diagnostics']['duplicate_wols'], 1)
        self.assertEqual(result['diagnostics']['excluded_terminal_wols'], 1)

    def test_alternate_step_identity_deduplicates(self):
        raw = rows()
        for step in raw[0]['erp_routing']:
            step['stepId'] = step.pop('id')
        raw[0]['erp_routing'].append(deepcopy(raw[0]['erp_routing'][0]))
        result = collect_stations(raw, True)
        self.assertTrue(result['complete'])
        self.assertEqual(result['workstations'][0]['pending_steps'], 4)

    def test_fingerprint_detects_status_swaps_between_named_jobs(self):
        raw = rows()
        raw[0]['erp_routing'] = [dict(workstationName='LASER', jobName=job, status=status)
            for job, status in [('A','started'), ('B','paused')]]
        before = collect_stations(raw, True)['workstations'][0]['state_fingerprint']
        raw[0]['erp_routing'][0]['status'] = 'paused'
        raw[0]['erp_routing'][1]['status'] = 'started'
        self.assertNotEqual(collect_stations(raw, True)['workstations'][0]['state_fingerprint'], before)

    def test_partial_and_unknown_never_claim_zero_pending(self):
        for raw, complete in [(rows(), False), (rows('mystery'), True)]:
            self.assertIsNone(collect_stations(raw, complete)['workstations'][0]['pending_steps'])

    def test_conflicting_step_rejected(self):
        raw = rows()
        raw[0]['erp_routing'].append(dict(raw[0]['erp_routing'][0], status='paused'))
        self.assertFalse(collect_stations(raw, True)['complete'])

    def test_star_boundaries(self):
        for seconds, expected in [(-1,'neutral'), (0,'gold'), (3599.999,'gold'),
                (3600,'neutral'), (7200,'neutral'), (7200.001,'red')]:
            with self.subTest(seconds=seconds):
                self.assertEqual(star_state((NOW-timedelta(seconds=seconds)).isoformat(), NOW), expected)
        for at in [None, '', 'invalid', '2026-09-14T12:00:00']:
            self.assertEqual(star_state(at, NOW), 'neutral')

    def test_stable_fingerprint_ignores_ordering_and_wol_duplicates(self):
        raw = rows()
        before = collect_stations(raw, True)['workstations'][0]['state_fingerprint']
        raw[0]['erp_routing'].reverse()
        raw.append(deepcopy(raw[0]))
        self.assertEqual(collect_stations(raw, True)['workstations'][0]['state_fingerprint'], before)
        raw[0]['erp_routing'][0]['status'] = 'started'
        raw.pop()
        self.assertNotEqual(collect_stations(raw, True)['workstations'][0]['state_fingerprint'], before)

    def test_counts_unchanged_state_change_still_observed(self):
        tracker = StationActivity()
        tracker.observe(collect_stations(rows(), True), NOW)
        self.assertIsNone(tracker.state['LASER']['last_change_at'])
        tracker.observe(collect_stations(rows('paused'), True), NOW+timedelta(minutes=1))
        self.assertEqual(tracker.state['LASER']['last_change_at'], (NOW+timedelta(minutes=1)).isoformat())

    def test_persistence_restart_and_incomplete_snapshots(self):
        with tempfile.TemporaryDirectory(dir='dashboard') as directory:
            tracker = StationActivity(directory)
            tracker.observe(collect_stations(rows(), True), NOW)
            restarted = StationActivity(directory)
            restarted.observe(collect_stations(rows('paused'), False), NOW+timedelta(minutes=1))
            self.assertIsNone(restarted.state['LASER']['last_change_at'])
            restarted.observe(collect_stations(rows('paused'), True), NOW+timedelta(minutes=2))
            again = StationActivity(directory)
            again.observe(collect_stations(rows('paused'), True), NOW+timedelta(hours=3))
            self.assertEqual(again.state, restarted.state)
            self.assertEqual(star_state(again.state['LASER']['last_change_at'], NOW+timedelta(hours=3)), 'red')

    def test_removal_and_new_station_work_are_changes(self):
        tracker = StationActivity()
        tracker.observe(collect_stations([], True), NOW)
        tracker.observe(collect_stations(rows(), True), NOW+timedelta(minutes=1))
        self.assertIsNotNone(tracker.state['LASER']['last_change_at'])
        tracker.observe(collect_stations([], True), NOW+timedelta(minutes=2))
        self.assertEqual(tracker.state['LASER']['last_change_at'], (NOW+timedelta(minutes=2)).isoformat())

    def test_explicit_targets_do_not_rescale_with_pending_counts(self):
        tracker = StationActivity()
        projection = collect_stations(rows(), True)
        projection['workstations'][0]['pending_steps'] = 70
        tracker.observe(projection, NOW)
        self.assertEqual(tracker.state['LASER']['capacity_target'], 100)
        projection['workstations'][0]['pending_steps'] = 140
        tracker.observe(projection, NOW)
        self.assertEqual(tracker.state['LASER']['capacity_target'], 100)
        with patch.dict(STATION_CAPACITY_TARGETS, LASER=200):
            self.assertEqual(tracker.decorate(projection['workstations'])[0]['capacity_target'], 200)

    def test_visible_stations_load_clamp_and_unknown(self):
        snapshot = empty_snapshot(NOW)
        tracker = StationActivity()
        projection = collect_stations(rows(), True)
        tracker.observe(projection, NOW)
        snapshot['workstations'] = tracker.decorate(projection['workstations']) + [
            dict(name='ΕΙΣΑΓΩΓΗ ΠΑΡΑΓΓΕΛΙΑΣ'), dict(name='ΠΑΡΑΛΑΒΗ ΠΑΡΑΓΓΕΛΙΑΣ')]
        model = map_snapshot(snapshot, NOW)
        self.assertEqual(set(row['name'] for row in model['workstations']), set(STATION_CAPACITY_TARGETS))
        self.assertIsNone(model['workstations'][0]['load_percent'])
        # Counts alone do not attest source completeness.
        for pending in (0, 200, -1, None):
            snapshot['workstations'][0]['pending_steps'] = pending
            self.assertIsNone(map_snapshot(snapshot, NOW)['workstations'][0]['load_percent'])

    def test_corrupt_persistence_resets_to_neutral(self):
        from dashboard.cache import SnapshotCache
        with tempfile.TemporaryDirectory(dir='dashboard') as directory:
            cache = SnapshotCache(directory)
            for payload in [[], ['invalid'], {'LASER': {'fingerprint':'x', 'capacity_target':10,
                    'last_change_at':'not a timestamp'}}]:
                cache.save('station-activity-v2', payload)
                tracker = StationActivity(directory)
                tracker.observe(collect_stations(rows(), True), NOW)
                self.assertIsNone(tracker.state['LASER']['last_change_at'])

    def test_snapshot_remains_consistent_until_new_publication(self):
        async def read(tool):
            return dict(ok=True, complete=True, wol_rows=rows())
        provider = LocalEpoptiaProvider(read=read, clock=lambda:NOW)
        asyncio.run(provider.refresh_core('workstation_wip'))
        before = provider.core_snapshot()['workstations']
        provider.station_activity.observe(collect_stations(rows('paused'), True), NOW+timedelta(minutes=1))
        self.assertEqual(provider.core_snapshot()['workstations'], before)

    def test_provider_restart_persists_observations_and_failures_do_not_change_them(self):
        with tempfile.TemporaryDirectory(dir='dashboard') as directory:
            raw = rows()
            async def read(tool):
                return dict(ok=True, complete=True, wol_rows=raw)
            provider = LocalEpoptiaProvider(read=read, cache_dir=directory, clock=lambda:NOW)
            asyncio.run(provider.refresh_core('workstation_wip'))
            raw[0]['erp_routing'][0]['status'] = 'paused'
            provider = LocalEpoptiaProvider(read=read, cache_dir=directory, clock=lambda:NOW+timedelta(minutes=5))
            asyncio.run(provider.refresh_core('workstation_wip'))
            self.assertEqual(map_snapshot(provider.core_snapshot(), NOW+timedelta(minutes=5))['workstations'][0]['star_state'], 'gold')
            async def failed(tool):
                return dict(ok=False)
            provider = LocalEpoptiaProvider(read=failed, cache_dir=directory, clock=lambda:NOW+timedelta(hours=3))
            asyncio.run(provider.refresh_core('workstation_wip'))
            station = map_snapshot(provider.core_snapshot(), NOW+timedelta(hours=3))['workstations'][0]
            self.assertEqual(station['star_state'], 'red')
            self.assertEqual(station['pending_steps'], 4)


class Factory2Tests(unittest.TestCase):
    def test_exact_station_config_and_alias(self):
        from dashboard.station_activity import station_name
        self.assertEqual(list(STATION_CAPACITY_TARGETS), ['LASER', 'ΚΟΠΗ ΨΑΛΙΔΙ',
            'ΣΤΡΑΝΤΖΑ', 'ΜΟΝΤΑΖ 1', 'ΜΟΝΤΑΖ 2', 'ΜΟΝΤΑΖ ΤΖΑΜΙΑ', 'ΨΥΚΤΙΚΑ'])
        self.assertEqual(station_name(' ΣΤΡΑΤΖΑ '), 'ΣΤΡΑΝΤΖΑ')

    def test_full_step_progress_changes_persist_without_count_changes(self):
        with tempfile.TemporaryDirectory(dir='dashboard') as directory:
            raw = rows()
            tracker = StationActivity(directory)
            tracker.observe(collect_stations(raw, True), NOW)
            for index, (field, value) in enumerate([('qty_done', 2),
                    ('progress_detail', {'done': 3}), ('quantity', 12)], 1):
                raw[0]['erp_routing'][0][field] = value
                at = NOW + timedelta(minutes=index)
                tracker.observe(collect_stations(raw, True), at)
                tracker = StationActivity(directory)
                self.assertEqual(tracker.state['LASER']['last_change_at'], at.isoformat())
                tracker.observe(collect_stations(raw, True), at + timedelta(seconds=30))
                self.assertEqual(tracker.state['LASER']['last_change_at'], at.isoformat())
                self.assertIsNone(tracker.state['ΨΥΚΤΙΚΑ']['last_change_at'])
                self.assertEqual(collect_stations(raw, True)['workstations'][0]['pending_steps'], 4)

    def test_v1_migration_baseline_neutral_uses_configured_capacity(self):
        from dashboard.cache import SnapshotCache
        with tempfile.TemporaryDirectory(dir='dashboard') as directory:
            SnapshotCache(directory).save('station-activity-v1', {'LASER': {
                'fingerprint': 'old-signature', 'last_change_at': NOW.isoformat(), 'capacity_target': 80}})
            tracker = StationActivity(directory)
            tracker.observe(collect_stations(rows(), True), NOW)
            tracker = StationActivity(directory)
            self.assertEqual(tracker.state['LASER']['capacity_target'], 100)
            self.assertIsNone(tracker.state['LASER']['last_change_at'])
            self.assertEqual(star_state(tracker.state['LASER']['last_change_at'], NOW), 'neutral')
            tracker.observe(collect_stations(rows('paused'), True), NOW+timedelta(minutes=1))
            self.assertIsNotNone(StationActivity(directory).state['LASER']['last_change_at'])

    def test_excluded_stations_never_enter_api(self):
        snapshot = empty_snapshot(NOW)
        snapshot['workstations'] += [dict(name=name, pending_steps=999, capacity_target=10)
            for name in ['PUNCHING', 'ΕΙΣΑΓΩΓΗ ΠΑΡΑΓΓΕΛΙΑΣ', 'ΠΑΡΑΛΑΒΗ ΠΑΡΑΓΓΕΛΙΑΣ']]
        self.assertEqual([s['name'] for s in map_snapshot(snapshot, NOW)['workstations']],
                         list(STATION_CAPACITY_TARGETS))
