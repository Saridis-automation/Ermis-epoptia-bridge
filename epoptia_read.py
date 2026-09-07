"""Read-only WOL queries. No credential loading or raw payload returns."""
from collections import Counter
from datetime import date, datetime, timezone
import math

import requests

from wol_details import technical_details, summary_fields, wol_details


class ReadError(Exception):
    pass


def fetch_wols(base_url, headers):
    """Read every page; fail closed rather than report partial aggregate totals."""
    def page(number):
        try:
            response = requests.get(
                f'{base_url}/api/3.03/workorderlines', headers=headers,
                params={'page': number, 'limit': 100}, timeout=20,
                allow_redirects=False)
            response.raise_for_status()
            if 300 <= response.status_code < 400:
                raise ReadError('Epoptia read failed')
            data = response.json()
        except (requests.RequestException, ValueError):
            raise ReadError('Epoptia read failed') from None
        if not isinstance(data, dict) or not isinstance(data.get('workorderLines'), list):
            raise ReadError('Invalid Epoptia page')
        return data

    first = page(1)
    pages = first.get('numberOfPages')
    if type(pages) is not int or not 0 <= pages <= 10000:
        raise ReadError('Invalid Epoptia pagination')
    rows = list(first['workorderLines'])
    if pages == 0 and rows:
        raise ReadError('Invalid Epoptia pagination')
    for number in range(2, pages + 1):
        data = page(number)
        if 'numberOfPages' in data and (
                type(data['numberOfPages']) is not int or data['numberOfPages'] != pages):
            raise ReadError('Invalid Epoptia pagination')
        rows.extend(data['workorderLines'])
    return [row for row in rows if isinstance(row, dict)]


def scalar(value):
    return value if isinstance(value, (str, int, float)) and not isinstance(value, bool) and (not isinstance(value, float) or math.isfinite(value)) else None


def day(value):
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value.replace('Z', '+00:00')).date()
    except ValueError:
        return None


def boundary(value):
    if value is None:
        return None
    if (not isinstance(value, str) or len(value) != 10 or day(value) is None
            or day(value).isoformat() != value):
        raise ValueError('Dates must use YYYY-MM-DD')
    return day(value)


def check_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('limit must be between 1 and 200')


def routing_progress(routing):
    """Completed-step progress only; lifecycle and quantities confer no credit.

    Paused and active counts are disjoint. Unknown statuses (including malformed
    entries) remain in the denominator and earn no credit. No steps means unknown.
    """
    steps = routing if isinstance(routing, list) else []
    counts = Counter(str(step.get('status')).casefold() if isinstance(step, dict)
                     else 'unknown' for step in steps)
    completed = counts['completed']
    active = counts['started'] + counts['in_progress']
    paused = counts['paused']
    not_started = counts['not_started']
    return {'total_steps': len(steps), 'completed_steps': completed,
            'active_steps': active, 'paused_steps': paused,
            'not_started_steps': not_started,
            'unknown_steps': len(steps) - completed - active - paused - not_started,
            'routing_completion_percent': round(100 * completed / len(steps), 2) if steps else None}


def aggregate_progress(items):
    """Aggregate all matches, weighted by supplied steps, before truncation."""
    keys = ('total_steps', 'completed_steps', 'active_steps', 'paused_steps',
            'not_started_steps', 'unknown_steps')
    totals = {key: sum(item['progress'][key] for item in items) for key in keys}
    totals['routing_completion_percent'] = (
        round(100 * totals['completed_steps'] / totals['total_steps'], 2)
        if totals['total_steps'] else None)
    return {'total_wols': len(items),
            'counts_by_status': dict(Counter(str(item['production_status'])
                if item['production_status'] is not None else 'unknown' for item in items)),
            'unknown_routing_wols': sum(item['progress']['total_steps'] == 0 for item in items),
            'progress': totals}


def summary(row):
    client = row.get('client')
    return {
        'workorderline_id': scalar(row.get('workorderline_id')),
        'description': scalar(row.get('description')),
        'client': scalar(client.get('name')) if isinstance(client, dict) else None,
        'production_status': scalar(row.get('production_status')),
        'state': scalar(row.get('state')),
        'progress': routing_progress(row.get('erp_routing')),
        'quantity': scalar(row.get('quantity')),
        'target_day': day(row.get('target_day')).isoformat() if day(row.get('target_day')) else None,
    }


