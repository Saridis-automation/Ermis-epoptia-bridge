"""Map an injected, already-read snapshot; never fetch or mutate Epoptia data."""

from datetime import datetime, timezone
import math
from dashboard.station_activity import STATION_CAPACITY_TARGETS, station_name, star_state
from dashboard.station_load import load_percent
from zoneinfo import ZoneInfo
ATHENS = ZoneInfo("Europe/Athens")
SNAPSHOT_FRESHNESS_SECONDS = 120


def number(value, *, percent=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    if percent and value > 100:
        return None
    if not percent and int(value) != value:
        return None
    return value


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp requires a timezone")
    return parsed.astimezone(timezone.utc)


def label(value):
    return str(value)[:160] if type(value) in (str, int) else "—"


def map_snapshot(snapshot, now):
    """Contract documented in README; missing metrics remain unknown.

    active_production is the existing active_production_progress output.
    Other fields require a verified provider, especially relative WIP and
    whole-order urgency. WOL lists are deliberately not accepted as orders.
    """
    observed = timestamp(snapshot["observed_at"])
    age = (now - observed).total_seconds()
    if age < -60:
        raise ValueError("Future snapshot")
    stations = []
    for row in (snapshot.get("workstations") or []):
        if not isinstance(row, dict) or station_name(row.get("name")) not in STATION_CAPACITY_TARGETS:
            continue
        pending = number(row.get("pending_steps"))
        target = number(row.get("capacity_target"))
        stations.append(dict(name=station_name(row.get("name")),
                             pending_steps=pending, capacity_target=target,
                             load_percent=None,
                             last_change_at=row.get('last_change_at'),
                             star_state=star_state(row.get('last_change_at'), now),
                             wip_steps=number(row.get("wip_steps")),
                             priority=None,
                             **{key: number(row.get(key)) for key in ("running_steps", "paused_steps", "waiting_steps", "unknown_steps", "distinct_wols")},
                             **{key: row.get(key) for key in ("units", "capacity_missing_inputs", "executable_queue_steps", "queue_reason", "waiting_scope", "status_counts")}))
    orders, seen = [], {}
    # Provider supplies whole orders in authoritative urgency order.
    for row in (snapshot.get("urgent_orders") or []):
        if row.get("entity_type") != "workorder":
            raise ValueError("Expected whole workorder")
        identity = row.get("id")
        if type(identity) not in (str, int) or not str(identity):
            raise ValueError("Missing order identity")
        if str(identity) in seen:
            if seen[str(identity)] != row:
                raise ValueError("Conflicting whole order")
            continue
        seen[str(identity)] = row
        deadline = row.get("deadline")
        if deadline is not None:
            deadline = datetime.strptime(deadline, "%Y-%m-%d").date().isoformat()
        orders.append(dict(id=label(identity), code=label(row.get("code")), customer=label(row.get("customer")),
                           completion_percent=number(row.get("completion_percent"), percent=True),
                           deadline=deadline))
        orders[-1].update(order_id=label(identity), client=orders[-1]['customer'],
                          native_progress=orders[-1]['completion_percent'],
                          issues=[issue for issue in row.get('issues', []) if issue in (
                              'conflicting_deadlines', 'missing_deadline', 'conflicting_customers',
                              'missing_customer', 'unverified_native_progress')])
    active = snapshot.get("active_production") or {}
    complete = (active.get("native_active_production_progress_source") or {}).get("complete") is True
    today = snapshot.get("today") or {}
    statuses = dict(snapshot.get('field_status') or {})
    observations = dict(snapshot.get('field_observed_at') or {})
    for key, value in [('urgent_orders', snapshot.get('urgent_orders')),
                       ('overdue_work', number(today.get('overdue_work'))),
                       ('completed_today', number(today.get('completed_today')))]:
        statuses.setdefault(key, 'available' if value is not None else 'unavailable')
        if value is not None and key in observations:
            at = timestamp(observations[key])
            if (now - at).total_seconds() > SNAPSHOT_FRESHNESS_SECONDS or (key in ('overdue_work', 'completed_today')
                                                   and at.astimezone(ATHENS).date() != now.astimezone(ATHENS).date()):
                statuses[key] = 'stale'
    for key in ('active_production', 'workstations'):
        if key in observations and (now - timestamp(observations[key])).total_seconds() > SNAPSHOT_FRESHNESS_SECONDS:
            statuses[key] = 'stale'
    # A relative denominator needs every visible count and a successful census.
    station_source = (snapshot.get('sources') or {}).get('workstation_wip', {})
    reliable_stations = (
        statuses.get('workstations') == 'available'
        and (snapshot.get('station_coverage') or {}).get('available') is True
        and station_source.get('state', 'available') == 'available'
        and not station_source.get('stale')
        and not station_source.get('failure_reason')
        and all(row['pending_steps'] is not None for row in stations)
    )
    busiest = max((row['pending_steps'] for row in stations), default=0) if reliable_stations else None
    for row in stations:
        row['load_percent'] = load_percent(row['pending_steps'], busiest)
    completion = snapshot.get('completed_today') or {}
    tracker = completion.get('tracker') or {}
    # Keep the count contract compact: gateway consumers validate these exact
    # two keys. Diagnostics belong beside it, including on loading/error paths.
    tracker_diagnostics = dict(
        source=tracker.get('source', 'shared_routing_parser_rest_scan'),
        status=tracker.get('status', 'pending'),
        baseline_exists=tracker.get('baseline_initialized', False),
        tracked_step_count=tracker.get('tracked_steps', 0),
        last_refresh_new_events=tracker.get('last_scan_new_events', 0),
        identity_version=tracker.get('identity_version'),
        recovery_status=tracker.get('recovery_status', 'awaiting_baseline'))
    return dict(schema_version=1, observed_at=observed.isoformat(),
                data_status=snapshot.get("data_status") or ("loading" if snapshot.get("loading") else "offline" if snapshot.get("offline") else "stale" if age > SNAPSHOT_FRESHNESS_SECONDS else "partial" if snapshot.get("partial") else "live"),
                overall_progress_percent=None,
                native_mean_order_progress_percent=number(active.get("native_active_production_progress_percent"), percent=True) if complete else None,
                capacity_missing_inputs=["standard_time_per_step", "remaining_quantity", "available_station_time", "capacity_horizon"],
                calendar_target_dates=snapshot.get("calendar_target_dates"),
                canonical_orders=snapshot.get("canonical_orders"), order_generation=snapshot.get("order_generation"),
                sources=snapshot.get("sources", {}), completion_coverage=snapshot.get("completion_coverage"),
                station_coverage=snapshot.get("station_coverage"),
                completed_today=dict(total=completion.get('total', 0),
                                     breakdown_by_workstation=completion.get('breakdown_by_workstation', {})),
                tracker_diagnostics=tracker_diagnostics,
                completion_tracker=snapshot.get('completion_tracker', dict(state='pending', reason='awaiting_verified_snapshot')),
                native_progress_coverage_percent=number(active.get("native_progress_coverage_percent"), percent=True) if complete else None,
                urgent_orders_status=statuses["urgent_orders"],
                field_status=statuses, field_observed_at=observations,
                workstations=stations, urgent_orders=orders[:5],
                production_wols=dict(total=None, status='unavailable',
                    excluded_workflow='Παραγγελία εμπορίου',
                    reason='exact_wol_workflow_name_field_unverified',
                    reader='epoptia_read.fetch_wols',
                    missing_field='verified exact WOL workflow-name path in /api/3.03/workorderlines'),
                today=dict(production_wols=None, active_work=number(active.get("active_workorders_total")) if complete else None,
                           overdue_work=number(today.get("overdue_work")),
                           completed_today=number(today.get("completed_today"))))
