"""Synthetic settings and HTTP responses; real default provider/readers/login/mappers.

Existing tool functions are extracted without executing server startup. MCP calls
are forbidden, including the default provider path. No real settings are read.
"""
import ast
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import Mock, patch
from time import monotonic

import requests
import epoptia_read
from dashboard.provider import LocalEpoptiaProvider, WOL_SNAPSHOT_REUSE_SECONDS
from dashboard.adapter import map_snapshot
from dashboard.server import create_app


def wol(wid, order=1, progress=25, status='production'):
    return dict(workorderline_id=wid, production_status=status, target_day=None,
        workorder=dict(id=order, code='SYNTHETIC', progress=progress),
        client={'name': 'Synthetic'}, erp_routing=[
            dict(id=i, workstationName='LASER', status=s) for i, s in enumerate(
                ('started', 'in_progress', 'paused', 'not_started', 'waiting', 'future', 'completed'))])


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tree = ast.parse(Path('mcp_server.py').read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in ('_read_query', 'production_overview', 'workstation_wip')]
        for node in nodes:
            node.decorator_list = []
        self.scope = dict(epoptia_read=epoptia_read, BASE_URL='https://synthetic.invalid',
                          HEADERS={}, WEB_USERNAME='synthetic', WEB_PASSWORD='synthetic')
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<synthetic MCP>', 'exec'), self.scope)
        self.orders = [[wol(1), wol(2, 2, 75)], [wol(3, '1', '25')]]
        self.stations = [[wol(1), wol(2, status='archive')],
                         [wol(3, status='completed'), wol(4, status='cancelled')]]
        self.failed_source = None
        self.order_count = self.station_count = 2
        self.block = None
        self.entered = Event()
        self.release = Event()
        self.addCleanup(self.release.set)
        self.calls = []

        def response(pages, number, count):
            return Mock(status_code=200, json=Mock(return_value={
                'numberOfPages': count, 'workorderLines': deepcopy(pages[number-1])}))

        def post(url, **kw):
            self.calls.append(('POST', url.rsplit('/', 1)[-1]))
            if url.endswith('/login'):
                return Mock(status_code=200, url='https://synthetic.invalid/home',
                            history=[Mock()], text='<html>Home</html>')
            if self.block == 'production_overview':
                self.entered.set()
                if not self.release.wait(2):
                    raise requests.Timeout('synthetic blocked request')
            page = kw['json']['page']
            if self.failed_source == 'orders' and page == 2:
                raise requests.Timeout('synthetic private detail')
            # Actual grouped capacity-planning shape, including multiple pages.
            result = response(self.orders, page, self.order_count)
            payload = result.json()
            result.json.return_value = dict(numberOfPages=payload['numberOfPages'],
                capacityPlanningData={'productionData': {
                    '1999-01-01': payload['workorderLines']}})
            return result

        def get(url, **kw):
            self.calls.append(('GET', url.rsplit('/', 1)[-1]))
            page = kw['params']['page']
            if self.failed_source == 'stations' and page == 2:
                raise requests.Timeout('synthetic private detail')
            result = response(self.stations, page, self.station_count)
            result.json.return_value['productionData'] = {
                getattr(self, 'production_date', 'past'): deepcopy(self.orders[page-1])}
            return result

        # Isolate provisioning without inspecting actual environment/dotenv files.
        settings = dict(EPOPTIA_BASE_URL='https://synthetic.invalid', EPOPTIA_API_KEY='synthetic',
                        EPOPTIA_USERNAME='synthetic', EPOPTIA_PASSWORD='synthetic')
        self.dotenv = self.stack.enter_context(patch('dotenv.load_dotenv'))
        self.stack.enter_context(patch('os.getenv', side_effect=lambda name, default=None: settings.get(name, default)))
        # Mock HTTP responses only: keep Session, cookies, login and pagination real.
        def session_get(session, url, **kw):
            if url.endswith('/api/3.03/workorderlines'):
                self.calls.append(('GET', 'grouped_workorderlines'))
                page = kw['params']['page']
                return Mock(status_code=200, json=Mock(return_value={
                    'numberOfPages': self.order_count, 'productionData': {
                        getattr(self, 'production_date', 'past'): deepcopy(self.orders[page-1])}}))
            self.calls.append(('GET', 'login'))
            return Mock(status_code=200, text='<input type="hidden" name="_token" value="synthetic">')
        self.stack.enter_context(patch.object(requests.Session, 'get', session_get))
        self.post = self.stack.enter_context(patch.object(requests.Session, 'post', side_effect=post))
        self.get = self.stack.enter_context(patch.object(epoptia_read.requests, 'get', side_effect=get))
        self.mcp = self.stack.enter_context(patch('mcp.ClientSession', side_effect=AssertionError('MCP forbidden')))
        self.transport = self.stack.enter_context(patch(
            'mcp.client.streamable_http.streamable_http_client', side_effect=AssertionError('MCP forbidden')))
        self.provider = LocalEpoptiaProvider()  # Production DEFAULT, no injected reader.

    async def test_shared_module_is_import_safe_and_uses_standard_provisioning(self):
        import importlib
        import epoptia_queries
        importlib.reload(epoptia_queries)
        self.dotenv.assert_not_called()
        self.assertEqual(self.calls, [])
        await self.provider.snapshot()
        self.assertEqual(self.dotenv.call_count, 2)
        self.assertEqual(self.get.call_args.kwargs['headers'],
                         {'X-Auth-Token': 'synthetic', 'Accept': 'application/json'})
        self.assertTrue(all(call.kwargs['timeout'] <= 20 for call in self.get.call_args_list))
        self.assertTrue(all(call.kwargs['timeout'] <= 20 for call in self.post.call_args_list))

    async def test_authoritative_deadlines_through_default_adapter(self):
        self.orders = [[wol(1, 701, 35)], [wol(2, 718, 65)]]
        self.stations = [[wol(1, 701, 99)], [wol(2, 718, 99)]]
        pages = []
        for i, due in enumerate(('2026-09-18', '2026-09-21')):
            rows = deepcopy(self.stations[i])
            for row in rows:
                row.update(completionDate='2000-01-01', dbCompletionDate='2001-01-01',
                           displayCompletionDate='2002-01-01', target_day=due)
            # Pagination exists only in the authoritative outer WOL envelope.
            pages.append(dict(numberOfPages=2, workorderLines=rows,
                              data={'productionData': {due: rows}, 'numberOfPages': 'unrelated'}))
        with patch.object(epoptia_read.requests, 'get', side_effect=lambda url, **kw:
                          Mock(status_code=200, json=Mock(return_value=pages[kw['params']['page']-1]))) as http:
            await self.provider.snapshot()
        model = map_snapshot(self.provider.core_snapshot(), self.provider.clock())
        table = model['canonical_orders']
        self.assertEqual(http.call_count, 2)
        self.assertEqual([r['id'] for r in table['orders']], [701, 718])
        self.assertEqual([r['deadline'] for r in table['orders']], ['2026-09-18', '2026-09-21'])
        self.assertEqual([r['native_progress'] for r in table['orders']], [35, 65])
        self.assertEqual(table['deadline_scan']['reader'], 'epoptia_read.fetch_wols')
        self.assertEqual(table['deadline_scan']['scan_id'], model['station_coverage']['source']['scan_id'])
        self.assertEqual(table['deadline_scan']['pages_read'], 2)
        self.assertNotIn('productionData', str(table['orders'][0]['customer']))
        from dashboard.verify_live import consistency
        self.assertTrue(consistency(model))
        self.assertNotIn(('GET', 'grouped_workorderlines'), self.calls)

    async def test_authoritative_multiple_dates_and_missing(self):
        self.orders = [[wol(1, 701, 35)], [wol(2, 718, 65)]]
        pages = [dict(numberOfPages=2, workorderLines=[dict(wol(i+1, 701), target_day=due)],
                      productionData={due: [wol(i+1, 701)], 'invalid': [wol(9, 718)]})
                 for i, due in enumerate(('2026-09-18', '2026-09-21'))]
        with patch.object(epoptia_read.requests, 'get', side_effect=lambda url, **kw:
                          Mock(status_code=200, json=Mock(return_value=pages[kw['params']['page']-1]))):
            await self.provider.snapshot()
        table = self.provider.core_snapshot()['canonical_orders']
        rows = table['orders']
        expected = ('2026-09-18' if table['deadline_coverage']['as_of'] <= '2026-09-18'
                    else '2026-09-21')
        self.assertEqual([r['deadline'] for r in rows], [expected, None])
        self.assertEqual([r['deadline_reason'] for r in rows],
                         [None, 'missing_or_invalid_deadline'])
        self.assertEqual([r['native_progress'] for r in rows], [35, 65])

    async def test_authoritative_incomplete_scan_keeps_native_progress(self):
        self.production_date = '2026-09-18'
        self.failed_source = 'stations'
        await self.provider.snapshot()
        table = self.provider.core_snapshot()['canonical_orders']
        self.assertFalse(table['deadline_scan']['complete'])
        self.assertEqual(table['deadline_scan']['status'], 'upstream_timeout')
        self.assertTrue(all(r['deadline'] is None and r['deadline_reason'] == 'incomplete_scan'
                            for r in table['orders']))
        self.assertEqual([r['native_progress'] for r in table['orders']], [25, 75])
        self.assertEqual(self.get.call_count, 2)

    async def test_shared_snapshot_consumed_once_per_worker(self):
        await self.provider.refresh_core('workstation_wip')
        await self.provider.refresh_core('production_overview')
        self.assertEqual(self.get.call_count, 2)
        first = self.provider.core_snapshot()['canonical_orders']['deadline_scan']['scan_id']
        await self.provider.refresh_core('workstation_wip')
        await self.provider.refresh_core('production_overview')
        snapshot = self.provider.core_snapshot()
        self.assertEqual(self.get.call_count, 4)
        self.assertNotEqual(first, snapshot['canonical_orders']['deadline_scan']['scan_id'])
        self.assertEqual(snapshot['canonical_orders']['deadline_scan']['scan_id'],
                         snapshot['station_coverage']['source']['scan_id'])

    async def test_shared_snapshot_expires(self):
        await self.provider.refresh_core('workstation_wip')
        self.provider.read._wol_at = monotonic() - WOL_SNAPSHOT_REUSE_SECONDS - 1
        await self.provider.refresh_core('production_overview')
        self.assertEqual(self.get.call_count, 4)

    async def test_all_rollforward_candidates_and_strict_dates(self):
        from epoptia_queries import read_wol_snapshot
        groups = {f'2026-09-{day}': [wol(day, 701)] for day in range(18, 23)}
        groups.update({key: [wol(100+i, 718)] for i, key in enumerate(
            ('2026-02-30', None, '', 'past'))})
        payload = dict(numberOfPages=1, workorderLines=[dict(row, target_day=key)
            for key, rows in groups.items() for row in rows], productionData={})
        with patch.object(epoptia_read.requests, 'get', return_value=Mock(
                status_code=200, json=Mock(return_value=payload))):
            result = read_wol_snapshot('https://synthetic.invalid', {})
        self.assertTrue(result['deadlines']['source']['complete'])
        # Intermediate dates must survive so the next deadline can roll forward.
        from datetime import date
        self.assertEqual(result['deadlines']['dates'][701],
                         {date(2026, 9, day) for day in range(18, 23)})
        self.assertNotIn(718, result['deadlines']['dates'])
        # The shared internal snapshot retains rows for completion/history readers.
        self.assertEqual(result['wol_rows'], payload['workorderLines'])

    async def test_missing_grouping_does_not_invalidate_authoritative_scan(self):
        pages = [dict(numberOfPages=2, workorderLines=rows) for rows in self.stations]
        with patch.object(epoptia_read.requests, 'get', side_effect=lambda url, **kw:
                          Mock(status_code=200, json=Mock(return_value=pages[kw['params']['page']-1]))):
            await self.provider.snapshot()
        table = self.provider.core_snapshot()['canonical_orders']
        self.assertTrue(table['deadline_scan']['complete'])
        self.assertTrue(all(r['deadline'] is None and r['deadline_reason'] == 'missing_or_invalid_deadline'
                            for r in table['orders']))

    async def test_adapter_handles_null_sections_and_invalid_station_counts(self):
        from dashboard.provider import empty_snapshot
        snapshot = empty_snapshot(self.provider.clock())
        snapshot.update(workstations=None, active_production=None, today=None,
                        field_status=None, field_observed_at=None)
        model = map_snapshot(snapshot, self.provider.clock())
        self.assertEqual(model['workstations'], [])
        self.assertIsNone(model['native_mean_order_progress_percent'])
        snapshot['workstations'] = [None, dict(name='LASER', running_steps=True,
                                             paused_steps='2', waiting_steps=0)]
        station = map_snapshot(snapshot, self.provider.clock())['workstations'][0]
        self.assertIsNone(station['running_steps'])
        self.assertIsNone(station['paused_steps'])
        self.assertEqual(station['waiting_steps'], 0)

    async def test_default_four_success_generations_then_last_good_failure(self):
        for generation in range(1, 5):
            await self.provider.snapshot()
            for source in ('production_overview', 'workstation_wip'):
                self.assertEqual(self.provider.sources[source]['generation'], generation)
                self.assertIsNotNone(self.provider.sources[source]['last_success'])
        before = self.provider.core_snapshot()
        self.failed_source = 'orders'
        await self.provider.snapshot()
        after = self.provider.core_snapshot()
        self.assertEqual(after['canonical_orders'], before['canonical_orders'])
        self.assertEqual(after['order_generation'], 4)
        self.assertEqual(after['sources']['workstation_wip']['generation'], 5)
        self.assertEqual(after['sources']['production_overview']['failure_reason'], 'upstream_timeout')
        self.assertEqual(self.get.call_count, 10)  # One shared two-page WOL scan per snapshot.
        self.mcp.assert_not_called()
        self.transport.assert_not_called()
        self.assertIsNotNone(after['sources']['workstation_wip']['shared_wol_scan_id'])
        self.assertEqual(sum(meta.get('http_requests', 0) for meta in after['sources'].values()), 20)
        self.assertNotIn('private', str(after))

    async def test_shared_wol_endpoint_failure_preserves_both_last_good_sources(self):
        await self.provider.snapshot()
        before = self.provider.core_snapshot()['workstations']
        self.failed_source = 'stations'
        await self.provider.snapshot()
        self.assertEqual(self.provider.sources['production_overview']['generation'], 2)
        self.assertIsNone(self.provider.sources['production_overview']['failure_reason'])
        self.assertEqual(self.provider.sources['workstation_wip']['generation'], 1)
        self.assertEqual(self.provider.sources['workstation_wip']['failure_reason'], 'upstream_timeout')
        self.assertEqual(self.provider.core_snapshot()['workstations'], before)

    async def test_shared_tool_results_equal_default_provider(self):
        await self.provider.snapshot()
        snapshot = self.provider.core_snapshot()
        tool_orders = self.scope['production_overview'](dashboard=True)['dashboard_orders']
        for orders in (snapshot['canonical_orders'], tool_orders):
            orders['deadline_scan'].pop('scan_id', None)
            orders['sources']['parent_deadlines'].pop('scan_id', None)
        self.assertEqual(snapshot['canonical_orders'], tool_orders)
        for raw in self.scope['workstation_wip'](dashboard=True)['dashboard_stations']['workstations']:
            decorated = next(row for row in snapshot['workstations'] if row['name'] == raw['name'])
            self.assertEqual({key: decorated[key] for key in raw}, raw)
        self.mcp.assert_not_called()
        self.transport.assert_not_called()

    async def test_native_identity_dedup_and_unknown_deadline(self):
        await self.provider.snapshot()
        model = map_snapshot(self.provider.core_snapshot(), self.provider.clock())
        orders = model['canonical_orders']
        self.assertEqual([r['id'] for r in orders['orders']], [1, 2])
        self.assertEqual(model['native_mean_order_progress_percent'], 50)
        self.assertEqual(model['today']['active_work'], 2)
        self.assertEqual(orders['source']['pages_read'], 2)
        self.assertEqual(orders['source']['rows_read'], 3)
        self.assertIsNone(model['today']['overdue_work'])
        self.assertFalse(orders['deadline_coverage']['complete'])
        for row in orders['orders']:
            self.assertIsNone(row['deadline'])
            self.assertEqual(row['deadline_reason'], 'missing_or_invalid_deadline')
            self.assertEqual(row['progress_provenance']['path'], 'workorder.progress')
        self.assertIsNone(model['today']['completed_today'])
        self.assertIsNone(model['overall_progress_percent'])

    async def test_verified_grouping_deadline_reaches_dashboard(self):
        self.production_date = '2001-01-01'
        self.stations[0][1]['workorder']['id'] = 2
        for page in self.stations:
            for row in page:
                row['target_day'] = self.production_date
        await self.provider.snapshot()
        model = map_snapshot(self.provider.core_snapshot(), self.provider.clock())
        self.assertTrue(model['canonical_orders']['deadline_coverage']['complete'])
        self.assertEqual([row['id'] for row in model['urgent_orders']], ['1', '2'])
        self.assertTrue(all(row['deadline'] == self.production_date
                            for row in model['urgent_orders']))
        self.assertEqual(model['today']['overdue_work'], 2)
        self.assertEqual(model['native_mean_order_progress_percent'], 50)
        self.assertIsNone(model['today']['completed_today'])

    async def test_compact_station_filter_states_and_provenance(self):
        await self.provider.snapshot()
        model = map_snapshot(self.provider.core_snapshot(), self.provider.clock())
        station = model['workstations'][0]
        self.assertEqual([station[s+'_steps'] for s in ('running', 'paused', 'waiting', 'unknown')], [2, 1, 3, 0])
        self.assertIn('including_future', station['waiting_scope'])
        self.assertIsNone(station['executable_queue_steps'])
        self.assertEqual(station['load_percent'], 0)
        self.assertEqual(model['station_coverage']['source']['pages_read'], 2)
        self.assertEqual(model['station_coverage']['diagnostics']['excluded_terminal_wols'], 3)
        payload = self.scope['workstation_wip'](dashboard=True)
        self.assertNotIn('wol_rows', payload)
        self.assertNotIn('Synthetic', str(payload))

    async def test_incomplete_orders_and_missing_identity_rejected(self):
        self.orders[1] = []
        await self.provider.snapshot()
        self.assertEqual(self.provider.sources['production_overview']['failure_reason'], 'invalid_pagination')
        self.assertEqual(self.provider.sources['workstation_wip']['generation'], 1)
        self.orders[1] = [dict(workorderline_id=3)]
        await self.provider.snapshot()
        self.assertEqual(self.provider.sources['production_overview']['generation'], 0)

    async def test_incomplete_station_pagination_rejected(self):
        self.stations[1] = []
        await self.provider.snapshot()
        self.assertEqual(self.provider.sources['workstation_wip']['failure_reason'], 'invalid_pagination')
        self.assertEqual(self.provider.sources['production_overview']['generation'], 1)

    async def test_repeated_station_page_rejected(self):
        self.stations[1] = self.stations[0]
        await self.provider.snapshot()
        self.assertEqual(self.provider.sources['workstation_wip']['failure_reason'], 'repeated_page')

    async def test_malformed_station_record_not_silently_dropped(self):
        self.stations[1].append(None)
        await self.provider.snapshot()
        self.assertEqual(self.provider.sources['workstation_wip']['failure_reason'], 'invalid_response')

    async def test_native_conflict_does_not_invalidate_other_order(self):
        self.orders[1][0]['workorder']['progress'] = 30
        await self.provider.snapshot()
        census = self.provider.core_snapshot()['canonical_orders']
        self.assertIsNone(census['orders'][0]['native_progress'])
        self.assertEqual(census['native_mean_order_progress_percent'], 75)
        self.assertEqual(census['native_progress_coverage_percent'], 50)

    async def wait_idle(self, *tools):
        for _ in range(400):
            if not any(self.provider.read.busy(tool) for tool in tools):
                return
            await asyncio.sleep(0.005)
        self.fail('reader still busy')

    async def test_blocked_request_serializes_sources_and_get_stays_nonblocking(self):
        # One Epoptia request at a time: while production_overview's request is
        # in flight, workstation_wip sends nothing; cached GETs never block.
        self.block = 'production_overview'
        task = asyncio.create_task(self.provider.refresh_core(self.block))
        for _ in range(100):
            if self.entered.is_set():
                break
            await asyncio.sleep(0.005)
        self.assertTrue(self.entered.is_set())
        try:
            before = len(self.calls)
            stations = asyncio.create_task(self.provider.refresh_core('workstation_wip'))
            await asyncio.sleep(0.1)
            self.assertEqual(len(self.calls), before)
            app = create_app(self.provider, timer=lambda: -1)
            app.extensions['dashboard_stop'].set()
            with app.test_client() as client:
                for _ in range(3):
                    start = monotonic()
                    self.assertEqual(client.get('/api/dashboard').status_code, 200)
                    self.assertLess(monotonic()-start, 0.1)
            self.assertEqual(len(self.calls), before)
        finally:
            self.release.set()
            await task
        await stations
        self.assertEqual(self.provider.sources['production_overview']['generation'], 1)
        self.assertEqual(self.provider.sources['workstation_wip']['generation'], 1)

    async def test_timeout_stops_next_page_and_does_not_count_busy_retries(self):
        self.block = 'production_overview'
        with patch('dashboard.provider.READ_TIMEOUT', 0.03):
            await self.provider.refresh_core('production_overview')
            self.assertTrue(self.entered.is_set())
            meta = deepcopy(self.provider.sources['production_overview'])
            self.assertEqual(meta['failure_reason'], 'timeout')
            for _ in range(3):
                await self.provider.refresh_core('production_overview')
                await self.provider.refresh_core('workstation_wip')
            self.assertEqual(self.provider.sources['production_overview'], meta)
            # The hung request still holds the shared Epoptia lock.
            self.assertEqual(self.provider.sources['workstation_wip']['generation'], 0)
        self.release.set()
        await self.wait_idle('production_overview', 'workstation_wip')
        self.block = None
        await self.provider.refresh_core('workstation_wip')
        meta = self.provider.sources['workstation_wip']
        self.assertIsNone(meta['failure_reason'])
        self.assertEqual(meta['generation'], meta['refresh_sequence'])
        self.assertEqual(self.provider.sources['production_overview']['http_requests'], 1)
        self.assertEqual(self.provider.sources['production_overview']['generation'], 0)

    async def test_three_refreshes_independent_failures_and_recovery(self):
        for failed in (None, 'orders', 'stations'):
            self.failed_source = failed
            await self.provider.snapshot()
        snapshot = self.provider.core_snapshot()
        self.assertEqual(snapshot['sources']['production_overview']['generation'], 3)
        self.assertIsNone(snapshot['sources']['production_overview']['failure_reason'])
        self.assertEqual(snapshot['sources']['workstation_wip']['generation'], 2)
        self.assertTrue(snapshot['sources']['workstation_wip']['stale'])
        self.assertEqual(snapshot['workstations'][0]['running_steps'], 2)
        self.assertEqual(snapshot['canonical_orders']['active_workorders_total'], 2)
        for name, meta in snapshot['sources'].items():
            if name == 'production_overview':
                self.assertEqual(meta['read_attempts'], 3)
        self.assertEqual(sum(meta.get('http_requests', 0) for meta in snapshot['sources'].values()), 12)

    async def test_legacy_defaults_and_dashboard_filter_validation(self):
        legacy = self.scope['workstation_wip'](limit=1)
        self.assertEqual(legacy, dict(ok=True, **epoptia_read.workstation_wip(
            self.stations[0]+self.stations[1], limit=1, workstation=None, step=None)))
        overview = self.scope['production_overview']()
        expected = epoptia_read.overview(self.stations[0]+self.stations[1])
        self.assertEqual({k: overview[k] for k in expected}, expected)
        before = self.get.call_count
        self.assertFalse(self.scope['workstation_wip'](dashboard=True, workstation='LASER')['ok'])
        self.assertEqual(self.get.call_count, before)

    async def test_diagnostic_requires_three_new_generations_not_polls(self):
        from dashboard.verify_live import Observer
        observer = Observer()
        await self.provider.snapshot()
        model = map_snapshot(self.provider.core_snapshot(), self.provider.clock())
        for _ in range(5):
            report = observer.observe(model, 1)
            self.assertFalse(report['passed'])
            self.assertEqual(observer.successes['production_overview'], 0)
        for index in range(3):
            await self.provider.snapshot()
            model = map_snapshot(self.provider.core_snapshot(), self.provider.clock())
            report = observer.observe(model, 1)
            self.assertEqual(report['passed'], index == 2)
        self.assertNotIn('SYNTHETIC', str(report))

    async def test_verifier_rejects_new_generation_without_new_http_read(self):
        from dashboard.verify_live import Observer
        observer = Observer()
        await self.provider.snapshot()
        model = map_snapshot(self.provider.core_snapshot(), self.provider.clock())
        observer.observe(model, 1)
        for generation in range(2, 6):
            replay = deepcopy(model)
            replay['order_generation'] = generation
            for meta in replay['sources'].values():
                meta.update(generation=generation, last_success=f'2026-09-09T12:00:0{generation}+00:00')
            self.assertFalse(observer.observe(replay, 1)['passed'])
        self.assertEqual(list(observer.successes.values()), [0, 0])

    async def test_api_cached_get_does_not_advance_read_evidence(self):
        await self.provider.snapshot()
        before = deepcopy(self.provider.sources)
        with patch('dashboard.server.Thread'):
            app = create_app(self.provider)
        with app.test_client() as client:
            for _ in range(5):
                model = client.get('/api/dashboard').json
                self.assertEqual(model['sources'], before)
                self.assertEqual(model['canonical_orders']['orders'][0]['id'], 1)
                self.assertEqual(model['canonical_orders']['orders'][0]['native_progress'], 25)
                self.assertIsNone(model['today']['completed_today'])
                self.assertEqual(model['workstations'][0]['load_percent'], 0)
        self.assertEqual(self.provider.sources, before)

    async def test_login_failure_does_not_claim_new_data_read(self):
        await self.provider.snapshot()
        before = deepcopy(self.provider.sources['production_overview'])
        with patch.object(requests.Session, 'get', return_value=Mock(status_code=401)):
            await self.provider.refresh_core('production_overview')
        after = self.provider.sources['production_overview']
        self.assertEqual(after['failure_reason'], 'login_failed')
        for field in ('read_id', 'successful_read_id', 'http_requests', 'read_attempts', 'generation'):
            self.assertEqual(after[field], before[field])
        self.assertEqual(after['attempts'], before['attempts']+1)

    async def test_diagnostic_rejects_duplicate_and_inconsistent_coverage(self):
        from dashboard.verify_live import consistency
        await self.provider.snapshot()
        model = map_snapshot(self.provider.core_snapshot(), self.provider.clock())
        self.assertTrue(consistency(model))
        bad = deepcopy(model)
        bad['canonical_orders']['orders'].append(bad['canonical_orders']['orders'][0])
        self.assertFalse(consistency(bad))
        bad = deepcopy(model)
        bad['today']['overdue_work'] = 0
        self.assertFalse(consistency(bad))
        bad = deepcopy(model)
        bad['canonical_orders']['deadline_coverage']['complete'] = True
        self.assertFalse(consistency(bad))

    async def test_diagnostic_timeout_and_no_redirect(self):
        import contextlib
        import io
        from dashboard.verify_live import main, NoRedirect
        output = io.StringIO()
        with patch('dashboard.verify_live.build_opener') as opener, patch(
                'dashboard.verify_live.monotonic', side_effect=[0, 0, 0, 2, 2]), contextlib.redirect_stdout(output):
            opener.return_value.open.side_effect = OSError('synthetic private detail')
            self.assertEqual(main(['--wait', '1']), 1)
        self.assertIn('"result": "timeout"', output.getvalue())
        self.assertNotIn('private', output.getvalue())
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://synthetic.invalid'))
