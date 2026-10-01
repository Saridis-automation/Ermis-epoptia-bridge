"""Current routing state census, using existing erp_routing status semantics."""
from collections import Counter
import hashlib
import json
from dashboard.station_activity import station_name
from dashboard.orders import ACTIVE, TERMINAL, deadline_date

ROUTING_ID_FIELDS = ('id', 'step_id', 'stepId', 'route_id', 'routeId', 'routing_id', 'routingId', 'job_id', 'jobId')

MISSING_CAPACITY = ['standard_time_per_step', 'remaining_quantity', 'available_station_time', 'capacity_horizon']


def collect_stations(rows, complete):
    seen, stations = {}, {}
    diagnostics = Counter()
    valid = bool(complete)
    for row in rows:
        key = row.get('workorderline_id', row.get('id'))
        if key is None:
            valid = False
            diagnostics['missing_wol_identity'] += 1
            continue
        key = str(key)
        if key in seen:
            diagnostics['duplicate_wols'] += 1
            if seen[key] != row:
                valid = False
                diagnostics['conflicting_wols'] += 1
            continue
        seen[key] = row
        # Match the lifecycle normalization used by completed_jobs.routing_from.
        # Filter only this projection; the shared scan still feeds history.
        lifecycle = str(row.get('production_status') or row.get('status') or '').strip().casefold()
        if lifecycle in TERMINAL:
            diagnostics['excluded_terminal_wols'] += 1
            continue
        if lifecycle not in ACTIVE:
            diagnostics['excluded_nonactive_wols'] += 1
            continue
        routing = row.get('erp_routing')
        if not isinstance(routing, list):
            diagnostics['missing_routing'] += 1
            valid = False
            continue
        step_ids = {}
        for step in routing:
            if not isinstance(step, dict):
                valid = False
                continue
            identity = next(((field, str(step[field])) for field in ROUTING_ID_FIELDS
                             if type(step.get(field)) in (str, int) and str(step[field])), None)
            if identity is not None:
                if identity in step_ids:
                    if step_ids[identity] != step:
                        valid = False
                    continue
                step_ids[identity] = step
            status = str(step.get('status') or '').strip().casefold()
            if status in TERMINAL:
                continue
            name = step.get('workstationName')
            if not isinstance(name, str) or not name:
                diagnostics['missing_station'] += 1
                valid = False
                continue
            name = station_name(name)
            station = stations.setdefault(name, dict(counts=Counter(), wols=set(), statuses=Counter(), state=[]))
            # Full normalized current-step signature captures qty_done and nested progress.
            relevant = dict(step, status=status, workstationName=name)
            work = {k: row[k] for k in ('quantity', 'qty_done', 'qty_total',
                'completed_quantity', 'remaining_quantity', 'progress') if k in row}
            station['state'].append(json.dumps([key, lifecycle, work, relevant], sort_keys=True, ensure_ascii=False))
            state = ('running' if status in ('started', 'in_progress') else 'paused' if status == 'paused'
                     else 'waiting' if status in ('not_started', 'waiting', 'future') else 'unknown')
            station['counts'][state] += 1
            station['statuses'][status if status in ('started','in_progress','paused','not_started','waiting','future') else 'unknown'] += 1
            station['wols'].add(key)
    result = []
    for name, item in sorted(stations.items()):
        counts = item['counts']
        deadlines = Counter()
        missing_dates = 0
        for key in item['wols']:
            deadline = deadline_date(seen[key].get('target_day'))
            if deadline is None:
                missing_dates += 1
            else:
                deadlines[deadline.isoformat()] += 1
        result.append(dict(name=name, load_percent=None, priority=None,
            pending_wol_deadlines=dict(deadlines) if valid else None,
            missing_target_day_wols=missing_dates if valid else None,
            pending_steps=(counts['running']+counts['paused']+counts['waiting']) if valid and not counts['unknown'] else None,
            state_fingerprint=hashlib.sha256(json.dumps(sorted(item['state'])).encode()).hexdigest() if valid else None,
            capacity_missing_inputs=MISSING_CAPACITY, units='routing_steps',
            wip_steps=counts['running']+counts['paused'] if valid else None,
            distinct_wols=len(item['wols']) if valid else None,
            **{state+'_steps': counts[state] if valid else None for state in ('running','paused','waiting','unknown')},
            executable_queue_steps=None, queue_reason='routing_dependencies_not_verified',
            waiting_scope='all_not_started_or_waiting_routing_steps_including_future',
            status_counts=dict(item['statuses'])))
    attach_load_model(result, list(seen.values()), valid)
    from dashboard.load_model import products_to_produce
    return dict(station_version=1, complete=valid, workstations=result,
        products_to_produce=products_to_produce(list(seen.values())) if valid else None,
        diagnostics=dict(diagnostics), coverage=dict(rows_read=len(rows),
            distinct_wols=len(seen), terminal_filtered=True,
            status_path='erp_routing.status', station_path='erp_routing.workstationName',
            lifecycle_path='production_status_or_status',
            commercial_flow_exclusion_verified=False,
            commercial_flow_missing_field='exact_wol_workflow_name_field_unverified',
            excluded_statuses=sorted(TERMINAL), executable_queue_verified=False))


def attach_load_model(result, rows, valid, today=None):
    """Weighted products vs capacity and deadlines (docs/dashboard_load_model.md).

    Only on a complete census; any failure leaves load_model None (shown as "—").
    """
    from dashboard.load_model import compute
    model = None
    if valid:
        try:
            if today is None:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                today = datetime.now(ZoneInfo('Europe/Athens')).date()
            model = compute(rows, today)['stations']
        except Exception:
            model = None
    for row in result:
        value = (model or {}).get(row['name'])
        row['load_model'] = None if value is None else dict(
            load_percent=value['load_percent'], products=value['products'],
            days_of_work=value['days_of_work'], capacity_per_day=value['capacity_per_day'],
            tightest=None if not value['tightest'] else dict(
                by=value['tightest']['by'], needed=value['tightest']['needed'],
                fits=value['tightest']['fits']))
