"""Revision 3 regression checks; synthetic data, no live reads."""
import unittest
from datetime import datetime, timezone
from dashboard.adapter import map_snapshot
from dashboard.orders import OrderCensus
from dashboard.provider import empty_snapshot
from dashboard.station_activity import STATION_CAPACITY_TARGETS, StationActivity
from dashboard.stations import collect_stations

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)


class Revision3Tests(unittest.TestCase):
    def test_legacy_station_budgets_cannot_attest_complete_counts(self):
        self.assertEqual(STATION_CAPACITY_TARGETS, {
            'LASER': 100, 'ΚΟΠΗ ΨΑΛΙΔΙ': 60, 'ΣΤΡΑΝΤΖΑ': 80,
            'ΜΟΝΤΑΖ 1': 50, 'ΜΟΝΤΑΖ 2': 50, 'ΜΟΝΤΑΖ ΤΖΑΜΙΑ': 40, 'ΨΥΚΤΙΚΑ': 30})
        snapshot = empty_snapshot(NOW)
        for pending in (0, 15, 30, 1000):
            snapshot['workstations'] = StationActivity().decorate([
                dict(name=name, pending_steps=pending) for name in STATION_CAPACITY_TARGETS])
            actual = map_snapshot(snapshot, NOW)['workstations']
            for row in actual:
                self.assertIsNone(row['load_percent'])

    def test_unverified_commercial_workflow_never_fabricates_production_total(self):
        # No verified workflow path in the existing fetch_wols contract. Even
        # plausible field guesses, customer names and descriptions cannot prove it.
        for fields in ({}, {'workflow': 'Παραγγελία εμπορίου'},
                       {'flow': {'name': 'Παραγγελία εμπορίου'}},
                       {'description': 'Παραγγελία εμπορίου', 'client': {'name': 'Production'}}):
            projection = collect_stations([dict(id=1, production_status='production',
                erp_routing=[dict(id=1, workstationName='LASER', status='waiting')], **fields)], True)
            self.assertFalse(projection['coverage']['commercial_flow_exclusion_verified'])
            snapshot = empty_snapshot(NOW)
            snapshot['workstations'] = projection['workstations']
            snapshot['today']['production_wols'] = 999  # Unattested counts must not leak into the API.
            model = map_snapshot(snapshot, NOW)
            self.assertIsNone(model['today']['production_wols'])
            metric = model['production_wols']
            self.assertIsNone(metric['total'])
            self.assertEqual(metric['status'], 'unavailable')
            self.assertEqual(metric['excluded_workflow'], 'Παραγγελία εμπορίου')
            self.assertEqual(metric['reader'], 'epoptia_read.fetch_wols')
            self.assertIn('workflow-name', metric['missing_field'])

    def test_five_urgent_orders_through_census_and_adapter(self):
        census = OrderCensus('workorder.deadline', 'synthetic verified contract')
        census.consume([dict(id=i, production_status='production',
            workorder=dict(id=i, deadline=f'2026-09-{i:02}', progress=i),
            client=dict(name='Synthetic')) for i in range(7, 0, -1)])
        orders = census.result(True, NOW)
        self.assertEqual([row['id'] for row in orders['urgent_orders']], [1, 2, 3, 4, 5])
        snapshot = empty_snapshot(NOW)
        snapshot['urgent_orders'] = orders['urgent_orders']
        self.assertEqual(len(map_snapshot(snapshot, NOW)['urgent_orders']), 5)

    def test_claimed_flow_join_and_empty_population_do_not_attest_a_total(self):
        for count in (0, 42):
            with self.subTest(count=count):
                snapshot = empty_snapshot(NOW)
                snapshot['today']['production_wols'] = count
                snapshot['production_wols'] = dict(total=count, status='available')
                snapshot['station_coverage'] = dict(commercial_flow_exclusion_verified=True)
                snapshot['field_status']['production_wols'] = 'available'
                model = map_snapshot(snapshot, NOW)
                self.assertIsNone(model['today']['production_wols'])
                self.assertIsNone(model['production_wols']['total'])
                self.assertEqual(model['production_wols']['status'], 'unavailable')
                self.assertEqual(model['production_wols']['reason'],
                                 'exact_wol_workflow_name_field_unverified')
