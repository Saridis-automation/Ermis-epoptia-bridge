"""REV4 native progress contract; synthetic reader input, no live IO."""
import unittest
from copy import deepcopy

from dashboard.adapter import map_snapshot
from dashboard.provider import LocalEpoptiaProvider
from test_dashboard_orders import project, wol
from test_dashboard_refresh import NOW


class Revision4Tests(unittest.IsolatedAsyncioTestCase):
    async def test_native_mean_reaches_ui_without_inversion_or_wol_weighting(self):
        # Two active orders average 38. Extra WOLs for the first order and an
        # archived order must not weight that mean. Only the UI inverts to 62.
        rows = [wol(order=1, progress=20, wid=101),
                wol(order=1, progress=20, wid=102),
                wol(order=2, progress=56, wid=103),
                wol(order=3, progress=90, wid=104, status='archive')]
        before = deepcopy(rows)

        async def read(tool):
            if tool == 'production_overview':
                return dict(ok=True, dashboard_orders=project(rows))
            return dict(ok=True, complete=True, wol_rows=[])

        provider = LocalEpoptiaProvider(read=read, clock=lambda: NOW)
        await provider.snapshot()
        model = map_snapshot(provider.core_snapshot(), NOW)
        self.assertEqual(model['native_mean_order_progress_percent'], 38)
        self.assertEqual(model['native_progress_coverage_percent'], 100)
        self.assertEqual(model['today']['active_work'], 2)
        self.assertIsNone(model['overall_progress_percent'])
        self.assertEqual(rows, before)
