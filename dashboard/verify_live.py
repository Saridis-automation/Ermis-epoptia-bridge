"""Read-only local GET diagnostic; no configuration, refresh writes or service operations.

Run: ./venv/bin/python -m dashboard.verify_live --wait 600
A pass requires three observed NEW successful generations per source after baseline.
Synthetic unit tests are separate and never constitute live validation.
"""
import argparse
from datetime import date
import json
import re
from time import monotonic, sleep
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from dashboard.adapter import timestamp
from dashboard.provider import SOURCE_ERRORS

SOURCES = ('production_overview', 'workstation_wip')
SAFE_ERRORS = SOURCE_ERRORS | {'timeout', 'cancelled', 'unverified_or_incomplete_source'}


def integer(value):
    return value if type(value) is int and value >= 0 else None


def safe_time(value):
    try:
        return timestamp(value).isoformat()
    except (ValueError, TypeError, AttributeError):
        return None


def consistency(model):
    """Inspect identities locally; return only a boolean, never customer/order data."""
    try:
        table = model['canonical_orders']
        if table['canonical_version'] != 2 or table['complete'] is not True:
            return False
        rows = table['orders']
        ids = [row['id'] for row in rows]
        if any(type(key) is not int or key <= 0 for key in ids) or len(ids) != len(set(ids)):
            return False
        source = model['sources']['production_overview']
        if model['order_generation'] != source['generation']:
            return False
        if table['source']['complete'] is not True:
            return False
        active = [row for row in rows if row['lifecycle'] == 'unfinished']
        dated = sorted((row for row in active if row['deadline'] is not None),
                       key=lambda row: (row['deadline'], row['id']))
        for row in rows:
            if row['progress_provenance']['path'] != 'workorder.progress':
                return False
            provenance = row['deadline_provenance']
            if row['deadline'] is not None:
                date.fromisoformat(row['deadline'])
                if (provenance['verified'] is not True or not provenance['evidence']
                        or not (provenance['path'].startswith('workorder.') or (
                            provenance['endpoint'] in ('/api/3.03/workorderlines', '/capacity-planning/workorderlines')
                            and provenance['path'] == 'target_day'
                            and provenance.get('derivation') == 'derived_from_rollforward_wol_dates'
                            and table['deadline_scan']['complete'] is True))):
                    return False
            elif not row['deadline_reason']:
                return False
        coverage = table['deadline_coverage']
        complete = len(dated) == len(active) and all(row['lifecycle'] != 'unknown' for row in rows)
        if (coverage['complete'] != complete or coverage['dated_unfinished_orders'] != len(dated)
                or coverage['unfinished_orders'] != len(active)
                or coverage['urgency_scope'] != 'verified_dated_unfinished_whole_orders'):
            return False
        as_of = date.fromisoformat(coverage['as_of']).isoformat()
        known = sum(row['deadline'] < as_of for row in dated)
        if (table['overdue_known_count'] != known
                or table['overdue_work'] != (known if complete else None)
                or model['today']['overdue_work'] != table['overdue_work']
                or model['today']['active_work'] != len(active)
                or table['undated_unfinished_orders'] != len(active)-len(dated)):
            return False
        if (table['urgent_orders'] != dated[:3]
                or [row['id'] for row in model['urgent_orders']] != [str(row['id']) for row in dated[:3]]):
            return False
        for projected, canonical in zip(model['urgent_orders'], dated[:3]):
            if (projected['deadline'] != canonical['deadline']
                    or projected['native_progress'] != canonical['native_progress']):
                return False
        stations = model['station_coverage']
        return (stations['available'] is True and stations['source']['complete'] is True
                and stations['coverage']['terminal_filtered'] is True
                and model['overall_progress_percent'] is None
                # Deadline inputs stay internal; the public contract is a capped
                # integer percentage (or unknown), independent of step counts.
                and all(row['load_percent'] is None or (type(row['load_percent']) is int
                    and 0 <= row['load_percent'] <= 100)
                    for row in model['workstations']))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


