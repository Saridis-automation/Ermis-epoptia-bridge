"""Full MCP entrypoint regression coverage with synthetic configuration and HTTP."""
import asyncio
import builtins
import importlib
import os
import runpy
import sys
import unittest
from unittest.mock import Mock, patch

import requests


class ReaderInitializationTests(unittest.TestCase):
    def setUp(self):
        # Replace (do not copy/read) the real environment; never load .env.
        self.enterContext(patch.object(os, 'environ', {}))
        self.enterContext(patch('dotenv.load_dotenv', return_value=False))
        self.enterContext(patch('os.getenv', side_effect=lambda key, default=None: {
            'EPOPTIA_BASE_URL': 'https://example.invalid',
            'EPOPTIA_USERNAME': 'synthetic',
            'EPOPTIA_PASSWORD': 'synthetic',
        }.get(key, default)))
        self.enterContext(patch('socket.socket.connect',
                                side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch('socket.getaddrinfo',
                                side_effect=AssertionError('Network forbidden')))
        self.http = self.enterContext(patch('requests.sessions.Session.request',
                                           side_effect=AssertionError('Unexpected HTTP')))
        from mcp.server.mcpserver import MCPServer
        run = self.enterContext(patch.object(MCPServer, 'run'))
        # Force the same transitive import/initialization as a fresh service.
        # AST-only wrapper tests and cached imports miss module-level failures.
        self.enterContext(patch.dict(sys.modules))
        for name in ('mcp_server', 'epoptia_read', 'wol_details', 'wol_technical', 'epoptia_queries'):
            sys.modules.pop(name, None)
        self.scope = runpy.run_module('mcp_server', run_name='__main__')
        self.reader = self.scope['epoptia_read']
        self.server = self.scope['mcp']
        run.assert_called_once_with(transport='streamable-http', host='127.0.0.1',
                                    port=8000, json_response=True, stateless_http=True)
        self.http.assert_not_called()

    def call(self, name, **arguments):
        # Exercise the registered synchronous callbacks, without an HTTP listener
        # or the SDK's worker-thread transport (unavailable in some sandboxes).
        return self.scope[name](**arguments)

    def supply_rows(self, **extra):
        row = dict(workorderline_id=3168, id=3168, description='Synthetic cabinet',
                   production_status='production', target_day='2026-09-13',
                   workorder={'id': 721, 'progress': 25},
                   erp_routing=[{'workstationName': 'Assembly', 'status': 'started'}])
        row.update(extra)
        self.http.side_effect = None
        self.http.return_value = Mock(status_code=200, json=lambda: {
            'numberOfPages': 1, 'workorderLines': [row]})
        return row

    def test_fresh_initialization_and_all_existing_reads(self):
        self.supply_rows()
        tools = {tool.name for tool in asyncio.run(self.server.list_tools())}
        self.assertEqual(tools, {'get_wol_status', 'get_wol_details', 'list_wols',
                                'production_overview', 'due_wols', 'workstation_wip',
                                'inspect_workorder_progress', 'calendar_target_dates'})
        with patch.object(self.reader, '_web_login', return_value=True):
            self.assertTrue(self.call('get_wol_status', wol_id=3168)['found'])
            self.assertTrue(self.call('get_wol_details', wol_id=3168)['ok'])
            for name, arguments in (
                ('list_wols', {}),
                ('due_wols', {'as_of': '2026-09-13'}),
                ('workstation_wip', {}),
            ):
                with self.subTest(tool=name):
                    result = self.call(name, **arguments)
                    self.assertTrue(result['ok'])
                    self.assertEqual(result['total_matches'], 1)
            overview = self.call('production_overview')
            self.assertTrue(overview['ok'])
            self.assertEqual(overview['total_wols'], 1)
            self.assertEqual(overview['native_active_production_progress_percent'], 25)
            progress = self.call('inspect_workorder_progress', workorder_id=721)
            self.assertTrue(progress['native_progress_verified'])
            self.assertEqual(progress['native_progress'], 25)

    def test_completion_raw_fields_survive_registered_callback(self):
        fields = {'completionDate': None, 'dbCompletionDate': '',
                  'completed_at': '2026-09-01T12:30:00.123456789Z',
                  'completionTimestamp': 1788265800}
        self.supply_rows(**fields)
        result = self.call('get_wol_details', wol_id=3168)
        self.assertTrue(result['ok'])
        self.assertEqual(result['completion'], {'raw_fields': fields})
        self.assertEqual(result['description'], 'Synthetic cabinet')

    def test_invalid_id_is_rejected_before_transport(self):
        result = self.call('get_wol_details', wol_id=0)
        self.assertFalse(result['ok'])
        self.http.assert_not_called()

    def test_clean_module_import_initializes_reader_and_both_tools(self):
        for name in ('mcp_server', 'epoptia_read', 'wol_details', 'wol_technical', 'epoptia_queries'):
            sys.modules.pop(name, None)
        module = importlib.import_module('mcp_server')
        reader = module.epoptia_read
        self.assertIs(reader, sys.modules['epoptia_read'])
        self.assertNotIn('wol_details', sys.modules)
        self.assertTrue(callable(reader.wol_details))
        self.assertTrue(callable(reader.fetch_wols))
        self.http.assert_not_called()
        self.supply_rows(completionDate=None)
        with patch.object(reader, '_web_login', return_value=True):
            overview = module.production_overview()
        details = module.get_wol_details(wol_id=3168)
        self.assertTrue(overview['ok'])
        self.assertEqual(overview['total_wols'], 1)
        self.assertTrue(details['ok'])
        self.assertTrue(details['found'])
        self.assertEqual(details['completion'], {'raw_fields': {'completionDate': None}})

    def test_details_import_failure_does_not_disable_core_reads(self):
        original_import = builtins.__import__

        for failure in (ImportError, NameError, SyntaxError):
            with self.subTest(failure=failure.__name__):
                def import_with_broken_details(name, *args, **kwargs):
                    if name == 'wol_details':
                        raise failure('Synthetic details initialization failure')
                    return original_import(name, *args, **kwargs)

                for name in ('mcp_server', 'epoptia_read', 'wol_details',
                             'wol_technical', 'epoptia_queries'):
                    sys.modules.pop(name, None)
                with patch('builtins.__import__', side_effect=import_with_broken_details):
                    module = importlib.import_module('mcp_server')
                    self.supply_rows()
                    # Broken enhancement remains observable, never a global sentinel.
                    with self.assertRaisesRegex(failure, 'Synthetic details initialization failure'):
                        module.get_wol_details(wol_id=3168)
                    with patch.object(module.epoptia_read, '_web_login', return_value=True):
                        self.assertTrue(module.production_overview()['ok'])
                    for name in ('list_wols', 'due_wols', 'workstation_wip'):
                        self.assertTrue(getattr(module, name)()['ok'])
                    self.assertTrue(module.get_wol_status(wol_id=3168)['found'])
                # A failed lazy import does not poison the reader or require reimport.
                self.assertTrue(module.get_wol_details(wol_id=3168)['ok'])

    def test_core_reader_import_failure_remains_visible(self):
        original_import = builtins.__import__

        def import_with_broken_reader(name, *args, **kwargs):
            if name == 'epoptia_read':
                raise ImportError('Synthetic core initialization failure')
            return original_import(name, *args, **kwargs)

        sys.modules.pop('mcp_server', None)
        with patch('builtins.__import__', side_effect=import_with_broken_reader):
            with self.assertRaisesRegex(ImportError, 'Synthetic core initialization failure'):
                importlib.import_module('mcp_server')
        self.assertNotIn('mcp_server', sys.modules)
        self.http.assert_not_called()

    def test_real_transport_failures_keep_unavailable_response(self):
        for failure in (requests.Timeout(), requests.HTTPError()):
            for name, arguments in (('production_overview', {}),
                                    ('get_wol_details', {'wol_id': 3168})):
                with self.subTest(failure=type(failure).__name__, tool=name):
                    self.http.reset_mock()
                    self.http.side_effect = failure
                    self.assertEqual(self.call(name, **arguments), {
                        'ok': False, 'error': 'Epoptia read unavailable'})
                    self.http.assert_called_once()


if __name__ == '__main__':
    unittest.main()
