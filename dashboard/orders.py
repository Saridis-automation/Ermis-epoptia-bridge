"""Canonical whole orders from the shared read-only capacity-planning scan.

Linked WOL target_day values roll forward to the next non-past order deadline.
"""
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo
from epoptia_read import native_workorder_progress_value, _scan_production_pages

ATHENS = ZoneInfo('Europe/Athens')
TERMINAL = {'archive', 'archived', 'completed', 'cancelled', 'canceled'}
ACTIVE = {'production', 'standby', 'started', 'in_progress', 'paused', 'not_started', 'waiting', 'queued'}
SOURCE = '/capacity-planning/workorderlines'
DEADLINE_PATH = 'target_day'
DEADLINE_EVIDENCE = ('User-verified business rule: select the earliest linked WOL target date '
                     'on or after today in Europe/Athens, or the latest date if all are past')


def identity(value):
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,9}', value):
        value = int(value)
    return value if type(value) is int and 1 <= value <= 2147483647 else None


def linked_wol_deadline(row):
    """Only the nested parent ID links a WOL target date to an order.

    Customer, order code, outer date buckets and flat ID hints are not links.
    Keep missing/invalid dates as None for the caller's coverage handling.
    """
    parent = row.get('workorder')
    key = identity(parent.get('id')) if isinstance(parent, dict) else None
    return key, deadline_date(row.get('target_day'))


def deadline_date(value):
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return date.fromisoformat(value)
        at = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return at.astimezone(ATHENS).date() if at.tzinfo else None
    except (ValueError, OverflowError):
        return None


