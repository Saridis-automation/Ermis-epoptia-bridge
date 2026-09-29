"""Temporary metadata-only discovery for details and workorder progress.

No configuration reads or network activity at import time. Remove this module
and the reader/MCP enrichments together when discovery is complete.
"""
from html.parser import HTMLParser
from collections import deque
import codecs
import re
import time
from urllib.parse import urljoin, urlsplit

import epoptia_throttle

MAX_ROUTES = 20
MAX_ENDPOINTS = 30
MAX_FOLLOW = 3
MAX_BYTES = 256 * 1024
KEYWORDS = re.compile(r'reports?|logs|history|daily|productivity|workstation|workhours|detailedreport|wolreport|completedtoday', re.I)
# Outside the navigation prefixes below, paths use a finite vocabulary.
WORDS = set('api ajax reports report logs log history daily productivity workstation '
            'workstations workhours detailedreport wolreport today workorders '
            'workorderlines capacity planning index list search filter filters data '
            'get read view show details summary production completedtoday completed time hours dates date '
            'range start end from to by all v1 v2 js scripts assets public '
            'dailyanalysis productiondata factory operator operators'.split())
PARAMETERS = set('id wol_id workorder_id workstation_id workorderline_id start end '
                 'from to date start_date end_date date_from date_to page limit '
                 'offset sort order search filter status type format daily today '
                 'workstation workorder workorderline report draw length columns'.split())
STATIC_PREFIXES = ('/reports/', '/workorders/history/', '/workstations/')
# Keep the temporary exception conservative: short lowercase route slugs only.
STATIC_SLUG = re.compile(r'[a-z][a-z0-9_-]{0,23}\Z')
OPAQUE_HEX = re.compile(r'[a-f0-9]{8,}\Z', re.I)
UNSAFE_SLUG_WORDS = set('delete remove destroy create add edit update save execute '
                        'run token secret password credential credentials auth login logout'.split())


def empty_result(status='unavailable'):
    return dict(status=status, reason=None if status == 'ok' else status,
                landing_paths=[], route_candidates=[],
                endpoint_candidates=[], follow_failures=[], notes=[])


def _path(base, raw, *, allow_static_slugs=True):
    if not isinstance(raw, str) or len(raw) > 2048 or re.search(r'[\\\s\x00-\x1f]', raw):
        return None
    try:
        origin, target = urlsplit(base), urlsplit(urljoin(base, raw))
    except ValueError:
        return None
    if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
        return None
    if target.username or target.password or origin.scheme not in ('http', 'https'):
        return None
    segments = target.path.split('/')
    if len(segments) > 12:
        return None
    static_prefix = next((prefix for prefix in STATIC_PREFIXES
                          if target.path.startswith(prefix)), None) if allow_static_slugs else None
    safe = []
    for index, segment in enumerate(segments):
        words = re.sub(r'([a-z])([A-Z])', r'\1-\2', segment).lower()
        parts = re.split(r'[-_.]', words)
        static_slug = (static_prefix is not None and index >= static_prefix.count('/') and
                       STATIC_SLUG.fullmatch(segment) and
                       not OPAQUE_HEX.fullmatch(segment.replace('-', '').replace('_', '')) and
                       not UNSAFE_SLUG_WORDS.intersection(parts))
        safe.append(segment if not segment or all(p in WORDS for p in parts) or static_slug
                    else ':redacted')
    return '/'.join(safe) or '/'


def _names(names):
    return sorted({name for name in names if name in PARAMETERS})[:20]


