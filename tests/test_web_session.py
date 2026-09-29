"""Synthetic HTTP transport; never load credentials or contact Epoptia."""
import ast
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import requests
from requests.adapters import BaseAdapter
import epoptia_read as read


class BrowserTransport(BaseAdapter):
    def __init__(self, case, failure=None):
        self.case = case
        self.failure = failure
        self.calls = []

    def send(self, request, **kwargs):
        self.calls.append(request)
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = 200
        response._content = b''
        if request.url.endswith('/login') and request.method == 'GET':
            response._content = b'<input value="synthetic&amp;token" name="_token" type="hidden">'
            if self.failure == 'missing_token':
                response._content = b'<html></html>'
            # Set cookies on the actual session, as a real HTTP response would.
            self.session.cookies.set('session', 'synthetic-session', domain='example.invalid', path='/')
        elif request.url.endswith('/login'):
            self.case.assertEqual(request.headers['Content-Type'], 'application/x-www-form-urlencoded')
            self.case.assertEqual(parse_qs(request.body), {
                '_token': ['synthetic&token'], 'username': ['synthetic-user'],
                'password': ['synthetic-password']})
            self.case.assertIn('session=synthetic-session', request.headers['Cookie'])
            if self.failure == 'login_419':
                response.status_code = 419
            elif self.failure == 'login_page':
                response._content = b'<input type="hidden" name="_token" value="synthetic">'
            else:
                self.session.cookies.set('XSRF-TOKEN', 'synthetic%2Bcsrf%3D', domain='example.invalid', path='/')
                response.status_code = 302
                response.headers['Location'] = '/capacity-planning'
        elif request.url.endswith('/planning/calendar'):
            self.case.assertEqual(request.method, 'GET')
            self.case.assertIsNone(request.body)
            response.headers['Content-Type'] = 'text/html'
            response._content = (b'<div class="workorderWol" data-date="2026-09-21">'
                                 b'<div class="wolCardComponent" data-id="3112" data-workorder="722"></div></div>')
            response._content_consumed = True
        elif request.method == 'GET':
            response._content = b'<html>Capacity planning<input type="hidden" name="_token" value="synthetic"></html>'
            if self.failure == 'redirect_login_page':
                response._content = b'<input type="password" name="password">'
        else:
            self.case.assertEqual(request.url, 'https://example.invalid/capacity-planning/workorderlines')
            self.case.assertEqual(request.headers['Origin'], 'https://example.invalid')
            self.case.assertEqual(request.headers['Referer'], 'https://example.invalid/capacity-planning')
            self.case.assertEqual(request.headers['X-XSRF-TOKEN'], 'synthetic+csrf=')
            self.case.assertIn('session=synthetic-session', request.headers['Cookie'])
            self.case.assertIn('XSRF-TOKEN=', request.headers['Cookie'])
            self.case.assertNotIn('X-Auth-Token', request.headers)
            self.case.assertEqual(request.headers['Content-Type'], 'application/json')
            payload = json.loads(request.body)
            self.case.assertEqual(payload, {'onlyList': True, 'page': len(self.calls) - 3})
            response._content = json.dumps({'capacityPlanningData': {'productionData': [
                {'id': payload['page'], 'workorder': {
                    'id': 721 if payload['page'] == 1 else 722, 'progress': 11}}
            ]}}).encode()
            if self.failure == 'page_419':
                response.status_code = 419
        return response

    def close(self):
        pass


class WebSessionTests(unittest.TestCase):
    def probe(self, failure=None):
        transport = BrowserTransport(self, failure)
        session = requests.Session()
        session.trust_env = False
        session.mount('https://', transport)
        transport.session = session
        with patch.object(read.requests, 'Session', return_value=session):
            result = read.inspect_workorder_progress('https://example.invalid', {}, 722,
                username='synthetic-user', password='synthetic-password')
        return result, transport.calls

    def test_login_redirect_cookies_headers_and_pagination(self):
        result, calls = self.probe()
        self.assertTrue(result['native_progress_verified'])
        self.assertEqual(result['native_progress'], 11)
        self.assertEqual([r.method for r in calls], ['GET', 'POST', 'GET', 'POST', 'POST', 'GET', 'GET', 'GET'])
        self.assertEqual(calls[-1].url, 'https://example.invalid/planning/calendar')
        self.assertEqual(result['target_date'], '2026-09-21')
        calls = calls[:-1]
        self.assertEqual(calls[-2].url, 'https://example.invalid/workorders/722')
        self.assertEqual(calls[-1].url, 'https://example.invalid/reports/factory/productiondata')
        self.assertEqual(calls[-1].headers['Cookie'], calls[-2].headers['Cookie'])
        self.assertEqual(calls[-1].headers['Referer'], calls[-2].headers['Referer'])
        self.assertEqual(calls[-1].headers['Accept'], 'text/html')
        self.assertEqual(result['actual_production_completion']['reason'], 'label_missing')

    def test_login_failures_never_query_progress(self):
        for failure, count in [('missing_token', 1), ('login_419', 2), ('login_page', 2),
                               ('redirect_login_page', 3)]:
            result, calls = self.probe(failure)
            self.assertEqual(len(calls), count)
            self.assertEqual(result['sources'][0]['status'], 'login_failed')
            self.assertNotIn('synthetic', str(result))

    def test_upstream_419_fails_closed_even_with_headers(self):
        result, _ = self.probe('page_419')
        self.assertEqual(result['sources'][0]['http_status'], 419)
        self.assertFalse(result['native_progress_verified'])
        self.assertIsNone(result['native_progress'])

    def test_missing_credentials_do_not_request(self):
        with patch.object(read.requests.Session, 'request') as request:
            result = read.inspect_workorder_progress('https://example.invalid', {}, 722)
        request.assert_not_called()
        self.assertEqual(result['sources'][0]['status'], 'login_failed')

    def test_dotenv_precedes_credential_reads(self):
        tree = ast.parse(Path('mcp_server.py').read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        load = next(n.lineno for n in calls if isinstance(n.func, ast.Name) and n.func.id == 'load_dotenv')
        reads = [n.lineno for n in calls if isinstance(n.func, ast.Attribute) and n.func.attr == 'getenv']
        self.assertTrue(reads)
        self.assertTrue(all(load < line for line in reads))
