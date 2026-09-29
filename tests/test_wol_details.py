"""Synthetic technical payloads; no credentials or live API calls."""
import unittest
import ast
from pathlib import Path
from unittest.mock import Mock, patch
import asyncio
import json
from mcp.server.mcpserver import MCPServer
from wol_details import technical_details, summary_fields, wol_details
import epoptia_read


class DetailsTests(unittest.TestCase):
    def test_active_completion_null_and_absent(self):
        row = {'workorderline_id': 3168, 'production_status': 'started'}
        self.assertEqual(wol_details([row], 3168)['completion'], {'raw_fields': {}})
        row.update(completionDate=None, completed_at=None, dbCompletionDate='')
        self.assertEqual(wol_details([row], 3168)['completion']['raw_fields'], {
            'completionDate': None, 'completed_at': None, 'dbCompletionDate': ''})

    def test_completed_completion_values_are_raw(self):
        fields = {
            'completionDate': '2026-09-01',
            'dbCompletionDate': '2026-09-01T12:30:00.123456Z',
            'displayCompletionDate': '01/09/2026',
            'completedAt': '2026-09-01T12:30:00+02:00',
            'completed_at': '2026-09-01 12:30:00',
            'completion_date': '2026-09-01T12:30:00-04:00',
            'productionCompletionDate': '2026-09-01T12:30:00.123456789Z',
            'completionTimestamp': 1788265800,
            'production_completed_at': '2026-09-01T12:30:00Z',
        }
        for state in ('archived', 'completed'):
            with self.subTest(state=state):
                row = {'workorderline_id': 3168, 'state': state, **fields}
                self.assertEqual(epoptia_read.wol_details([
                    {'workorderline_id': 1, 'completionDate': '2000-01-01'}, row
                ], 3168)['completion'], {'raw_fields': fields})

    def test_completion_allowlist_bounds_and_safety(self):
        row = {'workorderline_id': 3168,
               'completionDate': {'nested': 'omit'},
               'dbCompletionDate': ['omit'],
               'displayCompletionDate': 'x' * 129,
               'completedAt': 'token=synthetic',
               'completed_at': float('inf'),
               'completion_date': float('nan'),
               'productionCompletionDate': 'https://example.invalid',
               'completionTimestamp': True,
               'completionDateSecret': 'omit', 'completionPercentage': 100,
               'unrelated': 'omit',
               'data': {'completionDate': '2000-01-01'},
               'product': {'completedAt': '2000-01-01'}}
        self.assertEqual(wol_details([row], 3168)['completion'], {'raw_fields': {}})

    def test_full_details_and_legacy_projection(self):
        row = {'workorderline_id': 3168, 'description': 'Custom cabinet',
               'client': {'name': 'Example client', 'email': 'omit'},
               'quantity': 2, 'target_day': '2026-10-01', 'state': 'released',
               'production_status': 'production', 'width': 900, 'remarks': 'Fit on site',
               'custom': {'mountingPitch': 42},
               'product': {'width': 600, 'thermalClass': 'T2',
                           'attributes': [{'name': 'hingeType', 'value': 'concealed'}],
                           'password': 'omit', 'session': 'omit',
                           'connectionString': 'omit'}}
        result = wol_details([row], 3168)
        self.assertTrue(result['found'])
        for name in ('description', 'quantity', 'target_day', 'state', 'production_status'):
            self.assertEqual(result[name], row[name])
        self.assertEqual(result['client'], 'Example client')
        details = result['technical_details']
        self.assertEqual(details['specifications']['width'], {'value': 900, 'source': 'wol'})
        self.assertEqual(details['extra_technical_fields']['product']['product.thermalClass'], 'T2')
        self.assertEqual(details['extra_technical_fields']['product']['product.attributes.0.hingeType'], 'concealed')
        self.assertEqual(details['extra_technical_fields']['wol']['custom.mountingPitch'], 42)
        self.assertEqual(details['sources']['wol']['descriptions']['remarks'], 'Fit on site')
        self.assertNotIn('omit', json.dumps(result))
        self.assertNotIn('product.thermalClass', technical_details(row)['extra_technical_fields']['product'])
        self.assertFalse(result['truncated'])

    def test_details_bounds_missing_and_safety(self):
        row = {'workorderline_id': 3168, 'description': 'token=synthetic',
               'client': {'name': 'https://example.invalid'}, 'quantity': float('inf'),
               'product': {'attributes': {f'field{i}': 'x' * 3000 for i in range(100)}}}
        result = wol_details([row], 3168)
        self.assertIsNone(result['description'])
        self.assertIsNone(result['client'])
        self.assertIsNone(result['quantity'])
        self.assertTrue(result['truncated'])
        self.assertLess(len(json.dumps(result)), 3000000)
        self.assertFalse(wol_details([], 3168)['found'])
        for target in ('2026-10-01', '2026-10-01T12:30:00Z', '2026-10-01T12:30:00+02:00'):
            self.assertEqual(wol_details([{'workorderline_id': 3168, 'target_day': target}],
                                         3168)['target_day'], target)
        for invalid in (0, -1, True, '3168', 2**63):
            with self.assertRaises(ValueError):
                wol_details([], invalid)

    def test_details_tool_schema_dispatch_and_errors(self):
        tree = ast.parse(Path('mcp_server.py').read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in {'get_wol_details', '_read_query'}]
        server = MCPServer('Synthetic details tests')
        scope = {'mcp': server, 'epoptia_read': epoptia_read,
                 'BASE_URL': 'https://example.invalid', 'HEADERS': {}}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'mcp_server.py', 'exec'), scope)
        tool, = asyncio.run(server.list_tools())
        self.assertEqual(tool.name, 'get_wol_details')
        self.assertEqual(tool.input_schema['required'], ['wol_id'])
        self.assertEqual(tool.input_schema['properties']['wol_id']['type'], 'integer')
        with patch.object(epoptia_read, 'fetch_wols', return_value=[{'workorderline_id': 3168}]) as fetch:
            self.assertTrue(scope['get_wol_details'](3168)['found'])
            fetch.assert_called_once_with('https://example.invalid', {})
            fetch.reset_mock()
            self.assertFalse(scope['get_wol_details'](0)['ok'])
            fetch.assert_not_called()
        with patch.object(epoptia_read, 'fetch_wols', side_effect=epoptia_read.ReadError('omit')):
            self.assertEqual(scope['get_wol_details'](3168),
                             {'ok': False, 'error': 'Epoptia read unavailable'})

    def test_custom_precedence_and_descriptions(self):
        row = {'description': 'Ειδικό', 'width': 900, 'customDescription': 'Custom cabinet',
               'notes': 'Fit reinforced glass', 'specialConstruction': 'Raised base',
               'product': {'width': 600, 'model': 'CAB', 'code': 'C1',
                           'description': 'Standard cabinet', 'specifications': {'voltage': '230 V'}}}
        result = technical_details(row)
        self.assertEqual(result['specifications']['width'], {'value': 900, 'source': 'wol'})
        self.assertEqual(result['sources']['product']['specifications']['width']['product.width'], 600)
        self.assertEqual(len(result['sources']['wol']['descriptions']), 4)
        self.assertIn('product.description', result['sources']['product']['descriptions'])
        self.assertEqual(summary_fields(row), {'dimensions': {'width': 900}, 'model': 'CAB', 'code': 'C1'})
        self.assertEqual(epoptia_read.list_wols([row])['items'][0]['dimensions'], {'width': 900})

    def test_nested_attributes_unknown_fields_and_false_zero(self):
        row = {'specifications': {'attributes': [{'name': 'Height', 'value': 1200, 'unit': 'mm'}],
                                  'compressor': {'capacity': '300 W'}, 'doors': 0,
                                  'options': {'heated': False}, 'thermalRating': '40 mm'},
               'unrelated': 'omit'}
        result = technical_details(row)
        self.assertEqual(result['specifications']['height']['value'], 1200)
        self.assertEqual(result['specifications']['doors']['value'], 0)
        self.assertIs(result['specifications']['options']['value'], False)
        self.assertEqual(result['extra_technical_fields']['wol']['specifications.thermalRating'], '40 mm')
        self.assertNotIn('unrelated', repr(result))

    def test_sanitization_at_every_level(self):
        row = {'notes': 'token=synthetic', 'product': {'description': 'https://example.invalid'},
               'specifications': {'password': 'omit', 'contact': {'width': 123},
                                  'odd': 'person@example.invalid', 'url': 'omit',
                                  'attributes': [{'name': 'api_key', 'value': 'omit'}],
                                  'material': 'steel', 'privateNote': 'omit',
                                  'finish': 'safe prefix ' * 200 + ' token=synthetic'}}
        result = technical_details(row)
        self.assertEqual(result['specifications'], {'material': {'value': 'steel', 'source': 'wol'}})
        self.assertEqual(result['extra_technical_fields'], {'wol': {}, 'product': {}})
        self.assertEqual(result['sources']['wol']['descriptions'], {})

    def test_status_integration_preserves_legacy_fields(self):
        tree = ast.parse(Path('mcp_server.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'get_wol_status')
        node.decorator_list = []
        find = Mock(return_value={'workorderline_id': 1, 'description': 'Cabinet',
                                 'width': 700, 'erp_routing': []})
        scope = {'find_wol': find, 'epoptia_read': epoptia_read}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'mcp_server.py', 'exec'), scope)
        result = scope['get_wol_status'](1)
        self.assertEqual(result['description'], 'Cabinet')
        self.assertEqual(result['completed'], [])
        self.assertEqual(result['technical_details']['specifications']['width']['value'], 700)
        find.return_value = None
        self.assertEqual(scope['get_wol_status'](1), {
            'found': False, 'workorderline_id': 1, 'message': 'Work Order Line not found'})

    def test_bounds_malformed_and_empty(self):
        for row in ({}, None, [], {'width': float('nan')}, {'product': None}):
            self.assertEqual(technical_details(row)['specifications'], {})
        self.assertTrue(technical_details({'specs': {f'feature{i}': i for i in range(100)}})['truncated'])
        result = technical_details({'notes': 'x' * 3000})
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['sources']['wol']['descriptions']['notes']), 2000)
        nested = {'width': 2}
        for _ in range(12):
            nested = {'specs': nested}
        self.assertTrue(technical_details(nested)['truncated'])


if __name__ == '__main__':
    unittest.main()
