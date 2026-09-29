"""Fixed report probes use synthetic transport; no live configuration or data."""
import json
import unittest
from unittest.mock import Mock, patch

import epoptia_read
import report_discovery as discovery
from tests.test_report_discovery import BASE, response


class VerifiedReportRoutesTests(unittest.TestCase):
    def discover(self, pages):
        session = Mock()
        session.get.side_effect = [response('<title>Production Data</title>'), *pages]
        result = discovery.discover_workorder_reports(
            BASE, session, html_get=epoptia_read._web_html_get)
        return result, session

    def test_fixed_gets_labels_and_safe_bounded_inline_metadata(self):
        pages = []
        for path in discovery.REPORT_ROUTE_PROBES:
            label = 'reports_dailyanalysis' if 'daily' in path else 'Workstations'
            pages.append(response(f'''<title>{label}</title>
                <form action="/reports/filter?date=PRIVATE" method="post">
                <input name="start_date" value="PRIVATE">
                <input name="password" value="PRIVATE"></form>
                <script src="/js/never.js"></script><script>
                const endpoint = '/api/reports?end=PRIVATE';
                fetch('/reports/dynamic/' + value);
                fetch('https://outside.invalid/reports');
                </script>''' + ''.join(
                    f'<script>fetch("/reports/sample-{i}");</script>' for i in range(40)),
                status=201, content_type='text/html; private=PRIVATE'))
        result, session = self.discover(pages)
        self.assertEqual([call.args[0] for call in session.get.call_args_list],
                         [BASE + discovery.TARGET_REPORT] +
                         [BASE + path for path in discovery.REPORT_ROUTE_PROBES])
        self.assertEqual(len(result['verified_report_routes']), 7)
        for route in result['verified_report_routes']:
            self.assertEqual(set(route), {'path', 'http_status_class', 'labels_found', 'content_type'})
            self.assertEqual(route['http_status_class'], '2xx')
            self.assertEqual(route['content_type'], 'html')
        self.assertEqual(len(result['endpoint_candidates']), 30)
        self.assertIn(dict(path='/reports/filter', method='POST',
                           parameter_names=['date', 'start_date'], evidence_kind='form'),
                      result['endpoint_candidates'])
        self.assertIn('/api/reports', [e['path'] for e in result['endpoint_candidates']])
        for value in ('PRIVATE', 'outside.invalid', 'password', '?', '/reports/dynamic/'):
            self.assertNotIn(value, json.dumps(result))
        for call in session.get.call_args_list:
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertTrue(call.kwargs['stream'])
            self.assertLessEqual(call.kwargs['timeout'], 3)
        session.post.assert_not_called()

    def test_reject_status_wrong_labels_login_and_redirects(self):
        pages = [response('<title>Daily Analysis</title>', 302),
                 response('<title>Daily Analysis</title>', 404),
                 response('<title>Workstations summary</title>'),
                 response('<title>Workstations</title><input type="password">'),
                 response('<title>Daily Analysis</title><meta http-equiv="refresh" content="0;url=/login">'),
                 response('<title>Workstations</title>'),
                 response('<title>Workstations</title>')]
        pages[-1].url = BASE + '/login?private=PRIVATE'
        for page in pages:
            page.iter_content.return_value[0] += b'<form action="/reports/unverified"></form>'
        result, _ = self.discover(pages)
        self.assertEqual(result['verified_report_routes'], [])
        self.assertEqual(result['endpoint_candidates'], [])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_metadata_labels_and_non_html_body_limits(self):
        pages = [response('<meta name="title" content="reports_dailyanalysis">'),
                 response('<div data-i18n="reports_dailyanalysis"></div>'),
                 response('<title>Workstations</title>', content_type='application/json'),
                 response('x' * 501), response(''), response(''), response('')]
        with patch.object(discovery, 'MAX_TARGET_REPORT_BYTES', 500):
            result, _ = self.discover(pages)
        self.assertEqual(len(result['verified_report_routes']), 2)
        self.assertIn('body_limit', result['notes'])

    def test_deadline_stops_probes(self):
        session = Mock()
        session.get.return_value = response('<title>Production Data</title>')
        with patch.object(discovery.time, 'monotonic', side_effect=[0, 1, 2, 13]):
            result = discovery.discover_workorder_reports(
                BASE, session, html_get=epoptia_read._web_html_get)
        session.get.assert_called_once()
        self.assertEqual(result['verified_report_routes'], [])

    def test_history_login_form_and_script_only_labels_are_not_verification(self):
        pages = [response('<title>Daily Analysis</title>'),
                 response('<form action="/login"><h1>Daily Analysis</h1></form>'),
                 response('<script>const title="reports_workstations";</script>'),
                 response('<h1>NotWorkstations</h1>'),
                 response('<h1>reports_dailyanalysis_extra</h1>'),
                 response('<h1>Daily Analysis</h1>', 503),
                 response('<h1>Workstations</h1>', 401)]
        pages[0].history = [response('', 302)]
        result, session = self.discover(pages)
        self.assertEqual(result['verified_report_routes'], [])
        self.assertEqual(result['endpoint_candidates'], [])
        self.assertEqual(session.get.call_count, 8)
