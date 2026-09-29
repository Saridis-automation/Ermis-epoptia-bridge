"""Synthetic canonical order regressions; no credentials or live IO."""
import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch
from dashboard.orders import OrderCensus, collect_orders as _collect_orders
from epoptia_read import _scan_production_pages, _production_page, read_parent_deadlines


def grouped_get_payload(payload):
    # Legacy capacity parser fixtures also get a separate browser-shaped GET.
    rows, counts = _production_page(payload)
    groups = {}
    for row in rows:
        key = row.get('_production_data_date') or 'undated'
        groups.setdefault(key, []).append(row)
    result = {'productionData': groups}
    if counts:
        result['numberOfPages'] = counts[0]
    return result


def collect_orders(base_url, session):
    from itertools import tee
    responses = session.post.side_effect
    if responses is None:
        session.get.return_value = Mock(status_code=200, json=Mock(
            return_value=grouped_get_payload(session.post.return_value.json())))
    else:
        post, get = tee(responses)
        session.post.side_effect = post
        session.get.side_effect = [Mock(status_code=200, json=Mock(
            return_value=grouped_get_payload(response.json()))) for response in get]
    return _collect_orders(base_url, session)


def wol(order=1, progress=20, due='2026-09-07', status='production', wid=None):
    return dict(id=wid or order+9000, workorder=dict(id=order, code=f'ORDER-{order}', progress=progress,
                deadline=due), target_day='2000-01-01', production_status=status, client={'name':'Synthetic'})


def project(rows, complete=True, verified=True, today=date(2026,9,8)):
    c = OrderCensus('workorder.deadline', 'synthetic contract only') if verified else OrderCensus()
    c.consume(rows)
    return c.result(complete, today)


