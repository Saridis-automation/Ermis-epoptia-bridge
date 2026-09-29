"""Offline entrypoint and repo-local unit contract checks; no listeners/services."""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

from dashboard.server import main

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTest(unittest.TestCase):
    def test_production_server_options_and_routes(self):
        for argv, port in [([], 8010), (['--port', '8123'], 8123)]:
            waitress = MagicMock()
            with patch.dict(sys.modules, waitress=waitress), patch('dashboard.server.LocalEpoptiaProvider',
                    return_value=lambda: __import__('dashboard.provider', fromlist=['empty_snapshot']).empty_snapshot(
                        __import__('datetime').datetime.now(__import__('datetime').timezone.utc))):
                main(argv)
            app = waitress.serve.call_args.args[0]
            self.addCleanup(app.extensions['dashboard_stop'].set)
            options = waitress.serve.call_args.kwargs
            self.assertEqual(options['host'], '127.0.0.1')
            self.assertEqual(options['port'], port)
            self.assertEqual(options['threads'], 4)
            self.assertEqual(options['connection_limit'], 64)
            self.assertEqual(options['channel_timeout'], 30)
            self.assertEqual(options['max_request_body_size'], 65536)
            self.assertFalse(options['expose_tracebacks'])
            self.assertFalse(app.debug)
            with app.test_client() as client:
                self.assertEqual(client.get('/health').json,
                                 {'status': 'ok', 'component': 'dashboard'})
                response = client.get('/')
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_invalid_ports_never_start_server(self):
        waitress = MagicMock()
        for value in ['0', '80', '65536', 'invalid']:
            with patch.dict(sys.modules, waitress=waitress), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(['--port', value])
                self.assertEqual(error.exception.code, 2)
        waitress.serve.assert_not_called()

    def test_missing_dependency_is_actionable(self):
        output = io.StringIO()
        with patch.dict(sys.modules, waitress=None), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as error:
                main([])
        self.assertEqual(error.exception.code, 1)
        self.assertIn('dashboard/requirements.txt', output.getvalue())

    def test_unit_uses_standalone_entrypoint_and_project_venv(self):
        unit = (ROOT / 'dashboard/ermis-dashboard.service.in').read_text()
        directives = dict(line.split('=', 1) for line in unit.splitlines()
                          if '=' in line and not line.startswith('#'))
        self.assertEqual(directives['User'], 'ermis')
        self.assertEqual(directives['WorkingDirectory'], str(ROOT))
        self.assertEqual(directives['ExecStart'],
                         f'{ROOT}/venv/bin/python -m dashboard.server --port 8010')
        self.assertEqual(directives['ProtectSystem'], 'strict')
        self.assertNotIn('EnvironmentFile', directives)
        self.assertNotIn('ExecStartPre', directives)
        self.assertNotIn('gateway', unit)


if __name__ == '__main__':
    unittest.main()
