"""Synthetic native progress pages: no credential loading or live requests."""
import ast
import asyncio
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests
from mcp.server.mcpserver import MCPServer
import epoptia_read as read


class NativeProgressTests(unittest.TestCase):
    def setUp(self):
        self.session = requests.Session()
        self.session.get = Mock(return_value=Mock(status_code=200, text='<html></html>'))
        self.session.post = lambda *args, **kwargs: read.requests.post(*args, **kwargs)
        factory = patch.object(read.requests, 'Session', return_value=self.session)
        factory.start()
        self.addCleanup(factory.stop)
        login = patch.object(read, '_web_login', return_value=True)
        login.start()
        self.addCleanup(login.stop)

    def probe(self, pages, workorder_id=722):
        with patch.object(read.requests, 'post', side_effect=[Mock(
                status_code=200, json=Mock(return_value=data)) for data in pages]) as post:
            result = read.inspect_workorder_progress('https://example.invalid/', {}, workorder_id, username='synthetic', password='synthetic')
        for page, call in enumerate(post.call_args_list, 1):
            self.assertEqual(call.args, ('https://example.invalid/capacity-planning/workorderlines',))
            self.assertEqual(call.kwargs, dict(timeout=20,
                allow_redirects=False, json={'onlyList': True, 'page': page}))
        return result

    def row(self, ident=722, progress=11, wol=1):
        return {'id': wol, 'progress': 99, 'workorder': {'id': ident, 'progress': progress}}

    def grouped(self, production):
        return {'capacityPlanningData': {'productionData': production}}

    def test_grouped_page_scans_all_buckets_and_duplicates(self):
        result = self.probe([self.grouped({
            'past': [self.row(), self.row(wol=2)],
            'future': {'group': [[self.row('722', '11', 3)], self.row(721, 85)]},
            'empty': [],
        })])
        self.assertEqual(result['native_progress'], 11)
        self.assertTrue(result['native_progress_verified'])
        self.assertEqual(result['sources'][0]['linked_wol_records'], 3)
        self.assertEqual(result['sources'][0]['pages_read'], 1)
        self.assertTrue(result['sources'][0]['complete'])

    def test_grouped_conflicting_duplicates(self):
        result = self.probe([self.grouped({
            'past': [self.row()], 'another_bucket': [self.row(progress=12, wol=2)],
        })])
        self.assertEqual(result['sources'][0]['status'], 'conflicting_progress')
        self.assertIsNone(result['native_progress'])
        self.assertFalse(result['native_progress_verified'])

    def test_grouped_root_progress_cannot_replace_missing_nested_progress(self):
        result = self.probe([self.grouped({'past': [
            self.row(), {'id': 2, 'progress': 11, 'workorder': {'id': 722}},
        ]})])
        self.assertEqual(result['sources'][0]['status'], 'invalid_progress')
        self.assertFalse(result['native_progress_verified'])
        self.assertIsNone(result['native_progress'])

    def test_grouped_empty_or_unrelated(self):
        for production in ({}, {'past': []}, [self.row(721)],
                           {'past': [{'progress': 11, 'workorder': None}]}):
            result = self.probe([self.grouped(production), self.grouped({})])
            self.assertEqual(result['sources'][0]['status'], 'not_found')
            self.assertFalse(result['native_progress_verified'])

    def test_grouped_malformed_or_excessive_scan_is_unverified(self):
        for production in (None, 'private', {'past': [self.row(), 'private']},
                           [[]] * 100001):
            result = self.probe([self.grouped(production)])
            self.assertEqual(result['sources'][0]['status'], 'invalid_response')
            self.assertFalse(result['sources'][0]['complete'])
            self.assertFalse(result['native_progress_verified'])
            self.assertNotIn('private', str(result))

    def test_native_values_and_deduplication(self):
        for ident, value in ((676, 60.6), (721, 93.24), (722, 11), (999, 0)):
            result = self.probe([[self.row(ident, value), self.row(str(ident), str(value), 2)], []], ident)
            self.assertEqual(result['native_progress'], value)
            self.assertTrue(result['native_progress_verified'])
            self.assertEqual(result['sources'][0]['linked_wol_records'], 2)

    def test_metadata_pagination(self):
        result = self.probe([
            {'workorderLines': [self.row(721)], 'numberOfPages': 2},
            {'workorderLines': [self.row(722)], 'numberOfPages': 2}])
        self.assertEqual(result['native_progress'], 11)

    def test_duplicate_values_across_pages_without_wol_ids(self):
        row = {'workorder': {'id': 676, 'progress': 60.6}}
        result = self.probe([[row], [row], []], 676)
        self.assertEqual(result['native_progress'], 60.6)
        self.assertTrue(result['native_progress_verified'])

    def test_stops_at_first_matching_page(self):
        result = self.probe([[self.row()]])
        self.assertEqual(result['native_progress'], 11)
        self.assertEqual(result['sources'][0]['pages_read'], 1)

    def test_grouped_finds_later_page(self):
        result = self.probe([self.grouped({'past': [self.row(721)]}),
                             self.grouped({'future': {'group': [self.row()]}})])
        self.assertEqual(result['native_progress'], 11)
        self.assertTrue(result['native_progress_verified'])
        self.assertEqual(result['sources'][0]['pages_read'], 2)

    def test_grouped_exhaustion(self):
        for last in (self.grouped({}), self.grouped({'past': []}), []):
            result = self.probe([self.grouped({'past': [self.row(721)]}), last])
            self.assertEqual(result['sources'][0]['status'], 'not_found')
            self.assertEqual(result['sources'][0]['pages_read'], 2)
            self.assertTrue(result['sources'][0]['complete'])
            self.assertFalse(result['native_progress_verified'])

    def test_grouped_explicit_last_page(self):
        result = self.probe([dict(self.grouped([self.row(721)]), numberOfPages=1)])
        self.assertEqual(result['sources'][0]['status'], 'not_found')
        self.assertEqual(result['sources'][0]['pages_read'], 1)

    def test_identity_and_root_progress_ignored(self):
        result = self.probe([[self.row(721), {'id': 722, 'progress': 11},
                              self.row(True)], []])
        self.assertEqual(result['sources'][0]['status'], 'not_found')
        self.assertIsNone(result['native_progress'])

    def test_invalid_progress(self):
        for value in (None, True, -1, 101, float('nan'), float('inf'), 'private', {}):
            result = self.probe([[self.row(progress=value)], []])
            self.assertEqual(result['sources'][0]['status'], 'invalid_progress')
            self.assertIsNone(result['native_progress'])
            self.assertNotIn('private', str(result))

    def test_bad_pagination(self):
        for pages in (
            [[self.row(721)], [self.row(721)]],
            [{'workorderLines': [self.row()], 'numberOfPages': True}],
            [{'workorderLines': [self.row(721)], 'numberOfPages': 2},
             {'workorderLines': [], 'numberOfPages': 3}],
            [{'workorderLines': [], 'numberOfPages': 2}],
            [{'unknown': []}],
        ):
            result = self.probe(pages)
            self.assertFalse(result['native_progress_verified'])
            self.assertFalse(result['sources'][0]['complete'])

    def test_page_limit(self):
        result = self.probe([self.grouped([self.row(721, wol=i)]) for i in range(1000)])
        self.assertEqual(result['sources'][0]['status'], 'pagination_limit')
        self.assertIsNone(result['native_progress'])

    def test_errors_on_later_page_are_sanitized(self):
        for failure in (Mock(status_code=302), Mock(status_code=401), Mock(status_code=500),
                        Mock(status_code=200, json=Mock(side_effect=ValueError('private'))),
                        requests.Timeout('private')):
            with patch.object(read.requests, 'post', side_effect=[
                    Mock(status_code=200, json=lambda: [self.row(721)]), failure]):
                result = read.inspect_workorder_progress('', {}, 722, username='synthetic', password='synthetic')
            self.assertIsNone(result['native_progress'])
            self.assertFalse(result['native_progress_verified'])
            self.assertNotIn('private', str(result))

    def test_invalid_inputs_make_no_request(self):
        with patch.object(read.requests, 'post') as post:
            for value in (True, 0, -1, 2147483648, '722', '722/other', 722.0, None):
                with self.assertRaises(ValueError):
                    read.inspect_workorder_progress('', {}, value)
            post.assert_not_called()

    def test_registration_schema_and_wrapper(self):
        # Execute only this tool definition; never import the credential loader.
        tree = ast.parse(Path('mcp_server.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'inspect_workorder_progress')
        server = MCPServer('synthetic')
        scope = dict(mcp=server, epoptia_read=read,
                     BASE_URL='https://example.invalid', HEADERS={},
                     WEB_USERNAME='synthetic', WEB_PASSWORD='synthetic')
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<test>', 'exec'), scope)
        tool = asyncio.run(server.list_tools())[0]
        self.assertEqual(tool.name, 'inspect_workorder_progress')
        self.assertEqual(set(tool.input_schema['properties']),
                         {'workorder_id', 'include_actual_production_completion'})
        self.assertFalse(tool.input_schema['properties']['include_actual_production_completion']['default'])
        self.assertEqual(tool.input_schema['required'], ['workorder_id'])
        self.assertEqual(tool.input_schema['properties']['workorder_id']['type'], 'integer')
        with patch.object(read.requests, 'post', return_value=Mock(
                status_code=200, json=lambda: {'workorderLines': [self.row(721, 93.24)], 'numberOfPages': 1})):
            result = scope['inspect_workorder_progress'](721)
        self.assertTrue(result['ok'])
        self.assertEqual(result['native_progress'], 93.24)
        with patch.object(read.requests, 'post') as get:
            self.assertFalse(scope['inspect_workorder_progress'](0)['ok'])
            get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
