"""Synthetic fixtures only; all temporary files stay inside the project."""
import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from mcp.server.mcpserver import MCPServer

import technical_reports as reports


class ReportsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd(), prefix='.report-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'reports'
        self.store = reports.ReportStore(self.root)
        self.addCleanup(lambda: self.store.close())

    def test_creation_read_once_delete(self):
        result = self.store.create({'files_checked': 3})
        rid = result['report_id']
        self.assertRegex(rid, r'^[0-9a-f]{64}$')
        path = self.root / rid
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertLessEqual(path.stat().st_size, reports.MAX_BYTES)
        self.assertEqual(self.store.read(rid)['report']['findings'], {'files_checked': 3})
        self.assertFalse(path.exists())
        self.assertEqual(self.store.read(rid), reports.ERROR)

    def test_secret_redaction_by_schema(self):
        findings = {'files_checked': 2, 'checks_passed': 'synthetic-secret',
                    'token': 'synthetic-token', 'stdout': 'raw-output',
                    'stderr': 'raw-error', 'environment': {'DEMO': 'value'},
                    'path': '/arbitrary/path', 'private_key': 'synthetic-key'}
        result = self.store.create(findings)
        payload = (self.root / result['report_id']).read_text()
        self.assertEqual(json.loads(payload), {'findings': {'files_checked': 2}, 'redacted': True})
        self.assertEqual(self.store.read(result['report_id'])['report'], json.loads(payload))

    def test_expiry_and_failed_read_cleanup(self):
        rid = self.store.create({'warnings': 1}, 1)['report_id']
        with patch.object(reports.os, 'unlink', side_effect=OSError('synthetic')):
            self.assertEqual(self.store.read(rid), reports.ERROR)
        # Background cleanup happens without another bridge call.
        deadline = time.monotonic() + 4
        while (self.root / rid).exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertFalse((self.root / rid).exists())
        self.assertEqual(self.store.read(rid), reports.ERROR)

    def test_expired_read(self):
        rid = self.store.create({}, 1)['report_id']
        with patch.object(reports.time, 'monotonic', return_value=time.monotonic() + 10):
            self.assertEqual(self.store.read(rid), reports.ERROR)
        self.assertFalse((self.root / rid).exists())

    def test_paths_unknown_ids_and_tampering(self):
        for value in ('../AGENTS.md', '/etc/passwd', 'a' * 64 + '/../x', '', None, [], 'a' * 64):
            self.assertEqual(self.store.read(value), reports.ERROR)
        forged = self.root / ('b' * 64)
        forged.write_text('synthetic forged content')
        self.assertEqual(self.store.read(forged.name), reports.ERROR)
        rid = self.store.create({'warnings': 1})['report_id']
        path = self.root / rid
        path.unlink()
        path.symlink_to(forged)
        self.assertEqual(self.store.read(rid)['report']['findings'], {'warnings': 1})
        self.assertEqual(forged.read_text(), 'synthetic forged content')

    def test_limits_and_unique_ids(self):
        for ttl in (0, 301, True, '1'):
            self.assertEqual(self.store.create({}, ttl), reports.ERROR)
        self.assertEqual(self.store.create('raw stdout'), reports.ERROR)
        ids = {self.store.create({})['report_id'] for _ in range(reports.MAX_REPORTS)}
        self.assertEqual(len(ids), reports.MAX_REPORTS)
        self.assertEqual(self.store.create({}), reports.ERROR)

    def test_restart_removes_orphans(self):
        orphan = self.root / ('c' * 64)
        orphan.write_text('synthetic orphan')
        # Close releases the ownership lock; reopen discards orphan names.
        self.store.close()
        self.store = reports.ReportStore(self.root)
        self.assertFalse(orphan.exists())

    def bridge(self):
        # Keep the real decorator; omit startup/credential loading entirely.
        tree = ast.parse(Path('ermis_system_server.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'ermis_technical_report_read')
        server = MCPServer('Report test')
        namespace = {'technical_reports': reports, 'mcp': server}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<test>', 'exec'), namespace)
        return server, namespace[node.name]

    def test_bridge_registration(self):
        server, _ = self.bridge()
        tool, = asyncio.run(server.list_tools())
        self.assertEqual(tool.name, 'ermis_technical_report_read')
        self.assertEqual(set(tool.input_schema['properties']), {'report_id'})
        self.assertEqual(tool.input_schema['required'], ['report_id'])
        self.assertEqual(tool.input_schema['properties']['report_id']['type'], 'string')

    def test_bridge_wrapper_without_server_startup(self):
        _, read = self.bridge()
        with patch.object(reports, '_store', self.store):
            rid = self.store.create({'checks_passed': 2, 'stdout': 'synthetic'})['report_id']
            self.assertEqual(read(rid), {'ok': True, 'report': {
                'findings': {'checks_passed': 2}, 'redacted': True}})
            self.assertFalse((self.root / rid).exists())
            self.assertEqual(read(rid), reports.ERROR)

    def test_bridge_invalid_ids_never_dispatch(self):
        _, read = self.bridge()
        with patch.object(reports, 'read') as dispatch:
            for value in ('../AGENTS.md', '/arbitrary/path', 'A' * 64,
                          'a' * 65, 'a' * 64 + '\n', '', None, [], 123):
                self.assertEqual(read(value), reports.ERROR)
            dispatch.assert_not_called()

    def test_bridge_delete_failure_withholds_content_and_allows_retry(self):
        _, read = self.bridge()
        rid = self.store.create({'warnings': 1})['report_id']
        with patch.object(reports, '_store', self.store):
            with patch.object(reports.os, 'unlink', side_effect=OSError('synthetic')):
                self.assertEqual(read(rid), reports.ERROR)
            self.assertTrue((self.root / rid).exists())
            self.assertTrue(read(rid)['ok'])
            self.assertFalse((self.root / rid).exists())
            self.assertEqual(read(rid), reports.ERROR)

    def test_bridge_concurrent_reads_consume_once(self):
        _, read = self.bridge()
        rid = self.store.create({})['report_id']
        with patch.object(reports, '_store', self.store), ThreadPoolExecutor(8) as pool:
            results = list(pool.map(read, [rid] * 16))
        self.assertEqual(sum(result['ok'] for result in results), 1)
        self.assertTrue(all(result == reports.ERROR for result in results if not result['ok']))
        self.assertFalse((self.root / rid).exists())


if __name__ == '__main__':
    unittest.main()
