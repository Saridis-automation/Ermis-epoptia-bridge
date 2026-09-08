"""Synthetic payloads only; no server import, dotenv, credentials or network."""
import ast
import asyncio
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from mcp.server.mcpserver import MCPServer
import requests
import epoptia_read as read


def rows():
    return [
        {'workorderline_id': 1, 'description': 'Blue Gear', 'client': {'name': 'Acme'},
         'production_status': 'started', 'state': 'released', 'quantity': 5,
         'target_day': '2026-09-05', 'private_note': 'omit me',
         'erp_routing': [{'workstationName': 'Lathe', 'job_tag': {'name': 'Cut'},
                          'status': 'started', 'qty_done': 2},
                         {'workstationName': 'Lathe', 'status': 'completed'}]},
        {'workorderline_id': 2, 'description': 'Red Gear', 'client': {'name': 'Acme'},
         'production_status': 'paused', 'quantity': 3, 'target_day': '2026-09-06T10:00:00Z',
         'erp_routing': [{'workstationName': 'Lathe', 'job_tag': {'name': 'Polish'}, 'status': 'paused'}]},
        {'workorderline_id': 3, 'production_status': 'completed', 'target_day': '2026-09-01'},
        {'workorderline_id': 4, 'production_status': 'queued', 'target_day': '2026-09-13'},
        {'workorderline_id': 5, 'production_status': 'queued', 'target_day': '2026-09-14'},
    ]


class QueryTests(unittest.TestCase):
    def test_combined_filters_and_allowlist(self):
        result = read.list_wols(rows(), wol_id=1, client='ACM', product_text='gear',
                                status='STARTED', state='released',
                                target_from='2026-09-05', target_to='2026-09-05')
        self.assertEqual(result['total_matches'], 1)
        self.assertEqual(result['items'][0]['workorderline_id'], 1)
        self.assertNotIn('private_note', result['items'][0])
        self.assertNotIn('erp_routing', result['items'][0])
        self.assertEqual(read.list_wols(rows(), client='other')['items'], [])
        self.assertEqual(read.list_wols(rows(), wol_id=10)['total_matches'], 0)

    def test_limits_empty_and_validation(self):
        for query in (read.list_wols, read.due_wols, read.workstation_wip):
            self.assertEqual(query([])['total_matches'], 0)
            for limit in (0, -1, 201, True, '2'):
                with self.assertRaises(ValueError):
                    query([], limit=limit)
        result = read.list_wols(rows(), limit=2)
        self.assertEqual((result['returned'], result['total_matches'], result['truncated']), (2, 5, True))
        for filters in ({'target_from': 'bad'}, {'target_from': '2026-W37-1'}, {'target_to': '2026-02-30'},
                        {'target_from': '2026-09-10', 'target_to': '2026-09-01'}):
            with self.assertRaises(ValueError):
                read.list_wols([], **filters)

    def test_overview(self):
        result = read.overview(rows())
        self.assertEqual(result['total_wols'], 5)
        self.assertEqual(result['counts_by_status']['queued'], 2)
        self.assertEqual(result['counts_by_state'], {'released': 1, 'unknown': 4})
        self.assertEqual(result['total_quantity'], 8)
        self.assertEqual(result['quantity_known_wols'], 2)
        self.assertEqual(result['dated_wols'], 5)
        self.assertEqual(read.overview([])['counts_by_status'], {})
        self.assertEqual(read.overview([])['total_quantity'], 0)

    def test_due_boundaries_and_terminal_status(self):
        result = read.due_wols(rows(), as_of='2026-09-06', days=7, limit=1)
        self.assertEqual(result['total_matches'], 2)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['items'][0]['workorderline_id'], 2)
        self.assertEqual(read.due_wols(rows(), as_of='2026-09-06', days=0)['total_matches'], 1)
        result = read.due_wols(rows(), mode='overdue', as_of='2026-09-06')
        self.assertEqual([r['workorderline_id'] for r in result['items']], [1])
        self.assertEqual(read.due_wols(rows(), as_of='2026-09-06', target_from='2026-09-13')['total_matches'], 1)
        for options in ({'mode': 'bad'}, {'days': -1}, {'as_of': 'bad'}):
            with self.assertRaises(ValueError):
                read.due_wols([], **options)

    def test_wip_counts_filters_and_step_limit(self):
        result = read.workstation_wip(rows(), limit=1)
        self.assertEqual(result['counts_by_workstation'], {'Lathe': 2})
        self.assertEqual(result['total_matches'], 2)
        self.assertTrue(result['truncated'])
        result = read.workstation_wip(rows(), workstation='LAT', step='cut')
        self.assertEqual(result['total_matches'], 1)
        self.assertEqual(result['items'][0]['step'], 'Cut')
        self.assertEqual(read.workstation_wip(rows(), workstation='missing')['items'], [])

    def test_malformed_optional_fields(self):
        malformed = [{'client': [], 'quantity': float('nan'), 'target_day': 'bad',
                      'production_status': {}, 'state': [], 'description': {'private': 'omit'},
                      'erp_routing': [None, 5, {'status': 'in_progress', 'job_tag': [],
                                                'workstationName': {}, 'qty_done': {}}]},
                     {'client': None, 'target_day': {}, 'erp_routing': None}, {}]
        self.assertIsNone(read.list_wols(malformed)['items'][0]['description'])
        self.assertEqual(read.overview(malformed)['counts_by_status'], {'unknown': 3})
        self.assertEqual(read.overview(malformed)['total_quantity'], 0)
        self.assertEqual(read.due_wols(malformed)['items'], [])
        self.assertEqual(read.list_wols(malformed, target_from='2026-01-01')['items'], [])
        result = read.workstation_wip(malformed)
        self.assertEqual(result['counts_by_workstation'], {'unknown': 1})
        self.assertIsNone(result['items'][0]['qty_done'])


