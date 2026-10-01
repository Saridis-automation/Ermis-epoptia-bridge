"""Station load from the weighted product model, reliability gating and local serving."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import unittest

from dashboard.adapter import map_snapshot
from dashboard.provider import empty_snapshot
from dashboard.station_activity import StationActivity
from dashboard.schedule import freshness_seconds
from dashboard.stations import attach_load_model, collect_stations

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
NAMES = ('LASER', 'ΚΟΠΗ ΨΑΛΙΔΙ', 'ΜΟΝΤΑΖ 1', 'ΜΟΝΤΑΖ 2', 'ΜΟΝΤΑΖ ΤΖΑΜΙΑ', 'ΣΤΡΑΝΤΖΑ', 'ΨΥΚΤΙΚΑ')


def model(load, by='2026-10-08', needed=5.0, fits=4.4):
    return dict(load_percent=load, products=3, days_of_work=2.0, capacity_per_day=1.0,
                tightest=dict(by=by, needed=needed, fits=fits))


def snapshot(counts=(26, 35, 145, 50, 19, 69, 98), loads=(38, 40, 93, 45, 114, 70, 59)):
    data = empty_snapshot(NOW)
    data.update(workstations=[dict(name=name, pending_steps=count, load_percent=None,
                                   load_model=None if load is None else model(load))
                              for name, count, load in zip(NAMES, counts, loads)],
                station_coverage={'available': True},
                field_status={'workstations': 'available'},
                field_observed_at={'workstations': NOW.isoformat()})
    return data


def percentages(data, now=NOW):
    return [row['load_percent'] for row in map_snapshot(data, now)['workstations']]


class StationLoadTests(unittest.TestCase):
    def test_model_load_is_served_including_overload(self):
        data = snapshot()
        self.assertEqual(percentages(data), [38, 40, 93, 45, 114, 70, 59])
        served = map_snapshot(data, NOW)['workstations'][4]
        self.assertEqual(served['load_model']['tightest'], dict(by='2026-10-08', needed=5.0, fits=4.4))
        self.assertEqual([s['pending_steps'] for s in map_snapshot(data, NOW)['workstations']],
                         [26, 35, 145, 50, 19, 69, 98])

    def test_no_open_work_is_zero_and_missing_model_is_unknown(self):
        self.assertEqual(percentages(snapshot((0, 8), (None, 60))), [0, 60])
        self.assertEqual(percentages(snapshot((8,), (None,))), [None])
        self.assertTrue(all(v is None for v in percentages(empty_snapshot(NOW))))

    def test_invalid_count_makes_census_unreliable(self):
        for invalid in (None, True, -1, 0.5, '8', float('nan'), float('inf')):
            with self.subTest(invalid=invalid):
                self.assertEqual(percentages(snapshot((8, invalid, 0), (50, 50, 0))), [None] * 3)

    def test_partial_failed_stale_or_unverified_source(self):
        for status in ('partial', 'cached', 'unavailable', 'loading', 'refreshing', 'stale'):
            data = snapshot((8, 0), (80, 0))
            data['field_status']['workstations'] = status
            self.assertEqual(percentages(data), [None, None])
        for meta in ({'state': 'cached'}, {'failure_reason': 'timeout'}, {'stale': True}):
            data = snapshot((8, 0), (80, 0))
            data['sources'] = {'workstation_wip': meta}
            self.assertEqual(percentages(data), [None, None])
        data = snapshot((8, 0), (80, 0))
        data['station_coverage']['available'] = False
        self.assertEqual(percentages(data), [None, None])
        self.assertEqual(percentages(snapshot((8, 0), (80, 0)),
                                     NOW + timedelta(seconds=freshness_seconds(NOW) + 1)), [None, None])
        data = snapshot((8, 0), (80, 0))
        data['field_status']['urgent_orders'] = 'partial'
        self.assertEqual(percentages(data), [80, 0])

    def test_excluded_stations_are_not_served(self):
        data = snapshot((1, 4), (10, 20))
        data['workstations'] += [dict(name=name, pending_steps=9999, load_model=model(999)) for name in
                                ('PUNCHING', 'ΕΙΣΑΓΩΓΗ ΠΑΡΑΓΓΕΛΙΑΣ', 'ΠΑΡΑΛΑΒΗ ΠΑΡΑΓΓΕΛΙΑΣ')]
        self.assertEqual(percentages(data), [10, 20])

    def test_collect_stations_attaches_model_only_on_complete_scan(self):
        row = dict(id=1, workorderline_id=1, production_status='production', target_day='2026-10-20',
                   quantity=1, description='Τραπέζι εργασίας', erp_routing=[
            dict(id=1, elementId=11, previous=[], workstationName='LASER', status='started'),
            dict(id=2, elementId=12, previous=[11], workstationName='LASER', status='not_started'),
            dict(id=3, elementId=13, previous=[12], workstationName='ΜΟΝΤΑΖ 2', status='paused'),
            dict(id=4, elementId=14, previous=[13], workstationName='ΜΟΝΤΑΖ 2', status='completed')])
        terminal = dict(row, id=2, workorderline_id=2, production_status='archived')
        for complete in (True, False):
            projection = collect_stations([row, deepcopy(row), terminal], complete)
            data = snapshot()
            data['station_coverage']['available'] = projection['complete']
            data['workstations'] = StationActivity().decorate(projection['workstations'])
            values = {s['name']: s for s in map_snapshot(data, NOW)['workstations']}
            self.assertEqual(values['LASER']['pending_steps'], 2 if complete else None)
            if complete:
                self.assertIsInstance(values['LASER']['load_percent'], int)
                self.assertIsInstance(values['ΜΟΝΤΑΖ 2']['load_percent'], int)
                self.assertEqual(values['ΨΥΚΤΙΚΑ']['load_percent'], 0)
            else:
                self.assertIsNone(values['LASER']['load_percent'])

    def test_attach_model_failure_degrades_to_unknown(self):
        result = [dict(name='LASER')]
        attach_load_model(result, [None], True,
                          today=date(2026, 10, 1))
        self.assertIsNone(result[0]['load_model'])

    def test_local_serving_and_cache_headers(self):
        from dashboard.server import create_app
        class Provider:
            refresh_sources = ()
            async def refresh_core(self, tool):
                raise AssertionError('No source reads permitted')
            def core_snapshot(self):
                return snapshot((1, 4), (25, 114))
        app = create_app(Provider(), clock=lambda: NOW)
        self.addCleanup(app.extensions['dashboard_stop'].set)
        with app.test_client() as client:
            for path in ('/', '/static/dashboard.js', '/api/dashboard'):
                with client.get(path) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    if path == '/api/dashboard':
                        self.assertEqual([s['load_percent'] for s in response.json['workstations']], [25, 114])
                    elif path.endswith('.js'):
                        self.assertIn('Φόρτος σταθμού'.encode(), response.data)


if __name__ == '__main__':
    unittest.main()
