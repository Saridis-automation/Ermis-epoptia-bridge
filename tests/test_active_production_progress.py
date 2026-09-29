"""Synthetic aggregate tests; no credential loading or live requests."""
import ast
from pathlib import Path
import unittest
from unittest.mock import ANY, Mock, patch

import requests
import epoptia_read as read


def row(order, progress, wol, **fields):
    return dict(id=wol, status='production', progress=99,
                workorder=dict(id=order, progress=progress), **fields)


class ActiveProgressTests(unittest.TestCase):
    def probe(self, pages, login=True):
        session = requests.Session()
        session.post = Mock(side_effect=[
            item if isinstance(item, Exception) else Mock(status_code=200, json=lambda item=item: item)
            for item in pages])
        with patch.object(read.requests, 'Session', return_value=session), patch.object(
                read, '_web_login', return_value=login):
            result = read.active_production_progress('https://example.invalid',
                                                     username='synthetic', password='synthetic')
        for page, call in enumerate(session.post.call_args_list, 1):
            self.assertEqual(call.args, ('https://example.invalid/capacity-planning/workorderlines',))
            self.assertEqual(call.kwargs['json'], {'onlyList': True, 'page': page})
        self.assertNotIn('private', str(result))
        return result

    def test_distinct_orders_all_pages_and_conflicts(self):
        result = self.probe([
            {'capacityPlanningData': {'productionData': {'past': [
                row(1, 10, 1), row(2, 80, 2), row(3, None, 3),
                row(4, 100, 4, production_status='completed')]}}},
            {'capacityPlanningData': {'productionData': {'future': [
                row('1', '10', 5), row(2, 90, 6),
                row(5, 50, 7, production_status='standby')]}}}, []])
        self.assertEqual(result['active_workorders_total'], 4)
        self.assertEqual(result['active_workorders_with_native_progress'], 2)
        self.assertEqual(result['native_progress_coverage_percent'], 50)
        self.assertEqual(result['native_active_production_progress_percent'], 30)
        self.assertEqual(result['native_progress_conflict_count'], 1)
        self.assertEqual(result['native_active_production_progress_source']['pages_read'], 3)

    def test_invalid_values_and_identities(self):
        invalid = [None, True, -1, 101, float('nan'), float('inf'), 'private', {}]
        rows = [row(i + 1, value, i + 1) for i, value in enumerate(invalid)]
        rows += [row(True, 20, 20), row(None, 20, 21), row(9, 0, 22), row(10, 100, 23)]
        result = self.probe([{'workorderLines': rows, 'numberOfPages': 1}])
        self.assertEqual(result['active_workorders_total'], 10)
        self.assertEqual(result['active_workorders_with_native_progress'], 2)
        self.assertEqual(result['native_active_production_progress_percent'], 50)

    def test_invalid_duplicate_excluded(self):
        result = self.probe([[row(1, 10, 1)], [row(1, None, 2)], []])
        self.assertEqual(result['active_workorders_with_native_progress'], 0)
        self.assertIsNone(result['native_active_production_progress_percent'])

    def test_empty(self):
        result = self.probe([[]])
        self.assertEqual(result['active_workorders_total'], 0)
        self.assertEqual(result['native_progress_coverage_percent'], 0)
        self.assertIsNone(result['native_active_production_progress_percent'])

    def test_incomplete_scans_fail_closed(self):
        for pages in ([[],], [[row(1, 10, 1)], requests.Timeout('private')],
                      [[row(1, 10, 1)], [row(1, 10, 1)]],
                      [{'workorderLines': [], 'numberOfPages': 2}],
                      [{'private': 'private'}]):
            result = self.probe(pages, login=pages != [[]])
            self.assertIsNone(result['active_workorders_total'])
            self.assertIsNone(result['native_active_production_progress_percent'])
            self.assertFalse(result['native_active_production_progress_source']['complete'])

    def test_wrapper_preserves_existing_fields(self):
        tree = ast.parse(Path('mcp_server.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'production_overview')
        node.decorator_list = []
        existing = {'ok': True, **read.overview([])}
        scope = dict(epoptia_read=read, _read_query=Mock(return_value=existing.copy()),
                     BASE_URL='https://example.invalid', HEADERS={}, WEB_USERNAME='synthetic', WEB_PASSWORD='synthetic')
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<test>', 'exec'), scope)
        with patch.object(read, 'active_production_progress', return_value={
                'active_workorders_total': 0,
                'native_active_production_progress_source': {'complete': True}}) as aggregate, patch(
                    'epoptia_queries.read_wol_snapshot', return_value={'deadlines': {
                        'source': {'complete': True, 'status': 'ok'}, 'dates': {}}}) as snapshot:
            result = scope['production_overview']()
        self.assertEqual({k: result[k] for k in existing}, existing)
        self.assertEqual(result['active_workorders_total'], 0)
        aggregate.assert_called_once_with('https://example.invalid', username='synthetic',
                                         password='synthetic', consume_rows=ANY)
        snapshot.assert_called_once_with('https://example.invalid', {})
        self.assertEqual(result['dashboard_orders']['canonical_version'], 2)
        self.assertEqual(result['dashboard_orders']['urgent_orders'], [])
        self.assertEqual(result['dashboard_orders']['overdue_work'], 0)