class ProgressTests(unittest.TestCase):
    def test_lifecycle_does_not_confer_completion(self):
        for lifecycle in ('archive', 'production'):
            row = {'production_status': lifecycle,
                   'erp_routing': [{'status': 'not_started'}]}
            item = read.list_wols([row])['items'][0]
            self.assertEqual(item['production_status'], lifecycle)
            self.assertEqual(item['progress']['routing_completion_percent'], 0)
            self.assertEqual(item['progress']['not_started_steps'], 1)

    def test_mixed_production_and_paused(self):
        routing = [{'status': status, 'qty_done': 999} for status in
                   ('completed', 'started', 'in_progress', 'paused', 'not_started', 'COMPLETED')]
        progress = read.list_wols([{'production_status': 'production',
                                    'erp_routing': routing}])['items'][0]['progress']
        self.assertEqual(progress, {'total_steps': 6, 'completed_steps': 2,
                                   'active_steps': 2, 'paused_steps': 1,
                                   'not_started_steps': 1, 'unknown_steps': 0,
                                   'routing_completion_percent': 33.33})
        self.assertEqual(read.routing_progress([{'status': 'paused'}])['routing_completion_percent'], 0)

    def test_missing_empty_and_unknown_steps(self):
        for routing in (None, [], {}, 'invalid'):
            progress = read.routing_progress(routing)
            self.assertEqual(progress['total_steps'], 0)
            self.assertIsNone(progress['routing_completion_percent'])
        progress = read.routing_progress([None, {}, {'status': 'future'}, {'status': 'completed'}])
        self.assertEqual(progress['unknown_steps'], 3)
        self.assertEqual(progress['routing_completion_percent'], 25)

    def test_aggregate_is_step_weighted_filtered_and_before_limit(self):
        data = [
            {'client': {'name': 'Acme'}, 'production_status': 'archive',
             'erp_routing': [{'status': 'not_started'}] * 3},
            {'client': {'name': 'Acme'}, 'production_status': 'production',
             'erp_routing': [{'status': 'completed'}]},
            {'client': {'name': 'Acme'}, 'production_status': 'archive'},
            {'client': {'name': 'Other'}, 'erp_routing': [{'status': 'completed'}]},
        ]
        result = read.list_wols(data, client='ACME', limit=1)
        self.assertTrue(result['truncated'])
        aggregate = result['aggregate']
        self.assertEqual(aggregate['total_wols'], 3)
        self.assertEqual(aggregate['counts_by_status'], {'archive': 2, 'production': 1})
        self.assertEqual(aggregate['unknown_routing_wols'], 1)
        self.assertEqual(aggregate['progress']['total_steps'], 4)
        self.assertEqual(aggregate['progress']['completed_steps'], 1)
        self.assertEqual(aggregate['progress']['routing_completion_percent'], 25)
        for subset in ([], [data[2]]):
            self.assertIsNone(read.list_wols(subset)['aggregate']['progress']['routing_completion_percent'])
        mixed = read.list_wols(rows())['aggregate']
        self.assertEqual(mixed['progress']['active_steps'], 1)
        self.assertEqual(mixed['progress']['paused_steps'], 1)
        self.assertEqual(mixed['progress']['routing_completion_percent'], 33.33)

    def test_get_wol_status_preserves_fields_and_adds_progress(self):
        tree = ast.parse(Path('mcp_server.py').read_text())
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'get_wol_status')
        server = MCPServer('Progress tests')
        find = Mock()
        scope = {'mcp': server, 'find_wol': find, 'epoptia_read': read}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'mcp_server.py', 'exec'), scope)
        for routing in ([{'status': 'not_started'}], [{'status': 'paused'}], [], None):
            find.return_value = {'workorderline_id': 1, 'production_status': 'archive',
                                 'erp_routing': routing}
            result = scope['get_wol_status'](1)
            self.assertEqual(result['production_status'], 'archive')
            self.assertEqual(result['progress'], read.routing_progress(routing))
            self.assertTrue({'found', 'workorderline_id', 'description', 'production_status',
                             'quantity', 'target_day', 'client', 'completed',
                             'in_progress', 'not_started'}.issubset(result))
            if routing == [{'status': 'paused'}]:
                self.assertEqual(len(result['in_progress']), 1)
        find.return_value = None
        self.assertEqual(scope['get_wol_status'](1), {
            'found': False, 'workorderline_id': 1, 'message': 'Work Order Line not found'})


