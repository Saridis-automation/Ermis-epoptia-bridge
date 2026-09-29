"""Synthetic-only coverage; never load service settings or use a network."""
import ast
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import epoptia_read
import epoptia_throttle
import report_discovery as discovery


BASE = 'https://example.invalid'


def response(html, status=200, content_type='text/html'):
    result = Mock(status_code=status, headers={'Content-Type': content_type})
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.iter_content.return_value = [html.encode()]
    return result


class DiscoveryTests(unittest.TestCase):
    def test_static_navigation_slugs_are_visible_only_under_safe_prefixes(self):
        for path in ('/reports/daily/workstations', '/reports/weekly/station_totals-v2',
                     '/workorders/history/weekly', '/workstations/utilization'):
            with self.subTest(path=path):
                self.assertEqual(discovery._path(BASE, path + '?date=PRIVATE#PRIVATE'), path)
                self.assertEqual(discovery._path(BASE, BASE + path), path)
        for path in ('/api/reports/weekly', '/reports-other/weekly',
                     '/workorders/weekly', '/workorders/history-other/weekly'):
            with self.subTest(path=path):
                self.assertTrue(discovery._path(BASE, path).endswith('/:redacted'))

    def test_navigation_ids_and_unsafe_segments_stay_redacted(self):
        for segment in ('123', 'abcdef0123456789abcdef0123456789',
                        'abcdef12-3456-7890-abcd-ef1234567890', 'abcdef12',
                        'a' * 25, 'opaqueToken12345678901234567890',
                        'person@example.invalid', 'person%40example.invalid',
                        '%64aily', 'daily;private', 'PRIVATE_PATH', 'delete',
                        'delete-all', 'auth_token'):
            for prefix in discovery.STATIC_PREFIXES:
                with self.subTest(segment=segment, prefix=prefix):
                    self.assertEqual(discovery._path(BASE, prefix + segment),
                                     prefix + ':redacted')
        self.assertEqual(discovery._path(BASE, '/reports/123/abcdef0123456789abcdef'),
                         '/reports/:redacted/:redacted')
        for raw in ('https://outside.invalid/reports/weekly',
                    'https://synthetic:synthetic@example.invalid/reports/weekly',
                    '/reports/weekly\\private', '/reports/weekly\nprivate',
                    '/reports/' + 'weekly/' * 12, '/reports/' + 'a' * 2048):
            with self.subTest(raw=raw):
                self.assertIsNone(discovery._path(BASE, raw))

    def test_newly_visible_slugs_do_not_expand_discovery_requests(self):
        session = Mock()
        session.get.return_value = response('<html></html>')
        result = discovery.discover(BASE, session, initial_html=(
            '<a href="/reports/weekly/station_totals">Report</a>'
            '<a href="/reports/daily/workstations">Report</a>'))
        self.assertIn(dict(path='/reports/weekly/station_totals', evidence_kind='href'),
                      result['route_candidates'])
        self.assertEqual([call.args[0] for call in session.get.call_args_list],
                         [BASE + '/reports/daily/workstations'])

    def test_progress_reuses_parent_html_and_session(self):
        html = ('<a href="/completedtoday?date=PRIVATE">Today</a>'
                '<script>fetch("/detailedreport?workorder_id=PRIVATE");</script>')
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.side_effect = [Mock(status_code=200, text=html),
                                   response('<script>fetch("/reports/factory/productiondata?workorder_id=PRIVATE");</script>'),
                                   *(response('<title>Unrecognized</title>') for _ in discovery.REPORT_ROUTE_PROBES)]
        progress = dict(native_progress=42, native_progress_verified=True, sources=[])
        with patch.object(epoptia_read.requests, 'Session', return_value=session), \
                patch.object(epoptia_read, '_web_login', return_value=True) as login, \
                patch.object(epoptia_read, '_inspect_workorder_progress',
                             return_value=progress.copy()), \
                patch.object(discovery, 'discover_workorder_reports', wraps=discovery.discover_workorder_reports) as discover, \
                patch('actual_completion.parse_actual_completion',
                      return_value={'date': None, 'reason': 'label_missing'}) as completion:
            result = epoptia_read.inspect_workorder_progress(
                BASE, {}, 722, username='synthetic', password='synthetic')
        login.assert_called_once_with(session, BASE, 'synthetic', 'synthetic')
        completion.assert_called_once_with(html, 722)
        self.assertIs(discover.call_args.kwargs['html_get'], epoptia_read._web_html_get)
        self.assertIs(discover.call_args.args[1], session)
        self.assertEqual([c.args[0] for c in session.get.call_args_list],
                         [BASE + '/workorders/722', BASE + discovery.TARGET_REPORT] +
                         [BASE + path for path in discovery.REPORT_ROUTE_PROBES])
        diagnostic = result.pop('report_discovery')
        self.assertEqual(diagnostic['status'], 'ok')
        self.assertIsNone(diagnostic['reason'])
        self.assertNotIn('PRIVATE', json.dumps(diagnostic))
        self.assertEqual(diagnostic['endpoint_candidates'][0]['parameter_names'], ['workorder_id'])
        self.assertEqual(result.pop('actual_production_completion'),
                         {'date': None, 'reason': 'label_missing'})
        self.assertEqual(result, progress)

    def test_seeded_discovery_follows_at_most_eight_pages(self):
        html = ''.join(f'<a href="/reports/{word}">Report</a>'
                       for word in sorted(discovery.WORDS))
        html += '<script>' + ''.join(f"fetch('/wolreport/{word}?date=PRIVATE');"
                                    for word in sorted(discovery.WORDS)) + '</script>'
        session = Mock()
        session.get.side_effect = lambda *a, **kw: response(html)
        result = discovery.discover(BASE, session, start_path='/workorders/722',
                                    initial_html=html, max_follow=100)
        self.assertEqual(session.get.call_count, 8)
        self.assertEqual(len(result['route_candidates']), 20)
        self.assertEqual(len(result['endpoint_candidates']), 30)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('722', json.dumps(result))

    def test_progress_discovery_failure_preserves_existing_result(self):
        for authenticated, status in ((False, 200), (True, 403), (True, 200)):
            with self.subTest(authenticated=authenticated, status=status):
                session = Mock()
                session.__enter__ = Mock(return_value=session)
                session.__exit__ = Mock(return_value=False)
                session.get.return_value = Mock(status_code=status, text='<html></html>')
                with patch.object(epoptia_read.requests, 'Session', return_value=session), \
                        patch.object(epoptia_read, '_web_login', return_value=authenticated), \
                        patch.object(epoptia_read, '_inspect_workorder_progress',
                                     return_value={'native_progress': 42}), \
                        patch.object(discovery, 'discover_workorder_reports', side_effect=RuntimeError('PRIVATE')) as discover:
                    result = epoptia_read.inspect_workorder_progress(
                        BASE, {}, 722, username='synthetic', password='synthetic')
                self.assertEqual(result['native_progress'], 42)
                self.assertIn('actual_production_completion', result)
                self.assertTrue(result['report_discovery']['reason'])
                self.assertNotIn('PRIVATE', json.dumps(result))
                if not authenticated or status != 200:
                    discover.assert_not_called()
                if not authenticated:
                    session.get.assert_not_called()

    def test_safe_metadata_only_and_same_origin(self):
        html = '''<a href="/reports/daily?token=PRIVATE_QUERY">Daily report PRIVATE_TEXT</a>
          <a href="https://outside.invalid/reports">reports</a>
          <a href="//outside.invalid/logs">logs</a>
          <a href="/reports/PRIVATE_PATH">reports</a>
          <a href="/reports/delete">reports</a>
          <a href="/reports/%64elete">reports</a>
          <script src="/scripts/reports.js?token=PRIVATE_SCRIPT"></script>
          <form action="/reports/data?date=PRIVATE_DATE" method="POST">
            <input name="date" value="PRIVATE_VALUE">
            <input name="_token" value="PRIVATE_TOKEN">
            <input name="PRIVATE_NAME" value="PRIVATE_VALUE"></form>
          <script>
            fetch('/api/reports?start=PRIVATE_START', {method: 'POST'});
            axios.get('/reports/history?end=PRIVATE_END', {params: {page: 2, token: 'PRIVATE'}});
            xhr.open('POST', '/reports/logs?date=PRIVATE_DATE');
            $.ajax({url: '/reports/today', type: 'GET', data: {date: 'PRIVATE', token: 'PRIVATE'}});
            fetch('https://outside.invalid/reports');
          </script>PRIVATE_USER_DATA'''
        session = Mock()
        session.get.side_effect = [response(html), response('')]
        result = discovery.discover(BASE, session)
        rendered = json.dumps(result)
        self.assertNotIn('PRIVATE', rendered)
        self.assertNotIn('outside.invalid', rendered)
        self.assertNotIn('_token', rendered)
        self.assertNotIn('?', rendered)
        self.assertEqual(result['landing_paths'], ['/workorders', '/reports/daily'])
        self.assertEqual(session.get.call_count, 2)
        for call in session.get.call_args_list:
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertTrue(call.kwargs['stream'])
            self.assertLessEqual(call.kwargs['timeout'], 3)
        session.post.assert_not_called()
        self.assertIn(dict(path='/reports/data', method='POST', parameter_names=['date'],
                           evidence_kind='form'), result['endpoint_candidates'])
        self.assertEqual({e['evidence_kind'] for e in result['endpoint_candidates']},
                         {'form', 'fetch', 'axios', 'xhr', 'ajax'})
        self.assertEqual(next(e['parameter_names'] for e in result['endpoint_candidates']
                              if e['evidence_kind'] == 'axios'), ['end', 'page'])
        self.assertEqual(next(e['parameter_names'] for e in result['endpoint_candidates']
                              if e['evidence_kind'] == 'ajax'), ['date'])

    def test_output_and_request_bounds(self):
        words = sorted(discovery.WORDS)
        html = ''.join(f'<a href="/reports/{word}">Report</a>' for word in words)
        html += '<script>' + ''.join(f"fetch('/api/reports/{word}?page=hidden');"
                                    for word in words) + '</script>'
        session = Mock()
        session.get.side_effect = lambda *a, **kw: response(html)
        result = discovery.discover(BASE, session)
        self.assertEqual(len(result['route_candidates']), 20)
        self.assertEqual(len(result['endpoint_candidates']), 30)
        self.assertEqual(session.get.call_count, 4)
        self.assertEqual(len(result['landing_paths']), 4)

    def test_redirect_http_and_non_html_are_not_parsed(self):
        for status, mime in ((302, 'text/html'), (200, 'application/json')):
            with self.subTest(status=status, mime=mime):
                session = Mock()
                session.get.return_value = response('<a href="/reports">report</a>', status, mime)
                result = discovery.discover(BASE, session)
                self.assertEqual(result['status'], 'partial')
                self.assertEqual(result['route_candidates'], [])
                session.get.assert_called_once()

    def test_forbidden_page_halts_discovery(self):
        session = Mock()
        session.get.return_value = response('<a href="/reports">report</a>', 403, 'text/html')
        self.addCleanup(epoptia_throttle.default().clear)
        result = discovery.discover(BASE, session)
        self.assertEqual(result['route_candidates'], [])
        session.get.assert_called_once()
        self.assertEqual(epoptia_throttle.halted()['http_status'], 403)

    def test_size_time_and_exception_fail_closed(self):
        session = Mock()
        session.get.return_value = response('x' * (discovery.MAX_BYTES + 1))
        self.assertEqual(discovery.discover(BASE, session)['status'], 'unavailable')
        session.get.side_effect = RuntimeError('PRIVATE_EXCEPTION')
        self.assertNotIn('PRIVATE', json.dumps(discovery.discover(BASE, session)))
        session.reset_mock()
        with patch.object(discovery.time, 'monotonic', side_effect=[0, 13, 13]):
            self.assertEqual(discovery.discover(BASE, session)['status'], 'unavailable')
        session.get.assert_not_called()

    def test_follow_failure_preserves_sanitized_landing_metadata(self):
        session = Mock()
        session.get.side_effect = [response('<a href="/reports/daily">Report</a>'),
                                   RuntimeError('PRIVATE_RESPONSE')]
        result = discovery.discover(BASE, session)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['landing_paths'], ['/workorders'])
        self.assertEqual(result['route_candidates'][0]['path'], '/reports/daily')
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_large_parent_is_sampled_without_refetch(self):
        html = ('<a href="/reports/daily?date=PRIVATE">Report</a>'
                '<script>const route = "/detailedreport?date=PRIVATE";</script>'
                '<form action="/wolreport" method="post"><input name="date"></form>'
                + 'x' * discovery.MAX_BYTES)
        session = Mock()
        result = discovery.discover(BASE, session, start_path='/workorders/722',
                                    initial_html=html, max_follow=0)
        session.get.assert_not_called()
        self.assertEqual(result['landing_paths'], ['/workorders/:redacted'])
        self.assertEqual({r['path'] for r in result['route_candidates']},
                         {'/reports/daily', '/detailedreport', '/wolreport'})
        self.assertIn('parent_body_truncated', result['notes'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_follow_failures_are_safe_and_do_not_stop_later_follows(self):
        paths = ['/reports/daily', '/reports/history', '/reports/logs',
                 '/completedtoday', '/workhours']
        html = ''.join(f'<a href="{p}?date=PRIVATE">Report</a>' for p in paths)
        session = Mock()
        redirected = response('PRIVATE', 302)
        redirected.headers['Location'] = '/login?token=PRIVATE'
        session.get.side_effect = [response('PRIVATE', 503), redirected,
                                   response('<html>PRIVATE</html>'), RuntimeError('PRIVATE'),
                                   response('<script>fetch("/api/reports?date=PRIVATE", '
                                            '{method: "POST"});</script>')]
        result = discovery.discover(BASE, session, start_path='/workorders/722',
                                    initial_html=html, max_follow=8)
        self.assertEqual([c.args[0] for c in session.get.call_args_list],
                         [BASE + p for p in paths])
        self.assertEqual({r['path'] for r in result['route_candidates']} & set(paths), set(paths))
        self.assertEqual([f['reason'] for f in result['follow_failures']],
                         ['http_status_class', 'auth_redirect', 'parse_empty', 'request_failed'])
        self.assertEqual(result['follow_failures'][0]['http_status_class'], '5xx')
        self.assertEqual(result['endpoint_candidates'][0], dict(
            path='/api/reports', method='POST', parameter_names=['date'], evidence_kind='fetch'))
        self.assertEqual(result['landing_paths'][0], '/workorders/:redacted')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('?', json.dumps(result))
        session.post.assert_not_called()

    def test_malformed_route_does_not_discard_parent_metadata(self):
        session = Mock()
        result = discovery.discover(BASE, session, initial_html=(
            '<a href="https://[invalid/reports">Report</a>'
            '<a href="/reports/daily">Report</a>'), max_follow=0)
        self.assertEqual(result['route_candidates'][0]['path'], '/reports/daily')
        session.get.assert_not_called()

    def test_existing_reader_authentication_session_is_reused(self):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        with patch.object(epoptia_read.requests, 'Session', return_value=session), \
                patch.object(epoptia_read, '_web_login', return_value=True) as login, \
                patch.object(discovery, 'discover', return_value={'status': 'ok'}) as discover:
            self.assertEqual(discovery.runtime_discovery(BASE, 'synthetic', 'synthetic'), {'status': 'ok'})
            login.assert_called_once_with(session, BASE, 'synthetic', 'synthetic')
            discover.assert_called_once_with(BASE, session)
            login.return_value = False
            discover.reset_mock()
            self.assertEqual(discovery.runtime_discovery(BASE, 'synthetic', 'synthetic')['status'],
                             'authentication_unavailable')
            discover.assert_not_called()

    def test_mcp_enrichment_non_regression_and_failure_isolation(self):
        tree = ast.parse(Path('mcp_server.py').read_text())
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in ('_read_query', 'get_wol_details')]
        for node in nodes:
            node.decorator_list = []
        scope = dict(epoptia_read=epoptia_read, BASE_URL=BASE, HEADERS={},
                     WEB_USERNAME='synthetic', WEB_PASSWORD='synthetic')
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'mcp_server.py', 'exec'), scope)
        rows = [{'workorderline_id': 3168, 'description': 'Synthetic item'}]
        expected = dict(ok=True, **epoptia_read.wol_details(rows, 3168))
        with patch.object(epoptia_read, 'fetch_wols', return_value=rows) as fetch, \
                patch.object(discovery, 'runtime_discovery', return_value={'status': 'ok'}) as diagnostic:
            result = scope['get_wol_details'](3168)
            self.assertEqual(result.pop('report_discovery'), {'status': 'ok'})
            self.assertEqual(result, expected)
            diagnostic.assert_called_once_with(BASE, 'synthetic', 'synthetic')
            diagnostic.side_effect = RuntimeError('PRIVATE_FAILURE')
            result = scope['get_wol_details'](3168)
            self.assertEqual(result.pop('report_discovery')['status'], 'discovery_unavailable')
            self.assertEqual(result, expected)
            diagnostic.reset_mock()
            fetch.reset_mock()
            self.assertFalse(scope['get_wol_details'](0)['ok'])
            fetch.assert_not_called()
            diagnostic.assert_not_called()


if __name__ == '__main__':
    unittest.main()
