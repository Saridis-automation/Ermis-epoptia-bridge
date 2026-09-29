"""Dashboard refresh cadence in production hours (Europe/Athens).

Monday-Friday 07:00-17:00 the dashboard refreshes every 5 minutes, otherwise
every 30 minutes. An off-hours wait never runs past the start of the next
production window, so the first refresh of the day starts at 07:00.
"""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ATHENS = ZoneInfo("Europe/Athens")
WORK_DAYS = frozenset(range(5))  # Monday=0 .. Friday=4
WORK_START = time(7, 0)
WORK_END = time(17, 0)
WORK_INTERVAL = 5 * 60
OFF_INTERVAL = 30 * 60
# Headroom for a full throttled scan (~2-3 minutes) before data counts as stale.
FRESHNESS_MARGIN = 10 * 60
MAX_FRESHNESS_SECONDS = OFF_INTERVAL + FRESHNESS_MARGIN


def _local(now):
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ATHENS)


def in_work_hours(now):
    local = _local(now)
    return local.weekday() in WORK_DAYS and WORK_START <= local.time() < WORK_END


def _next_window_start(now):
    local = _local(now)
    for days in range(8):
        day = (local + timedelta(days=days)).date()
        start = datetime.combine(day, WORK_START, ATHENS)
        if day.weekday() in WORK_DAYS and start > local:
            return start
    raise AssertionError("no production window within a week")


def refresh_delay(now):
    """Seconds to wait after a completed refresh before the next one."""
    if in_work_hours(now):
        return WORK_INTERVAL
    until_window = (_next_window_start(now) - _local(now)).total_seconds()
    return max(0.0, min(OFF_INTERVAL, until_window))


def freshness_seconds(now):
    """Maximum snapshot age that still counts as current at ``now``."""
    local = _local(now)
    if in_work_hours(now):
        window_start = datetime.combine(local.date(), WORK_START, ATHENS)
        # The first refresh of the day may still carry the last off-hours scan.
        if (local - window_start).total_seconds() < FRESHNESS_MARGIN:
            return MAX_FRESHNESS_SECONDS
        return WORK_INTERVAL + FRESHNESS_MARGIN
    return MAX_FRESHNESS_SECONDS
