"""Browser-shaped grouped GET regressions, entirely synthetic and offline."""
import unittest
from datetime import date
from unittest.mock import Mock, patch
import requests
from dashboard.orders import OrderCensus, collect_orders
from epoptia_queries import production_overview
from epoptia_read import read_parent_deadlines, bounded_read
from threading import Event
from datetime import datetime, timezone
from dashboard.provider import LocalEpoptiaProvider
from dashboard.adapter import map_snapshot


def wol(order, line, progress=99):
    return dict(id=line, workorder=dict(id=order, progress=progress),
                production_status='production', completionDate='2000-01-01',
                dbCompletionDate='2001-01-01', displayCompletionDate='2002-01-01')


def fixture(pages=1):
    return dict(productionData={'2026-09-18': [wol(701, 3045, 74.4), wol(701, 2)],
                               '2026-09-21': [wol(718, 3)]}, numberOfPages=pages)


def authoritative(payload):
    """Grouping is incidental; linked flat WOL targets supply deadlines."""
    return dict(numberOfPages=payload.get('numberOfPages', 1),
                workorderLines=[dict(wol(701, 3045), target_day='2026-09-18'),
                                dict(wol(718, 3), target_day='2026-09-21')], data=payload)


def session_for(pages):
    session = Mock(headers={}, cookies=[])
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.get.side_effect = [p if isinstance(p, Exception) else
                              Mock(status_code=200, json=Mock(return_value=p)) for p in pages]
    session.post.return_value = Mock(status_code=200, json=Mock(return_value={
        'productionData': {'1999-01-01': [wol(701, 1, 35), wol(718, 3, 65)]},
        'numberOfPages': 1}))
    return session


class CalendarIsolated:
    """These tests cover deadlines/progress; the calendar read is tested in test_calendar_target_dates."""

    def setUp(self):
        from calendar_target_dates import result
        patcher = patch('calendar_target_dates.refresh_calendar',
                        side_effect=lambda *a, **k: result('calendar_unavailable'))
        patcher.start()
        self.addCleanup(patcher.stop)


class ParentDeadlineContractTests(unittest.TestCase):
    """Parent dates come only from complete productionData bucket scans."""

    def read(self, payload):
        return read_parent_deadlines('https://synthetic.invalid', session_for([payload]))

    def test_701_deadline(self):
        report = self.read(fixture())
        self.assertTrue(report['source']['complete'])
        self.assertEqual(report['dates'][701], {date(2026, 9, 18)})

    def test_another_parent_deadline(self):
        report = self.read(fixture())
        self.assertTrue(report['source']['complete'])
        self.assertEqual(report['dates'][718], {date(2026, 9, 21)})

    def test_same_parent_same_date_duplicates(self):
        payload = fixture()
        payload['productionData']['2026-09-18'] *= 2
        report = self.read(payload)
        self.assertTrue(report['source']['complete'])
        self.assertEqual(report['dates'][701], {date(2026, 9, 18)})

    def test_multiple_dates_roll_forward(self):
        payload = fixture()
        payload['productionData']['2026-09-20'] = [wol(701, 9)]
        census = OrderCensus()
        census.consume([wol(701, 1), wol(718, 3)])
        census.consume_deadlines(self.read(payload))
        orders = census.result(True, date(2026, 9, 13))['orders']
        self.assertEqual(orders[0]['deadline'], '2026-09-18')
        self.assertIsNone(orders[0]['deadline_reason'])
        self.assertEqual(orders[1]['deadline'], '2026-09-21')

    def test_wol_completion_date_cannot_supply_parent_deadline(self):
        payload = dict(productionData={'undated': [wol(701, 1)]}, numberOfPages=1)
        report = self.read(payload)
        self.assertTrue(report['source']['complete'])
        self.assertEqual(report['dates'][701], {None})

    def test_genuinely_incomplete_response_discards_parent_dates(self):
        session = session_for([fixture(2), requests.Timeout()])
        report = read_parent_deadlines('https://synthetic.invalid', session)
        self.assertFalse(report['source']['complete'])
        self.assertEqual(report['source']['status'], 'upstream_timeout')
        self.assertEqual(report['dates'], {})


