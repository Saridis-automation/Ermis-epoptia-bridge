"""Synthetic fixtures only; no live requests or credential loading."""
import ast
import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests
import epoptia_read as read
from actual_completion import ACTUAL_LABEL, parse_actual_completion
from ermis_gateway import Gateway, validate


FIXTURES = Path(__file__).parent / 'fixtures' / 'actual_completion'


class ActualCompletionTests(unittest.TestCase):
    def setUp(self):
        # Parent-completion transport assertions isolate the independent calendar reader.
        calendar = patch('calendar_target_dates.refresh_calendar', return_value={
            'status': 'calendar_auth_missing', 'reason': 'calendar_auth_missing', 'records': []})
        calendar.start()
        self.addCleanup(calendar.stop)
        # Discovery transport is covered independently; keep these assertions
        # focused on the existing parent-completion request contract.
        discovery = patch('report_discovery.discover_workorder_reports', return_value={})
        discovery.start()
        self.addCleanup(discovery.stop)

    def assertCompletion(self, actual, expected):
        self.assertEqual({k: v for k, v in actual.items() if k != 'dom_diagnostics'}, expected)

    def fixture(self, name):
        return (FIXTURES / (name + '.html')).read_text()

    def test_active_empty_excludes_scheduled(self):
        result = parse_actual_completion(self.fixture('active'), 701)
        self.assertCompletion(result, dict(date=None, source_endpoint='/workorders/701',
                                     verified_label=ACTUAL_LABEL, reason='not_completed'))
        self.assertNotIn('2026-09-18', json.dumps(result))

    def test_completed(self):
        result = parse_actual_completion(self.fixture('completed'), 722)
        self.assertEqual(result['date'], '2026-09-12')
        self.assertIsNone(result['reason'])

    def test_ambiguous_and_missing(self):
        for fixture, reason in [('ambiguous', 'ambiguous_label'), ('missing', 'label_missing')]:
            with self.subTest(fixture=fixture):
                result = parse_actual_completion(self.fixture(fixture), 701)
                self.assertIsNone(result['date'])
                self.assertEqual(result['reason'], reason)

    def test_strict_values_and_associations(self):
        for value in ('31/02/2026', 'private-marker', '2026-09-12 extra', '2026-09-12T00:00:00'):
            with self.subTest(value=value):
                result = parse_actual_completion(f'<div><span>{ACTUAL_LABEL}</span><span>{value}</span></div>', 701)
                self.assertIsNone(result['date'])
                self.assertNotIn('private-marker', json.dumps(result))
        html = f'<div><span>{ACTUAL_LABEL}</span><span>-</span><span>2026-09-18</span></div>'
        self.assertEqual(parse_actual_completion(html, 701)['reason'], 'ambiguous_value')

    def test_supported_display_layouts(self):
        for html in (f'<dl><dt>{ACTUAL_LABEL}</dt><dd>2026-09-12</dd></dl>',
                     f'<div><span>{ACTUAL_LABEL}</span><span>12-09-2026</span></div>'):
            self.assertEqual(parse_actual_completion(html, 722)['date'], '2026-09-12')

    def test_dom_diagnostics_values_and_structure(self):
        for value, values, parsed in (
                ('-', ['-'], []), ('12-09-2026', ['12-09-2026'], ['2026-09-12']),
                ('2026-09-12', ['2026-09-12'], ['2026-09-12']),
                ('31-02-2026', ['31-02-2026'], []), ('12/09/2026', [], [])):
            with self.subTest(value=value):
                html = (f'<div class="form-group row"><label for="actual">{ACTUAL_LABEL}</label>'
                        f'<input id="actual" name="actual_date" value="{value}"></div>')
                diagnostics = parse_actual_completion(html, 701)['dom_diagnostics']
                self.assertEqual(diagnostics, dict(exact_label_occurrences=1,
                    candidate_count=1 if value != '12/09/2026' else 0,
                    candidate_values=values, parsed_date_candidates=parsed,
                    candidates=([dict(parent_tag='div', parent_classes=['form-group', 'row'],
                                     sibling_or_control_type='input', input_name='actual_date')]
                                if value != '12/09/2026' else [])))

    def test_dom_diagnostics_cannot_leak_page_text_or_attributes(self):
        for value in ('private-marker', '&lt;b&gt;private-marker&lt;/b&gt;',
                      '2026-09-12 private-marker', '2026-09-12T00:00:00'):
            html = (f'<p>unrelated-page-text</p><div title="private-title">'
                    f'<span>{ACTUAL_LABEL}</span><span data-extra="private-data">{value}</span>'
                    '</div><script>private-script</script>')
            result = parse_actual_completion(html, 701)
            self.assertEqual(result['dom_diagnostics']['candidate_values'], [])
            self.assertEqual(result['dom_diagnostics']['parsed_date_candidates'], [])
            serialized = json.dumps(result)
            for forbidden in ('private-', 'unrelated-page-text', '<', '>'):
                self.assertNotIn(forbidden, serialized)

    def test_dom_diagnostics_bounds_and_attribute_sanitization(self):
        html = (f'<private-tag class="row &lt;private-markup&gt; {"x" * 100}">'
                f'<label for="actual">{ACTUAL_LABEL}</label>'
                '<input id="actual" name="&lt;private-name&gt;" value="-"></private-tag>')
        diagnostics = parse_actual_completion(html, 701)['dom_diagnostics']
        self.assertEqual(diagnostics['candidates'], [dict(parent_tag='other',
            parent_classes=['row'], sibling_or_control_type='input')])
        self.assertNotIn('private-', json.dumps(diagnostics))
        row = (f'<div class="{" ".join("class" + str(i) for i in range(12))}">'
               f'<span>{ACTUAL_LABEL}</span><span>2026-09-12</span></div>')
        result = parse_actual_completion(row * 25, 701)
        self.assertEqual(result['reason'], 'ambiguous_label')
        diagnostics = result['dom_diagnostics']
        self.assertEqual(diagnostics['exact_label_occurrences'], 25)
        self.assertEqual(diagnostics['candidate_count'], 25)
        for key in ('candidates', 'candidate_values', 'parsed_date_candidates'):
            self.assertEqual(len(diagnostics[key]), 20)
        self.assertEqual(len(diagnostics['candidates'][0]['parent_classes']), 8)

    def test_dom_diagnostics_missing_hidden_and_uncertain(self):
        for html, occurrences in (
                ('<div>other-page-text</div>', 0),
                (f'<div hidden><span>{ACTUAL_LABEL}</span><span>2026-09-12</span></div>', 0),
                (f'<script>{ACTUAL_LABEL}</script>', 0)):
            diagnostics = parse_actual_completion(html, 701)['dom_diagnostics']
            self.assertEqual(diagnostics, dict(exact_label_occurrences=occurrences,
                candidate_count=0, candidate_values=[], parsed_date_candidates=[], candidates=[]))

    def test_local_layouts_for_active_and_completed_workorders(self):
        layouts = (
            '<table><tr><th><b>{label}</b></th><td><span>{value}</span></td>'
            '<td>unrelated help</td></tr></table>',
            '<div class="row"><div class="col-sm-4"><label>{label}</label></div>'
            '<div class="col-sm-8"><div><span>{value}</span></div><i>help</i></div></div>',
            '<div class="row"><div><label>{label}</label></div>'
            '<div><span>{value}</span></div><i>help</i></div>',
            '<div class="form-group"><label for="actual"><b>{label}</b></label>'
            '<div><input id="actual" value="{value}"></div><span>help</span></div>',
            '<div class="form-group"><label for="actual">{label}</label>'
            '<select id="actual" value="{value}"><option>ignored text</option></select></div>',
            '<label for="actual">{label}</label><input id="actual" value="{value}">',
        )
        for workorder_id in (701, 326):
            for layout in layouts:
                for value, date in (('-', None), ('12-09-2026', '2026-09-12'),
                                    ('2026-09-12', '2026-09-12')):
                    with self.subTest(workorder_id=workorder_id, layout=layout, value=value):
                        result = parse_actual_completion(layout.format(label=ACTUAL_LABEL, value=value), workorder_id)
                        self.assertEqual(result['date'], date)
                        self.assertEqual(result['reason'], None if date else 'not_completed')
                        self.assertEqual(result['dom_diagnostics']['candidate_count'], 1)

    def test_local_ambiguity_and_nearest_duplicate(self):
        for second, reason in (('2026-09-13', 'ambiguous_value'),
                               ('31-02-2026', 'ambiguous_value'),
                               ('-', 'ambiguous_value'), ('12-09-2026', None)):
            html = (f'<div class="row"><label>{ACTUAL_LABEL}</label>'
                    f'<div><span>{second}</span></div><span>2026-09-12</span></div>')
            result = parse_actual_completion(html, 701)
            self.assertEqual(result['reason'], reason)
            self.assertEqual(result['dom_diagnostics']['candidate_values'][0], '2026-09-12')
            self.assertEqual(result['date'], '2026-09-12' if reason is None else None)

    def test_never_cross_field_boundaries_or_follow_external_for(self):
        for local in (
            f'<div class="form-group"><label for="outside">{ACTUAL_LABEL}</label></div>',
            f'<div><span>{ACTUAL_LABEL}</span></div>',
            f'<div><label>{ACTUAL_LABEL}</label><div class="row">'
            '<span>2026-09-18</span></div></div>',
            f'<table><tr><th>{ACTUAL_LABEL}</th><td>unavailable</td></tr>'
            '<tr><td>2026-09-18</td></tr></table>',
        ):
            html = (f'<main>{local}<input id="outside" value="2026-09-18">'
                    '<div><span>Ημ. ολοκλ. παραγωγής</span><span>2026-09-18</span></div></main>')
            result = parse_actual_completion(html, 701)
            self.assertIsNone(result['date'])
            self.assertEqual(result['dom_diagnostics']['candidate_count'], 0)
            self.assertNotIn('2026-09-18', json.dumps(result))

    def test_scheduled_and_hidden_values_inside_row_are_excluded(self):
        for scheduled in (
            '<div><label>Ημ. ολοκλ. παραγωγής</label><input value="2026-09-18"></div>',
            '<span>Ημ. ολοκλ. παραγωγής</span><span>2026-09-18</span>',
        ):
            html = (f'<div class="row"><label>{ACTUAL_LABEL}</label><span>-</span>'
                    '<div><span hidden>2026-09-19</span></div>' + scheduled + '</div>')
            result = parse_actual_completion(html, 326)
            self.assertEqual(result['reason'], 'not_completed')
            self.assertEqual(result['dom_diagnostics']['candidate_values'], ['-'])

    def test_only_dash_and_hyphen_dates_are_accepted(self):
        for value in ('', '–', '—', '12/09/2026', '31-02-2026', 'private-marker'):
            result = parse_actual_completion(
                f'<div><label>{ACTUAL_LABEL}</label><input value="{value}"></div>', 701)
            self.assertIsNone(result['date'])
            self.assertEqual(result['reason'], 'ambiguous_value')

    def test_dom_diagnostics_reaches_progress(self):
        result, _ = self.probe()
        self.assertEqual(result['actual_production_completion']['dom_diagnostics'], dict(
            exact_label_occurrences=1, candidate_count=1, candidate_values=['-'],
            parsed_date_candidates=[], candidates=[dict(parent_tag='tr', parent_classes=[],
                                                        sibling_or_control_type='td')]))

    def probe(self, response=None, error=None, enabled=True, authenticated=True):
        session = Mock(headers={}, cookies=[])
        session.post.return_value = Mock(status_code=200, json=lambda: {
            'workorderLines': [{'completionDate': '2026-09-18',
                               'dbCompletionDate': '2026-09-18',
                               'displayCompletionDate': '18/09/2026',
                               'workorder': {'id': 701, 'progress': 11}}], 'numberOfPages': 1})
        session.get.return_value = response or Mock(status_code=200, text=self.fixture('active'))
        session.get.side_effect = error
        with patch.object(read.requests, 'Session') as factory, patch.object(
                read, '_web_login', return_value=authenticated):
            factory.return_value.__enter__.return_value = session
            result = read.inspect_workorder_progress('https://example.invalid/', {}, 701,
                username='synthetic', password='synthetic',
                include_actual_production_completion=enabled)
        return result, session

    def test_existing_session_and_wol_dates_excluded(self):
        result, session = self.probe()
        self.assertEqual(result['native_progress'], 11)
        self.assertIsNone(result['actual_production_completion']['date'])
        session.get.assert_called_once_with('https://example.invalid/workorders/701',
            headers={'Accept': 'text/html'}, timeout=20, allow_redirects=False)
        self.assertNotIn('<', json.dumps(result))

    def test_parent_failure_preserves_progress(self):
        for response, error, reason in (
                (None, requests.Timeout('private-marker'), 'upstream_timeout'),
                (None, requests.ConnectionError('private-marker'), 'read_unavailable'),
                (Mock(status_code=503), None, 'upstream_http_error'),
                (Mock(status_code=302), None, 'upstream_http_error'),
                (Mock(status_code=200, text='<html>private-marker</html>'), None, 'label_missing')):
            with self.subTest(reason=reason):
                result, _ = self.probe(response, error)
                self.assertTrue(result['native_progress_verified'])
                self.assertEqual(result['native_progress'], 11)
                self.assertEqual(result['actual_production_completion']['reason'], reason)
                self.assertNotIn('private-marker', json.dumps(result))

    def test_false_flag_still_reads_and_login_failure_still_returns_object(self):
        result, session = self.probe(enabled=False)
        session.get.assert_called_once()
        self.assertEqual(result['actual_production_completion']['reason'], 'not_completed')
        result, session = self.probe(authenticated=False)
        session.get.assert_not_called()
        self.assertEqual(result['actual_production_completion']['reason'], 'login_failed')

    def test_parser_exceptions_are_safe_and_preserve_existing_fields(self):
        baseline, _ = self.probe()
        for error in (ValueError, RecursionError, RuntimeError):
            with self.subTest(error=error.__name__), patch(
                    'actual_completion.parse_actual_completion',
                    side_effect=error('private-marker')):
                result, _ = self.probe()
                self.assertEqual(result.pop('actual_production_completion'), dict(
                    date=None, source_endpoint='/workorders/701',
                    verified_label=ACTUAL_LABEL, reason='invalid_page'))
                self.assertEqual(result, {k: v for k, v in baseline.items()
                                         if k != 'actual_production_completion'})
                self.assertNotIn('private-marker', json.dumps(result))

    def test_progress_scan_failure_still_reads_parent(self):
        with patch.object(read, '_scan_production_pages', return_value=False):
            result, session = self.probe(response=Mock(
                status_code=200, text=self.fixture('completed')))
        self.assertIsNone(result['native_progress'])
        self.assertFalse(result['native_progress_verified'])
        self.assertIn('completion_candidates', result)
        self.assertCompletion(result['actual_production_completion'], dict(
            date='2026-09-12', source_endpoint='/workorders/701',
            verified_label=ACTUAL_LABEL, reason=None))
        session.get.assert_called_once()

    def test_default_tool_and_gateway_return_path(self):
        # Execute the real tool body without importing startup/credential code.
        node = next(n for n in ast.parse(Path('mcp_server.py').read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'inspect_workorder_progress')
        node.decorator_list = []
        scope = dict(epoptia_read=read, BASE_URL='https://example.invalid', HEADERS={},
                     WEB_USERNAME='synthetic', WEB_PASSWORD='synthetic')
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<synthetic>', 'exec'), scope)

        async def invoke(server, tool, arguments):
            self.assertEqual((server, tool), ('Epoptia_MES', 'inspect_workorder_progress'))
            return scope[tool](**arguments)

        for workorder_id in (701, 326):
            for html, date, reason in (
                    (self.fixture('completed'), '2026-09-12', None),
                    ('<div><span>Ημ. ολοκλ. παραγωγής</span><span>2026-09-18</span></div>',
                     None, 'label_missing')):
                for gateway in (False, True):
                    with self.subTest(workorder_id=workorder_id, gateway=gateway, reason=reason):
                        session = Mock(headers={}, cookies=[])
                        session.post.return_value = Mock(status_code=200, json=lambda: {
                            'workorderLines': [], 'numberOfPages': 0})
                        session.get.return_value = Mock(status_code=200, text=html)
                        with patch.object(read.requests, 'Session') as factory, patch.object(
                                read, '_web_login', return_value=True):
                            factory.return_value.__enter__.return_value = session
                            if gateway:
                                envelope = asyncio.run(Gateway(invoke=invoke).execute(
                                    'workorder_progress', {'workorder_id': workorder_id}))
                                self.assertEqual(envelope['status'], 'completed')
                                result = envelope['result']
                            else:
                                result = scope['inspect_workorder_progress'](workorder_id)
                        self.assertCompletion(result['actual_production_completion'], dict(
                            date=date, source_endpoint=f'/workorders/{workorder_id}',
                            verified_label=ACTUAL_LABEL, reason=reason))
                        self.assertIn('native_progress', result)
                        self.assertIn('completion_candidates', result)
                        session.get.assert_called_once_with(
                            f'https://example.invalid/workorders/{workorder_id}',
                            headers={'Accept': 'text/html'}, timeout=20, allow_redirects=False)

    def test_gateway_optional_boolean(self):
        for args in ({'workorder_id': 701}, {'workorder_id': 701, 'include_actual_production_completion': True}):
            self.assertEqual(validate('workorder_progress', args).tool, 'inspect_workorder_progress')
        for value in ('true', 1, None):
            with self.assertRaises(ValueError):
                validate('workorder_progress', {'workorder_id': 701, 'include_actual_production_completion': value})
            with self.assertRaises(ValueError):
                read.inspect_workorder_progress('', {}, 701, include_actual_production_completion=value)

    def test_tool_forwards_option_without_importing_server(self):
        node = next(n for n in ast.parse(Path('mcp_server.py').read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'inspect_workorder_progress')
        node.decorator_list = []
        reader = Mock()
        reader.inspect_workorder_progress.return_value = {}
        scope = dict(epoptia_read=reader, BASE_URL='', HEADERS={}, WEB_USERNAME=None, WEB_PASSWORD=None)
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<synthetic>', 'exec'), scope)
        self.assertTrue(scope['inspect_workorder_progress'](701, True)['ok'])
        self.assertTrue(reader.inspect_workorder_progress.call_args.kwargs['include_actual_production_completion'])


if __name__ == '__main__':
    unittest.main()