class Metadata(HTMLParser):
    def __init__(self, base, result):
        super().__init__(convert_charrefs=True)
        self.base, self.result = base, result
        self.anchor = None
        self.form = None
        self.script = False
        self.script_parts = []
        self.has_metadata = False

    def route(self, raw, kind, text=''):
        path = _path(self.base, raw)
        if path and (KEYWORDS.search(path) or KEYWORDS.search(text)):
            self.has_metadata = True
            item = dict(path=path, evidence_kind=kind)
            routes = self.result['route_candidates']
            if item not in routes and len(routes) < MAX_ROUTES:
                routes.append(item)

    def endpoint(self, raw, method, names, kind):
        path = _path(self.base, raw)
        if not path:
            return
        self.has_metadata = True
        self.route(raw, kind)
        # Parse names only; never decode, store, or emit query values.
        names = list(names) + [part.split('=', 1)[0] for part in urlsplit(raw).query.split('&')]
        item = dict(path=path, method=method if method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD') else None,
                    parameter_names=_names(names), evidence_kind=kind)
        endpoints = self.result['endpoint_candidates']
        if item not in endpoints and len(endpoints) < MAX_ENDPOINTS:
            endpoints.append(item)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for attr in (('href', 'action', 'src') if tag == 'script' else ('href', 'action')):
            if attr in attrs:
                self.route(attrs[attr], 'script' if tag == 'script' else attr)
        if tag == 'a':
            self.anchor = attrs.get('href')
        if tag == 'form':
            self.form = (attrs.get('action', self.base), attrs.get('method', 'GET').upper(), [])
        if tag in ('input', 'select', 'textarea', 'button') and self.form:
            if len(self.form[2]) < 100:
                self.form[2].append(attrs.get('name', ''))
        if tag == 'script':
            self.script = True
            self.script_parts = []

    def handle_data(self, data):
        if self.anchor:
            self.route(self.anchor, 'href_text', data)
        if self.script:
            self.script_parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'a':
            self.anchor = None
        if tag == 'form' and self.form:
            self.endpoint(*self.form, 'form')
            self.form = None
        if tag == 'script':
            self.javascript(''.join(self.script_parts))
            self.script = False
            self.script_parts = []

    def javascript(self, source):
        # Literal calls only: no JS evaluation, dynamic URL guessing or execution.
        for match in re.finditer(r'''['"]([^'"\r\n]{1,2048})['"]''', source):
            raw = match.group(1)
            if raw.startswith(('/', './', '../', 'https://', 'http://')):
                self.route(raw, 'script')
        patterns = (
            (r'\bfetch\s*\(\s*[\'"]([^\'"\r\n]+)[\'"](?=\s*[,\)])([^;]{0,1024})', 'fetch'),
            (r'\baxios\.(get|post|put|patch|delete|head)\s*\(\s*[\'"]([^\'"\r\n]+)[\'"](?=\s*[,\)])([^;]{0,1024})', 'axios'),
            (r'\.open\s*\(\s*[\'"](GET|POST|PUT|PATCH|DELETE|HEAD)[\'"]\s*,\s*[\'"]([^\'"\r\n]+)[\'"](?=\s*[,\)])', 'xhr'),
            (r'\b(?:ajax|axios)\s*\(\s*\{([\s\S]{0,2048}?)\}\s*\)', 'ajax'),
        )
        for pattern, kind in patterns:
            for match in re.finditer(pattern, source):
                if len(self.result['endpoint_candidates']) >= MAX_ENDPOINTS:
                    return
                options = ''
                if kind == 'axios':
                    method, raw, options = match.groups()
                elif kind == 'xhr':
                    method, raw = match.groups()
                else:
                    if kind == 'fetch':
                        raw, options = match.groups()
                    else:
                        options = match.group(1)
                        url = re.search(r'\burl\s*:\s*[\'"]([^\'"]+)[\'"](?=\s*(?:[,}]|$))', options)
                        if not url:
                            continue
                        raw = url.group(1)
                    verb = re.search(r'\b(?:method|type)\s*:\s*[\'"](\w+)[\'"]', options)
                    method = verb.group(1) if verb else None
                # Only names inside an explicit params/data object.
                objects = re.findall(r'\b(?:params|data)\s*:\s*\{([^}]{0,512})\}', options)
                names = [name for obj in objects for name in re.findall(r'\b(\w+)\s*:', obj)]
                self.endpoint(raw, method.upper() if method else None, names, kind)