class RuntimeDeadlineTests(CalendarIsolated, unittest.IsolatedAsyncioTestCase):
    async def overview(self, pages):
        session = session_for(pages)
        capacity = session.post.return_value.json.return_value
        for rows in capacity['productionData'].values():
            for row in rows:
                row['workorder'].update(code=f"ORDER-{row['workorder']['id']}",
                                        client={'name': 'Capacity customer'})
        provider = LocalEpoptiaProvider(clock=lambda: datetime(2026, 9, 19, tzinfo=timezone.utc))
        with patch('epoptia_queries.application_settings', return_value=dict(
                base_url='https://synthetic.invalid', username='synthetic', password='synthetic', headers={'X-Auth-Token': 'synthetic'})), \
                patch('epoptia_read.requests.get', side_effect=session.get), \
                patch('epoptia_read.requests.Session', return_value=session), \
                patch('epoptia_read._web_login', return_value=True):
            await provider.refresh_core('production_overview')
        model = map_snapshot(provider.core_snapshot(), provider.clock())
        self.assertEqual(model['sources']['production_overview']['generation'], 1)
        orders = model['canonical_orders']
        self.assertEqual(orders['sources']['canonical_progress']['endpoint'], '/capacity-planning/workorderlines')
        self.assertEqual(orders['sources']['parent_deadlines']['endpoint'], '/api/3.03/workorderlines')
        self.assertEqual([r['id'] for r in orders['orders']], [701, 718])
        self.assertEqual([r['native_progress'] for r in orders['orders']], [35, 65])
        self.assertEqual([r['code'] for r in orders['orders']], ['ORDER-701', 'ORDER-718'])
        self.assertTrue(all(r['customer'] == 'Capacity customer' for r in orders['orders']))
        self.assertIsNone(model['today']['completed_today'])
        self.assertIsNone(model['overall_progress_percent'])
        return orders

    async def test_default_provider_merges_separate_response_envelopes(self):
        for payload, path in (
            (fixture(), 'productionData.<date>'),
            ({'data': fixture()}, 'data.productionData.<date>'),
            ({'workorderLines': fixture()}, 'workorderLines.productionData.<date>'),
        ):
            with self.subTest(path=path):
                orders = await self.overview([authoritative(payload)])
                self.assertEqual([r['deadline'] for r in orders['orders']],
                                 ['2026-09-18', '2026-09-21'])
                self.assertEqual([r['id'] for r in orders['urgent_orders']], [701, 718])
                # The query uses its own Athens date, exposed for deterministic checking.
                self.assertEqual(orders['overdue_work'], sum(
                    r['deadline'] < orders['deadline_coverage']['as_of'] for r in orders['orders']))
                self.assertTrue(orders['deadline_scan']['complete'])
                self.assertEqual(orders['deadline_scan']['endpoint'], '/api/3.03/workorderlines')
                self.assertTrue(all(r['deadline_provenance']['path'] == 'target_day'
                                    for r in orders['orders']))

    async def test_runtime_multiple_dates_keep_both_orders_urgent(self):
        from epoptia_queries import read_wol_snapshot
        rows = [dict(wol(701, 1), target_day='2026-09-18'),
                dict(wol(701, 9), target_day='2026-09-20'),
                dict(wol(718, 3), target_day='2026-09-21')]
        with patch('epoptia_read.requests.get', return_value=Mock(status_code=200,
                json=Mock(return_value=dict(numberOfPages=1, workorderLines=rows)))):
            report = read_wol_snapshot('https://synthetic.invalid', {})
        census = OrderCensus()
        census.consume(rows)
        census.consume_deadlines(report['deadlines'])
        orders = census.result(True, date(2026, 9, 19))
        self.assertEqual(orders['orders'][0]['deadline'], '2026-09-20')
        self.assertIsNone(orders['orders'][0]['deadline_reason'])
        self.assertEqual([r['id'] for r in orders['urgent_orders']], [701, 718])
        self.assertEqual(orders['overdue_work'], 0)

    async def test_runtime_missing_incomplete_and_unavailable_source(self):
        for pages in (
            [{'workorderLines': [wol(701, 1)]}],
            [fixture(2), {'productionData': {}, 'numberOfPages': 2}],
            [requests.Timeout('synthetic')],
            [{'numberOfPages': 2, 'data': fixture(1)}],
            [{'data': dict(wol(701, 1), **fixture())}],
        ):
            # session_for also accepts exceptions to model a failed HTTP read.
            with self.subTest(pages=len(pages)):
                orders = await self.overview(pages)
                self.assertTrue(all(r['deadline'] is None for r in orders['orders']))
                self.assertTrue(all(r['deadline_reason'] == 'incomplete_scan' for r in orders['orders']))
                self.assertFalse(orders['deadline_scan']['complete'])
                self.assertEqual(orders['urgent_orders'], [])
                self.assertIsNone(orders['overdue_work'])


