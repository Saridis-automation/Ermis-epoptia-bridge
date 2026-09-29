"""Deterministic required-source availability checks; synthetic reads only."""
import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch

from dashboard.schedule import freshness_seconds
from dashboard.provider import LocalEpoptiaProvider
from dashboard.server import create_app
from dashboard.orders import OrderCensus
from test_dashboard_refresh import read, NOW


class DataStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = NOW
        self.provider = LocalEpoptiaProvider(read=read, clock=lambda: self.now)

    def model(self):
        with patch('dashboard.server.Thread'):
            app = create_app(self.provider, clock=lambda: self.now)
        return app.test_client().get('/api/dashboard').get_json()

    async def test_startup_and_initial_refresh_offline(self):
        self.assertEqual(self.model()['data_status'], 'offline')
        await self.check_refreshing(False, 'offline')

    async def check_refreshing(self, seeded=True, expected='online', names=('production_overview', 'workstation_wip')):
        if seeded:
            await self.provider.snapshot()
        release = asyncio.Event()
        entered = {name: asyncio.Event() for name in names}
        async def blocked(name):
            entered[name].set()
            await release.wait()
            return await read(name)
        self.provider.read = blocked
        tasks = [asyncio.create_task(self.provider.refresh_core(name)) for name in names]
        try:
            for event in entered.values():
                await event.wait()
            model = self.model()
            self.assertEqual(model['data_status'], expected)
            for name in names:
                self.assertEqual(model['sources'][name]['state'], 'refreshing')
                self.assertTrue(model['sources'][name]['refreshing'])
        finally:
            release.set()
            await asyncio.gather(*tasks)

    async def test_both_refreshing_fresh_online(self):
        await self.check_refreshing()

    async def test_one_refreshing_one_ready_online(self):
        await self.check_refreshing(names=('production_overview',))

    async def test_failure_with_fresh_success_online(self):
        await self.provider.snapshot()
        canonical = self.model()['canonical_orders']
        self.now += timedelta(seconds=45)
        self.provider.read = AsyncMock(side_effect=ValueError())
        await self.provider.snapshot()
        model = self.model()
        self.assertEqual(model['data_status'], 'online')
        self.assertEqual(model['canonical_orders'], canonical)
        for meta in model['sources'].values():
            self.assertEqual(meta['failure_reason'], 'read_unavailable')
            self.assertIsNotNone(meta['last_failure'])
        self.now += timedelta(seconds=freshness_seconds(self.now) - 44)
        self.assertEqual(self.model()['data_status'], 'offline')

    async def test_missing_stale_and_boundary(self):
        await self.provider.refresh_core('production_overview')
        self.assertEqual(self.model()['data_status'], 'partial')
        await self.provider.refresh_core('workstation_wip')
        fresh = freshness_seconds(NOW)
        for seconds, expected in ((0, 'online'), (fresh, 'online'),
                                  (fresh + 0.001, 'offline')):
            self.now = NOW + timedelta(seconds=seconds)
            self.assertEqual(self.model()['data_status'], expected)
        await self.provider.refresh_core('workstation_wip')
        self.assertEqual(self.model()['data_status'], 'partial')

    async def test_invalid_initial_snapshots_offline(self):
        self.provider.read = AsyncMock(return_value={'ok': True})
        await self.provider.snapshot()
        self.assertEqual(self.model()['data_status'], 'offline')

    async def test_canonical_deadlines_701_and_718(self):
        self.now = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
        census = OrderCensus()
        census.consume([dict(workorderline_id=i, production_status='production',
                             target_day=deadline, workorder={'id': parent, 'progress': 35})
                        for i, (parent, deadline) in enumerate(
                            ((701, '2026-09-18'), (718, '2026-09-21'), (718, '2026-08-06')), 1)])
        projection = census.result(True, self.now)
        async def fixture(name):
            if name == 'production_overview':
                return dict(ok=True, dashboard_orders=projection)
            return await read(name)
        self.provider.read = fixture
        await self.provider.snapshot()
        model = self.model()
        self.assertEqual(model['canonical_orders'], projection)
        orders = {row['id']: row for row in model['canonical_orders']['orders']}
        self.assertEqual(orders[701]['deadline'], '2026-09-18')
        self.assertEqual(orders[718]['deadline'], '2026-09-21')