def discover(base_url, session, *, start_path='/workorders', initial_html=None,
             max_follow=MAX_FOLLOW):
    """Use the caller's authenticated session and optionally its parent HTML.

    Each response has byte/time bounds. No forms or discovered endpoints are
    executed. Follow at most eight navigation pages, without redirects.
    """
    result = empty_result('ok')
    result['notes'] = ['temporary_metadata_only', 'unknown_path_segments_redacted',
                       'parameter_names_allowlisted', 'external_scripts_not_fetched',
                       'literal_endpoints_only', 'bounded_sample_not_exhaustive']
    base = urlsplit(base_url)
    origin = f'{base.scheme}://{base.netloc}'
    if base.username or base.password or base.scheme not in ('http', 'https') or not base.netloc:
        return empty_result('invalid_origin')
    deadline = time.monotonic() + 12
    max_follow = min(8, max(0, max_follow))
    pending, visited = [start_path], set()
    while pending and len(visited) < 1 + max_follow:
        path = pending.pop(0)
        if path in visited:
            continue
        visited.add(path)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            if path == start_path and initial_html is not None:
                if not isinstance(initial_html, str):
                    raise ValueError
                # Parse a bounded prefix of the already-fetched body. Never issue
                # a replacement parent GET, including when the body is large.
                prefix = initial_html[:MAX_BYTES].encode('utf-8')
                html = prefix[:MAX_BYTES].decode('utf-8', errors='ignore')
                if len(initial_html) > MAX_BYTES or len(prefix) > MAX_BYTES:
                    result['status'] = 'partial'
                    result['notes'].append('parent_body_truncated')
            else:
                html = _read_page(session, origin + path, remaining, deadline, result)
                if html is None:
                    continue
            parser = Metadata(origin + path, result)
            parser.feed(html)
            parser.close()
            result['landing_paths'].append(_path(origin, path))
            if not parser.has_metadata:
                _failure(result, origin + path, 'parse_empty')
            for candidate in result['route_candidates']:
                route = candidate['path']
                # Only known report/log navigation routes, never scripts, forms,
                # unknown segments, query values or action-like route segments.
                if (candidate['evidence_kind'] in ('href', 'href_text') and
                        ':redacted' not in route and KEYWORDS.search(route) and
                        _path(origin, route, allow_static_slugs=False) == route and
                        not route.endswith('.js') and route not in visited and route not in pending):
                    pending.append(route)
        except Exception:
            _failure(result, origin + path, 'request_failed')
            if not result['landing_paths']:
                result['status'] = 'unavailable'
        if time.monotonic() >= deadline:
            break
    result['notes'] = list(dict.fromkeys(result['notes']))
    result['reason'] = None if result['status'] == 'ok' else result['notes'][-1]
    return result


def _failure(result, url, reason, status=None):
    result['status'] = 'partial'
    result['notes'].append(reason)
    item = dict(path=_path(url, url), reason=reason)
    if status is not None:
        item['http_status_class'] = f'{status // 100}xx'
    if len(result['follow_failures']) < 9:
        result['follow_failures'].append(item)


def _read_page(session, url, remaining, deadline, result):
    with epoptia_throttle.call(session.get, url, headers={'Accept': 'text/html'},
                     timeout=min(3, remaining), allow_redirects=False, stream=True) as response:
        if response.status_code != 200:
            _failure(result, url, 'auth_redirect' if 300 <= response.status_code < 400
                     else 'http_status_class', response.status_code)
            return None
        if 'text/html' not in response.headers.get('Content-Type', '').lower():
            _failure(result, url, 'parse_empty')
            return None
        body = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            if time.monotonic() >= deadline:
                raise TimeoutError
            if len(body) + len(chunk) > MAX_BYTES:
                raise ValueError
            body.extend(chunk)
        return body.decode('utf-8', errors='replace')