def matches(value, query, exact=False):
    if query is None:
        return True
    if value is None:
        return False
    value, query = str(value).casefold(), str(query).casefold()
    return value == query if exact else query in value


def filtered(rows, wol_id=None, client=None, product_text=None, status=None,
             state=None, target_from=None, target_to=None):
    start, end = boundary(target_from), boundary(target_to)
    if start and end and start > end:
        raise ValueError('target_from must not follow target_to')
    result = []
    for row in rows:
        item = summary(row)
        target = day(item['target_day'])
        if ((start or end) and target is None) or (start and target < start) or (end and target > end):
            continue
        if all((matches(item['workorderline_id'], wol_id, True),
                matches(item['client'], client), matches(item['description'], product_text),
                matches(item['production_status'], status, True), matches(item['state'], state, True))):
            result.append(item)
    return result


def limited(items, limit):
    check_limit(limit)
    return {'total_matches': len(items), 'returned': min(len(items), limit),
            'truncated': len(items) > limit, 'items': items[:limit]}


def list_wols(rows, limit=50, **filters):
    items = filtered(rows, **filters)
    result = {**limited(items, limit), 'aggregate': aggregate_progress(items)}
    # Preserve filtering and aggregation semantics; enrich only returned rows.
    matches_in_order = (row for row in rows if filtered([row], **filters))
    for item, row in zip(result['items'], matches_in_order):
        item.update(summary_fields(row))
    return result


def overview(rows):
    items = filtered(rows)
    quantities = [item['quantity'] for item in items if type(item['quantity']) in (int, float)]
    today = datetime.now(timezone.utc).date()
    return {'total_wols': len(items),
            'counts_by_status': dict(Counter(str(item['production_status']) if item['production_status'] is not None else 'unknown' for item in items)),
            'counts_by_state': dict(Counter(str(item['state']) if item['state'] is not None else 'unknown' for item in items)),
            'total_quantity': sum(quantities), 'quantity_known_wols': len(quantities),
            'dated_wols': sum(item['target_day'] is not None for item in items),
            'past_target_wols': sum(day(item['target_day']) < today for item in items if item['target_day'])}


def due_wols(rows, mode='due_soon', days=7, as_of=None, target_from=None, target_to=None, limit=50):
    if mode not in ('due_soon', 'overdue'):
        raise ValueError('mode must be due_soon or overdue')
    if type(days) is not int or not 0 <= days <= 3650:
        raise ValueError('days must be between 0 and 3650')
    today = boundary(as_of) or datetime.now(timezone.utc).date()
    items = filtered(rows, target_from=target_from, target_to=target_to)
    result = []
    for item in items:
        target = day(item['target_day'])
        if target is None or str(item['production_status']).casefold() in ('completed', 'cancelled', 'canceled'):
            continue
        delta = (target - today).days
        if (mode == 'overdue' and delta < 0) or (mode == 'due_soon' and 0 <= delta <= days):
            result.append(item)
    result.sort(key=lambda item: (item['target_day'], str(item['workorderline_id'])))
    return {'as_of': today.isoformat(), 'mode': mode, **limited(result, limit)}


def workstation_wip(rows, workstation=None, step=None, limit=50):
    items = []
    for row in rows:
        routing = row.get('erp_routing')
        if not isinstance(routing, list):
            continue
        for entry in routing:
            if not isinstance(entry, dict):
                continue
            status = scalar(entry.get('status'))
            if str(status).casefold() not in ('started', 'paused', 'in_progress'):
                continue
            station = scalar(entry.get('workstationName'))
            tag = entry.get('job_tag')
            job = scalar(tag.get('name')) if isinstance(tag, dict) else None
            if matches(station, workstation) and matches(job, step):
                items.append({**summary(row), 'workstation': station, 'step': job,
                              'step_status': status, 'qty_done': scalar(entry.get('qty_done'))})
    result = limited(items, limit)
    result['counts_by_workstation'] = dict(Counter(str(item['workstation']) if item['workstation'] is not None else 'unknown' for item in items))
    return result
