"""Synthetic calendar fixtures only; no configuration or live network."""
import asyncio
import unittest
from unittest.mock import Mock, AsyncMock, patch
import calendar_target_dates as c


def card(wol=3112, order=718, due='2026-09-21', outer='past'):
    return (f'<div class="mainWolItem" data-id="{wol}" data-workorder="{order}" data-date="{outer}">'
            f'<div class="workorderWol" data-date="{due}"><span class="wolCardComponent" '
            f'data-id="{wol}" data-workorder="{order}">Άλλη ημερομηνία</span></div></div>')


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.cache = patch.object(c, '_cached', None)
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_dates_overdue_and_deduplication(self):
        for due in ('2026-09-16', '2026-09-21', '2024-02-29'):
            value = c.parse_calendar(card(due=due) * 2 + card(3113, due=due))
            self.assertEqual(value['status'], 'ok')
            self.assertEqual(len(value['records']), 2)
            self.assertEqual(value['records'][0], dict(wol_id=3112, workorder_id=718, target_date=due))

    def test_invalid_and_conflicting(self):
        for html in (card(wol='0'), card(wol='-1'), card(wol='1.0'), card(order='abc'),
                     card(due='2026-02-29'), card(due='2026-9-21'), card(due='past'),
                     card() + card(order=719), card() + card(due='2026-09-22'),
                     card().replace('data-id="3112"', 'data-id="3112" data-id="5"')):
            self.assertEqual(c.parse_calendar(html)['status'], 'calendar_invalid_dom')

    def test_fallback_only_without_inner_date(self):
        html = card(outer='2026-09-22').replace('class="workorderWol" data-date="2026-09-21"', 'class="other"')
        self.assertEqual(c.parse_calendar(html)['records'][0]['target_date'], '2026-09-22')
        self.assertEqual(c.parse_calendar(card(due='invalid', outer='2026-09-22'))['records'], [])
        self.assertEqual(c.parse_calendar('<div id="app"></div>')['status'], 'render_required')
        self.assertEqual(c.parse_calendar('<input type="password">')['status'], 'calendar_auth_missing')

    def session(self, html=None, status=200, url='https://example.invalid/planning/calendar', headers=None):
        response = Mock(status_code=status, url=url, headers=headers or {'Content-Type': 'text/html'})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [(html or card()).encode()]
        session = Mock()
        session.get.return_value = response
        return session

    def test_get_only_and_auth(self):
        session = self.session()
        self.assertEqual(c.fetch_calendar('https://example.invalid/path', session, authenticated=True)['status'], 'ok')
        session.get.assert_called_once_with('https://example.invalid/planning/calendar', headers={'Accept': 'text/html'},
                                            stream=True, timeout=5, allow_redirects=False)
        self.assertEqual([call[0] for call in session.method_calls], ['get'])
        session.reset_mock()
        self.assertEqual(c.fetch_calendar('https://example.invalid', session)['status'], 'calendar_auth_missing')
        session.get.assert_not_called()

    def test_redirect_public_error_size_time(self):
        cases = [(self.session(status=302), 'calendar_auth_missing'),
                 (self.session(url='https://other.invalid/planning/calendar'), 'calendar_auth_missing'),
                 (self.session(url='https://example.invalid/login'), 'calendar_auth_missing'),
                 (self.session(status=500), 'calendar_http_error'),
                 (self.session(html='<input type="password">'), 'calendar_auth_missing'),
                 (self.session(html='<div id="app"></div>'), 'render_required'),
                 (self.session(headers={'Content-Type': 'text/html', 'Content-Length': str(c.MAX_BYTES+1)}), 'calendar_size_limit'),
                 (self.session(html='x'*(c.MAX_BYTES+1)), 'calendar_size_limit')]
        for session, expected in cases:
            self.assertEqual(c.fetch_calendar('https://example.invalid', session, authenticated=True)['status'], expected)
        self.assertEqual(c.fetch_calendar('https://example.invalid', self.session(), authenticated=True,
                                         clock=Mock(side_effect=[0, 6]))['status'], 'calendar_timeout')
        session = self.session()
        session.get.side_effect = TimeoutError('must never be exposed')
        self.assertEqual(c.fetch_calendar('https://example.invalid', session, authenticated=True), c.result('calendar_timeout'))

    def test_aggregate_detail_filters_and_expiry(self):
        value = c.parse_calendar(card() + card(3113, due='2026-09-22'))
        order = c.order_targets(718, value)
        self.assertIsNone(order['target_date'])
        self.assertEqual((order['target_date_min'], order['target_date_max']), ('2026-09-21', '2026-09-22'))
        self.assertEqual(c.order_targets(718, c.parse_calendar(card()*2))['target_date'], '2026-09-21')
        self.assertIsNone(c.order_targets(719, value)['target_date'])
        self.assertIsNone(c.wol_target(1)['target_date'])
        c.refresh_calendar('https://example.invalid', self.session(card()+card(3113)), authenticated=True)
        self.assertEqual(c.calendar_target_dates(wol_ids=[3113])['records'][0]['wol_id'], 3113)
        self.assertEqual(c.calendar_target_dates(workorder_ids=[719])['records'], [])
        self.assertTrue(c.calendar_target_dates(limit=1)['truncated'])
        from wol_details import wol_details
        self.assertEqual(wol_details([{'workorderline_id': 3112}], 3112)['target_date'], '2026-09-21')
        with patch.object(c, '_cached_at', -1000):
            self.assertEqual(c.calendar_target_dates()['status'], 'calendar_auth_missing')

    def test_gateway_validation_and_separation(self):
        from ermis_gateway import validate
        action = validate('calendar_target_dates', {'wol_ids': [3112], 'limit': 1})
        self.assertEqual((action.server, action.tool, action.write), ('Epoptia_MES', 'calendar_target_dates', False))
        for args in ({'wol_ids': [True]}, {'wol_ids': ['1']}, {'workorder_ids': [0]}, {'limit': 201}, {'url': '/login'}, {'limit': True}):
            with self.assertRaises(ValueError):
                validate('calendar_target_dates', args)

    def test_rendered_dom_no_navigation(self):
        page = Mock(url='https://example.invalid/planning/calendar', content=AsyncMock(return_value=card()))
        value = asyncio.run(c.rendered_calendar(page, 'https://example.invalid', authenticated=True))
        self.assertEqual(value['status'], 'ok')
        self.assertEqual([call[0] for call in page.method_calls], ['content'])

    def test_dashboard_calendar_independent(self):
        from dashboard.provider import LocalEpoptiaProvider
        from dashboard.adapter import map_snapshot
        from test_dashboard_refresh import read, NOW
        for status in ('calendar_auth_missing', 'render_required'):
            async def source(tool):
                value = await read(tool)
                if tool == 'production_overview':
                    value['dashboard_orders']['calendar_target_dates'] = c.result(status)
                return value
            provider = LocalEpoptiaProvider(read=source, clock=lambda: NOW)
            asyncio.run(provider.snapshot())
            snapshot = provider.core_snapshot()
            self.assertEqual(snapshot['field_status']['target_date'], status)
            self.assertEqual(snapshot['data_status'], 'online')
            self.assertEqual(map_snapshot(snapshot, NOW)['calendar_target_dates']['status'], status)

    def test_query_composition_order_dates(self):
        import epoptia_queries
        def progress(base, **kwargs):
            kwargs['consume_rows']([dict(workorderline_id=3112, production_status='production',
                                        workorder={'id': 718, 'progress': 35})])
            return dict(native_active_production_progress_source={'complete': True},
                        calendar_target_dates=c.parse_calendar(card()+card(3113, due='2026-09-22')))
        with patch('epoptia_read.active_production_progress', side_effect=progress):
            value = epoptia_queries.production_overview('https://example.invalid')
        order = value['dashboard_orders']['orders'][0]
        self.assertIsNone(order['target_date'])
        self.assertEqual(order['target_date_min'], '2026-09-21')
        self.assertEqual(order['target_date_max'], '2026-09-22')

    def test_registered_mcp_action(self):
        import ast
        from pathlib import Path
        node = next(n for n in ast.parse(Path('mcp_server.py').read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'calendar_target_dates')
        node.decorator_list = []
        scope = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<calendar-test>', 'exec'), scope)
        self.assertEqual(scope['calendar_target_dates']()['status'], 'calendar_auth_missing')
        self.assertFalse(scope['calendar_target_dates'](limit=201)['ok'])


if __name__ == '__main__':
    unittest.main()
