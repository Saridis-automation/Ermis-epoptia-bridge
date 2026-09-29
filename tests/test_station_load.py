"""Relative station counts, reliability gating and local serving regressions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from dashboard.adapter import map_snapshot
from dashboard.provider import empty_snapshot
from dashboard.station_activity import StationActivity
from dashboard.stations import collect_stations

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)


def snapshot(counts=(26, 35, 145, 50, 19, 69, 98)):
    data = empty_snapshot(NOW)
    names = ('LASER', 'ΚΟΠΗ ΨΑΛΙΔΙ', 'ΜΟΝΤΑΖ 1', 'ΜΟΝΤΑΖ 2', 'ΜΟΝΤΑΖ ΤΖΑΜΙΑ', 'ΣΤΡΑΝΤΖΑ', 'ΨΥΚΤΙΚΑ')
    data.update(workstations=[dict(name=name, pending_steps=count, load_percent=None)
                              for name, count in zip(names, counts)],
                station_coverage={'available': True},
                field_status={'workstations': 'available'},
                field_observed_at={'workstations': NOW.isoformat()})
    return data


def percentages(data, now=NOW):
    return [row['load_percent'] for row in map_snapshot(data, now)['workstations']]


class StationLoadTests(unittest.TestCase):
    def test_varied_counts_with_missing_capacity_and_null_upstream(self):
        data = snapshot()
        self.assertEqual(percentages(data), [18, 24, 100, 34, 13, 48, 68])
        self.assertEqual([s['pending_steps'] for s in map_snapshot(data, NOW)['workstations']],
                         [26, 35, 145, 50, 19, 69, 98])

    def test_capacity_deadlines_and_wip_do_not_override_pending_metric(self):
        data = snapshot((1, 4))
        expected = map_snapshot(data, NOW)
        for row in data['workstations']:
            row.update(capacity_target=1, pending_wol_deadlines={'2020-01-01': 999}, wip_steps=0)
        self.assertEqual(percentages(data), [25, 100])
        self.assertEqual(percentages(data, NOW + timedelta(seconds=1)), [25, 100])
        self.assertEqual(map_snapshot(data, NOW)['urgent_orders'], expected['urgent_orders'])
        self.assertEqual(map_snapshot(data, NOW)['today'], expected['today'])

    def test_zero_single_and_absent_counts(self):
        self.assertEqual(percentages(snapshot((0, 0))), [0, 0])
        self.assertEqual(percentages(snapshot((0, 8))), [0, 100])
        self.assertEqual(percentages(snapshot((8,))), [100])
        self.assertEqual(percentages(snapshot((0,))), [0])
        self.assertEqual(percentages(snapshot((None,))), [None])
        self.assertTrue(all(v is None for v in percentages(empty_snapshot(NOW))))

    def test_invalid_count_invalidates_shared_denominator(self):
        for invalid in (None, True, -1, 0.5, '8', float('nan'), float('inf')):
            with self.subTest(invalid=invalid):
                self.assertEqual(percentages(snapshot((8, invalid, 0))), [None] * 3)
        data = snapshot((8, 2))
        del data['workstations'][1]['pending_steps']
        self.assertEqual(percentages(data), [None, None])

    def test_partial_failed_stale_or_unverified_source(self):
        for status in ('partial', 'cached', 'unavailable', 'loading', 'refreshing', 'stale'):
            data = snapshot((8, 0))
            data['field_status']['workstations'] = status
            self.assertEqual(percentages(data), [None, None])
        for meta in ({'state': 'cached'}, {'failure_reason': 'timeout'}, {'stale': True}):
            data = snapshot((8, 0))
            data['sources'] = {'workstation_wip': meta}
            self.assertEqual(percentages(data), [None, None])
        data = snapshot((8, 0))
        data['station_coverage']['available'] = False
        self.assertEqual(percentages(data), [None, None])
        self.assertEqual(percentages(snapshot((8, 0)), NOW + timedelta(seconds=121)), [None, None])
        data = snapshot((8, 0))
        data['field_status']['urgent_orders'] = 'partial'
        self.assertEqual(percentages(data), [100, 0])

    def test_exclusions_do_not_inflate_denominator(self):
        data = snapshot((1, 4))
        data['workstations'] += [dict(name=name, pending_steps=9999) for name in
                                ('PUNCHING', 'ΕΙΣΑΓΩΓΗ ΠΑΡΑΓΓΕΛΙΑΣ', 'ΠΑΡΑΛΑΒΗ ΠΑΡΑΓΓΕΛΙΑΣ')]
        self.assertEqual(percentages(data), [25, 100])

    def test_routing_counts_dedup_terminal_dates_and_incomplete_scan(self):
        row = dict(id=1, production_status='production', target_day=None, erp_routing=[
            dict(id=1, workstationName='LASER', status='started'),
            dict(id=2, workstationName='LASER', status='not_started'),
            dict(id=3, workstationName='ΜΟΝΤΑΖ 2', status='paused'),
            dict(id=4, workstationName='ΜΟΝΤΑΖ 2', status='completed')])
        terminal = dict(row, id=2, production_status='archived')
        for complete in (True, False):
            projection = collect_stations([row, deepcopy(row), terminal], complete)
            data = snapshot()
            data['station_coverage']['available'] = projection['complete']
            data['workstations'] = StationActivity().decorate(projection['workstations'])
            values = {s['name']: s for s in map_snapshot(data, NOW)['workstations']}
            self.assertEqual(values['LASER']['pending_steps'], 2 if complete else None)
            self.assertEqual(values['LASER']['load_percent'], 100 if complete else None)
            self.assertEqual(values['ΜΟΝΤΑΖ 2']['load_percent'], 50 if complete else None)
            self.assertEqual(values['ΨΥΚΤΙΚΑ']['load_percent'], 0 if complete else None)

    def test_local_serving_and_cache_headers(self):
        from dashboard.server import create_app
        class Provider:
            refresh_sources = ()
            async def refresh_core(self, tool):
                raise AssertionError('No source reads permitted')
            def core_snapshot(self):
                return snapshot((1, 4))
        app = create_app(Provider(), clock=lambda: NOW)
        self.addCleanup(app.extensions['dashboard_stop'].set)
        with app.test_client() as client:
            for path in ('/', '/static/dashboard.js', '/api/dashboard'):
                with client.get(path) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    if path == '/api/dashboard':
                        self.assertEqual([s['load_percent'] for s in response.json['workstations']], [25, 100])
                    elif path.endswith('.js'):
                        self.assertIn('Σχετικός αριθμός'.encode(), response.data)