class DeadlineEnvelopeTests(CalendarIsolated, unittest.TestCase):
    def test_default_overview_reuses_capacity_targets_and_preserves_progress(self):
        session = session_for([fixture()])
        rows = session.post.return_value.json.return_value['productionData']['1999-01-01']
        for row, target in zip(rows, ('2026-09-18', '2026-09-21')):
            row['target_day'] = target
        with patch('epoptia_read.requests.Session', return_value=session), patch(
                'epoptia_read._web_login', return_value=True):
            result = production_overview('https://synthetic.invalid', username='synthetic', password='synthetic')
        orders = result['dashboard_orders']
        self.assertEqual([r['deadline'] for r in orders['orders']], ['2026-09-18', '2026-09-21'])
        self.assertEqual([r['native_progress'] for r in orders['orders']], [35, 65])
        self.assertEqual(result['native_active_production_progress_percent'], 50)
        self.assertTrue(orders['deadline_coverage']['complete'])
        session.get.assert_not_called()
        self.assertEqual(session.post.call_count, 1)
        self.assertTrue(session.post.call_args.args[0].endswith('/capacity-planning/workorderlines'))
        for row in orders['orders']:
            self.assertEqual(row['deadline_provenance']['path'], 'target_day')
            self.assertEqual(row['deadline_provenance']['method'], 'POST')
            self.assertEqual(row['deadline_provenance']['endpoint'], '/capacity-planning/workorderlines')
            self.assertEqual(row['progress_provenance']['endpoint'], '/capacity-planning/workorderlines')

    def test_coverage_urgency_overdue_and_duplicate_same_date(self):
        census = OrderCensus()
        census.consume([wol(701, 1, 35), wol(718, 3, 65)])
        self.assertIsNone(census.result(True)['overdue_work'])
        census.consume_deadlines(read_parent_deadlines('https://synthetic.invalid', session_for([fixture()])))
        result = census.result(True, date(2026, 9, 19))
        self.assertEqual(result['overdue_work'], 1)
        self.assertEqual([r['id'] for r in result['urgent_orders']], [701, 718])
        self.assertEqual(result['deadline_coverage']['dated_unfinished_orders'], 2)

    def test_cross_date_rollforward(self):
        census = OrderCensus()
        census.consume([dict(wol(701, 1), target_day='2026-09-18'),
                        dict(wol(701, 9), target_day='2026-09-19')])
        row = census.result(True, date(2026, 9, 19))['orders'][0]
        self.assertEqual(row['deadline'], '2026-09-19')
        self.assertIsNone(row['deadline_reason'])

    def test_malformed_date(self):
        for key in ('past', '2026-02-30', '2026-09-18T00:00:00Z'):
            payload = fixture()
            payload['productionData'][key] = payload['productionData'].pop('2026-09-18')
            row = collect_orders('https://synthetic.invalid', session_for([payload]))['orders'][0]
            self.assertIsNone(row['deadline'])
            self.assertEqual(row['deadline_reason'], 'missing_or_invalid_deadline')

    def test_incomplete_scan_keeps_capacity_progress(self):
        session = session_for([fixture(2), dict(productionData={}, numberOfPages=3)])
        census = OrderCensus()
        census.consume([wol(701, 1, 35), wol(718, 3, 65)])
        census.consume_deadlines(read_parent_deadlines('https://synthetic.invalid', session))
        result = census.result(True, date(2026, 9, 13))
        self.assertTrue(result['complete'])
        self.assertFalse(result['deadline_coverage']['complete'])
        self.assertEqual([r['native_progress'] for r in result['orders']], [35, 65])
        self.assertTrue(all(r['deadline'] is None and r['deadline_reason'] == 'incomplete_scan'
                            for r in result['orders']))

    def test_repeated_page_and_timeout_fail_closed(self):
        for failure in ('repeat', 'timeout'):
            session = session_for([fixture(2), fixture(2)])
            if failure == 'timeout':
                session.get.side_effect = requests.Timeout()
            report = read_parent_deadlines('https://synthetic.invalid', session)
            self.assertFalse(report['source']['complete'])
            self.assertEqual(report['dates'], {})

    def test_missing_grouping_cannot_use_capacity_or_child_dates(self):
        result = collect_orders('https://synthetic.invalid', session_for([{'workorderLines': [wol(701, 1)]}]))
        self.assertTrue(all(r['deadline'] is None for r in result['orders']))

    def test_cancelled_budget_prevents_request(self):
        session = session_for([fixture()])
        cancelled = Event(); cancelled.set()
        with bounded_read(20, cancelled, lambda: None):
            result = read_parent_deadlines('https://synthetic.invalid', session)
        session.get.assert_not_called()
        self.assertFalse(result['source']['complete'])

    def test_multiple_pages_same_date(self):
        second = dict(productionData={'2026-09-18': [wol(701, 4)]}, numberOfPages=2)
        census = OrderCensus()
        census.consume([wol(701, 1), wol(718, 3)])
        census.consume_deadlines(read_parent_deadlines(
            'https://synthetic.invalid', session_for([fixture(2), second])))
        result = census.result(True, date(2026, 9, 13))
        self.assertEqual(result['orders'][0]['deadline'], '2026-09-18')

    def test_implicit_pagination_requires_terminal_empty_page(self):
        first = fixture()
        first.pop('numberOfPages')
        session = session_for([first, {'productionData': {}}])
        report = read_parent_deadlines('https://synthetic.invalid', session)
        self.assertTrue(report['source']['complete'])
        self.assertEqual(report['source']['pages_read'], 2)

    def test_invalid_rows_discard_all_candidates(self):
        payload = fixture()
        payload['productionData']['2026-09-22'] = [None]
        report = read_parent_deadlines('https://synthetic.invalid', session_for([payload]))
        self.assertFalse(report['source']['complete'])
        self.assertEqual(report['dates'], {})


