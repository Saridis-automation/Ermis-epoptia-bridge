"""Production-hours refresh cadence, serialized scheduler and the halt switch."""
import asyncio
from datetime import datetime, timezone
import threading
import time
import unittest

from dashboard.provider import empty_snapshot
from dashboard.schedule import (MAX_FRESHNESS_SECONDS, OFF_INTERVAL, WORK_INTERVAL,
                                freshness_seconds, in_work_hours, refresh_delay)
from dashboard.server import create_app


def athens(year, month, day, hour, minute=0):
    # September 2026 is EEST (UTC+3).
    return datetime(year, month, day, hour - 3, minute, tzinfo=timezone.utc)


class ScheduleTests(unittest.TestCase):
    def test_work_hours_are_weekdays_seven_to_five_athens_time(self):
        self.assertTrue(in_work_hours(athens(2026, 9, 28, 7)))       # Monday 07:00
        self.assertTrue(in_work_hours(athens(2026, 9, 25, 16, 59)))  # Friday 16:59
        self.assertFalse(in_work_hours(athens(2026, 9, 28, 6, 59)))
        self.assertFalse(in_work_hours(athens(2026, 9, 28, 17)))
        self.assertFalse(in_work_hours(athens(2026, 9, 26, 10)))     # Saturday

    def test_refresh_every_five_minutes_in_work_hours(self):
        self.assertEqual(refresh_delay(athens(2026, 9, 29, 10)), WORK_INTERVAL)

    def test_refresh_every_thirty_minutes_off_hours(self):
        self.assertEqual(refresh_delay(athens(2026, 9, 29, 20)), OFF_INTERVAL)
        self.assertEqual(refresh_delay(athens(2026, 9, 27, 12)), OFF_INTERVAL)  # Sunday

    def test_off_hours_wait_stops_at_the_next_window(self):
        self.assertEqual(refresh_delay(athens(2026, 9, 29, 6, 50)), 600)
        self.assertEqual(refresh_delay(athens(2026, 9, 29, 16, 59)), WORK_INTERVAL)

    def test_freshness_follows_the_cadence(self):
        self.assertEqual(freshness_seconds(athens(2026, 9, 29, 10)), WORK_INTERVAL + 600)
        self.assertEqual(freshness_seconds(athens(2026, 9, 29, 7, 5)), MAX_FRESHNESS_SECONDS)
        self.assertEqual(freshness_seconds(athens(2026, 9, 29, 22)), MAX_FRESHNESS_SECONDS)


class FakeProvider:
    refresh_sources = ('production_overview', 'workstation_wip')

    def __init__(self):
        self.calls, self.active, self.peak = [], 0, 0
        self.lock = threading.Lock()

    async def refresh_core(self, tool):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append(tool)
        await asyncio.sleep(0.02)
        with self.lock:
            self.active -= 1

    def core_snapshot(self):
        return empty_snapshot(datetime.now(timezone.utc))


class SchedulerTests(unittest.TestCase):
    def app(self, provider, halted):
        app = create_app(provider, timer=lambda: 0, halted=halted)
        self.addCleanup(app.extensions['dashboard_stop'].set)
        return app

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail('condition not reached')

    def test_sources_refresh_one_after_another(self):
        provider = FakeProvider()
        self.app(provider, halted=lambda: None)
        self.wait_for(lambda: len(provider.calls) == 2 and provider.active == 0)
        time.sleep(0.3)
        self.assertEqual(provider.calls, ['production_overview', 'workstation_wip'])
        self.assertEqual(provider.peak, 1)

    def test_halt_stops_refreshes_and_is_shown(self):
        provider = FakeProvider()
        record = dict(halted_at='2026-09-29T10:00:00+00:00', http_status=429,
                      path='/api/3.03/workorderlines', pid=1)
        app = self.app(provider, halted=lambda: record)
        time.sleep(0.3)
        self.assertEqual(provider.calls, [])
        with app.test_client() as client:
            model = client.get('/api/dashboard').get_json()
        self.assertEqual(model['data_status'], 'halted')
        self.assertEqual(model['halt'], dict(halted_at='2026-09-29T10:00:00+00:00', http_status=429,
                                             path='/api/3.03/workorderlines'))
        self.assertIn('freshness_seconds', model)


if __name__ == '__main__':
    unittest.main()