class Observer:
    def __init__(self):
        self.baseline = {}
        self.last = {}
        self.successes = dict.fromkeys(SOURCES, 0)
        self.polls = 0
        self.reads = {}

    def observe(self, model, latency_ms):
        self.polls += 1
        consistent = consistency(model)
        report = dict(get_polls=self.polls, get_latency_ms=round(latency_ms, 2),
                      canonical_checks_passed=consistent, sources={},
                      validation_scope='direct_reader_http_attempts_and_successful_scans',
                      upstream_read_verified=False,
                      upstream_read_evidence='Application instrumentation at shared HTTP page boundary; not an independent upstream audit',
                      coverage=dict(deadlines='only_verified_dated_subset',
                                    completion_history='unverified', capacity='unverified'))
        for name in SOURCES:
            meta = model.get('sources', {}).get(name, {})
            generation = integer(meta.get('generation'))
            success = safe_time(meta.get('last_success'))
            failure = meta.get('failure_reason')
            failure = failure if failure in SAFE_ERRORS else 'unrecognized_error' if failure else None
            read_id = meta.get('successful_read_id')
            attempts = integer(meta.get('read_attempts'))
            requests = integer(meta.get('http_requests'))
            direct = (meta.get('transport') == 'direct_python' and isinstance(read_id, str)
                      and re.fullmatch(r'[0-9a-f]{32}', read_id) is not None
                      and attempts is not None and attempts > 0
                      and requests is not None and requests >= attempts)
            shared_id = meta.get('shared_wol_scan_id') if name == 'workstation_wip' else None
            shared = (isinstance(shared_id, str)
                      and re.fullmatch(r'[0-9a-f]{32}', shared_id) is not None
                      and model.get('station_coverage', {}).get('source', {}).get('scan_id') == shared_id)
            if shared:
                # The station projection can consume HTTP pages read by the
                # order worker. Count the unique authoritative scan, not polls.
                direct = True
                read_id = shared_id
            previous_read = self.reads.get(name, (None, 0, 0))
            new_read = (direct and read_id != previous_read[0]
                        and (shared or (attempts > previous_read[1] and requests > previous_read[2])))
            healthy = (direct and consistent and generation is not None and generation > 0 and success is not None
                       and failure is None and meta.get('stale') is False
                       and meta.get('state') in ('available', 'refreshing'))
            pair = (generation or 0, success)
            if name not in self.baseline:
                self.baseline[name] = pair
                self.last[name] = pair
                self.reads[name] = (read_id, attempts or 0, requests or 0)
            elif not healthy:
                self.successes[name] = 0
            elif pair[0] < self.last[name][0]:
                self.successes[name] = 0
                self.last[name] = pair
                self.reads[name] = (read_id, attempts or 0, requests or 0)
            elif new_read and pair[0] > self.last[name][0] and (self.last[name][1] is None
                                                or pair[1] > self.last[name][1]):
                # A skipped generation counts as one observed success, never several.
                self.successes[name] += 1
                self.last[name] = pair
                self.reads[name] = (read_id, attempts, requests)
            duration = meta.get('duration_ms')
            report['sources'][name] = dict(generation=generation, last_success=success,
                transport='direct_python' if direct else 'unverified',
                read_attempts=attempts, http_requests=requests, direct_read_evidence=direct,
                error=failure, duration_ms=duration if type(duration) in (int, float)
                and 0 <= duration < 86400000 else None,
                new_successful_generations=self.successes[name])
        today = model.get('today', {})
        report['aggregates'] = {name: integer(today.get(name)) for name in
                                ('active_work', 'overdue_work', 'completed_today')}
        table = model.get('canonical_orders') or {}
        report['aggregates']['undated_unfinished_orders'] = integer(table.get('undated_unfinished_orders'))
        report['aggregates']['overdue_known_count'] = integer(table.get('overdue_known_count'))
        report['passed'] = consistent and all(value >= 3 for value in self.successes.values())
        return report


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8010)
    parser.add_argument('--wait', type=int, default=600)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535 or not 1 <= args.wait <= 1800:
        parser.error('port must be 1024..65535 and wait must be 1..1800 seconds')
    opener = build_opener(ProxyHandler({}), NoRedirect())  # No proxy/environment configuration.
    observer = Observer()
    deadline = monotonic()+args.wait
    attempts, failures = 0, 0
    while monotonic() < deadline:
        started = monotonic()
        attempts += 1
        try:
            request = Request(f'http://127.0.0.1:{args.port}/api/dashboard', method='GET')
            with opener.open(request, timeout=min(5, max(0.01, deadline-started))) as response:
                # Never follow a local redirect to a different endpoint.
                if response.url != request.full_url or response.status != 200:
                    raise ValueError()
                raw = bytearray()
                while True:
                    if monotonic() >= deadline:
                        raise TimeoutError()
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    raw.extend(chunk)
                    if len(raw) > 16*1024*1024:
                        raise ValueError()
                model = json.loads(raw)
            report = observer.observe(model, (monotonic()-started)*1000)
        except Exception:
            failures += 1
            observer.successes = dict.fromkeys(SOURCES, 0)
            print(json.dumps(dict(get_attempts=attempts, get_failures=failures,
                                  error='local_get_or_response_unavailable', passed=False)), flush=True)
        else:
            print(json.dumps(report, allow_nan=False), flush=True)
            if report['passed']:
                print(json.dumps(dict(result='passed', get_attempts=attempts,
                    get_failures=failures, new_successful_generations=observer.successes,
                    validation_scope='direct_reader_http_attempts_and_successful_scans',
                    upstream_read_verified=False)), flush=True)
                return 0
        remaining = deadline-monotonic()
        if remaining > 0:
            sleep(min(5, remaining))
    print(json.dumps(dict(result='timeout', passed=False, get_attempts=attempts,
        get_failures=failures, new_successful_generations=observer.successes)), flush=True)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