class OrderCensus:
    def __init__(self, deadline_path=None, deadline_evidence=None):
        # An injected verified parent field, never guessed from a field name.
        if deadline_path and (not deadline_path.startswith('workorder.') or not deadline_evidence):
            raise ValueError('Verified parent deadline provenance required')
        self.deadline_path = deadline_path or DEADLINE_PATH
        self.deadline_evidence = deadline_evidence or DEADLINE_EVIDENCE
        self.deadline_scan = dict(complete=True, endpoint=SOURCE,
                                 method='POST', path=DEADLINE_PATH, pages_read=0, status='ok')
        self.deadlines = None
        self.orders = {}
        self.valid = True
        self.rows = 0

    def consume(self, rows):
        self.rows += len(rows)
        if self.rows > 100000:
            self.valid = False
            return
        for row in rows:
            if not isinstance(row, dict):
                self.valid = False
                continue
            parent = row.get('workorder')
            key, target_date = linked_wol_deadline(row)
            if key is None:
                self.valid = False
                continue
            item = self.orders.setdefault(key, dict(progress=set(), dates=set(), customers=set(),
                codes=set(), customer_paths=set(), statuses=set(), children=set()))
            item['progress'].add(native_workorder_progress_value(parent.get('progress')))
            item['statuses'].add(str(parent.get('status') or '').casefold())
            item['children'].add(str(row.get('production_status') or row.get('status') or '').casefold())
            code = parent.get('code')
            item['codes'].add(code.strip() if isinstance(code, str) and code.strip() else None)
            item['customer_paths'].add('workorder.client.name' if 'client' in parent else 'client.name')
            client = parent.get('client', row.get('client'))
            client = client.get('name') if isinstance(client, dict) else None
            item['customers'].add(client.strip() if isinstance(client, str) and client.strip() else None)
            if self.deadline_path == DEADLINE_PATH:
                item['dates'].add(target_date)
            else:
                value = row
                for part in self.deadline_path.split('.'):
                    value = value.get(part) if isinstance(value, dict) else None
                item['dates'].add(deadline_date(value))

    def consume_deadlines(self, report):
        self.deadline_scan = report['source']
        self.deadlines = report['dates']

    def result(self, complete, today=None):
        complete = bool(complete and self.valid)
        today = today or datetime.now(ATHENS).date()
        if isinstance(today, datetime):
            today = today.astimezone(ATHENS).date()
        orders = []
        for key, item in sorted(self.orders.items()):
            def unique(field):
                values = item[field]
                return next(iter(values)) if len(values) == 1 and None not in values else None
            progress = unique('progress') if complete else None
            statuses = item['statuses'] - {''}
            parent_terminal = statuses and statuses <= TERMINAL
            conflict = bool(statuses & TERMINAL and statuses - TERMINAL)
            children = item['children']
            if conflict:
                lifecycle = 'unknown'
            elif parent_terminal or progress == 100 or children and children <= TERMINAL:
                lifecycle = 'terminal'
            elif statuses & ACTIVE or children & ACTIVE:
                lifecycle = 'unfinished'
            else:
                lifecycle = 'unknown'
            deadline_values = (self.deadlines.get(key, set())
                               if self.deadline_path == DEADLINE_PATH and self.deadlines is not None else item['dates'])
            dates = deadline_values - {None}
            due = (next(iter(dates)) if len(dates) == 1 else None) if complete else None
            if complete and dates and self.deadline_path == DEADLINE_PATH:
                ordered_dates = sorted(dates)
                due = next((value for value in ordered_dates if value >= today), ordered_dates[-1])
            missing = not dates if self.deadline_path == DEADLINE_PATH else None in item['dates']
            reason = ('incomplete_scan' if (
                      self.deadline_path == DEADLINE_PATH and not self.deadline_scan['complete']) else
                      'incomplete_order_scan' if not complete else
                      'conflicting_deadlines' if self.deadline_path != DEADLINE_PATH and len(dates) > 1 else
                      'missing_or_invalid_deadline' if missing else
                      'parent_not_unfinished' if lifecycle != 'unfinished' and self.deadline_path != DEADLINE_PATH else None)
            if reason:
                due = None
            issues = [r for r in (reason, 'unverified_native_progress' if progress is None else None,
                       'unknown_lifecycle' if lifecycle == 'unknown' else None) if r]
            orders.append(dict(entity_type='workorder', id=key, order_id=key, code=unique('codes'),
                customer=unique('customers'), client=unique('customers'), lifecycle=lifecycle,
                native_progress=progress, completion_percent=progress,
                code_reason=None if unique('codes') is not None else 'missing_or_conflicting_order_code',
                customer_reason=None if unique('customers') is not None else 'missing_or_conflicting_customer',
                code_provenance=dict(endpoint=SOURCE, path='workorder.code'),
                customer_provenance=dict(endpoint=SOURCE, paths=sorted(item['customer_paths']),
                                         verified=unique('customers') is not None),
                progress_reason=None if progress is not None else 'incomplete_scan' if not complete else 'missing_invalid_or_conflicting_native_progress',
                progress_provenance=dict(endpoint=SOURCE, method='POST', path='workorder.progress'),
                deadline=due.isoformat() if due else None, deadline_reason=reason,
                deadline_provenance=dict(endpoint=self.deadline_scan['endpoint'] if self.deadline_path == DEADLINE_PATH else SOURCE,
                                         method=self.deadline_scan['method'] if self.deadline_path == DEADLINE_PATH else 'POST',
                                         derivation='derived_from_rollforward_wol_dates' if self.deadline_path == DEADLINE_PATH else None,
                                         path=self.deadline_scan.get('path', DEADLINE_PATH)
                                         if self.deadline_path == DEADLINE_PATH else self.deadline_path,
                                         scan_reader=self.deadline_scan.get('reader'),
                                         evidence=self.deadline_evidence, verified=due is not None), issues=issues))
        active = [row for row in orders if row['lifecycle'] == 'unfinished']
        dated = sorted((row for row in active if row['deadline']), key=lambda row: (row['deadline'], row['id']))
        covered = [row['native_progress'] for row in active if row['native_progress'] is not None]
        return dict(canonical_version=2, complete=complete, orders=orders,
            deadline_scan=dict(self.deadline_scan),
            urgent_orders=dated[:5] if complete else None,
            overdue_work=(sum(row['deadline'] < today.isoformat() for row in dated)
                          if complete and len(dated) == len(active)
                          and all(row['lifecycle'] != 'unknown' for row in orders) else None),
            overdue_known_count=sum(row['deadline'] < today.isoformat() for row in dated) if complete else None,
            deadline_coverage=dict(complete=complete and len(dated) == len(active)
                                  and all(row['lifecycle'] != 'unknown' for row in orders),
                dated_unfinished_orders=len(dated), unfinished_orders=len(active),
                urgency_scope='verified_dated_unfinished_whole_orders',
                as_of=today.isoformat(), timezone='Europe/Athens'),
            undated_unfinished_orders=sum(row['deadline'] is None for row in active) if complete else None,
            unknown_lifecycle_orders=sum(row['lifecycle'] == 'unknown' for row in orders) if complete else None,
            active_workorders_total=len(active) if complete else None,
            native_progress_covered=len(covered) if complete else None,
            native_mean_order_progress_percent=sum(covered)/len(covered) if complete and covered else None,
            native_progress_coverage_percent=100*len(covered)/len(active) if complete and active else None)


def collect_orders(base_url, session, *, deadline_path=None, deadline_evidence=None):
    """Reuse authenticated caller session; never load configuration or credentials.

    Shared scanner POST is a read. Per-page timeout and consistency checks are
    inherited. The provider additionally bounds the async source read.
    """
    census = OrderCensus(deadline_path, deadline_evidence)
    source = dict(complete=False, pages_read=0, status='unavailable')
    complete = _scan_production_pages(base_url, session, source, census.consume)
    census.deadline_scan.update(source)
    return dict(census.result(complete), scan=source)
