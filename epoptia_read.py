"""Read-only WOL queries. No credential loading or raw payload returns."""
from collections import Counter
from datetime import date, datetime, timezone
import math
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

import requests
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

from wol_technical import technical_details, summary_fields


def wol_details(rows, wol_id):
    """Load the details enhancement only when that query is requested.

    Import failures remain visible to the caller without disabling core reads.
    """
    from wol_details import wol_details as project_details
    return project_details(rows, wol_id)


class ReadError(Exception):
    pass


_read_scope = ContextVar('epoptia_read_scope', default=None)


@contextmanager
def bounded_read(seconds, cancelled, on_request):
    """Optional dashboard budget; legacy tool calls keep their timeouts."""
    token = _read_scope.set((monotonic() + seconds, cancelled, on_request))
    try:
        yield
    finally:
        _read_scope.reset(token)


def _request_timeout():
    scope = _read_scope.get()
    if scope is None:
        return 20
    deadline, cancelled, on_request = scope
    remaining = deadline - monotonic()
    if cancelled.is_set() or remaining <= 0:
        raise requests.Timeout('Read budget exhausted')
    on_request()
    return min(20, remaining)


def _workorderlines_page(base_url, page, *, headers=None, session=None):
    """Shared WOL transport; retain raw envelopes before normalization."""
    client = session if session is not None else requests
    options = {} if headers is None else {'headers': headers}
    return client.get(base_url.rstrip('/') + WORKORDERLINES_ENDPOINT,
                      params={'page': page, 'limit': 100}, timeout=_request_timeout(),
                      allow_redirects=False, **options)


def fetch_wols(base_url, headers, *, scan=None, deadlines=None):
    """Read every page; optional scan metadata enables strict dashboard validation.

    Optional deadlines collect bounded parent bucket dates from these exact pages.
    Consumers must check scan completeness before accepting any candidates.
    Legacy callers retain their existing record filtering and return shape.
    """
    if scan is not None:
        scan.update(complete=False, pages_read=0, rows_read=0,
                    endpoint="/api/3.03/workorderlines", method="GET", status="read_unavailable")
    seen_pages = set()
    deadline_valid = True
    if deadlines is not None:
        deadlines.clear()

    def page(number):
        nonlocal deadline_valid
        if scan is not None:
            scan['status'] = 'read_unavailable'
        try:
            response = _workorderlines_page(base_url, number, headers=headers)
            response.raise_for_status()
            if 300 <= response.status_code < 400:
                raise ReadError('Epoptia read failed')
            data = response.json()
        except requests.Timeout:
            if scan is not None:
                scan['status'] = 'upstream_timeout'
            raise ReadError('Epoptia read failed') from None
        except (requests.RequestException, ValueError):
            raise ReadError('Epoptia read failed') from None
        if not isinstance(data, dict) or not isinstance(data.get('workorderLines'), list):
            if scan is not None:
                scan['status'] = 'invalid_response'
            raise ReadError('Invalid Epoptia page')
        if scan is not None:
            scan['pages_read'] += 1
            rows = data['workorderLines']
            scan['rows_read'] += len(rows)
            if any(not isinstance(row, dict) for row in rows):
                scan['status'] = 'invalid_response'
                raise ReadError('Invalid Epoptia page')
            signature = tuple(str(row.get('workorderline_id', row.get('id'))) for row in rows)
            if rows and signature in seen_pages:
                scan['status'] = 'repeated_page'
                raise ReadError('Invalid Epoptia pagination')
            seen_pages.add(signature)
        if deadlines is not None and deadline_valid:
            try:
                _capture_parent_deadlines(data, deadlines)
            except ReadError:
                # Grouping errors cannot redefine WOL pagination completeness.
                # Discard all date evidence, while station rows remain usable.
                deadlines.clear()
                deadline_valid = False
        return data

    first = page(1)
    pages = first.get('numberOfPages')
    if scan is not None:
        scan['status'] = 'invalid_pagination'
    if type(pages) is not int or not 0 <= pages <= 10000:
        raise ReadError('Invalid Epoptia pagination')
    rows = list(first['workorderLines'])
    if scan is not None and pages > 0 and not rows:
        raise ReadError('Invalid Epoptia pagination')
    if pages == 0 and rows:
        raise ReadError('Invalid Epoptia pagination')
    for number in range(2, pages + 1):
        data = page(number)
        if scan is not None:
            scan['status'] = 'invalid_pagination'
        if 'numberOfPages' in data and (
                type(data['numberOfPages']) is not int or data['numberOfPages'] != pages):
            raise ReadError('Invalid Epoptia pagination')
        if scan is not None and not data['workorderLines']:
            scan['status'] = 'invalid_pagination'
            raise ReadError('Invalid Epoptia pagination')
        rows.extend(data['workorderLines'])
    if scan is not None:
        scan.update(complete=True, status='ok')
    return [row for row in rows if isinstance(row, dict)]


