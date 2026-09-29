"""Verified calendar contract. No configuration, login, or network at import time."""
from datetime import date
from html.parser import HTMLParser
import re
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

import epoptia_throttle

ROUTE = '/planning/calendar'
MAX_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 5
MAX_RECORDS = 10000
CACHE_SECONDS = 60
_VOID = frozenset('area base br col embed hr img input link meta param source track wbr'.split())


def positive_id(value):
    if type(value) is int:
        return value if 0 < value <= 9223372036854775807 else None
    if type(value) is str and re.fullmatch(r'[1-9][0-9]{0,18}', value):
        return positive_id(int(value))
    return None


def iso_date(value):
    if type(value) is not str or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def result(status, records=()):
    return dict(status=status, reason=None if status == 'ok' else status,
                records=list(records))


class CalendarDOM(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.records = [], {}
        self.invalid = False
        self.login = False
        self.cards = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = a.get('class', '').split()
        if tag == 'input' and a.get('type', '').lower() == 'password':
            self.login = True
        if 'wolCardComponent' in classes:
            self.cards += 1
            wol, order = positive_id(a.get('data-id')), positive_id(a.get('data-workorder'))
            inner = next((v for _, v in reversed(self.stack)
                          if 'workorderWol' in v.get('class', '').split() and 'data-date' in v), None)
            parent = next((v for _, v in reversed(self.stack)
                           if 'mainWolItem' in v.get('class', '').split()
                           and all(k in v for k in ('data-id', 'data-workorder', 'data-date'))), None)
            due = iso_date(inner.get('data-date')) if inner is not None else iso_date(parent.get('data-date')) if parent else None
            if not wol or not order or not due or len(self.records) >= MAX_RECORDS:
                self.invalid = True
            elif wol in self.records and self.records[wol] != dict(wol_id=wol, workorder_id=order, target_date=due):
                self.invalid = True
            else:
                self.records[wol] = dict(wol_id=wol, workorder_id=order, target_date=due)
        if len(attrs) != len(a) or len(self.stack) > 256:
            self.invalid = True
        if tag not in _VOID:
            self.stack.append((tag, a))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def parse_calendar(html):
    if type(html) is not str or len(html.encode('utf-8')) > MAX_BYTES:
        return result('calendar_size_limit')
    parser = CalendarDOM()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return result('calendar_invalid_dom')
    if parser.login:
        return result('calendar_auth_missing')
    if parser.invalid:
        return result('calendar_invalid_dom')
    if not parser.cards:
        return result('render_required')
    return result('ok', sorted(parser.records.values(), key=lambda r: r['wol_id']))


def fetch_calendar(base_url, session=None, *, authenticated=False, clock=monotonic):
    """Use an existing protected session only. Zero redirects; GET without a body."""
    if session is None or authenticated is not True:
        return result('calendar_auth_missing')
    try:
        origin = urlsplit(base_url)
        if origin.scheme not in ('https', 'http') or not origin.netloc or origin.username or origin.password:
            return result('calendar_invalid_origin')
        url = origin.scheme + '://' + origin.netloc + ROUTE
        deadline = clock() + MAX_SECONDS
        with epoptia_throttle.call(session.get, url, headers={'Accept': 'text/html'}, stream=True,
                         timeout=MAX_SECONDS, allow_redirects=False) as response:
            if 300 <= response.status_code < 400:
                return result('calendar_auth_missing')
            if response.status_code in (401, 403):
                return result('calendar_auth_missing')
            if response.status_code != 200:
                return result('calendar_http_error')
            if response.url != url:
                return result('calendar_auth_missing')
            if response.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'text/html':
                return result('calendar_invalid_response')
            length = response.headers.get('Content-Length')
            if length is not None and (not length.isdigit() or int(length) > MAX_BYTES):
                return result('calendar_size_limit')
            body = bytearray()
            # One-byte reads also enforce the total deadline against trickle responses.
            for chunk in response.iter_content(chunk_size=1):
                if clock() >= deadline:
                    return result('calendar_timeout')
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    return result('calendar_size_limit')
            if clock() >= deadline:
                return result('calendar_timeout')
            return parse_calendar(body.decode('utf-8', errors='strict'))
    except Exception as exc:
        from requests import Timeout
        return result('calendar_timeout' if isinstance(exc, (Timeout, TimeoutError)) else 'calendar_unavailable')


_lock = Lock()
_cached = None
_cached_at = 0


def refresh_calendar(base_url, session, *, authenticated=False):
    """Publish a bounded date-only snapshot from an existing authenticated reader."""
    value = fetch_calendar(base_url, session, authenticated=authenticated)
    global _cached, _cached_at
    with _lock:
        _cached, _cached_at = value, monotonic()
    return value


def validate_filters(wol_ids=None, workorder_ids=None, limit=200):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('limit must be an integer from 1 to 200')
    for values in (wol_ids, workorder_ids):
        if values is not None and (type(values) is not list or len(values) > 200 or
                                  any(type(v) is not int or positive_id(v) is None for v in values)):
            raise ValueError('filters must contain at most 200 positive integer IDs')


def calendar_target_dates(wol_ids=None, workorder_ids=None, limit=200):
    validate_filters(wol_ids, workorder_ids, limit)
    with _lock:
        value = _cached if _cached is not None and monotonic() - _cached_at < CACHE_SECONDS else result('calendar_auth_missing')
        rows = [dict(r) for r in value['records']
                if (wol_ids is None or r['wol_id'] in wol_ids)
                and (workorder_ids is None or r['workorder_id'] in workorder_ids)]
        return dict(ok=True, **result(value['status'], rows[:limit]),
                    total_matches=len(rows), truncated=len(rows) > limit)


def wol_target(wol_id, calendar=None):
    value = calendar if calendar is not None else calendar_target_dates(wol_ids=[wol_id])
    row = next((r for r in value['records'] if r['wol_id'] == wol_id), None)
    status = 'ok' if row else value['status'] if value['status'] != 'ok' else 'calendar_date_missing'
    return dict(target_date=row['target_date'] if row else None,
                target_date_status=status, target_date_reason=None if row else status)


def order_targets(workorder_id, calendar):
    dates = sorted({r['target_date'] for r in calendar['records'] if r['workorder_id'] == workorder_id})
    status = ('ok' if len(dates) == 1 else 'calendar_dates_differ') if dates else (
        calendar['status'] if calendar['status'] != 'ok' else 'calendar_date_missing')
    return dict(target_date=dates[0] if len(dates) == 1 else None,
                target_date_min=dates[0] if dates else None,
                target_date_max=dates[-1] if dates else None,
                target_date_status=status, target_date_reason=None if status == 'ok' else status)


async def rendered_calendar(page, base_url, *, authenticated=False):
    """Optional already-authenticated Playwright page; no navigation or interaction.

    The caller owns its existing runtime/session. page.content serializes the live
    DOM; the same exact class/attribute contract and parser are used for both paths.
    """
    import asyncio
    if page is None or authenticated is not True:
        return result('calendar_auth_missing')
    try:
        origin = urlsplit(base_url)
        expected = origin.scheme + '://' + origin.netloc + ROUTE
        if page.url != expected:
            return result('calendar_auth_missing')
        html = await asyncio.wait_for(page.content(), timeout=MAX_SECONDS)
        if page.url != expected:
            return result('calendar_auth_missing')
        return parse_calendar(html)
    except TimeoutError:
        return result('calendar_timeout')
    except Exception:
        return result('calendar_unavailable')
