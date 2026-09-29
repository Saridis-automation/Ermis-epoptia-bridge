"""Targeted discovery uses synthetic pages and mocked transport only."""
import json
import unittest
from unittest.mock import Mock, patch

import epoptia_read
import report_discovery as discovery
from tests.test_report_discovery import BASE, response


class TargetReportTests(unittest.TestCase):
    def setUp(self):
        # Isolate existing target/asset coverage from the new fixed probes.
        probes = patch.object(discovery, 'REPORT_ROUTE_PROBES', ())
        probes.start()
        self.addCleanup(probes.stop)

    def discover(self, html, assets=()):
        session = Mock()
        session.get.side_effect = [response(html), *assets]
        result = discovery.discover_workorder_reports(
            BASE, session, html_get=epoptia_read._web_html_get)
        return result, session

    def test_target_siblings_forms_inline_and_script_metadata(self):
        html = '''<a href="/reports/factory/dailyanalysis?date=PRIVATE">Daily</a>
        <a href="/reports/operator/workstations">Workstations</a>
        <a href="/reports/weekly">Daily analysis</a>
        <a href="/reports/factory/123/abcdef12-3456-7890-abcd-ef1234567890">Factory</a>
        <a href="https://outside.invalid/reports/daily">Daily</a>
        <form action="/reports/factory/productiondata?date=PRIVATE" method="post">
          <input name="start_date" value="PRIVATE"><input name="_token" value="PRIVATE">
        </form><script>
          fetch('/reports/daily', {method: 'POST', data: {date: 'PRIVATE'}});
          axios.get('/reports/workstations', {params: {page: 1}});
          xhr.open('GET', '/reports/operator');
          $.ajax({url: '/reports/factory', type: 'POST', data: {end: 'PRIVATE'}});
        </script>
        <script src="/assets/reports.js?token=PRIVATE"></script>
        <script src="https://outside.invalid/reports.js"></script>PRIVATE_USER'''
        js = '''const endpoint = '/api/factory/productiondata?date=PRIVATE';
          fetch('/reports/dailyanalysis?start=PRIVATE'); fetch('/api/list');
          fetch('https://outside.invalid/reports');'''
        result, session = self.discover(html, [response(js, content_type='text/javascript')])
        self.assertEqual([c.args[0] for c in session.get.call_args_list],
                         [BASE + discovery.TARGET_REPORT, BASE + '/assets/reports.js'])
        for call in session.get.call_args_list:
            self.assertEqual(call.kwargs['headers'], {'Accept': 'text/html'})
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertTrue(call.kwargs['stream'])
        paths = {r['path'] for r in result['route_candidates']}
        self.assertIn('/reports/factory/dailyanalysis', paths)
        self.assertIn('/reports/operator/workstations', paths)
        self.assertIn('/reports/weekly', paths)
        self.assertIn('/reports/factory/:redacted/:redacted', paths)
        self.assertIn(dict(path='/reports/factory/productiondata', method='POST',
                           parameter_names=['date', 'start_date'], evidence_kind='form'),
                      result['endpoint_candidates'])
        self.assertIn('/api/factory/productiondata', {e['path'] for e in result['endpoint_candidates']})
        self.assertNotIn('/api/list', {e['path'] for e in result['endpoint_candidates']})
        self.assertEqual(result['script_src_paths'], ['/assets/reports.js'])
        self.assertEqual(result['target_page'], dict(path=discovery.TARGET_REPORT,
                         http_status_class='2xx', content_type='html', failure_category=None))
        for private in ('PRIVATE', 'outside.invalid', '_token', '?', 'abcdef12', '123'):
            self.assertNotIn(private, json.dumps(result))
        session.post.assert_not_called()

    def test_eight_assets_thirty_endpoints_no_navigation_or_recursive_scripts(self):
        html = '<a href="/reports/daily">Daily</a>' + ''.join(
            f'<script src="/assets/bundle{i}.js"></script>' for i in range(15))
        js = ''.join(f"fetch('/reports/daily/{i}');" for i in range(50))
        # Distinct parameter metadata still stops at the endpoint cap.
        js += ''.join(f"fetch('/reports/{word}');" for word in sorted(discovery.WORDS))
        result, session = self.discover(html, [response(js, content_type='application/javascript') for _ in range(10)])
        self.assertEqual(session.get.call_count, 9)
        self.assertEqual(len(result['endpoint_candidates']), 30)
        self.assertEqual(result['landing_paths'], [discovery.TARGET_REPORT])

    def test_safe_target_failure_categories(self):
        for status, mime, failure in ((302, 'text/html', 'auth_redirect'),
                                     (403, 'text/html', 'authentication_failed'),
                                     (503, 'application/json', 'http_error'),
                                     (200, 'private/mime; token=PRIVATE', 'unexpected_content_type')):
            with self.subTest(status=status):
                session = Mock()
                session.get.return_value = response('PRIVATE', status, mime)
                result = discovery.discover_workorder_reports(BASE, session, html_get=epoptia_read._web_html_get)
                self.assertEqual(result['target_page']['failure_category'], failure)
                self.assertEqual(result['target_page']['http_status_class'], f'{status // 100}xx')
                self.assertNotIn('PRIVATE', json.dumps(result))
                session.get.assert_called_once()
        for error, failure in ((RuntimeError('PRIVATE'), 'request_failed'),
                               (epoptia_read.requests.Timeout('PRIVATE'), 'timeout')):
            session.get.side_effect = error
            result = discovery.discover_workorder_reports(BASE, session, html_get=epoptia_read._web_html_get)
            self.assertEqual(result['target_page']['failure_category'], failure)
            self.assertIsNone(result['target_page']['http_status_class'])
            self.assertNotIn('PRIVATE', json.dumps(result))

    def test_inline_dynamic_urls_are_not_literal_endpoints(self):
        result, session = self.discover('''<script>
          fetch('/reports/daily/' + value);
          axios.get('/reports/factory/' + value);
          xhr.open('GET', '/reports/operator/' + value);
          $.ajax({url: '/reports/workstations/' + value});
        </script>''')
        self.assertEqual(result['endpoint_candidates'], [])
        session.get.assert_called_once()

    def test_large_report_parses_safe_metadata_through_hard_cap(self):
        metadata = '''<a href="/reports/daily?date=PRIVATE">Daily</a>
          <form action="/reports/factory/productiondata" method="post">
            <input name="date" value="PRIVATE"></form>
          <script>fetch('/reports/operator?date=PRIVATE');</script>
          <script src="https://outside.invalid/reports.js"></script>'''
        for size in (discovery.MAX_BYTES + 1, discovery.MAX_TARGET_REPORT_BYTES):
            with self.subTest(size=size):
                body = (' ' * (size - len(metadata.encode())) + metadata).encode()
                page = response('')
                page.iter_content.return_value = (
                    body[i:i + 8192] for i in range(0, len(body), 8192))
                session = Mock()
                session.get.return_value = page
                result = discovery.discover_workorder_reports(
                    BASE, session, html_get=epoptia_read._web_html_get)
                self.assertIsNone(result['target_page']['failure_category'])
                self.assertEqual(result['landing_paths'], [discovery.TARGET_REPORT])
                self.assertIn('/reports/daily', {r['path'] for r in result['route_candidates']})
                self.assertEqual({e['path'] for e in result['endpoint_candidates']},
                                 {discovery.TARGET_REPORT, '/reports/operator'})
                self.assertNotIn('PRIVATE', json.dumps(result))
                self.assertNotIn('outside.invalid', json.dumps(result))
                self.assertLess(len(json.dumps(result)), 2048)
                session.get.assert_called_once()
                page.__exit__.assert_called_once()

    def test_over_cap_report_fails_without_metadata_or_asset_requests(self):
        prefix = b'<script src="/assets/reports.js"></script>'
        for chunked in (False, True):
            with self.subTest(chunked=chunked):
                # Multibyte padding verifies the cap counts bytes, not characters.
                body = prefix + ('\u00e9' * (discovery.MAX_TARGET_REPORT_BYTES // 2)).encode()
                page = response('')
                page.iter_content.return_value = (
                    (body[i:i + 8192] for i in range(0, len(body), 8192))
                    if chunked else [body])
                session = Mock()
                session.get.return_value = page
                result = discovery.discover_workorder_reports(
                    BASE, session, html_get=epoptia_read._web_html_get)
                self.assertEqual(result['target_page']['failure_category'], 'body_limit')
                for key in ('landing_paths', 'route_candidates', 'endpoint_candidates', 'script_src_paths'):
                    self.assertEqual(result[key], [])
                session.get.assert_called_once()
                page.__exit__.assert_called_once()

    def test_script_byte_cap_stops_stream_and_closes_response(self):
        asset = response('', content_type='text/javascript')
        consumed = []
        def chunks():
            for chunk in (b"fetch('/reports/daily');", b'x' * 100, b"fetch('/reports/history');"):
                consumed.append(True)
                yield chunk
        asset.iter_content.return_value = chunks()
        with patch.object(discovery, 'MAX_SCRIPT_BYTES', 64):
            result, session = self.discover('<script src="/js/report-view.js"></script>', [asset])
        self.assertIsNone(result['target_page']['failure_category'])
        self.assertEqual(result['follow_failures'][0]['reason'], 'body_limit')
        self.assertEqual(result['follow_failures'][0]['path'], '/js/report-view.js')
        self.assertEqual([e['path'] for e in result['endpoint_candidates']], ['/reports/daily'])
        self.assertEqual(len(consumed), 2)
        asset.__exit__.assert_called_once()
        self.assertEqual(session.get.call_count, 2)

    def test_static_js_display_redacts_values_and_leaves_generic_paths_unchanged(self):
        for name in ('app.js', 'production-data.js', 'factory/daily-analysis.min.js'):
            path = '/js/' + name
            self.assertEqual(discovery._script_path(BASE, path + '?token=PRIVATE#PRIVATE'), path)
        for name in ('123.js', 'app.123.js', 'app.abcdef123456.js',
                     'abcdef12-3456-7890-abcd-ef1234567890.js',
                     'opaqueToken123.js', 'a' * 49 + '.js', 'auth-token.js', '%61pp.js'):
            self.assertEqual(discovery._script_path(BASE, '/js/' + name), '/js/:redacted')
        self.assertEqual(discovery._path(BASE, '/js/app.js'), '/js/:redacted')
        self.assertIsNone(discovery._script_path(BASE, 'https://outside.invalid/js/app.js'))

    def test_stream_search_after_old_limit_across_every_token_boundary(self):
        source = '''fetch('/api/productiondata?date=PRIVATE', {
            method: 'POST', params: {start_date: 'PRIVATE', page: 7, token: 'PRIVATE'}});
          axios.get('/reports/workstations', {params: {end_date: 'PRIVATE'}});
          xhr.open('GET', '/reports/operator');
          const route = '/reports/history'; const log = '/reports/logs';
          fetch('/reports/daily/' + variable);
          const external = 'https://outside.invalid/reports/daily';
          const irrelevant = '/api/list';
          // fetch('/reports/comment');
          /* '/reports/commentblock'; */
          const template = `/reports/template/${value}`;
          const escaped = '/reports/escape\\x64';
          const regex = /["']/reports/regex/;
        '''
        asset = response('', content_type='application/javascript')
        asset.iter_content.return_value = iter([b'/*' + b'x' * (discovery.MAX_BYTES + 1) + b'*/;',
                                              *(c.encode() for c in source)])
        result, session = self.discover('<script src="/js/app.js?token=PRIVATE"></script>', [asset])
        endpoints = {e['path']: e for e in result['endpoint_candidates']}
        self.assertEqual(set(endpoints), {'/api/productiondata', '/reports/workstations',
                                        '/reports/operator', '/reports/history', '/reports/logs'})
        self.assertEqual(endpoints['/api/productiondata']['method'], 'POST')
        self.assertEqual(endpoints['/api/productiondata']['parameter_names'], ['date', 'page', 'start_date'])
        self.assertEqual(endpoints['/reports/workstations']['method'], 'GET')
        self.assertEqual(endpoints['/reports/workstations']['parameter_names'], ['end_date'])
        self.assertEqual(endpoints['/reports/operator']['method'], 'GET')
        self.assertIsNone(endpoints['/reports/history']['method'])
        for value in ('PRIVATE', 'outside.invalid', 'fetch(', 'variable', 'token', '?'):
            self.assertNotIn(value, json.dumps(result))
        self.assertEqual(session.get.call_args.args[0], BASE + '/js/app.js')
        asset.__exit__.assert_called_once()

    def test_script_at_sixteen_mib_limit_and_no_recursive_requests(self):
        end = b"*/; const next='/js/another.js'; fetch('/reports/history');"
        size = discovery.MAX_SCRIPT_BYTES
        self.assertEqual(size, 16 * 1024 * 1024)
        asset = response('', content_type='text/javascript')
        padding = size - len(end) - 2
        def chunks():
            yield b'/*'
            for _ in range(padding // 8192):
                yield b'x' * 8192
            yield b'x' * (padding % 8192)
            yield end
        asset.iter_content.return_value = chunks()
        result, session = self.discover('<script src="/js/app.js"></script>', [asset])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual([e['path'] for e in result['endpoint_candidates']], ['/reports/history'])
        self.assertEqual(session.get.call_count, 2)

    def test_script_metadata_does_not_guess_dynamic_methods_or_nested_parameter_names(self):
        js = '''fetch('/reports/daily', {method: 'POST' + variable,
                    params: {filter: {date: 'PRIVATE'}, [variable]: 'PRIVATE'},
                    data: {method: 'GET'}});
                fetch('/reports/history', options);
                fetch('/reports/operator', {method: 'DELETE'});
                $.ajax({url: '/reports/factory?date=PRIVATE', type: 'POST',
                        data: {'start_date': 'PRIVATE'}});
                const value = 'PRIVATE';
                const route = '/api/history/123/abcdef12-3456-7890-abcd-ef1234567890?token=PRIVATE';'''
        result, _ = self.discover('<script src="/js/app.js"></script>',
                                 [response(js, content_type='text/javascript')])
        endpoints = {e['path']: e for e in result['endpoint_candidates']}
        for path in ('/reports/daily', '/reports/history', '/reports/operator'):
            self.assertIsNone(endpoints[path]['method'])
        self.assertEqual(endpoints['/reports/daily']['parameter_names'], ['filter'])
        self.assertEqual(endpoints['/reports/factory']['method'], 'POST')
        self.assertEqual(endpoints['/reports/factory']['parameter_names'], ['date', 'start_date'])
        self.assertIn('/api/history/:redacted/:redacted', endpoints)
        for private in ('PRIVATE', 'variable', '123', 'abcdef12', 'DELETE'):
            self.assertNotIn(private, json.dumps(result))

    def test_script_failure_preserves_page(self):
        result, _ = self.discover('<a href="/reports/daily">Daily</a><script src="/assets/reports.js"></script>',
                                  [response('PRIVATE', 302)])
        self.assertIsNone(result['target_page']['failure_category'])
        self.assertEqual(result['follow_failures'][0]['reason'], 'auth_redirect')
        self.assertEqual(result['route_candidates'][0]['path'], '/reports/daily')

    def test_deadline_and_invalid_origin_issue_no_requests(self):
        session = Mock()
        with patch.object(discovery.time, 'monotonic', side_effect=[0, 13]):
            result = discovery.discover_workorder_reports(BASE, session, html_get=epoptia_read._web_html_get)
        self.assertEqual(result['target_page']['failure_category'], 'timeout')
        self.assertEqual(discovery.discover_workorder_reports('file:///private', session,
                         html_get=epoptia_read._web_html_get)['status'], 'invalid_origin')
        session.get.assert_not_called()