WORKORDERLINES_ENDPOINT = '/api/3.03/workorderlines'


def _capture_parent_deadlines(envelope, candidates):
    """Bounded date evidence only; pagination belongs exclusively to fetch_wols.

    Never descend into WOL records. Two distinct dates already prove conflict.
    """
    pending = [envelope]
    visited = 0
    while pending:
        node = pending.pop()
        visited += 1
        if visited + len(pending) > 100000:
            raise ReadError('Deadline envelope limit')
        if isinstance(node, dict):
            if 'workorder' in node:
                continue
            groups = node.get('productionData')
            if isinstance(groups, dict):
                for key, rows in groups.items():
                    visited += 1
                    try:
                        due = (date.fromisoformat(key) if isinstance(key, str)
                               and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', key) else None)
                    except ValueError:
                        due = None
                    if not isinstance(rows, list):
                        raise ReadError('Invalid deadline bucket')
                    visited += len(rows)
                    if visited > 100000:
                        raise ReadError('Deadline envelope limit')
                    for row in rows:
                        parent = row.get('workorder') if isinstance(row, dict) else None
                        identity = parent.get('id') if isinstance(parent, dict) else None
                        if type(identity) is str and re.fullmatch(r'[1-9][0-9]{0,9}', identity):
                            identity = int(identity)
                        if type(identity) is int and 1 <= identity <= 2147483647 and due:
                            if identity not in candidates and len(candidates) >= 100000:
                                raise ReadError('Deadline candidate limit')
                            values = candidates.setdefault(identity, set())
                            if len(values) < 2:
                                values.add(due)
            pending.extend(value for key, value in node.items()
                           if key != 'productionData' and isinstance(value, (dict, list)))
        elif isinstance(node, list):
            pending.extend(node)


def _deadline_envelope(data):
    """Unwrap response containers, never descend into individual WOL records."""
    counts, path = [], []
    for _ in range(10):
        if not isinstance(data, dict) or 'workorder' in data:
            raise ValueError('Invalid grouped response')
        if 'numberOfPages' in data:
            counts.append(data['numberOfPages'])
        if 'productionData' in data:
            if not isinstance(data['productionData'], dict):
                raise ValueError('Invalid grouped response')
            if any(type(count) is not int or count != counts[0] for count in counts):
                raise ValueError('Conflicting pagination')
            return data['productionData'], counts, '.'.join(path + ['productionData', '<date>'])
        wrappers = [key for key in ('data', 'workorderLines')
                    if isinstance(data.get(key), dict)]
        if len(wrappers) != 1:
            raise ValueError('Missing or ambiguous grouped response')
        path.append(wrappers[0])
        data = data[wrappers[0]]
    raise ValueError('Grouped envelope limit')


def read_parent_deadlines(base_url, session=None, *, headers=None):
    """Read raw WOL envelopes with API headers or an explicit caller session."""
    source = dict(endpoint=WORKORDERLINES_ENDPOINT, method='GET',
                  path='productionData.<date>', complete=False, pages_read=0,
                  status='read_unavailable')
    candidates, seen = {}, set()
    pages, total = None, 0
    try:
        for page in range(1, 1001):
            response = _workorderlines_page(base_url, page, headers=headers, session=session)
            if response.status_code != 200:
                source['status'] = 'upstream_http_error'
                break
            groups, counts, path = _deadline_envelope(response.json())
            if source['pages_read'] and source['path'] != path:
                raise ValueError('Inconsistent grouped envelope')
            source['path'] = path
            count = counts[0] if counts else None
            if counts:
                if (type(count) is not int or not 0 <= count <= 1000
                        or pages is not None and pages != count or page > max(1, count)):
                    raise ValueError('Invalid pagination')
                pages = count
            if len(groups) > 100000:
                raise ValueError('Bucket limit')
            signature = []
            for key, rows in groups.items():
                if not isinstance(rows, list):
                    raise ValueError('Invalid bucket')
                total += len(rows)
                if total > 100000:
                    raise ValueError('Scan limit')
                try:
                    valid_date = boundary(key)
                except ValueError:
                    valid_date = None
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get('workorder'), dict):
                        raise ValueError('Invalid linked row')
                    identity = row['workorder'].get('id')
                    if type(identity) is str and re.fullmatch(r'[1-9][0-9]{0,9}', identity):
                        identity = int(identity)
                    if type(identity) is not int or not 1 <= identity <= 2147483647:
                        raise ValueError('Invalid parent identity')
                    candidates.setdefault(identity, set()).add(valid_date)
                    signature.append((key, identity, str(row.get('id', row.get('workorderline_id')))))
            source['pages_read'] += 1
            signature = tuple(sorted(signature))
            if (pages == 0 and signature or pages and not signature
                    or signature and signature in seen):
                raise ValueError('Invalid pagination')
            seen.add(signature)
            if not signature or pages is not None and page >= pages:
                source.update(complete=True, status='ok')
                break
        else:
            source['status'] = 'pagination_limit'
    except requests.Timeout:
        source['status'] = 'upstream_timeout'
    except requests.RequestException:
        source['status'] = 'read_unavailable'
    except (ValueError, TypeError):
        source['status'] = 'invalid_response'
    return dict(source=source, dates=candidates if source['complete'] else {})


