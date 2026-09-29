"""Verified date contract regressions using synthetic reads only.

Run from the project root with the installed application dependencies:
    venv/bin/python -B -m unittest discover -s tests -p test_date_dashboard_hardening.py
Discovery also makes the sibling test fixture imports available.
"""
import asyncio
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import calendar_target_dates as calendar
import epoptia_queries
from dashboard.provider import LocalEpoptiaProvider
from dashboard.server import create_app
from wol_details import wol_details
from test_calendar_target_dates import card
from test_dashboard_refresh import read


class DateHardeningTests(unittest.TestCase):
    def setUp(self):
        # Keep process-local optional calendar evidence isolated between suites.
        cache = patch.object(calendar, '_cached', None)
        cache.start()
        self.addCleanup(cache.stop)

    def test_api_date_survives_optional_calendar_unavailability(self):
        for status in ('render_required', 'calendar_auth_missing', 'calendar_timeout'):
            with self.subTest(status=status), patch.object(
                    calendar, 'calendar_target_dates', return_value=calendar.result(status)):
                value = wol_details([dict(workorderline_id=3112, target_day='2026-09-21')], 3112)
                self.assertEqual(value['target_day'], '2026-09-21')
                self.assertEqual(value['effective_target_date'], '2026-09-21')
                self.assertEqual(value['effective_target_date_status'], 'ok')
                self.assertEqual(value['effective_target_date_provenance'], dict(
                    endpoint='/api/3.03/workorderlines', path='target_day',
                    derivation='date_component', verified=True))
                self.assertIsNone(value['target_date'])
                self.assertEqual(value['target_date_status'], status)
                self.assertEqual(value['target_date_reason'], status)

    def test_calendar_never_replaces_missing_invalid_or_conflicting_api_date(self):
        with patch.object(calendar, 'calendar_target_dates',
                          return_value=calendar.parse_calendar(card(due='2026-09-22'))):
            for raw in (None, '2026-02-30', 'past', '2026-09-21', '2026-09-21T23:30:00Z'):
                value = wol_details([dict(workorderline_id=3112, target_day=raw)], 3112)
                valid = raw in ('2026-09-21', '2026-09-21T23:30:00Z')
                self.assertEqual(value['effective_target_date'], '2026-09-21' if valid else None)
                self.assertEqual(value['effective_target_date_status'],
                                 'ok' if valid else 'missing_or_invalid_target_day')
                self.assertEqual(value['target_date'], '2026-09-22')
                if valid:
                    self.assertEqual(value['target_day'], raw)

    def test_overdue_3061_uses_closest_date_not_past_wrapper(self):
        parsed = calendar.parse_calendar(card(wol=3061, due='2026-07-31', outer='past'))
        self.assertEqual(parsed['status'], 'ok')
        self.assertEqual(parsed['records'][0]['target_date'], '2026-07-31')
        value = wol_details([dict(workorderline_id=3061, target_day='2026-07-31')], 3061)
        self.assertEqual(value['effective_target_date'], '2026-07-31')

    def test_full_dashboard_api_deadline_coverage_despite_optional_calendar(self):
        now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
        # 21 synthetic unfinished parents, with the five verified WOL dates.
        pairs = [(3061, '2026-07-31'), (3102, '2026-09-21'),
                 (3112, '2026-09-21'), (3113, '2026-09-21'), (3219, '2026-09-22')]
        pairs += [(4000+i, '2026-09-22') for i in range(16)]
        rows = [dict(workorderline_id=wol, target_day=due, production_status='production',
                     workorder=dict(id=700+i, progress=35)) for i, (wol, due) in enumerate(pairs)]
        for status in ('render_required', 'calendar_auth_missing', 'calendar_timeout'):
            def progress(base, **kwargs):
                # Canonical order scan lacks dates: only the REST WOL scan supplies them.
                kwargs['consume_rows']([dict(row, target_day=None) for row in rows])
                return dict(native_active_production_progress_source=dict(complete=True, status='ok'),
                            calendar_target_dates=calendar.result(status))

            def fetch(base, headers, *, scan):
                scan.update(complete=True, status='ok', endpoint='/api/3.03/workorderlines', method='GET')
                return rows

            with patch('epoptia_read.active_production_progress', side_effect=progress), patch(
                    'epoptia_read.fetch_wols', side_effect=fetch):
                projection = epoptia_queries.production_overview('https://example.invalid', headers={})
            async def source(tool):
                return projection if tool == 'production_overview' else await read(tool)
            provider = LocalEpoptiaProvider(read=source, clock=lambda: now)
            asyncio.run(provider.snapshot())
            with patch('dashboard.server.Thread'):
                app = create_app(provider, clock=lambda: now)
            response = app.test_client().get('/api/dashboard')
            self.assertEqual(response.status_code, 200)
            model = response.get_json()
            self.assertEqual(model['data_status'], 'online')
            self.assertEqual(model['field_status']['deadline'], 'available')
            self.assertEqual(model['calendar_target_dates']['status'], status)
            canonical = model['canonical_orders']
            self.assertTrue(canonical['deadline_coverage']['complete'])
            self.assertEqual(canonical['deadline_coverage']['dated_unfinished_orders'], 21)
            self.assertEqual(canonical['deadline_coverage']['unfinished_orders'], 21)
            for order, (_, due) in zip(canonical['orders'], pairs):
                self.assertEqual(order['deadline'], due)
                self.assertIsNone(order['target_date'])
                self.assertEqual(order['target_date_status'], status)
                self.assertEqual(order['target_date_reason'], status)
                self.assertEqual(order['native_progress'], 35)
                self.assertEqual(order['deadline_provenance']['path'], 'target_day')
                self.assertEqual(order['deadline_provenance']['endpoint'], '/api/3.03/workorderlines')
            self.assertEqual(model['urgent_orders'][0]['deadline'], '2026-07-31')
