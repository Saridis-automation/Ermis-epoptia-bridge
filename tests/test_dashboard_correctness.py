"""Synthetic station and completion history semantic coverage."""
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch
from datetime import timedelta
from dashboard.stations import collect_stations
from dashboard.completion import completion_count
from test_dashboard_refresh import NOW


class StationTests(unittest.TestCase):
    def test_filtered_counts_survive_dashboard_mapping_for_each_station(self):
        from dashboard.adapter import map_snapshot
        from dashboard.provider import empty_snapshot
        rows = [dict(id=1, production_status='production', erp_routing=[
            dict(workstationName='LASER', status='started'),
            dict(workstationName='LASER', status='paused'),
            dict(workstationName='PUNCHING', status='waiting')])]
        for i, status in enumerate(('archive', 'archived', 'completed', 'cancelled', 'canceled'), 2):
            rows.append(dict(id=i, production_status=status, erp_routing=[
                dict(workstationName='LASER', status='started'),
                dict(workstationName='HISTORY ONLY', status='paused')]))
        before = deepcopy(rows)
        projection = collect_stations(rows, True)
        snapshot = empty_snapshot(NOW)
        snapshot['workstations'] = projection['workstations']
        stations = map_snapshot(snapshot, NOW)['workstations']
        self.assertEqual([(s['name'], s['wip_steps'], s['units']) for s in stations],
                         [('LASER', 2, 'routing_steps')])
        self.assertNotIn('PUNCHING', [s['name'] for s in stations])
        self.assertEqual(projection['diagnostics']['excluded_terminal_wols'], 5)
        self.assertEqual(rows, before)

    def test_active_lifecycles_retain_current_workload(self):
        from dashboard.orders import ACTIVE
        for status in sorted(ACTIVE):
            with self.subTest(status=status):
                result = collect_stations([dict(id=1, production_status=status, erp_routing=[
                    dict(workstationName='LASER', status=s)
                    for s in ('started', 'paused', 'waiting', 'completed')])], True)
                self.assertTrue(result['complete'])
                station = result['workstations'][0]
                self.assertEqual(station['wip_steps'], 2)
                self.assertEqual(station['waiting_steps'], 1)
                self.assertEqual(station['distinct_wols'], 1)

    def test_normalized_terminal_lifecycle_excluded_before_routing(self):
        for field in ('production_status', 'status'):
            for status in ('archive', 'archived', 'completed', 'cancelled', 'canceled'):
                with self.subTest(field=field, status=status):
                    rows = [dict(id=1, production_status='production', erp_routing=[
                        dict(workstationName='LASER', status='started')]),
                        dict(id=2, **{field: ' \t' + status.upper() + '\n'}, erp_routing=[
                            dict(workstationName='HISTORY ONLY', status='started')]),
                        dict(id=3, **{field: status}, erp_routing=None)]
                    before = deepcopy(rows)
                    result = collect_stations(rows, True)
                    self.assertTrue(result['complete'])
                    self.assertEqual(result['diagnostics']['excluded_terminal_wols'], 2)
                    self.assertEqual([s['name'] for s in result['workstations']], ['LASER'])
                    self.assertEqual(result['workstations'][0]['wip_steps'], 1)
                    self.assertEqual(rows, before)

    def test_full_scan_filters_stations_but_preserves_history_and_deadlines(self):
        from epoptia_queries import read_wol_snapshot
        active = dict(id=1, production_status='production', erp_routing=[
            dict(workstationName='LASER', status=s)
            for s in ('started', 'paused', 'waiting')])
        terminal = dict(id=2, status=' ARCHIVED ', workorder={'id': 99},
                        target_day='2026-09-14', erp_routing=deepcopy(active['erp_routing']))
        rows = [active, terminal]
        before = deepcopy(rows)
        history = Mock()
        pages = [Mock(status_code=200, json=Mock(return_value=dict(
            numberOfPages=2, workorderLines=[row]))) for row in rows]
        with patch('epoptia_read.requests.get', side_effect=pages) as get:
            snapshot = read_wol_snapshot('https://synthetic.invalid', {}, routing_snapshot=history)
        self.assertEqual(get.call_count, 2)
        self.assertTrue(all(call.args[0].endswith('/api/3.03/workorderlines')
                            for call in get.call_args_list))
        self.assertTrue(snapshot['complete'])
        station = snapshot['stations']['dashboard_stations']['workstations'][0]
        self.assertEqual([station[s + '_steps'] for s in ('running', 'paused', 'waiting')], [1, 1, 1])
        self.assertEqual(station['distinct_wols'], 1)
        history.assert_called_once_with(before, complete=True)
        self.assertEqual(snapshot['wol_rows'], before)
        self.assertIn(99, snapshot['deadlines']['dates'])
        self.assertEqual(rows, before)

    def test_archive_terminal_filter_and_distinct_states(self):
        def row(i,status):
            return dict(id=i,production_status=status,erp_routing=[dict(workstationName='LASER',status=s)
                for s in ('started','in_progress','paused','not_started','waiting','mystery','completed')])
        active=row(1,'production')
        r=collect_stations([row(2,'archive'),row(3,'completed'),row(4,'cancelled'),active,active],True)
        self.assertTrue(r['complete'])
        s=r['workstations'][0]
        self.assertEqual([s[k+'_steps'] for k in ('running','paused','waiting','unknown')],[2,1,2,1])
        self.assertEqual(s['wip_steps'],3);self.assertEqual(s['distinct_wols'],1)
        self.assertIsNone(s['executable_queue_steps']);self.assertIsNone(s['load_percent'])
        self.assertEqual(len(s['capacity_missing_inputs']),4)
        self.assertEqual(r['diagnostics']['excluded_terminal_wols'],3)

    def test_partial_conflict_missing_identity_unknown(self):
        a=dict(id=1,production_status='production',erp_routing=[dict(workstationName='LASER',status='weird')])
        for rows,complete in (([a],False),([a,dict(a,production_status='archive')],True)):
            r=collect_stations(rows,complete)
            self.assertFalse(r['complete']);self.assertIsNone(r['workstations'][0]['wip_steps'])
        self.assertFalse(collect_stations([{}],True)['complete'])

    def test_nonactive_lifecycles_cannot_inflate_workload(self):
        rows = [dict(id=i, production_status=status, erp_routing=[
            dict(workstationName='HIDDEN', status='started')])
            for i, status in enumerate((None, '', 'unknown', 'draft'))]
        result = collect_stations(rows, True)
        self.assertEqual(result['workstations'], [])
        self.assertEqual(result['diagnostics']['excluded_nonactive_wols'], 4)

    def test_unknown_is_never_running(self):
        r=collect_stations([dict(id=1,production_status='production',erp_routing=[dict(workstationName='S',status='unknown')])],True)
        self.assertEqual(r['workstations'][0]['running_steps'],0)
        self.assertEqual(r['workstations'][0]['unknown_steps'],1)