def runtime_discovery(base_url, username, password):
    """Reuse the existing reader's web authentication inside the MCP process."""
    import epoptia_read
    try:
        if not username or not password:
            return empty_result('authentication_unavailable')
        with epoptia_read.requests.Session() as session:
            if not epoptia_read._web_login(session, base_url, username, password):
                return empty_result('authentication_unavailable')
            return discover(base_url, session)
    except Exception:
        return empty_result('discovery_unavailable')


TARGET_REPORT = '/reports/factory/productiondata'
REPORT_ROUTE_PROBES = (
    '/reports/factory/dailyanalysis', '/reports/factory/daily-analysis',
    '/reports/workstations', '/reports/workstation',
    '/reports/workstations/dailyanalysis', '/reports/workstation/dailyanalysis',
    '/reports/factory/workstations',
)


class ReportLabels(HTMLParser):
    """Recognize finite labels without retaining arbitrary page text."""
    LABELS = {'reports_dailyanalysis': 'Daily Analysis',
              'daily analysis': 'Daily Analysis',
              'reports_workstations': 'Workstations',
              'workstations': 'Workstations'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.labels = set()
        self.auth_page = False
        self.hidden = 0

    def label(self, value):
        label = self.LABELS.get(' '.join(value.lower().split()))
        if label:
            self.labels.add(label)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style'):
            self.hidden += 1
        if (tag == 'input' and attrs.get('type', '').lower() == 'password' or
                tag == 'meta' and attrs.get('http-equiv', '').lower() == 'refresh'):
            self.auth_page = True
        if tag == 'form' and re.search(r'/(?:login|signin|auth)(?:/|[?#]|$)',
                                      attrs.get('action', ''), re.I):
            self.auth_page = True
        for key in ('title', 'aria-label', 'data-i18n', 'data-label', 'data-report'):
            self.label(attrs.get(key, ''))
        if tag == 'meta':
            self.label(attrs.get('content', ''))

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.label(data)


class VerifiedMetadata(Metadata):
    """Only forms and literal inline endpoints; never follow page links/assets."""
    def route(self, raw, kind, text=''):
        pass

    def javascript(self, source):
        ScriptSearch(self).feed(source, final=True)


MAX_TARGET_REPORT_BYTES = 4 * 1024 * 1024
REPORT_TERMS = re.compile(r'reports?|dailyanalysis|daily|workstations?|productiondata|factory|operator|logs|history', re.I)
SIBLING_TERMS = re.compile(r'dailyanalysis|daily|workstations?|productiondata|factory|operator', re.I)
MAX_SCRIPTS = 8
MAX_SCRIPT_BYTES = 16 * 1024 * 1024


def _script_path(base, raw):
    """Display short static /js/ names; never preserve queries or opaque parts."""
    safe = _path(base, raw)
    if safe is None:
        return None
    path = urlsplit(urljoin(base, raw)).path
    if not path.startswith('/js/'):
        return safe
    parts = path.split('/')
    for index in range(2, len(parts)):
        part = parts[index]
        stem = part[:-3] if index == len(parts) - 1 and part.endswith('.js') else part
        words = re.split(r'[-_.]', stem)
        valid = ((index < len(parts) - 1 or part.endswith('.js')) and len(stem) <= 48 and all(re.fullmatch(r'[a-z]{1,24}', word)
                 and not OPAQUE_HEX.fullmatch(word) for word in words)
                 and not UNSAFE_SLUG_WORDS.intersection(words))
        parts[index] = part if valid else ':redacted'
    return '/'.join(parts)


class ScriptSearch:
    """Streaming lexical search. Retain only a bounded statement, never a body.

    Comments, templates and escaped strings are deliberately ignored. Large
    statements retain a bounded suffix; this is discovery, not a JS interpreter.
    """
    def __init__(self, parser):
        self.parser = parser
        self.tokens = deque(maxlen=512)
        self.state = 'code'
        self.value = ''
        self.quote = ''
        self.escaped = False
        self.invalid = False
        self.regex_class = False

    def emit(self, kind, value):
        self.tokens.append((kind, value))

    def flush(self):
        tokens = list(self.tokens)
        self.tokens.clear()
        for i, (kind, raw) in enumerate(tokens):
            if kind != 'string' or not raw.startswith(('/', './', '../', 'https://', 'http://')):
                continue
            # Concatenations/computed expressions are not literal endpoints.
            before = tokens[i - 1][1] if i else ''
            after = tokens[i + 1][1] if i + 1 < len(tokens) else ''
            if before in ('+', '`') or after not in ('', ',', ')', '}', ']', ';'):
                continue
            method, names = None, []
            # Only attach metadata to a recognizable call/object around this URL.
            prefix = tokens[max(0, i - 4):i]
            values = [v for _, v in prefix]
            options = []
            if values[-2:] == ['fetch', '('] or (len(values) == 4 and
                    values[0:2] == ['axios', '.'] and values[3] == '('):
                if len(values) == 4 and values[0] == 'axios' and values[2] in ('get', 'post'):
                    method = values[2].upper()
                # Stop at the end of this call; never borrow from later calls.
                for token in tokens[i + 1:]:
                    if token == ('punct', ')'):
                        break
                    options.append(token)
            elif values[-2:] == ['url', ':']:
                # Only the containing object, with a short bounded neighborhood.
                left = i - 3
                while left >= 0 and tokens[left] != ('punct', '{'):
                    left -= 1
                right = i + 1
                while right < len(tokens) and tokens[right] != ('punct', '}'):
                    right += 1
                options = tokens[left:right + 1] if left >= 0 else []
            if i >= 4 and tokens[i - 1] == ('punct', ',') and tokens[i - 2] in (
                    ('string', 'GET'), ('string', 'POST')) and tokens[i - 3] == ('punct', '(') and tokens[i - 4] == ('word', 'open'):
                method = tokens[i - 2][1]
            depth = 0
            for j in range(len(options) - 2):
                if options[j] == ('punct', '{'):
                    depth += 1
                elif options[j] == ('punct', '}'):
                    depth -= 1
                if depth != 1 or j == 0 or options[j - 1] not in (('punct', '{'), ('punct', ',')):
                    continue
                key, colon, value = options[j:j + 3]
                if (key[1] in ('method', 'type') and colon == ('punct', ':')
                        and value in (('string', 'GET'), ('string', 'POST'))
                        and j + 3 < len(options) and options[j + 3] in (('punct', ','), ('punct', '}'))):
                    method = value[1]
                if key[1] in ('params', 'data') and colon == ('punct', ':') and value == ('punct', '{'):
                    nested = 1
                    for k in range(j + 3, len(options) - 1):
                        if options[k] == ('punct', '{'):
                            nested += 1
                        elif options[k] == ('punct', '}'):
                            nested -= 1
                        if nested == 0:
                            break
                        if (nested == 1 and options[k][0] in ('word', 'string') and options[k + 1] == ('punct', ':')
                                and options[k - 1][1] in ('{', ',')):
                            names.append(options[k][1])
            self.parser.endpoint(raw, method, names, 'script_literal')

    def feed(self, source, final=False):
        for char in source:
            if self.state == 'regex':
                if self.escaped:
                    self.escaped = False
                elif char == '\\':
                    self.escaped = True
                elif char == '[':
                    self.regex_class = True
                elif char == ']':
                    self.regex_class = False
                elif (char == '/' and not self.regex_class) or char in '\r\n':
                    self.state = 'code'
                continue
            if self.state == 'line':
                if char in '\r\n':
                    self.state = 'code'
                continue
            if self.state in ('block', 'star'):
                self.state = 'code' if self.state == 'star' and char == '/' else ('star' if char == '*' else 'block')
                continue
            if self.state == 'string':
                if self.escaped:
                    self.escaped = False
                    continue
                if char == '\\':
                    self.escaped = self.invalid = True
                elif char == self.quote:
                    self.emit('ignored' if self.invalid else 'string', '' if self.invalid else self.value)
                    self.state, self.value = 'code', ''
                elif len(self.value) < 2048 and not self.invalid:
                    self.value += char
                else:
                    self.invalid = True
                continue
            if self.state == 'slash':
                self.state = 'line' if char == '/' else 'block' if char == '*' else 'code'
                if self.state != 'code':
                    continue
                # Conservatively skip non-comment slash expressions as regexes.
                self.state = 'regex'
                self.regex_class = char == '['
                self.escaped = char == '\\'
                continue
            if char.isascii() and (char.isalnum() or char in '_$'):
                self.value = (self.value + char)[-64:]
                continue
            if self.value:
                self.emit('word', self.value)
                self.value = ''
            if char in "\"'`":
                self.state, self.quote, self.invalid = 'string', char, char == '`'
            elif char == '/':
                self.state = 'slash'
            elif not char.isspace():
                self.emit('punct', char)
                if char == ';':
                    self.flush()
        if final:
            self.flush()


class TargetMetadata(Metadata):
    """Collect page metadata without following navigation or executing JS."""
    def __init__(self, base, result):
        super().__init__(base, result)
        self.assets = []
        self.asset_mode = False

    def route(self, raw, kind, text=''):
        path = _path(self.base, raw)
        if (kind in ('href', 'href_text') and path and path.startswith('/reports/')
                and (SIBLING_TERMS.search(path) or SIBLING_TERMS.search(text))):
            item = dict(path=path, evidence_kind=kind)
            if item not in self.result['route_candidates'] and len(self.result['route_candidates']) < MAX_ROUTES:
                self.result['route_candidates'].append(item)

    def endpoint(self, raw, method, names, kind):
        path = _path(self.base, raw)
        if self.asset_mode and (not path or not REPORT_TERMS.search(path)):
            return
        super().endpoint(raw, method, names, kind)

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag != 'script':
            return
        raw = dict(attrs).get('src')
        safe = _script_path(self.base, raw)
        if safe is None:
            return
        paths = self.result['script_src_paths']
        if safe not in paths and len(paths) < 30:
            paths.append(safe)
        # Use the verified same-origin literal path internally, never the
        # redacted display path. Discard query values and fragments entirely.
        path = urlsplit(urljoin(self.base, raw)).path
        if path not in self.assets and len(self.assets) < MAX_SCRIPTS:
            self.assets.append(path)

    def javascript(self, source):
        if self.asset_mode:
            search = ScriptSearch(self)
            search.feed(source, final=True)
        else:
            super().javascript(source)


def _content_category(value):
    mime = value.split(';', 1)[0].strip().lower()
    if mime in ('text/html', 'application/xhtml+xml'):
        return 'html'
    if mime in ('application/javascript', 'text/javascript', 'application/x-javascript'):
        return 'javascript'
    if mime == 'application/json':
        return 'json'
    return 'other' if mime else 'missing'


def discover_workorder_reports(base_url, session, *, html_get):
    """Target GET, at most eight script GETs, and seven fixed report probes.

    html_get is the very same helper used by the caller for /workorders/{id}.
    All output is sanitized metadata; raw response and exception text stay local.
    """
    if _path(base_url, TARGET_REPORT) != TARGET_REPORT:
        return empty_result('invalid_origin')
    origin_parts = urlsplit(base_url)
    origin = f'{origin_parts.scheme}://{origin_parts.netloc}'
    result = empty_result('ok')
    result['script_src_paths'] = []
    result['verified_report_routes'] = []
    result['target_page'] = dict(path=TARGET_REPORT, http_status_class=None,
                                 content_type=None, failure_category=None)
    result['notes'] = ['temporary_metadata_only', 'parameter_names_allowlisted',
                       'unknown_path_segments_redacted', 'literal_endpoints_only',
                       'bounded_sample_not_exhaustive']
    deadline = time.monotonic() + 12

    def read(path, script=False, verification=None):
        metadata = (verification if verification is not None else
                    result['target_page'] if not script else {})
        failure = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            with html_get(session, origin + path, timeout=min(3, remaining), stream=True) as response:
                status = response.status_code
                category = _content_category(response.headers.get('Content-Type', ''))
                metadata.update(http_status_class=f'{status // 100}xx' if 100 <= status <= 599 else 'other',
                                content_type=category)
                history = getattr(response, 'history', None)
                final_url = getattr(response, 'url', None)
                redirected = (isinstance(history, (list, tuple)) and bool(history) or
                              isinstance(final_url, str) and final_url != origin + path)
                if verification is not None and redirected:
                    failure = 'auth_redirect'
                elif not (200 <= status < 300 if verification is not None else status == 200):
                    failure = ('auth_redirect' if 300 <= status < 400 else
                               'authentication_failed' if status in (401, 403) else 'http_error')
                elif category not in (('javascript',) if script else ('html',)):
                    failure = 'unexpected_content_type'
                else:
                    body_limit = MAX_SCRIPT_BYTES if script else MAX_TARGET_REPORT_BYTES
                    if script:
                        asset_parser = TargetMetadata(origin + path, result)
                        asset_parser.asset_mode = True
                        search = ScriptSearch(asset_parser)
                        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                    body = bytearray()
                    received = 0
                    for chunk in response.iter_content(chunk_size=8192):
                        if time.monotonic() >= deadline:
                            raise TimeoutError
                        allowed = chunk[:max(0, body_limit - received)]
                        if script:
                            # Even a transport returning an oversized chunk is
                            # searched in small windows, only up to the byte cap.
                            for offset in range(0, len(allowed), 8192):
                                if time.monotonic() >= deadline:
                                    raise TimeoutError
                                search.feed(decoder.decode(allowed[offset:offset + 8192]))
                        received += len(chunk)
                        if received > body_limit:
                            failure = 'body_limit'
                            break
                        if not script:
                            body.extend(chunk)
                    if failure is None:
                        if script:
                            search.feed(decoder.decode(b'', final=True), final=True)
                            return True
                        return body.decode('utf-8', errors='replace')
        except TimeoutError:
            failure = 'timeout'
        except Exception as error:
            # Only the exception class is inspected, never its message.
            from requests import Timeout
            failure = 'timeout' if isinstance(error, Timeout) else 'request_failed'
        metadata['failure_category'] = failure
        _failure(result, origin + path, failure)
        if script:
            result['follow_failures'][-1]['path'] = _script_path(origin, path)
        return None

    html = read(TARGET_REPORT)
    if html is not None:
        result['landing_paths'].append(TARGET_REPORT)
        parser = TargetMetadata(origin + TARGET_REPORT, result)
        try:
            parser.feed(html)
            parser.close()
            for path in parser.assets:
                read(path, script=True)
                if time.monotonic() >= deadline:
                    break
        except Exception:
            _failure(result, origin + TARGET_REPORT, 'parse_failed')
            result['target_page']['failure_category'] = 'parse_failed'
        for path in REPORT_ROUTE_PROBES:
            if time.monotonic() >= deadline:
                break
            metadata = {}
            page = read(path, verification=metadata)
            if page is None:
                continue
            try:
                labels = ReportLabels()
                labels.feed(page)
                labels.close()
                expected = 'Daily Analysis' if 'daily' in path else 'Workstations'
                if labels.auth_page or expected not in labels.labels:
                    continue
                result['verified_report_routes'].append(dict(
                    path=path, http_status_class=metadata['http_status_class'],
                    labels_found=sorted(labels.labels), content_type=metadata['content_type']))
                verified = VerifiedMetadata(origin + path, result)
                verified.feed(page)
                verified.close()
            except Exception:
                _failure(result, origin + path, 'parse_failed')
    result['reason'] = None if result['status'] == 'ok' else result['notes'][-1]
    return result