class _LoginToken(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None
        self.has_password = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('type', '').lower() == 'password':
            self.has_password = True
        if (tag == 'input' and attrs.get('name') == '_token'
                and attrs.get('type', '').lower() == 'hidden'):
            self.token = attrs.get('value')


def _web_login(session, base_url, username, password):
    """Authenticate without exposing upstream bodies or credential diagnostics."""
    parts = urlsplit(base_url)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        return False
    login_url = base_url.rstrip('/') + '/login'
    session.headers.update({'Origin': f'{parts.scheme}://{parts.netloc}',
                            'Referer': login_url})
    response = session.get(login_url, timeout=20, allow_redirects=True)
    if response.status_code != 200:
        return False
    parser = _LoginToken()
    parser.feed(response.text)
    if not parser.token:
        return False
    response = session.post(login_url, data={
        '_token': parser.token, 'username': username, 'password': password,
    }, timeout=20, allow_redirects=True)
    final_url = urlsplit(response.url)
    parser = _LoginToken()
    parser.feed(response.text)
    if (response.status_code != 200 or not response.history
            or (final_url.scheme, final_url.netloc) != (parts.scheme, parts.netloc)
            or final_url.path.rstrip('/') == urlsplit(login_url).path.rstrip('/')
            or parser.has_password):
        return False
    session.headers.update({'Accept': 'application/json',
                            'Referer': base_url.rstrip('/') + '/capacity-planning'})
    return True


def inspect_workorder_progress(base_url, headers, workorder_id, *, username=None, password=None,
                               include_actual_production_completion=False):
    """Read progress and parent completion using one cookie-preserving session.

    include_actual_production_completion is retained for caller compatibility;
    the actual completion object is now always included, even when false.
    """
    if type(workorder_id) is not int or not 1 <= workorder_id <= 2147483647:
        raise ValueError('workorder_id must be an integer from 1 to 2147483647')
    if type(include_actual_production_completion) is not bool:
        raise ValueError('include_actual_production_completion must be a boolean')
    with requests.Session() as session:
        authenticated = False
        if username and password:
            try:
                authenticated = _web_login(session, base_url, username, password)
            except (requests.RequestException, ValueError):
                pass
        report = _inspect_workorder_progress(base_url, session, workorder_id, authenticated)
        parent_page = []
        report['actual_production_completion'] = _parent_actual_completion(
            base_url, session, workorder_id, authenticated, page_sink=parent_page)
        # TEMPORARY: target the verified report using the parent helper/session.
        try:
            from report_discovery import discover_workorder_reports, empty_result
            report['report_discovery'] = (
                discover_workorder_reports(base_url, session, html_get=_web_html_get)
                if parent_page else empty_result(
                    'page_unavailable' if authenticated else 'authentication_unavailable'))
        except Exception:
            report['report_discovery'] = dict(
                status='discovery_unavailable', reason='bounded_read_failed',
                landing_paths=[], route_candidates=[], endpoint_candidates=[], notes=[])
        from calendar_target_dates import refresh_calendar, order_targets
        calendar = refresh_calendar(base_url, session, authenticated=authenticated)
        report.update(order_targets(workorder_id, calendar))
        return report


def _web_html_get(session, url, *, timeout=None, stream=False):
    """Shared authenticated HTML GET for parent pages and temporary discovery."""
    options = dict(headers={'Accept': 'text/html'},
                   timeout=_request_timeout() if timeout is None else min(_request_timeout(), timeout),
                   allow_redirects=False)
    if stream:
        options['stream'] = True
    return session.get(url, **options)


def _parent_actual_completion(base_url, session, workorder_id, authenticated, *, page_sink=None):
    from actual_completion import completion_result, parse_actual_completion
    result = completion_result(workorder_id, 'login_failed')
    if not authenticated:
        return result
    try:
        response = _web_html_get(session, base_url.rstrip('/') + result['source_endpoint'])
        if response.status_code != 200:
            result['reason'] = 'upstream_http_error'
            return result
        html = response.text
        if page_sink is not None:
            page_sink.append(html)
    except requests.Timeout:
        result['reason'] = 'upstream_timeout'
    except (requests.RequestException, ValueError):
        result['reason'] = 'read_unavailable'
    else:
        try:
            return parse_actual_completion(html, workorder_id)
        except Exception:
            # Parser failures must not discard progress or expose page/error text.
            result['reason'] = 'invalid_page'
    return result


def _production_workorder_lines(data):
    """Carry the outer productionData key without inspecting WOL date fields."""
    pending = [(data, None)]
    rows = []
    visited = 0
    while pending:
        node, production_date = pending.pop()
        visited += 1
        if visited > 100000:
            raise ValueError('Production scan limit exceeded')
        if isinstance(node, dict) and 'workorder' in node:
            rows.append(dict(node, _production_data_date=production_date))
        elif isinstance(node, (dict, list)):
            children = ([(value, key if production_date is None and
                         re.fullmatch(r'\d{4}-\d{2}-\d{2}', key) else production_date)
                         for key, value in node.items()] if isinstance(node, dict)
                        else [(value, production_date) for value in node])
            if visited + len(pending) + len(node) > 100000:
                raise ValueError('Production scan limit exceeded')
            pending.extend(children)
        else:
            raise ValueError('Invalid production bucket')
    return rows


def _production_page(data):
    """Normalize the JSON envelope before discarding wrappers for row callers.

    Locate productionData outside WOL records, including direct responses and
    wrapped/list containers. Keep date evidence attached to each copied row and
    retain pagination metadata from the envelope, never from nested workorders.
    """
    pending = [data]
    grouped_rows, flat_rows, counts = [], [], []
    grouped = flat = False
    visited = 0
    while pending:
        node = pending.pop()
        visited += 1
        if visited + len(pending) > 100000:
            raise ValueError('Production envelope scan limit exceeded')
        if isinstance(node, dict):
            if 'workorder' in node:
                continue
            if 'numberOfPages' in node:
                counts.append(node['numberOfPages'])
            if 'productionData' in node:
                grouped = True
                grouped_rows.extend(_production_workorder_lines(node['productionData']))
            if 'workorderLines' in node:
                rows = node['workorderLines']
                if isinstance(rows, dict):
                    # A response container can occupy the same key as flat
                    # rows. Inspect it before normalization loses date keys.
                    pending.append(rows)
                elif not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise ValueError('Invalid production rows')
                else:
                    flat = True
                    for row in rows:
                        if 'workorder' not in row and any(
                                isinstance(value, (dict, list)) for value in row.values()):
                            pending.append(row)
                        else:
                            flat_rows.append(row)
            pending.extend(value for key, value in node.items()
                           if key not in ('productionData', 'workorderLines')
                           and isinstance(value, (dict, list)))
        elif isinstance(node, list):
            pending.extend(node)
    if grouped:
        # Some envelopes also carry flat rows (including ungrouped orders).
        # Preserve them for progress/client consumers without inventing dates.
        return grouped_rows + [dict(row, _production_data_date=None)
                               for row in flat_rows], counts
    if isinstance(data, list) and all(isinstance(row, dict) for row in data):
        flat, flat_rows = True, data
    if not flat:
        raise ValueError('Invalid production envelope')
    # Flat payload fields cannot manufacture internal grouping provenance.
    return [dict(row, _production_data_date=None) for row in flat_rows], counts


def native_workorder_progress_value(value):
    """Validate the native parent progress field without deriving WOL progress."""
    if type(value) is str and re.fullmatch(r'\d{1,3}(?:\.\d{1,8})?', value):
        value = float(value)
    return value if type(value) in (int, float) and 0 <= value <= 100 else None


WORKORDER_COMPLETION_FIELDS = (
    'completionDate', 'dbCompletionDate', 'displayCompletionDate',
    'completedAt', 'completed_at', 'completion_date',
    'actualCompletionDate', 'productionCompletionDate',
)


def _safe_completion_candidate(value):
    """Accept only empty values, bounded timestamps, and date-shaped text.

    Never pass arbitrary text or containers through a completion field. Keep
    accepted values unchanged; these are candidates, not verified completion.
    """
    if value is None or type(value) is str and value == '':
        return True
    if type(value) in (int, float):
        return 0 <= value <= 253402300799999
    return type(value) is str and len(value) <= 64 and re.fullmatch(
        r'(?:[0-9]{4}[-/][0-9]{2}[-/][0-9]{2}|[0-9]{2}[-/][0-9]{2}[-/][0-9]{4})'
        r'(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,9})?)?'
        r'(?:Z|[+-][0-9]{2}:?[0-9]{2})?)?', value) is not None


ROUTING_COMPLETION_FIELDS = tuple(dict.fromkeys((
    *WORKORDER_COMPLETION_FIELDS,
    'completionTimestamp', 'completion_timestamp', 'completionTime', 'completion_time',
    'completedDate', 'completed_date', 'endDate', 'end_date', 'endTime', 'end_time',
    'endedAt', 'ended_at', 'finishedAt', 'finished_at', 'finishedDate', 'finished_date',
    'finishDate', 'finish_date', 'finishTime', 'finish_time',
    'updatedAt', 'updated_at', 'updateDate', 'update_date',
    'lastUpdated', 'last_updated', 'date', 'time', 'timestamp',
    'historyDate', 'history_date',
)))
ROUTING_COMPLETION_LIMIT = 32


def parse_routing_step(step):
    """Parse the routing fields exposed by get_wol_status without inferring state."""
    if not isinstance(step, dict):
        return None
    job_tag = step.get('job_tag')
    return {
        'workstation': step.get('workstationName'),
        'job': job_tag.get('name') if isinstance(job_tag, dict) else None,
        'status': step.get('status'),
        'qty_done': step.get('qty_done'),
    }


def routing_completion_details(step, index):
    """Bounded evidence for an already completed step; never infer completion.

    Paths are JSON pointers into the supplied WOL. Inspect only explicit fields
    on this step and its immediate execution/history dictionaries, never arrays,
    unrelated metadata, or deeper objects. Dates remain unverified raw values.
    Identifiers accept only bounded integers, decimal ID strings, or UUIDs, so
    arbitrary names/text cannot escape through an ID field.
    """
    if not isinstance(step, dict) or step.get('status') != 'completed':
        return {}
    prefix = f'/erp_routing/{index}/'
    fields = {}
    identifiers = {}
    records = [('', step)]
    for name in ('execution', 'history'):
        if isinstance(step.get(name), dict):
            records.append((name + '/', step[name]))
    for relative, record in records:
        for name in ROUTING_COMPLETION_FIELDS:
            if len(fields) >= ROUTING_COMPLETION_LIMIT:
                break
            if name in record and _safe_completion_candidate(record[name]):
                fields[prefix + relative + name] = record[name]
    id_records = [('', step)]
    if isinstance(step.get('job_tag'), dict):
        id_records.append(('job_tag/', step['job_tag']))
    for relative, record in id_records:
        for name in ('id', 'step_id', 'stepId', 'job_id', 'jobId', 'routing_id', 'routingId'):
            value = record.get(name)
            if (type(value) is int and 0 <= value <= 9223372036854775807
                    or type(value) is str and len(value) <= 36 and re.fullmatch(
                        r'(?:[0-9]{1,19}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})', value)):
                identifiers[prefix + relative + name] = value
    result = {'completion_candidates': fields}
    if identifiers:
        result['step_identifiers'] = identifiers
    return result


def _collect_completion_candidates(candidates, row, parent):
    """Compare exact values at each allowlisted, record-relative source path.

    Different paths may represent different dates and are never merged. Missing
    keys are not evidence; explicit null/empty values participate in conflicts.
    """
    for prefix, record in (('/', row), ('/workorder/', parent)):
        for name in WORKORDER_COMPLETION_FIELDS:
            if name not in record:
                continue
            value = record[name]
            if not _safe_completion_candidate(value):
                candidates['omitted_unsafe_values'] += 1
                continue
            path = prefix + name
            values = candidates['fields'].setdefault(path, [])
            if not any(type(value) is type(previous) and value == previous
                       for previous in values):
                values.append(value)
            if len(values) > 1:
                candidates['conflicts'][path] = list(values)


def _inspect_workorder_progress(base_url, session, workorder_id, authenticated):
    """Read nested native workorder progress; fail closed on incomplete scans."""
    if type(workorder_id) is not int or not 1 <= workorder_id <= 2147483647:
        raise ValueError('workorder_id must be an integer from 1 to 2147483647')
    endpoint = '/capacity-planning/workorderlines'
    source = dict(endpoint=endpoint, method='POST', path='/workorder/progress',
                  status='unavailable', complete=False, pages_read=0,
                  linked_wol_records=0, order_identity_verified=False)
    report = dict(workorder_id=workorder_id, native_progress=None,
                  native_progress_verified=False, sources=[source],
                  completion_candidates=dict(
                      source_endpoint=endpoint, scope='already_fetched_linked_records',
                      fields={}, conflicts={}, omitted_unsafe_values=0))
    if not authenticated:
        source['status'] = 'login_failed'
        return report
    values = set()
    invalid_progress = False

    def consume(rows):
        nonlocal invalid_progress
        for row in rows:
            parent = row.get("workorder")
            if not isinstance(parent, dict):
                continue
            identity = parent.get('id')
            if not (type(identity) is int and identity == workorder_id
                    or type(identity) is str and identity == str(workorder_id)):
                continue
            source['linked_wol_records'] += 1
            source['order_identity_verified'] = True
            _collect_completion_candidates(report['completion_candidates'], row, parent)
            value = native_workorder_progress_value(parent.get('progress'))
            if value is None:
                invalid_progress = True
            else:
                values.add(value)
        return source["order_identity_verified"]

    if not _scan_production_pages(base_url, session, source, consume):
        return report
    if len(values) > 1:
        source['status'] = 'conflicting_progress'
    elif invalid_progress:
        source['status'] = 'invalid_progress'
    elif not values:
        source['status'] = 'not_found'
    else:
        source['status'] = 'ok'
        report.update(native_progress=values.pop(), native_progress_verified=True)
    return report


def _scan_production_pages(base_url, session, source, consume):
    """Shared authenticated pagination; consumers may stop early."""
    endpoint = '/capacity-planning/workorderlines'
    pages = None
    seen_pages = set()
    for page in range(1, 1001):
        try:
            # Cookies can rotate between pages.
            session.headers.pop('X-XSRF-TOKEN', None)
            for cookie in session.cookies:
                if cookie.name == 'XSRF-TOKEN':
                    session.headers['X-XSRF-TOKEN'] = unquote(cookie.value)
            response = session.post(
                base_url.rstrip('/') + endpoint,
                json={'onlyList': True, 'page': page}, timeout=_request_timeout(),
                allow_redirects=False)
            source['http_status'] = response.status_code
            if response.status_code != 200:
                source['status'] = 'upstream_http_error'
                return False
            data = response.json()
        except requests.Timeout:
            source['status'] = 'upstream_timeout'
            return False
        except (requests.RequestException, ValueError):
            source['status'] = 'read_unavailable'
            return False
        try:
            rows, page_counts = _production_page(data)
        except ValueError:
            source['status'] = 'invalid_response'
            return False
        for count in page_counts:
            if (type(count) is not int or not 0 <= count <= 1000
                    or (pages is not None and count != pages)
                    or page > max(1, count) or (count == 0 and rows)):
                source['status'] = 'invalid_pagination'
                return False
            pages = count
        source['pages_read'] += 1
        if not rows:
            if pages is not None and pages > 0:
                source['status'] = 'invalid_pagination'
                return False
            break
        # Only fixed identity/progress fields enter the signature, never raw records.
        signature = []
        for row in rows:
            parent = row.get('workorder')
            if not isinstance(parent, dict):
                continue
            signature.append(tuple(scalar(value) for value in (
                row.get('id'), row.get('workorderline_id'),
                parent.get('id'), parent.get('progress'), row.get('_production_data_date'))))
        signature = tuple(signature)
        has_wol_ids = signature and all(item[0] is not None or item[1] is not None
                                        for item in signature)
        if has_wol_ids and signature in seen_pages:
            source['status'] = 'repeated_page'
            return False
        seen_pages.add(signature)
        if consume(rows) or pages is not None and page >= pages:
            break
    else:
        source['status'] = 'pagination_limit'
        return False
    source['complete'] = True
    return True


def active_production_progress(base_url, *, username=None, password=None, consume_rows=None, consume_deadlines=None):
    """Average distinct active orders after a complete authenticated scan.

    Invalid or conflicting duplicates exclude an order. Empty populations have
    zero coverage and no mean; incomplete scans have null aggregate values.
    """
    report = dict(active_workorders_total=None,
                  active_workorders_with_native_progress=None,
                  native_progress_coverage_percent=None,
                  native_active_production_progress_percent=None,
                  native_progress_conflict_count=None)
    source = dict(status='login_failed', complete=False, pages_read=0)
    orders = {}

    def consume(rows):
        if consume_rows is not None:
            consume_rows(rows)
        for row in rows:
            status = row.get('production_status')
            if status is None:
                status = row.get('status')
            if status not in ('production', 'standby'):
                continue
            parent = row.get('workorder')
            if not isinstance(parent, dict):
                continue
            identity = parent.get('id')
            if type(identity) is str and re.fullmatch(r'[1-9][0-9]{0,9}', identity):
                identity = int(identity)
            if type(identity) is not int or not 1 <= identity <= 2147483647:
                continue
            values, invalid = orders.setdefault(identity, (set(), False))
            value = native_workorder_progress_value(parent.get('progress'))
            if value is not None:
                values.add(value)
            else:
                invalid = True
            orders[identity] = values, invalid
        return False

    with requests.Session() as session:
        try:
            authenticated = bool(username and password and
                                 _web_login(session, base_url, username, password))
        except (requests.RequestException, ValueError):
            authenticated = False
        if authenticated:
            source['status'] = 'unavailable'
            if _scan_production_pages(base_url, session, source, consume):
                source['status'] = 'ok'
                values = [next(iter(values)) for values, invalid in orders.values()
                          if len(values) == 1 and not invalid]
                report.update(
                    active_workorders_total=len(orders),
                    active_workorders_with_native_progress=len(values),
                    native_progress_coverage_percent=100 * len(values) / len(orders) if orders else 0,
                    native_active_production_progress_percent=sum(values) / len(values) if values else None,
                    native_progress_conflict_count=sum(len(values) > 1 for values, _ in orders.values()))
            if consume_deadlines is not None:
                consume_deadlines(read_parent_deadlines(base_url, session))
        from calendar_target_dates import refresh_calendar
        report['calendar_target_dates'] = refresh_calendar(
            base_url, session, authenticated=authenticated)
    report['native_active_production_progress_source'] = source
    return report


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


def native_progress_metadata():
    return {'native_progress': None, 'native_progress_verified': False,
            'native_progress_verification': 'No verified per-WOL native progress mapping'}


def summary(row):
    client = row.get('client')
    return {
        **native_progress_metadata(),
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
