"""Exercise actual MCP registrations without credentials or running listeners."""
import ast
import asyncio
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

from mcp.server.mcpserver import MCPServer
import ermis_system_server as system


BUSINESS = {'get_wol_status', 'get_wol_details', 'list_wols',
            'production_overview', 'due_wols', 'workstation_wip',
            'inspect_workorder_progress'}
ADMIN = {'ermis_git_status', 'ermis_git_commit', 'ermis_service_status', 'ermis_service_control',
         'ermis_health', 'ermis_codex_start', 'ermis_codex_inspect',
         'ermis_codex_status', 'ermis_codex_wait', 'ermis_codex_logs',
         'ermis_technical_report_read', 'ermis_gateway_execute'}


class SeparationTest(unittest.TestCase):
    def test_commit_schema_accepts_only_message(self):
        tool = next(t for t in asyncio.run(system.mcp.list_tools())
                    if t.name == 'ermis_git_commit')
        self.assertEqual(set(tool.input_schema['properties']), {'message'})
        self.assertEqual(tool.input_schema['required'], ['message'])

    def test_health_reports_all_four_services(self):
        expected = (
            'ermis-epoptia-mcp.service', 'ermis-epoptia-tunnel.service',
            'ermis-system-mcp.service', 'ermis-system-tunnel.service',
        )
        active = dict(ok=True, LoadState='loaded', ActiveState='active')
        git = dict(ok=True, clean=True)
        with patch.object(system, 'ermis_service_status', return_value=active) as status, \
                patch.object(system, 'ermis_git_status', return_value=git), \
                patch.object(system.socket, 'create_connection') as connect:
            result = system.ermis_health()
            self.assertEqual(result, dict(
                healthy=True, services=dict.fromkeys(expected, active), git=git,
                local_mcp_port_listening=True))
            self.assertEqual(status.call_args_list,
                             [unittest.mock.call(service) for service in expected])
            connect.assert_called_once_with(('127.0.0.1', 8000), timeout=1)

    def test_health_fails_for_each_unhealthy_service(self):
        for service in system.ALLOWED_SERVICES:
            for failure in (dict(ok=False), dict(ok=True, LoadState='not-found',
                           ActiveState='active'), dict(ok=True, LoadState='loaded',
                           ActiveState='failed')):
                with self.subTest(service=service, failure=failure), \
                        patch.object(system, 'ermis_service_status', side_effect=lambda name:
                            failure if name == service else dict(
                                ok=True, LoadState='loaded', ActiveState='active')), \
                        patch.object(system, 'ermis_git_status', return_value=dict(ok=True)), \
                        patch.object(system.socket, 'create_connection'):
                    self.assertFalse(system.ermis_health()['healthy'])

    def test_health_fails_for_git_or_listener_failure(self):
        with patch.object(system, 'ermis_service_status', return_value=dict(
                ok=True, LoadState='loaded', ActiveState='active')), \
                patch.object(system, 'ermis_git_status', return_value=dict(ok=False)), \
                patch.object(system.socket, 'create_connection') as connect:
            self.assertFalse(system.ermis_health()['healthy'])
            with patch.object(system, 'ermis_git_status', return_value=dict(ok=True)):
                connect.side_effect = OSError('unavailable')
                result = system.ermis_health()
                self.assertFalse(result['healthy'])
                self.assertFalse(result['local_mcp_port_listening'])

    def test_exact_disjoint_registrations(self):
        # Execute all registration code, including any non-decorator additions,
        # without loading credentials, calling upstream, or opening a listener.
        with patch('dotenv.load_dotenv'), patch('os.getenv', return_value=None), \
                patch('requests.get') as get, patch.object(MCPServer, 'run') as run:
            namespace = runpy.run_path('mcp_server.py', run_name='separation_test')
            get.assert_not_called()
            run.assert_not_called()
        business = namespace['mcp']
        business_tools = {t.name for t in asyncio.run(business.list_tools())}
        admin_tools = {t.name for t in asyncio.run(system.mcp.list_tools())}
        self.assertEqual(business_tools, BUSINESS)
        self.assertEqual(admin_tools, ADMIN)
        self.assertFalse(business_tools & admin_tools)

    def test_separate_fixed_listener_configuration(self):
        for filename, port in [('mcp_server.py', 8000), ('ermis_system_server.py', 8001)]:
            tree = ast.parse(Path(filename).read_text())
            guard = next(n for n in tree.body if isinstance(n, ast.If))
            server = unittest.mock.Mock()
            exec(compile(ast.Module(body=[guard], type_ignores=[]), '<test>', 'exec'),
                 {'mcp': server, '__name__': '__main__'})
            server.run.assert_called_once_with(
                transport='streamable-http', host='127.0.0.1', port=port,
                json_response=True, stateless_http=True)

    def test_system_service_wrapper_rejects_inputs_before_execution(self):
        with patch('service_control.subprocess.run') as run:
            self.assertFalse(system.ermis_service_control('ssh.service', 'restart')['ok'])
            self.assertFalse(system.ermis_service_control(
                system.ALLOWED_SERVICES[0], 'restart --all')['ok'])
            run.assert_not_called()

    def test_entrypoint_import_boundaries(self):
        for filename, forbidden in [
            ('ermis_system_server.py', {'mcp_server', 'epoptia_read', 'dotenv'}),
            ('mcp_server.py', {'ermis_system_server', 'codex_jobs', 'technical_reports', 'service_control'}),
        ]:
            tree = ast.parse(Path(filename).read_text())
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.add(node.module)
            self.assertFalse(imports & forbidden)