class FetchTests(unittest.TestCase):
    @patch.object(read.requests, 'get')
    def test_pagination_and_non_records(self, get):
        get.side_effect = [Mock(status_code=200, json=lambda: {'numberOfPages': 2, 'workorderLines': [rows()[0], None]}),
                           Mock(status_code=200, json=lambda: {'workorderLines': [rows()[1]]})]
        self.assertEqual(len(read.fetch_wols('https://example.invalid', {})), 2)
        self.assertEqual([call.kwargs['params']['page'] for call in get.call_args_list], [1, 2])
        self.assertTrue(all(call.kwargs['timeout'] == 20 for call in get.call_args_list))
        self.assertTrue(all(call.kwargs['allow_redirects'] is False for call in get.call_args_list))

    @patch.object(read.requests, 'get')
    def test_empty_and_malformed_pages(self, get):
        for payload in (None, {}, {'workorderLines': None},
                        {'workorderLines': [], 'numberOfPages': '2'}):
            get.return_value = Mock(status_code=200, json=lambda: payload)
            with self.assertRaises(read.ReadError):
                read.fetch_wols('https://example.invalid', {})
        get.return_value = Mock(status_code=200, json=lambda: {'workorderLines': [], 'numberOfPages': 0})
        self.assertEqual(read.fetch_wols('https://example.invalid', {}), [])

    @patch.object(read.requests, 'get')
    def test_redirects_are_rejected(self, get):
        for status in (301, 302, 307, 308):
            get.return_value = Mock(status_code=status)
            with self.assertRaisesRegex(read.ReadError, '^Epoptia read failed$'):
                read.fetch_wols('https://example.invalid', {})
            get.return_value.json.assert_not_called()

    @patch.object(read.requests, 'get')
    def test_inconsistent_pagination_is_rejected(self, get):
        get.return_value = Mock(status_code=200, json=lambda: {
            'numberOfPages': 0, 'workorderLines': rows()})
        with self.assertRaises(read.ReadError):
            read.fetch_wols('https://example.invalid', {})
        for count in (1, 3, True, '2'):
            get.side_effect = [
                Mock(status_code=200, json=lambda: {
                    'numberOfPages': 2, 'workorderLines': rows()}),
                Mock(status_code=200, json=lambda: {
                    'numberOfPages': count, 'workorderLines': []})]
            with self.assertRaises(read.ReadError):
                read.fetch_wols('https://example.invalid', {})

    @patch.object(read.requests, 'get')
    def test_failed_later_page_discards_partial_results(self, get):
        get.side_effect = [Mock(status_code=200, json=lambda: {'numberOfPages': 2, 'workorderLines': rows()}),
                           requests.RequestException('synthetic upstream detail')]
        with self.assertRaisesRegex(read.ReadError, '^Epoptia read failed$'):
            read.fetch_wols('https://example.invalid', {})


class MCPTests(unittest.TestCase):
    def test_schemas_dispatch_validation_and_safe_errors(self):
        names = {'list_wols', 'production_overview', 'due_wols', 'workstation_wip'}
        tree = ast.parse(Path('mcp_server.py').read_text())
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in names | {'_read_query'}]
        server = MCPServer('Synthetic tests')
        scope = {'mcp': server, 'epoptia_read': read, 'BASE_URL': 'https://example.invalid', 'HEADERS': {},
                 'WEB_USERNAME': None, 'WEB_PASSWORD': None}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'mcp_server.py', 'exec'), scope)
        registered = asyncio.run(server.list_tools())
        self.assertEqual({tool.name for tool in registered}, names)
        for tool in registered:
            self.assertTrue(tool.description)
            if tool.name != 'production_overview':
                self.assertIn('limit', tool.input_schema['properties'])
        with patch.object(read, 'fetch_wols', return_value=rows()) as fetch:
            for name in names:
                self.assertTrue(scope[name]() ['ok'])
            self.assertEqual(fetch.call_count, 4)
            fetch.reset_mock()
            self.assertFalse(scope['list_wols'](limit=0)['ok'])
            fetch.assert_not_called()
        with patch.object(read, 'fetch_wols', side_effect=read.ReadError('synthetic private detail')):
            self.assertEqual(scope['production_overview'](), {'ok': False, 'error': 'Epoptia read unavailable'})


if __name__ == '__main__':
    unittest.main()
