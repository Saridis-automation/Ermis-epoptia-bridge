"""Synthetic source publication and cache regression checks."""
import asyncio
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from dashboard.provider import LocalEpoptiaProvider
from dashboard.adapter import map_snapshot
from dashboard.schedule import freshness_seconds
from test_dashboard_orders import project, wol

NOW=datetime(2026,9,8,12,tzinfo=timezone.utc)

async def read(tool):
    if tool=='production_overview':
        return dict(ok=True,dashboard_orders=project([wol()]))
    return dict(ok=True,complete=True,wol_rows=[dict(wol(),erp_routing=[
        dict(workstationName='LASER',status='started')])])


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_canonical_counts_and_generation(self):
        p=LocalEpoptiaProvider(read=read,clock=lambda:NOW)
        await p.snapshot();s=p.core_snapshot()
        self.assertEqual(s['today']['overdue_work'],s['canonical_orders']['overdue_work'])
        self.assertEqual(s['order_generation'],s['sources']['production_overview']['generation'])
        self.assertEqual(s['active_production']['active_workorders_total'],1)

    async def test_last_good_failure_stale_recovery(self):
        now=[NOW]; p=LocalEpoptiaProvider(read=read,clock=lambda:now[0])
        await p.snapshot();first=p.core_snapshot()
        p.read=AsyncMock(side_effect=ValueError('synthetic private detail'))
        now[0]+=timedelta(seconds=freshness_seconds(NOW)+1)
        await p.snapshot();s=p.core_snapshot()
        self.assertEqual(s['canonical_orders'],first['canonical_orders'])
        self.assertEqual(s['order_generation'],1)
        meta=s['sources']['production_overview']
        self.assertEqual(meta['refresh_sequence'],2)
        self.assertEqual(meta['consecutive_failures'],1)
        self.assertTrue(meta['stale'])
        self.assertNotIn('private',str(s))
        self.assertEqual(map_snapshot(s,now[0])['field_status']['urgent_orders'],'stale')
        p.read=read;await p.snapshot()
        self.assertEqual(p.core_snapshot()['order_generation'],3)

    async def test_independent_failure_and_no_duplicate_refresh(self):
        entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def blocked(tool):
            calls.append(tool)
            if tool=='production_overview':
                entered.set();await release.wait();raise ValueError()
            return await read(tool)
        p=LocalEpoptiaProvider(read=blocked,clock=lambda:NOW)
        task=asyncio.create_task(p.refresh_core('production_overview'))
        await entered.wait()
        await p.refresh_core('production_overview')
        for _ in range(3): await p.refresh_core('workstation_wip')
        self.assertEqual(calls.count('production_overview'),1)
        self.assertEqual(p.core_snapshot()['sources']['workstation_wip']['generation'],3)
        release.set();await task
        self.assertEqual(p.core_snapshot()['workstations'][0]['running_steps'],1)

    async def test_timeout_and_cancellation_release_source(self):
        async def blocked(tool): await asyncio.Event().wait()
        p=LocalEpoptiaProvider(read=blocked)
        with patch('dashboard.provider.READ_TIMEOUT',0.01): await p.refresh_core('production_overview')
        self.assertEqual(p.sources['production_overview']['failure_reason'],'timeout')
        task=asyncio.create_task(p.refresh_core('production_overview'));await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(p.sources['production_overview']['refreshing'])

    async def test_legacy_false_metrics_rejected(self):
        p=LocalEpoptiaProvider(read=AsyncMock(return_value=dict(ok=True,
            counts_by_workstation={'LASER':3000},dashboard_orders={'urgent_orders':[],'overdue_work':99})))
        s=map_snapshot(await p.snapshot(),p.clock())
        self.assertIsNone(s['today']['overdue_work']);self.assertIsNone(s['overall_progress_percent'])
        self.assertIsNone(s['workstations'][0]['wip_steps'])

    async def test_cache_restore_and_partial_scan_preserve_generation(self):
        with tempfile.TemporaryDirectory(dir='dashboard') as directory:
            p=LocalEpoptiaProvider(read=read,clock=lambda:NOW,cache_dir=directory)
            await p.snapshot()
            q=LocalEpoptiaProvider(read=AsyncMock(return_value=dict(ok=True,dashboard_orders=project([wol()],complete=False))),
                clock=lambda:NOW,cache_dir=directory)
            await q.snapshot()
            self.assertEqual(q.core_snapshot()['order_generation'],1)
            self.assertEqual(q.core_snapshot()['field_status']['urgent_orders'],'cached')

    async def test_optional_counts_cannot_mix_order_generations(self):
        p=LocalEpoptiaProvider(read=read,whole_orders=AsyncMock(return_value=project([wol(2),wol(3)])),
            overdue_orders=AsyncMock(return_value=999),clock=lambda:NOW)
        await p.snapshot()
        self.assertEqual(p.core_snapshot()['today']['overdue_work'],2)
        self.assertEqual(p.core_snapshot()['active_production']['active_workorders_total'],2)

    async def test_completion_refreshes_repeatedly_while_orders_block(self):
        from test_dashboard_correctness import history
        release=asyncio.Event();entered=asyncio.Event()
        async def orders():entered.set();await release.wait();return project([wol()])
        p=LocalEpoptiaProvider(read=read,whole_orders=orders,
            completed_today=AsyncMock(return_value=history([])),clock=lambda:NOW)
        task=asyncio.create_task(p.refresh_core('whole_orders'));await entered.wait()
        for _ in range(3):await p.refresh_core('completed_today')
        self.assertEqual(p.sources['completed_today']['generation'],3)
        self.assertEqual(p.core_snapshot()['today']['completed_today'],0)
        release.set();await task
