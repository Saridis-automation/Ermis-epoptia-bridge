"""Synthetic routing evidence tests: no server import, credentials or network."""
import ast
import copy
import json
from pathlib import Path
import unittest

import epoptia_read as reader


def status_tool(row, legacy=False):
    tree = ast.parse(Path('mcp_server.py').read_text())
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == 'get_wol_status')
    node.decorator_list = []
    if legacy:
        for block in ast.walk(node):
            if isinstance(block, ast.If):
                block.body = [n for n in block.body if not (
                    isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                    and isinstance(n.value.func, ast.Attribute)
                    and n.value.func.attr == 'update')]
    scope = {'find_wol': lambda _: row, 'epoptia_read': reader}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'mcp_server.py', 'exec'), scope)
    return scope['get_wol_status'](1)


class RoutingCompletionTests(unittest.TestCase):
    def test_status_progress_and_existing_output_unchanged(self):
        steps = [None] + [dict(status=s, workstationName='Station A',
                              job_tag={'name': 'Cut', 'id': 12}, id=42,
                              qty_done=3, completedAt='2026-09-13T10:00:00Z')
                          for s in ('completed', 'started', 'paused', 'in_progress',
                                    'not_started', 'unknown', 'Completed', None)]
        row = {'workorderline_id': 1, 'erp_routing': steps}
        before = copy.deepcopy(row)
        result = status_tool(row)
        item = result['completed'][0]
        self.assertEqual(item.pop('completion_candidates'), {
            '/erp_routing/1/completedAt': '2026-09-13T10:00:00Z'})
        self.assertEqual(item.pop('step_identifiers'), {
            '/erp_routing/1/id': 42, '/erp_routing/1/job_tag/id': 12})
        self.assertEqual(result, status_tool(row, legacy=True))
        self.assertEqual(row, before)
        self.assertEqual(result['progress'], reader.routing_progress(steps))

    def test_exact_paths_and_only_immediate_allowlisted_fields(self):
        step = {'status': 'completed', 'finished_at': '2026-09-13',
                'execution': {'endTime': '2026-09-13T11:00:00+02:00',
                              'history': {'timestamp': 100}},
                'history': {'timestamp': 1789293600000, 'comment': 'omit'},
                'job_tag': {'completedAt': '2000-01-01'},
                'product': {'endDate': '2000-01-01'},
                'completionDateSecret': '2000-01-01', 'username': 'omit',
                'comments': 'omit', 'history_url': 'https://example.invalid'}
        self.assertEqual(reader.routing_completion_details(step, 4), {
            'completion_candidates': {
                '/erp_routing/4/finished_at': '2026-09-13',
                '/erp_routing/4/execution/endTime': '2026-09-13T11:00:00+02:00',
                '/erp_routing/4/history/timestamp': 1789293600000}})

    def test_unsafe_values_omitted_even_under_allowed_names(self):
        for value in ('token=synthetic', 'Jane Doe', 'arbitrary comment',
                      'https://example.invalid', 'person@example.invalid',
                      'abc123opaque', '2026-09-13\n', 'x' * 65,
                      float('inf'), float('nan'), -1, 10**100, True,
                      ['2026-09-13'], {'date': '2026-09-13'}):
            with self.subTest(value_type=type(value).__name__):
                step = {'status': 'completed', 'completedAt': value,
                        'execution': {'endDate': value}, 'history': {'timestamp': value}}
                self.assertEqual(reader.routing_completion_details(step, 0),
                                 {'completion_candidates': {}})
        for value in ('Jane Doe', 'https://example.invalid', 'token=synthetic', True, 10**100):
            self.assertNotIn('step_identifiers', reader.routing_completion_details(
                {'status': 'completed', 'id': value}, 0))

    def test_bounds_and_only_allowlisted_names(self):
        fields = dict.fromkeys(reader.ROUTING_COMPLETION_FIELDS, '2026-09-13T10:00:00Z')
        step = {'status': 'completed', **fields, 'execution': fields, 'history': fields,
                **{f'updatedSecret{i}': '2026-09-13' for i in range(1000)}}
        result = reader.routing_completion_details(step, 0)
        self.assertEqual(len(result['completion_candidates']), 32)
        self.assertLess(len(json.dumps(result)), 4000)
        for path in result['completion_candidates']:
            self.assertIn(path.rsplit('/', 1)[-1], reader.ROUTING_COMPLETION_FIELDS)

    def test_no_invented_timestamp_or_non_completed_candidates(self):
        self.assertEqual(reader.routing_completion_details({'status': 'completed'}, 0),
                         {'completion_candidates': {}})
        for value in (None, '', 0, 1789293600, '13/09/2026 10:00'):
            result = reader.routing_completion_details(
                {'status': 'completed', 'completedAt': value}, 0)
            self.assertEqual(result['completion_candidates'], {'/erp_routing/0/completedAt': value})
        for status in ('started', 'paused', 'in_progress', 'Completed', None):
            self.assertEqual(reader.routing_completion_details(
                {'status': status, 'completedAt': '2026-09-13'}, 0), {})
        for routing in (None, {}, [], [None]):
            row = {'erp_routing': routing, 'completionDate': '2026-09-13'}
            self.assertEqual(status_tool(row), status_tool(row, legacy=True))
        self.assertEqual(status_tool(None), status_tool(None, legacy=True))


if __name__ == '__main__':
    unittest.main()
