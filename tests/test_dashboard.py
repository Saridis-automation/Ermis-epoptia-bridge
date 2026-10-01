"""Local dashboard HTTP, adapter and transport checks; no live service."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from threading import Event
from time import monotonic, sleep
from dashboard.adapter import map_snapshot
from dashboard.provider import LocalEpoptiaProvider, empty_snapshot, read_local
from dashboard.server import create_app
from test_dashboard_refresh import read, NOW

class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.provider=LocalEpoptiaProvider(read=read,clock=lambda:NOW)
        asyncio.run(self.provider.snapshot())
        self.app=create_app(self.provider,clock=lambda:NOW)
        self.addCleanup(self.app.extensions['dashboard_stop'].set)
        self.client=self.app.test_client()

    def test_read_only_routes(self):
        for path in ['/', '/api/dashboard', '/health', '/static/dashboard.js', '/static/dashboard.css']:
            response=self.client.get(path)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            self.assertIn("script-src 'self'",response.headers['Content-Security-Policy'])
            response.close()
            for method in ['post','put','patch','delete']:
                self.assertEqual(getattr(self.client,method)(path).status_code,405)
        self.assertEqual(self.client.get('/static/../server.py').status_code,404)

    def test_factual_native_mean_not_capacity(self):
        model=self.client.get('/api/dashboard').json
        self.assertIsNone(model['overall_progress_percent'])
        self.assertEqual(model['native_mean_order_progress_percent'],20)
        self.assertEqual(model['workstations'][0]['load_percent'], 0)   # fixture line has no delivery date
        self.assertEqual(model['workstations'][0]['running_steps'],1)
        self.assertIsNone(model['today']['completed_today'])

    def test_get_never_schedules_upstream_and_is_fast(self):
        self.app.extensions['dashboard_stop'].set()
        sleep(0.03)
        self.provider.read=AsyncMock(side_effect=AssertionError('GET must not read'))
        for _ in range(5):
            start=monotonic();response=self.client.get('/api/dashboard')
            self.assertEqual(response.status_code,200)
            self.assertLess(monotonic()-start,0.1)
        self.provider.read.assert_not_called()

    def test_scheduler_refreshes_without_get(self):
        entered=Event()
        async def source(tool): entered.set();return await read(tool)
        p=LocalEpoptiaProvider(read=source)
        app=create_app(p);self.addCleanup(app.extensions['dashboard_stop'].set)
        self.assertTrue(entered.wait(1))

    def test_stale_athens_midnight(self):
        at=NOW.replace(hour=20,minute=59,second=59)
        snapshot=empty_snapshot(at)
        snapshot.update(field_observed_at={'completed_today':at.isoformat()},today={'completed_today':2})
        self.assertEqual(map_snapshot(snapshot,at+timedelta(seconds=2))['field_status']['completed_today'],'stale')

    def test_invalid_observation_and_metrics(self):
        snapshot=empty_snapshot(NOW)
        snapshot['observed_at']='invalid'
        with self.assertRaises(ValueError):map_snapshot(snapshot,NOW)
        for value in (True,-1,101,float('nan'),'10'):
            snapshot=empty_snapshot(NOW)
            snapshot['active_production']={'native_active_production_progress_source':{'complete':True},
                'native_active_production_progress_percent':value}
            self.assertIsNone(map_snapshot(snapshot,NOW)['native_mean_order_progress_percent'])

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_failure_is_sanitized_and_cancellation_propagates(self):
        with patch('dashboard.provider._read_local', new_callable=AsyncMock) as read:
            read.side_effect = RuntimeError('synthetic private diagnostic')
            with self.assertRaisesRegex(ValueError, '^Dashboard read unavailable$'):
                await read_local('production_overview')
            read.side_effect = asyncio.CancelledError()
            with self.assertRaises(asyncio.CancelledError):
                await read_local('production_overview')

    async def test_transport_allowlist_and_protocol(self):
        for name in ['restart_service', 'due_wols', 'arbitrary']:
            with self.assertRaises(ValueError):
                await read_local(name)
        with patch('dashboard.provider._read_local', new_callable=AsyncMock, return_value={'ok': True}) as read:
            self.assertEqual(await read_local('production_overview'), {'ok': True})
            read.assert_awaited_once_with('production_overview')
