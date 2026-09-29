"""Synthetic cache/transport tests: no live services or Epoptia reads."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch

import httpx2

from dashboard.adapter import map_snapshot
from dashboard.provider import LocalEpoptiaProvider
from ermis_gateway import Gateway, call_existing
from dashboard.schedule import MAX_FRESHNESS_SECONDS
from ermis_gateway_cache import CACHE_URL, call_cached_production, cached_overview
import ermis_system_server as system


def snapshot():
    now = datetime.now(timezone.utc).isoformat()
    return dict(schema_version=1, data_status="online", order_generation=2,
        field_observed_at={"active_production": now},
        sources={"production_overview": dict(last_success=now, generation=2,
                                              stale=False, state="available")},
        canonical_orders=dict(canonical_version=2, complete=True, orders=[],
            active_workorders_total=0, native_progress_covered=0,
            native_mean_order_progress_percent=None, native_progress_coverage_percent=None,
            source=dict(complete=True, endpoint="/capacity-planning/workorderlines")))


class CachedProductionTests(unittest.IsolatedAsyncioTestCase):
    async def exchange(self, model=None, *, status=200, raw=None, error=None):
        requests = []
        client = httpx2.AsyncClient

        async def handler(request):
            requests.append(request)
            self.assertEqual(str(request.url), CACHE_URL)
            self.assertEqual(request.method, "GET")
            if error:
                raise error
            return (httpx2.Response(status, content=raw) if raw is not None
                    else httpx2.Response(status, json=model))

        def factory(**kwargs):
            self.assertEqual(kwargs, dict(trust_env=False, follow_redirects=False, timeout=3))
            return client(transport=httpx2.MockTransport(handler), **kwargs)

        with patch("httpx2.AsyncClient", side_effect=factory), \
                patch("ermis_gateway_cache.call_existing", new_callable=AsyncMock) as direct:
            result = await asyncio.wait_for(system.ermis_gateway_execute("request", {
                "session_id": "cache_test_session", "action": "production_overview",
                "arguments": {}}), 0.5)
            direct.assert_not_awaited()
        self.assertEqual(len(requests), 1)
        return result

    async def failure(self, category, **kwargs):
        result = await self.exchange(**kwargs)
        self.assertEqual(result, dict(ok=False, status="upstream_unavailable", retry_safe=True,
                                     failure_stage="dashboard_cache", error_category=category))

    async def test_immediate_cache_success_and_metadata(self):
        model = snapshot()
        result = await self.exchange(model)
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "production_overview")
        self.assertEqual(result["result"]["dashboard_orders"], model["canonical_orders"])
        self.assertEqual(result["result"]["sources"], model["sources"])
        self.assertEqual(result["result"]["active_workorders_total"], 0)

    async def test_refreshing_recent_success_is_usable(self):
        model = snapshot()
        model["sources"]["production_overview"]["state"] = "refreshing"
        self.assertTrue((await self.exchange(model))["ok"])

    async def test_completed_today_passes_through_system_gateway(self):
        for completed in ({"total": 7, "breakdown_by_workstation": {"Assembly": 4, "Cutting": 3}},
                          {"total": 0, "breakdown_by_workstation": {}},
                          {"total": 7, "breakdown_by_workstation": {"Assembly": 2}}):
            with self.subTest(completed=completed):
                model = dict(snapshot(), completed_today=completed)
                result = await self.exchange(model)
                self.assertTrue(result["ok"])
                self.assertEqual(result["result"]["completed_today"], completed)

    async def test_missing_completed_today_is_omitted(self):
        result = await self.exchange(snapshot())
        self.assertTrue(result["ok"])
        self.assertNotIn("completed_today", result["result"])

    async def test_malformed_completed_today_is_omitted(self):
        malformed = [None, [], "unknown", 0, {}, {"total": 1},
                     {"breakdown_by_workstation": {}},
                     {"total": 1, "breakdown_by_workstation": {}, "extra": 1}]
        malformed += [{"total": value, "breakdown_by_workstation": {}}
                      for value in (None, True, False, "1", 1.0, -1)]
        malformed += [{"total": 1, "breakdown_by_workstation": value}
                      for value in (None, [], "unknown")]
        malformed += [{"total": 1, "breakdown_by_workstation": {"Assembly": value}}
                      for value in (None, True, False, "1", 1.0, -1, {})]
        for completed in malformed:
            with self.subTest(completed=completed):
                result = await self.exchange(dict(snapshot(), completed_today=completed))
                self.assertTrue(result["ok"])
                self.assertNotIn("completed_today", result["result"])
        # JSON coerces object keys to strings; check invalid Python keys directly.
        model = dict(snapshot(), completed_today={"total": 1, "breakdown_by_workstation": {1: 1}})
        self.assertNotIn("completed_today", cached_overview(model, datetime.now(timezone.utc)))

    def test_completed_today_preserves_existing_fields_and_snapshot(self):
        model = snapshot()
        now = datetime.now(timezone.utc)
        model["canonical_orders"].update(active_workorders_total=5, native_progress_covered=3,
            native_mean_order_progress_percent=42.5, native_progress_coverage_percent=60)
        expected = cached_overview(model, now)
        for completed in ({"total": 7, "breakdown_by_workstation": {"Assembly": 2}}, None, {}):
            with self.subTest(completed=completed):
                model["completed_today"] = completed
                original = deepcopy(model)
                result = cached_overview(model, now)
                if completed:
                    self.assertIs(result.pop("completed_today"), completed)
                self.assertEqual(result, expected)
                self.assertEqual(model, original)

    async def test_stale_and_future_success_rejected(self):
        for age in (MAX_FRESHNESS_SECONDS + 60, -30):
            model = snapshot()
            model["sources"]["production_overview"]["last_success"] = (
                datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
            await self.failure("cache_stale", model=model)

    async def test_offline_partial_loading_rejected(self):
        for status in ("offline", "partial", "loading", "live"):
            model = snapshot()
            model["data_status"] = status
            await self.failure("cache_not_online", model=model)

    async def test_failed_source_rejected_even_when_dashboard_online(self):
        model = snapshot()
        model["sources"]["production_overview"].update(stale=True, state="cached")
        await self.failure("cache_stale", model=model)

    async def test_malformed_cache_rejected(self):
        for model in (None, [], {}, dict(snapshot(), canonical_orders={}),
                      dict(snapshot(), sources={}), dict(snapshot(), order_generation=3)):
            await self.failure("cache_invalid", model=model)
        for raw in (b"not json", b"\xff", b"x" * (4 * 1024 * 1024 + 1)):
            await self.failure("cache_invalid", raw=raw)
        for key, value in (("last_success", "invalid"), ("generation", True)):
            model = snapshot()
            model["sources"]["production_overview"][key] = value
            await self.failure("cache_invalid", model=model)
        for key, value in (("complete", False), ("active_workorders_total", -1),
                           ("native_mean_order_progress_percent", "unknown")):
            model = snapshot()
            model["canonical_orders"][key] = value
            await self.failure("cache_invalid", model=model)

    async def test_unavailable_no_fallback_or_redirect(self):
        for status in (302, 500, 404):
            await self.failure("cache_unavailable", status=status)
        for error in (httpx2.ConnectError("synthetic detail"), httpx2.ReadTimeout("detail")):
            await self.failure("cache_unavailable", error=error)

    async def test_overall_deadline_no_fallback(self):
        client = httpx2.AsyncClient
        async def stalled(request):
            await asyncio.Event().wait()
        with patch("ermis_gateway_cache.CACHE_TIMEOUT_SECONDS", 0.01), \
                patch("httpx2.AsyncClient", side_effect=lambda **kw: client(
                    transport=httpx2.MockTransport(stalled), **kw)), \
                patch("ermis_gateway_cache.call_existing", new_callable=AsyncMock) as direct:
            result = await Gateway(call_cached_production).execute("production_overview", {})
            self.assertEqual(result["error_category"], "cache_unavailable")
            direct.assert_not_awaited()

    async def test_real_dashboard_projection_is_compatible(self):
        now = datetime.now(timezone.utc)
        model = snapshot()
        orders = dict(model["canonical_orders"], urgent_orders=[], overdue_work=0)
        provider = LocalEpoptiaProvider(read=AsyncMock(), clock=lambda: now)
        provider.sources = deepcopy(model["sources"])
        provider._good = {
            "production_overview": dict(value=orders, observed_at=now.isoformat(), generation=2),
            "workstation_wip": dict(value={"workstations": []}, observed_at=now.isoformat(), generation=2)}
        provider.sources["production_overview"]["last_success"] = now.isoformat()
        self.assertTrue((await self.exchange(map_snapshot(provider.core_snapshot(), now)))["ok"])

    async def test_voice_default_and_other_actions_unchanged(self):
        self.assertIs(Gateway().invoke, call_existing)
        with patch("ermis_gateway_cache.call_existing", new_callable=AsyncMock) as direct:
            await call_cached_production("Epoptia_MES", "workstation_wip", {})
            direct.assert_awaited_once_with("Epoptia_MES", "workstation_wip", {})