def history(events):
    return dict(complete=True,semantics='whole_order_status_transitions',verified_source='synthetic contract',
                coverage_through=NOW.isoformat(),events=events)

def event(i=1,order=1,state='completed',at=NOW):
    return dict(event_id=i,order_id=order,state=state,at=at.isoformat())

class CompletionTests(unittest.TestCase):
    def test_duplicate_events_and_reopen_recomplete(self):
        a=event(at=NOW-timedelta(hours=2))
        events=[a,a,event(2,state='reopened',at=NOW-timedelta(hours=1))]
        self.assertEqual(completion_count(history(events),NOW),0)
        events.append(event(3))
        self.assertEqual(completion_count(history(events),NOW),1)

    def test_athens_day_and_distinct_orders(self):
        at=NOW.replace(hour=0)
        events=[event(at=at-timedelta(hours=2)),event(2,2,at=at-timedelta(hours=4)),event(3,1)]
        self.assertEqual(completion_count(history(events),NOW),1)

    def test_unverified_partial_invalid_and_noncompletion_rejected(self):
        valid=history([event()])
        candidates=[{},dict(valid,complete=False),dict(valid,verified_source=None),
                    dict(valid,coverage_through=(NOW-timedelta(seconds=1)).isoformat()),
                    dict(valid,events=[dict(event(),at='invalid')]),
                    dict(valid,events=[dict(event(),at='2026-09-08T12:00:00')]),
                    dict(valid,events=[event(at=NOW+timedelta(seconds=1))]),
                    dict(valid,events=[dict(event(),state='updated')]),
                    {'complete':True,'orders':[{'id':1,'completed_at':NOW.isoformat()}]}]
        for value in candidates:self.assertIsNone(completion_count(value,NOW))

    def test_conflicting_duplicate_and_simultaneous_transition_unknown(self):
        self.assertIsNone(completion_count(history([event(),event(order=2)]),NOW))
        self.assertIsNone(completion_count(history([event(),event(2,state='reopened')]),NOW))

    def test_verified_empty_is_zero(self):
        self.assertEqual(completion_count(history([]),NOW),0)
