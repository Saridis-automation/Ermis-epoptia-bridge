"""Service tests use mocks only; no server startup or real service access."""
import ast
import asyncio
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import service_control as services


class ServiceControlTest(unittest.TestCase):
    def setUp(self):
        mocked = patch.object(services.subprocess, 'run')
        self.run = mocked.start()
        self.addCleanup(mocked.stop)
        self.service = services.ALLOWED_SERVICES[0]
        self.run.return_value = SimpleNamespace(returncode=0, stdout=(
            'LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\n'))

    def test_central_whitelist_contains_only_approved_services(self):
        self.assertEqual(services.ALLOWED_SERVICES, (
            'ermis-epoptia-mcp.service',
            'ermis-epoptia-tunnel.service',
            'ermis-system-mcp.service',
            'ermis-system-tunnel.service',
        ))

    def test_rejects_unapproved_inputs_without_execution(self):
        for service in ('ssh.service', '--all', self.service + ';id', '*', '', None, [],
                        '/usr/bin/systemctl', '/etc/systemd/system/' + self.service,
                        self.service + ' --force', 'sudo systemctl restart ' + self.service):
            for operation in ('status', 'restart'):
                with self.subTest(service=service, operation=operation):
                    self.assertFalse(services.control(service, operation)['ok'])
        for service in services.ALLOWED_SERVICES:
            for operation in ('start', 'stop', 'reload', '--force', 'restart --all',
                              'restart;id', '/usr/bin/systemctl', '', None, []):
                self.assertFalse(services.control(service, operation)['ok'])
        self.run.assert_not_called()

    def test_status_compatibility_and_fixed_command(self):
        result = services.control(self.service)
        self.assertEqual(result, dict(ok=True, service=self.service, LoadState='loaded',
                                     ActiveState='active', SubState='running', UnitFileState='enabled'))
        self.run.assert_called_once_with(
            ['/usr/bin/systemctl', 'show', self.service, '--no-pager',
             '--property=LoadState,ActiveState,SubState,UnitFileState'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5,
            check=False, shell=False)

    def test_restart_is_fixed_noninteractive_sudo_request(self):
        for service in services.ALLOWED_SERVICES:
            self.run.reset_mock()
            self.assertEqual(services.control(service, 'restart'),
                             dict(ok=True, service=service, operation='restart', accepted=True))
            self.run.assert_called_once_with(
                ['sudo', '-n', '/usr/bin/systemctl', 'restart', service],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                text=True, timeout=5, check=False, shell=False)

    def test_all_approved_operations_use_argv_without_shell(self):
        for service in services.ALLOWED_SERVICES:
            for operation in ('status', 'restart'):
                with self.subTest(service=service, operation=operation):
                    self.run.reset_mock()
                    self.assertTrue(services.control(service, operation)['ok'])
                    args, kwargs = self.run.call_args
                    self.assertIsInstance(args[0], list)
                    self.assertIs(kwargs['shell'], False)
                    if operation == 'status':
                        self.assertEqual(args[0], [
                            '/usr/bin/systemctl', 'show', service, '--no-pager',
                            '--property=LoadState,ActiveState,SubState,UnitFileState'])

    def test_output_sanitization_and_bounds(self):
        self.run.return_value.stdout += 'Description=withheld fixture\n'
        self.run.return_value.stdout = self.run.return_value.stdout.replace('running', 'withheld fixture\x1b')
        result = services.control(self.service)
        self.assertEqual(result['SubState'], 'unknown')
        self.assertNotIn('withheld fixture', str(result))
        for output in ('LoadState=loaded\n', 'x' * 4097):
            self.run.return_value.stdout = output
            self.assertFalse(services.control(self.service)['ok'])

    def test_failures_withhold_output_and_exception_details(self):
        for operation in ('status', 'restart'):
            for error in (OSError('withheld fixture'), UnicodeError('withheld fixture'),
                          subprocess.TimeoutExpired('withheld fixture', 5, output='withheld fixture')):
                self.run.side_effect = error
                result = services.control(self.service, operation)
                self.assertFalse(result['ok'])
                self.assertNotIn('withheld fixture', str(result))
            self.run.side_effect = None
            self.run.return_value = SimpleNamespace(returncode=1, stdout='withheld fixture')
            result = services.control(self.service, operation)
            self.assertFalse(result['ok'])
            self.assertNotIn('withheld fixture', str(result))

    def test_mcp_registration_and_legacy_dispatch_without_server_import(self):
        from mcp.server.mcpserver import MCPServer
        server = MCPServer('Test')
        names = {'ermis_service_control', 'ermis_service_status'}
        tree = ast.parse(Path('ermis_system_server.py').read_text())
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        namespace = {'mcp': server, 'service_control': services}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'ermis_system_server.py', 'exec'), namespace)
        with patch.object(services, 'control', return_value={'ok': True}) as control:
            namespace['ermis_service_status'](self.service)
            control.assert_called_once_with(self.service, 'status')
            control.reset_mock()
            namespace['ermis_service_control'](self.service, 'restart')
            control.assert_called_once_with(self.service, 'restart')
        registered = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        self.assertEqual(set(registered), names)
        schema = registered['ermis_service_control'].input_schema
        self.assertEqual(set(schema['properties']), {'service', 'operation'})
        self.assertEqual(schema['properties']['operation']['default'], 'status')
        self.assertEqual(schema['required'], ['service'])


if __name__ == '__main__':
    unittest.main()
