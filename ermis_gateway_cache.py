"""Bounded dashboard reads for the ChatGPT gateway, without Epoptia fallback."""
import asyncio
from datetime import datetime, timezone
import math

from dashboard.adapter import timestamp
from dashboard.schedule import freshness_seconds
from ermis_gateway import UpstreamDiagnosticError, call_existing

CACHE_URL = "http://127.0.0.1:8010/api/dashboard"
# Freshness follows the dashboard's production-hours refresh cadence.
CACHE_TIMEOUT_SECONDS = 3
MAX_CACHE_BYTES = 4 * 1024 * 1024


def reject(category):
    raise UpstreamDiagnosticError("dashboard_cache", category)


def cached_overview(model, now):
    """Validate source identity/freshness before mapping canonical order fields."""
    if not isinstance(model, dict) or model.get("schema_version") != 1:
        reject("cache_invalid")
    if model.get("data_status") != "online":
        reject("cache_not_online")
    try:
        source = model["sources"]["production_overview"]
        age = (now - timestamp(source["last_success"])).total_seconds()
        generation = source["generation"]
        if type(generation) is not int or generation < 1:
            reject("cache_invalid")
        if not 0 <= age <= freshness_seconds(now):
            reject("cache_stale")
        if source["stale"] is not False or source["state"] not in ("available", "refreshing"):
            reject("cache_stale")
        orders = model["canonical_orders"]
        if (model["order_generation"] != generation or orders["canonical_version"] != 2
                or orders["complete"] is not True or not isinstance(orders["orders"], list)
                or orders["source"]["complete"] is not True):
            reject("cache_invalid")
        observed = timestamp(model["field_observed_at"]["active_production"])
        if observed != timestamp(source["last_success"]):
            reject("cache_invalid")
        for field in ("active_workorders_total", "native_progress_covered"):
            if type(orders[field]) is not int or orders[field] < 0:
                reject("cache_invalid")
        if orders["native_progress_covered"] > orders["active_workorders_total"]:
            reject("cache_invalid")
        for field in ("native_mean_order_progress_percent", "native_progress_coverage_percent"):
            value = orders[field]
            if value is not None and (type(value) not in (int, float)
                    or not math.isfinite(value) or not 0 <= value <= 100):
                reject("cache_invalid")
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        reject("cache_invalid")
    result = dict(ok=True, active_workorders_total=orders["active_workorders_total"],
                active_workorders_with_native_progress=orders["native_progress_covered"],
                native_active_production_progress_percent=orders["native_mean_order_progress_percent"],
                native_progress_coverage_percent=orders["native_progress_coverage_percent"],
                native_progress_conflict_count=None,
                native_active_production_progress_source=orders["source"],
                dashboard_orders=orders, sources=model["sources"],
                cache=dict(url=CACHE_URL, last_success=source["last_success"],
                           age_seconds=age, freshness_seconds=freshness_seconds(now)))
    completed = model.get("completed_today")
    # Optional snapshot data: omit invalid counts without deriving replacements.
    if (isinstance(completed, dict)
            and set(completed) == {"total", "breakdown_by_workstation"}
            and type(completed["total"]) is int and completed["total"] >= 0
            and isinstance(completed["breakdown_by_workstation"], dict)
            and all(isinstance(station, str) and type(count) is int and count >= 0
                    for station, count in completed["breakdown_by_workstation"].items())):
        result["completed_today"] = completed
    return result


async def call_cached_production(server, tool, arguments):
    if (server, tool) != ("Epoptia_MES", "production_overview"):
        return await call_existing(server, tool, arguments)
    from httpx2 import AsyncClient
    try:
        async with asyncio.timeout(CACHE_TIMEOUT_SECONDS), AsyncClient(
                trust_env=False, follow_redirects=False, timeout=CACHE_TIMEOUT_SECONDS) as client:
            async with client.stream("GET", CACHE_URL) as response:
                if response.status_code != 200:
                    reject("cache_unavailable")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_CACHE_BYTES:
                        reject("cache_invalid")
                import json
                try:
                    model = json.loads(content)
                except (ValueError, UnicodeError):
                    reject("cache_invalid")
        return cached_overview(model, datetime.now(timezone.utc))
    except UpstreamDiagnosticError:
        raise
    except Exception:
        reject("cache_unavailable")
