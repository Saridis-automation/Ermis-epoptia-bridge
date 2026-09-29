"""Synthetic regressions for the verified rolling WOL delivery-date rule."""
import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

from dashboard.orders import OrderCensus, collect_orders
from dashboard.provider import LocalEpoptiaProvider
from epoptia_queries import production_overview, read_wol_snapshot


def wol(parent, wid, target=None, status='production'):
    return dict(workorderline_id=wid, target_day=target, production_status=status,
                workorder=dict(id=parent, progress=35), erp_routing=[])


class UniformDeadlineTests(unittest.TestCase):
    def test_date_selection_edges_through_capacity_and_snapshot(self):
        cases = (
            ('one date', ['2026-09-18'], '2026-09-18', 0),
            ('multiple future', ['2026-09-25', '2026-09-21', '2026-09-18'], '2026-09-18', 0),
            ('today plus future', ['2026-09-25', '2026-09-21', '2026-09-13'], '2026-09-13', 0),
            ('mixed past/future', ['2026-08-06', '2026-09-12', '2026-09-18'], '2026-09-18', 0),
            ('all past', ['2026-08-06', '2026-09-01', '2026-09-12'], '2026-09-12', 1),
            ('invalid plus valid', [None, 'invalid', '2026-02-30', True, {}, '2026-09-18'], '2026-09-18', 0),
            ('duplicates', ['2026-09-21', '2026-09-21', '2026-09-18', '2026-09-18'], '2026-09-18', 0),
            ('no valid dates', [None, 'invalid', '2026-02-30'], None, None),
            ('Athens summer midnight', ['2026-09-12T21:00:00Z'], '2026-09-13', 0),
            ('Athens winter midnight', ['2026-01-01T22:00:00Z'], '2026-01-02', 1),
            ('positive offset', ['2026-09-14T00:00:00+05:00'], '2026-09-13', 0),
            ('invalid timestamp', ['9999-12-31T23:00:00Z', '2026-09-13T00:00:00', '2026-09-18'], '2026-09-18', 0),
        )
        for name, targets, expected, overdue in cases:
            for snapshot in (False, True):
                for reverse in (False, True):
                    with self.subTest(case=name, snapshot=snapshot, reverse=reverse):
                        rows = [wol(718, i + 1, target) for i, target in enumerate(targets)]
                        if reverse:
                            rows.reverse()
                        census = OrderCensus()
                        census.consume(rows)
                        if snapshot:
                            # Split candidates across pages to cover the real reader's merge.
                            pages = [dict(numberOfPages=2, workorderLines=rows[:1]),
                                     dict(numberOfPages=2, workorderLines=rows[1:])]
                            if len(rows) == 1:
                                pages = [dict(numberOfPages=1, workorderLines=rows)]
                            with patch('epoptia_read.requests.get', side_effect=[
                                    Mock(status_code=200, json=Mock(return_value=p)) for p in pages]):
                                report = read_wol_snapshot('https://synthetic.invalid', {})
                            self.assertTrue(report['deadlines']['source']['complete'])
                            census.consume_deadlines(report['deadlines'])
                        result = census.result(True, date(2026, 9, 13))
                        row = result['orders'][0]
                        self.assertEqual(row['deadline'], expected)
                        self.assertEqual(row['deadline_reason'],
                                         None if expected else 'missing_or_invalid_deadline')
                        self.assertEqual(row['issues'], [] if expected else ['missing_or_invalid_deadline'])
                        self.assertEqual(result['overdue_work'], overdue)
                        self.assertEqual(result['overdue_known_count'], overdue or 0)
                        self.assertEqual(result['deadline_coverage']['complete'], expected is not None)

    def test_deadlines_roll_forward_and_drive_urgency_and_overdue(self):
        census = OrderCensus()
        census.consume([wol(701, 1, '2026-09-18'),
                        wol(718, 2, '2026-09-21'), wol(718, 3, '2026-08-06')])
        for as_of, expected_718, urgent, overdue in (
            (date(2026, 8, 6), '2026-08-06', [718, 701], 0),
            (date(2026, 8, 7), '2026-09-21', [701, 718], 0),
            (date(2026, 9, 13), '2026-09-21', [701, 718], 0),
            (date(2026, 9, 21), '2026-09-21', [701, 718], 1),
            (date(2026, 9, 22), '2026-09-21', [701, 718], 2),
        ):
            with self.subTest(as_of=as_of):
                result = census.result(True, as_of)
                self.assertEqual([r['deadline'] for r in result['orders']],
                                 ['2026-09-18', expected_718])
                self.assertTrue(all(r['deadline_reason'] is None and not r['issues']
                                    for r in result['orders']))
                self.assertEqual([r['id'] for r in result['urgent_orders']], urgent)
                self.assertEqual(result['overdue_work'], overdue)
                self.assertEqual(result['overdue_known_count'], overdue)

    def test_sorted_distinct_dates_today_latest_past_and_invalid(self):
        for targets, expected in (
            (['2026-09-25', '2026-09-18', '2026-09-18', '2026-09-21'], '2026-09-18'),
            (['2026-09-21', '2026-09-13', '2026-08-06'], '2026-09-13'),
            (['2026-08-06', '2026-09-12', '2026-09-01'], '2026-09-12'),
            ([None, 'invalid', '2026-02-30', '2026-09-18'], '2026-09-18'),
            ([None, 'invalid', '2026-02-30'], None),
        ):
            with self.subTest(targets=targets):
                census = OrderCensus()
                census.consume([wol(718, i + 1, target) for i, target in enumerate(targets)])
                result = census.result(True, date(2026, 9, 13))
                row = result['orders'][0]
                self.assertEqual(row['deadline'], expected)
                self.assertEqual(row['deadline_reason'],
                                 None if expected else 'missing_or_invalid_deadline')
                self.assertEqual(row['deadline_provenance']['verified'], expected is not None)
                self.assertEqual(result['overdue_work'],
                                 int(expected < '2026-09-13') if expected else None)

    def test_athens_midnight_advances_deadline_with_explicit_and_default_clock(self):
        census = OrderCensus()
        census.consume([wol(718, 1, '2026-09-21'), wol(718, 2, '2026-09-25')])
        for hour, expected in ((20, '2026-09-21'), (21, '2026-09-25')):
            instant = datetime(2026, 9, 21, hour, 30, tzinfo=timezone.utc)
            with self.subTest(hour=hour):
                self.assertEqual(census.result(True, instant)['orders'][0]['deadline'], expected)
                class Clock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return instant.astimezone(tz)
                with patch('dashboard.orders.datetime', Clock):
                    result = census.result(True)
                self.assertEqual(result['orders'][0]['deadline'], expected)

    def parent_orders(self, rows, snapshot):
        census = OrderCensus()
        if snapshot:
            payload = dict(numberOfPages=1, workorderLines=rows,
                           productionData={'2026-08-06': rows})
            with patch('epoptia_read.requests.get', return_value=Mock(
                    status_code=200, json=Mock(return_value=payload))):
                report = read_wol_snapshot('https://synthetic.invalid', {})
            census.consume([wol(key, key) for key in (701, 718, 600)])
            census.consume_deadlines(report['deadlines'])
        else:
            census.consume(rows)
        return {row['id']: row for row in census.result(True, date(2026, 9, 13))['orders']}

    def same_customer_rows(self):
        rows = [wol(701, 3045, '2026-09-18')]
        rows += [wol(718, wid, '2026-09-21')
                 for wid in (3102, 3103, 3111, 3112, 3113)]
        rows += [wol(600, 2001, '2026-08-06', 'archive'),
                 wol('600', 2002, '2026-08-06', 'archive')]
        for row in rows:
            row['client'] = {'name': 'Synthetic shared customer'}
            row['workorder'].update(client=row['client'], code='Shared code')
            # Flat hints cannot override the verified nested identity.
            row['workorder_id'] = 718
        return rows

    def test_exact_parent_dates_ignore_archived_same_customer(self):
        for snapshot in (False, True):
            with self.subTest(snapshot=snapshot):
                orders = self.parent_orders(self.same_customer_rows(), snapshot)
                for key, expected in ((701, '2026-09-18'), (718, '2026-09-21'),
                                      (600, '2026-08-06')):
                    self.assertEqual(orders[key]['deadline'], expected)
                    self.assertIsNone(orders[key]['deadline_reason'])

    def test_multiple_parent_dates_preserve_customer_isolation(self):
        for snapshot in (False, True):
            with self.subTest(snapshot=snapshot):
                rows = self.same_customer_rows()
                rows.append(wol('718', 3200, '2026-09-22', 'archive'))
                orders = self.parent_orders(rows, snapshot)
                self.assertEqual(orders[718]['deadline'], '2026-09-21')
                self.assertIsNone(orders[718]['deadline_reason'])
                self.assertEqual(orders[701]['deadline'], '2026-09-18')
                self.assertEqual(orders[600]['deadline'], '2026-08-06')

    def test_unverified_parent_cannot_supply_snapshot_deadline(self):
        rows = self.same_customer_rows()
        for wid, parent in enumerate((None, {}, {'id': True}, {'id': 'invalid'}), 4000):
            row = wol(None, wid, '2026-08-06', 'archive')
            row.update(workorder=parent, workorder_id=718,
                       client={'name': 'Synthetic shared customer'})
            rows.append(row)
        orders = self.parent_orders(rows, True)
        self.assertEqual(orders[718]['deadline'], '2026-09-21')

    def test_linked_dates_lifecycle_missing_rollforward_and_progress(self):
        rows = [
            wol(701, 1, '2026-09-18'), wol('701', 2, '2026-09-18', 'archive'),
            wol(701, 3, '2026-09-18', 'completed'), wol(701, 4),
            wol(718, 5, '2026-09-21'),
            wol(800, 6), wol(800, 7, '2026-02-30'),
            wol(801, 8, '2026-09-18'), wol(801, 9, '2026-09-19', 'archive'),
            wol(802, 10, '2026-09-18', 'completed'),
            wol(None, 11, '2026-09-22'),
        ]
        payload = dict(numberOfPages=1, workorderLines=rows,
                       productionData={'1999-01-01': rows})
        with patch('epoptia_read.requests.get', return_value=Mock(
                status_code=200, json=Mock(return_value=payload))), patch(
                'epoptia_read._capture_parent_deadlines', side_effect=AssertionError('legacy disabled')):
            snapshot = read_wol_snapshot('https://synthetic.invalid', {})
        census = OrderCensus()
        census.consume([wol(key, key) for key in (701, 718, 800, 801)] +
                       [wol(802, 802, status='completed')])
        census.consume_deadlines(snapshot['deadlines'])
        result = census.result(True, date(2026, 9, 20))
        orders = {row['id']: row for row in result['orders']}
        self.assertEqual(orders[701]['deadline'], '2026-09-18')
        self.assertEqual(orders[718]['deadline'], '2026-09-21')
        self.assertEqual(orders[802]['deadline'], '2026-09-18')
        self.assertEqual(orders[800]['deadline_reason'], 'missing_or_invalid_deadline')
        self.assertEqual(orders[801]['deadline'], '2026-09-19')
        self.assertIsNone(orders[801]['deadline_reason'])
        for key in (800,):
            self.assertIsNone(orders[key]['deadline'])
            self.assertFalse(orders[key]['deadline_provenance']['verified'])
        for key in (701, 718, 801, 802):
            provenance = orders[key]['deadline_provenance']
            self.assertTrue(provenance['verified'])
            self.assertEqual(provenance['derivation'], 'derived_from_rollforward_wol_dates')
            self.assertEqual(provenance['path'], 'target_day')
            self.assertEqual(orders[key]['native_progress'], 35)
        self.assertEqual([row['id'] for row in result['urgent_orders']], [701, 801, 718])
        self.assertEqual(result['overdue_known_count'], 2)
        self.assertIsNone(result['overdue_work'])

    def test_shared_capacity_rows_need_no_separate_deadline_read(self):
        session = Mock(headers={}, cookies=[])
        session.post.return_value = Mock(status_code=200, json=Mock(return_value=dict(
            numberOfPages=1, workorderLines=[wol(701, 1, '2026-09-18'),
                                            wol(701, 2, '2026-09-18')])))
        session.get.side_effect = AssertionError('separate read forbidden')
        result = collect_orders('https://synthetic.invalid', session)
        self.assertTrue(result['complete'])
        self.assertEqual(result['orders'][0]['deadline'], '2026-09-18')
        session.get.assert_not_called()


class ProductionAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_reader_unavailable_does_not_affect_production(self):
        def progress(base_url, **kwargs):
            kwargs['consume_rows']([wol(701, 1)])
            self.assertNotIn('consume_deadlines', kwargs)
            return dict(native_active_production_progress_source=dict(complete=True, status='ok'))

        payload = dict(numberOfPages=1, workorderLines=[wol(701, 1, '2026-09-18')])
        with patch('epoptia_read.active_production_progress', side_effect=progress), patch(
                'epoptia_read.read_parent_deadlines', side_effect=AssertionError('legacy unavailable')) as legacy, patch(
                'epoptia_read.requests.get', return_value=Mock(
                    status_code=200, json=Mock(return_value=payload))):
            result = production_overview('https://synthetic.invalid', headers={})
        legacy.assert_not_called()

        async def read(tool):
            return result
        provider = LocalEpoptiaProvider(read=read)
        await provider.refresh_core('production_overview')
        self.assertEqual(provider.sources['production_overview']['state'], 'available')
        self.assertEqual(provider.core_snapshot()['canonical_orders']['orders'][0]['deadline'], '2026-09-18')
        # Failure of optional date evidence must not reject fresh native production.
        result['dashboard_orders']['deadline_scan']['complete'] = False
        await provider.refresh_core('production_overview')
        self.assertEqual(provider.sources['production_overview']['state'], 'available')
        self.assertIsNone(provider.sources['production_overview']['failure_reason'])