class AuthenticatedMergeTests(CalendarIsolated, unittest.IsolatedAsyncioTestCase):
    async def test_default_api_transport_and_sanitized_diagnostic(self):
        from dashboard.deadline_diagnostic import diagnose_deadlines
        session = session_for([])
        session.post.return_value.json.return_value['productionData']['1999-01-01'][0]['workorder']['progress'] = 74.4
        with patch('epoptia_queries.application_settings', return_value=dict(
                base_url='https://synthetic.invalid', username='synthetic', password='synthetic',
                headers={'X-Auth-Token': 'synthetic'})), \
                patch('epoptia_read.requests.Session', return_value=session), \
                patch('epoptia_read._web_login', return_value=True), \
                patch('epoptia_read.requests.get', return_value=Mock(
                    status_code=200, json=Mock(return_value=authoritative(fixture())))) as api:
            facts = await diagnose_deadlines([701, 718])
        self.assertEqual(facts, [dict(order_id=701, canonical_progress=74.4,
            merged_deadline='2026-09-18', deadline_reason=None, deadline_source_status='ok'),
            dict(order_id=718, canonical_progress=65, merged_deadline='2026-09-21',
                 deadline_reason=None, deadline_source_status='ok')])
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.kwargs['headers'], {'X-Auth-Token': 'synthetic'})
        self.assertTrue(api.call_args.args[0].endswith('/api/3.03/workorderlines'))
        self.assertTrue(session.post.call_args.args[0].endswith('/capacity-planning/workorderlines'))
        session.get.assert_not_called()

    async def test_failed_optional_deadline_refresh_preserves_production_availability(self):
        from copy import deepcopy
        session = session_for([fixture()])
        with patch('epoptia_read.requests.Session', return_value=session), patch(
                'epoptia_read._web_login', return_value=True):
            good = production_overview('https://synthetic.invalid', username='synthetic', password='synthetic')
        bad = deepcopy(good)
        bad['dashboard_orders']['deadline_scan'].update(complete=False, status='upstream_timeout')
        for row in bad['dashboard_orders']['orders']:
            row.update(deadline=None, deadline_reason='incomplete_scan')
        async def read(tool):
            return current
        current = good
        provider = LocalEpoptiaProvider(read=read)
        await provider.refresh_core('production_overview')
        current = bad
        await provider.refresh_core('production_overview')
        orders = provider.core_snapshot()['canonical_orders']
        self.assertEqual([row['native_progress'] for row in orders['orders']], [35, 65])
        self.assertTrue(all(row['deadline'] is None for row in orders['orders']))
        self.assertEqual(provider.sources['production_overview']['state'], 'available')
        self.assertIsNone(provider.sources['production_overview']['failure_reason'])

    async def test_stable_top_five_and_terminal_exclusion(self):
        # The dashboard shows up to five urgent orders (dashboard/orders.py, adapter.py).
        census = OrderCensus()
        census.consume([wol(key, key, 100 if key == 705 else 74.4) for key in (705, 704, 703, 702, 701)])
        payload = dict(numberOfPages=1, productionData={
            '2026-09-18': [wol(key, key) for key in (704, 703, 702, 701)],
            '2026-09-01': [wol(705, 705)]})
        census.consume_deadlines(read_parent_deadlines('https://synthetic.invalid', session_for([payload])))
        result = census.result(True, date(2026, 9, 19))
        self.assertEqual([row['id'] for row in result['urgent_orders']], [701, 702, 703, 704])
        self.assertEqual(result['overdue_work'], 4)