class OrderTests(unittest.TestCase):
    def grouped(self, groups):
        session = Mock(headers={}, cookies=[])
        session.post.return_value = Mock(status_code=200, json=Mock(return_value={
            'numberOfPages': 1, 'capacityPlanningData': {'productionData': groups}}))
        census = OrderCensus()
        complete = _scan_production_pages('https://synthetic.invalid', session,
                                          {'pages_read': 0}, census.consume)
        session.get.return_value = Mock(status_code=200, json=Mock(return_value={
            'productionData': groups, 'numberOfPages': 1}))
        census.consume_deadlines(read_parent_deadlines('https://synthetic.invalid', session))
        return census.result(complete, date(2026, 9, 8))

    def test_verified_production_dates_and_native_progress(self):
        row = wol(701, 35)
        row.update(completionDate='2000-01-01', dbCompletionDate='2001-01-01',
                   displayCompletionDate='2002-01-01', progress=99)
        result = self.grouped({'2026-09-09': [wol(718, 65)],
                               '2026-09-07': [row, wol(701, 35, wid=9999)]})
        first, second = result['orders']
        self.assertEqual((first['id'], first['deadline'], first['native_progress']),
                         (701, '2026-09-07', 35))
        self.assertEqual((second['id'], second['deadline']), (718, '2026-09-09'))
        self.assertTrue(first['deadline_provenance']['verified'])
        self.assertEqual(first['deadline_provenance']['path'],
                         'productionData.<date>')
        self.assertEqual([o['id'] for o in result['urgent_orders']], [701, 718])
        self.assertEqual(result['overdue_work'], 1)

    def test_multiple_dates_roll_forward_and_missing_dates_fail_closed(self):
        for key in ('2026-09-09', 'past', '2026-02-30'):
            with self.subTest(key=key):
                groups = {key: [wol(701, wid=9999)]}
                if key == '2026-09-09':
                    groups['2026-09-07'] = [wol(701)]
                result = self.grouped(groups)
                row = result['orders'][0]
                if key == '2026-09-09':
                    self.assertEqual(row['deadline'], key)
                    self.assertIsNone(row['deadline_reason'])
                    self.assertEqual(result['overdue_work'], 0)
                    continue
                self.assertIsNone(row['deadline'])
                self.assertEqual(row['deadline_reason'], 'missing_or_invalid_deadline')
                self.assertFalse(row['deadline_provenance']['verified'])
                self.assertEqual(result['urgent_orders'], [])
                self.assertIsNone(result['overdue_work'])

    def test_terminal_rows_require_same_production_data_evidence(self):
        terminal = wol(718, status='archive')
        result = self.grouped({'2026-09-07': [terminal, wol(701, status='archive'),
                                             wol(701, wid=9999)]})
        self.assertEqual(result['orders'][0]['deadline'], '2026-09-07')
        self.assertEqual(result['orders'][1]['deadline'], '2026-09-07')
        self.assertTrue(result['orders'][1]['deadline_provenance']['verified'])
        self.assertEqual(project([terminal], verified=False)['orders'][0]['deadline'], '2000-01-01')
        self.assertEqual(result['overdue_work'], 1)
        # Archived WOL dates also participate when the parent is unfinished.
        result = self.grouped({'2026-09-07': [wol(701)],
                               '2026-09-09': [wol(701, status='archive', wid=9999)]})
        self.assertEqual(result['orders'][0]['deadline'], '2026-09-09')
        self.assertIsNone(result['orders'][0]['deadline_reason'])

    def test_response_envelopes_reach_production_overview(self):
        from epoptia_queries import production_overview
        row = wol(701, 35)
        row.update(completionDate='2000-01-01', dbCompletionDate='2001-01-01',
                   displayCompletionDate='2002-01-01', progress=99)
        row['workorder']['client'] = {'name': 'Parent client'}
        row['target_day'] = '2026-09-07'
        groups = {'2026-09-07': {'client-group': [row]},
                  '2026-09-09': [dict(wol(718, 65), target_day='2026-09-09')]}
        # Existing capacity envelope, direct XHR mapping, and extra wrappers.
        for envelope in (
            {'capacityPlanningData': {'productionData': groups}, 'numberOfPages': 1},
            {'productionData': groups, 'numberOfPages': 1},
            {'data': {'capacityPlanningData': {'productionData': groups}, 'numberOfPages': 1}},
            {'workorderLines': [row], 'productionData': groups, 'numberOfPages': 1},
            {'data': [{'productionData': [{'dates': groups}], 'numberOfPages': 1}]},
        ):
            with self.subTest(envelope=list(envelope)):
                session = Mock(headers={}, cookies=[])
                session.__enter__ = Mock(return_value=session)
                session.__exit__ = Mock(return_value=False)
                session.post.return_value = Mock(status_code=200, json=Mock(return_value=envelope))
                session.get.return_value = Mock(status_code=200, json=Mock(
                    return_value=grouped_get_payload(envelope)))
                with patch('epoptia_read.requests.Session', return_value=session), patch(
                        'epoptia_read._web_login', return_value=True):
                    result = production_overview('https://synthetic.invalid',
                        username='synthetic', password='synthetic')
                first, second = result['dashboard_orders']['orders']
                self.assertEqual((first['id'], first['deadline'], first['native_progress'], first['client']),
                                 (701, '2026-09-07', 35, 'Parent client'))
                self.assertEqual((second['id'], second['deadline'], second['native_progress'], second['client']),
                                 (718, '2026-09-09', 65, 'Synthetic'))
                self.assertTrue(all(r['deadline_provenance']['verified']
                                    for r in result['dashboard_orders']['orders']))
                self.assertEqual(result['native_active_production_progress_percent'], 50)
                self.assertEqual(session.post.call_count, 1)
                self.assertNotIn('_production_data_date', row)

    def test_target_dates_multi_page_merge_and_rollforward(self):
        for second_date in ('2026-09-07', '2026-09-10', 'past', '2026-02-30'):
            with self.subTest(second_date=second_date):
                session = Mock(headers={}, cookies=[])
                pages = [
                    {'workorderLines': [dict(wol(701, 35), target_day='2026-09-07')], 'numberOfPages': 2},
                    {'workorderLines': [dict(wol(701, 35, wid=9999), target_day=second_date),
                                        dict(wol(718, 65), target_day='2026-09-09')], 'numberOfPages': 2},
                ]
                session.post.side_effect = [Mock(status_code=200, json=Mock(return_value=p)) for p in pages]
                result = collect_orders('https://synthetic.invalid', session)
                self.assertTrue(result['complete'])
                first, second = result['orders']
                self.assertEqual(first['deadline'], '2026-09-10' if second_date == '2026-09-10' else '2026-09-07')
                self.assertIsNone(first['deadline_reason'])
                self.assertEqual(second['deadline'], '2026-09-09')
                self.assertEqual(result['native_mean_order_progress_percent'], 50)
                self.assertEqual(result['scan']['pages_read'], 2)

    def test_same_wol_different_date_on_next_page_rolls_forward(self):
        session = Mock(headers={}, cookies=[])
        session.post.side_effect = [Mock(status_code=200, json=Mock(return_value={
            'productionData': {key: [dict(wol(701), target_day=key)]}, 'numberOfPages': 2}))
            for key in ('2026-09-07', '2026-09-09')]
        result = collect_orders('https://synthetic.invalid', session)
        self.assertTrue(result['complete'])
        self.assertEqual(result['orders'][0]['deadline'], '2026-09-09')
        self.assertIsNone(result['orders'][0]['deadline_reason'])

    def test_wrapped_pagination_fails_closed_and_implicit_pages_merge(self):
        for explicit, complete in ((True, False), (False, True)):
            session = Mock(headers={}, cookies=[])
            pages = [{'data': {'productionData': {key: [dict(wol(order), target_day=key)]}}}
                     for key, order in (('2026-09-07', 701), ('2026-09-09', 718))]
            if explicit:
                pages[0]['data']['numberOfPages'] = 2
                pages[1]['data']['numberOfPages'] = 3
            else:
                pages.append({'data': {'productionData': {}}})
            session.post.side_effect = [Mock(status_code=200, json=Mock(return_value=p)) for p in pages]
            result = collect_orders('https://synthetic.invalid', session)
            self.assertEqual(result['complete'], complete)
            if complete:
                self.assertEqual([r['deadline'] for r in result['orders']],
                                 ['2026-09-07', '2026-09-09'])
                self.assertEqual(result['scan']['pages_read'], 3)
            else:
                self.assertEqual(result['scan']['status'], 'invalid_pagination')
                self.assertTrue(all(r['deadline'] is None for r in result['orders']))

    def test_flat_wol_target_is_used_without_grouping_evidence(self):
        row = wol(701)
        row.update(_production_data_date='2026-09-07', completionDate='2026-09-07',
                   dbCompletionDate='2026-09-07', displayCompletionDate='2026-09-07')
        row['workorder']['productionData'] = {'2026-09-07': [wol(718)]}
        session = Mock(headers={}, cookies=[])
        session.post.return_value = Mock(status_code=200, json=Mock(return_value={
            'workorderLines': [row], 'numberOfPages': 1}))
        result = collect_orders('https://synthetic.invalid', session)
        self.assertEqual(len(result['orders']), 1)
        self.assertEqual(result['orders'][0]['deadline'], '2000-01-01')
        self.assertTrue(result['orders'][0]['deadline_provenance']['verified'])

    def test_grouped_and_flat_rows_preserve_ungrouped_orders(self):
        session = Mock(headers={}, cookies=[])
        session.post.return_value = Mock(status_code=200, json=Mock(return_value={
            'productionData': {'2026-09-07': [wol(701, 35)]},
            'workorderLines': [wol(701, 35), wol(718, 65)], 'numberOfPages': 1}))
        result = collect_orders('https://synthetic.invalid', session)
        self.assertEqual([r['deadline'] for r in result['orders']], ['2000-01-01', '2000-01-01'])
        self.assertEqual([r['client'] for r in result['orders']], ['Synthetic', 'Synthetic'])
        self.assertEqual(result['native_mean_order_progress_percent'], 50)

    def test_unique_native_and_rank_before_limit(self):
        rows=[wol(n) for n in (4,3,2,1)]
        rows += [wol(1), wol(5,100)]
        r=project(rows)
        self.assertEqual(len(r['orders']),5)
        self.assertEqual([o['id'] for o in r['urgent_orders']],[1,2,3,4])
        self.assertEqual(r['overdue_work'],4)
        self.assertEqual(r, project(list(reversed(rows))))
        self.assertEqual(r['orders'][0]['code'],'ORDER-1')
        self.assertEqual(r['orders'][0]['native_progress'],20)

    def test_no_child_deadline_or_root_progress(self):
        row=wol(); row['progress']=99
        row['target_day'] = None
        r=project([row],verified=False)
        self.assertEqual(r['orders'][0]['native_progress'],20)
        self.assertIsNone(r['orders'][0]['deadline'])
        self.assertEqual(r['undated_unfinished_orders'],1)
        self.assertIsNone(r['overdue_work'])
        self.assertEqual(r['urgent_orders'],[])

    def test_progress_conflict_missing_invalid_never_zero(self):
        for value in (30,None,True,-1,101,float('nan'),'bad',100):
            with self.subTest(value=value):
                r=project([wol(),wol(progress=value,wid=10000)])
                self.assertIsNone(r['orders'][0]['native_progress'])
                self.assertEqual(r['active_workorders_total'],1)

    def test_deadline_conflicts_missing_invalid_excluded(self):
        for value in (None,'bad','2026-02-30','2026-09-09','2026-09-07T00:00:00'):
            r=project([wol(),wol(due=value,wid=10000)])
            self.assertIsNone(r['orders'][0]['deadline'])
            self.assertIsNone(r['overdue_work'])
            self.assertEqual(r['undated_unfinished_orders'],1)

    def test_incomplete_scan_retains_explicit_unknown_rows(self):
        r=project([wol()],complete=False)
        self.assertFalse(r['complete'])
        for key in ('native_progress','deadline'):
            self.assertIsNone(r['orders'][0][key])
        self.assertIsNone(r['overdue_work'])
        self.assertIsNone(r['urgent_orders'])
        self.assertFalse(project([{'id':1}])['complete'])

    def test_lifecycle_archive_and_mixed_children(self):
        rows=[wol(1,status='archive'),wol(2,status='cancelled'),wol(3,100),
              wol(4,status='archive'),wol(4,status='production',wid=10001),wol(5)]
        rows[-1]['workorder']['status']='completed'
        r=project(rows)
        self.assertEqual(r['active_workorders_total'],1)
        self.assertEqual(r['urgent_orders'][0]['id'],4)

    def test_conflicting_parent_lifecycle_excluded(self):
        a,b=wol(),wol(wid=10000)
        a['workorder']['status']='completed';b['workorder']['status']='production'
        r=project([a,b])
        self.assertEqual(r['unknown_lifecycle_orders'],1)
        self.assertEqual(r['urgent_orders'],[])

    def test_athens_midnight_date_only_and_timestamp(self):
        now=datetime(2026,9,7,21,1,tzinfo=timezone.utc)
        r=project([wol(due='2026-09-08'),wol(2,due='2026-09-07'),
                   wol(3,due='2026-09-07T21:00:00Z')],today=now)
        self.assertEqual(r['overdue_work'],1)
        self.assertEqual(r['orders'][2]['deadline'],'2026-09-08')

    def test_shared_post_scanner_pages_and_inconsistent_pagination(self):
        for pages, complete in (([2,2],True),([2,3],False)):
            session=Mock();session.headers={};session.cookies=[]
            session.post.side_effect=[Mock(status_code=200,json=Mock(return_value={
                'numberOfPages':n,'workorderLines':[wol(i+1)]})) for i,n in enumerate(pages)]
            r=collect_orders('https://synthetic.invalid',session)
            self.assertEqual(r['complete'],complete)
            self.assertEqual(session.post.call_count,2)
            self.assertTrue(session.post.call_args.args[0].endswith('/capacity-planning/workorderlines'))
            self.assertEqual(session.post.call_args.kwargs['timeout'],20)

    def test_parent_deadline_requires_evidence(self):
        with self.assertRaises(ValueError): OrderCensus('target_day','synthetic')
        with self.assertRaises(ValueError): OrderCensus('workorder.deadline')
        self.assertEqual(project([wol()])['orders'][0]['deadline_provenance']['path'],'workorder.deadline')
