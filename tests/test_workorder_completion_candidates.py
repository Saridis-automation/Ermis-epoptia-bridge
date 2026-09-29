"""Synthetic completion candidates; no credential loading or live requests."""
import json
import unittest
from unittest.mock import Mock, patch

import epoptia_read as read


class CompletionCandidatesTests(unittest.TestCase):
    def row(self, **fields):
        return dict(id=1, workorder={'id': 722, 'progress': 11}, **fields)

    def probe(self, rows):
        session = Mock(headers={}, cookies=[])
        session.post.return_value = Mock(status_code=200, json=lambda: {
            'workorderLines': rows, 'numberOfPages': 1})
        with patch.object(read.requests, 'Session') as factory, patch.object(
                read, '_web_login', return_value=True):
            factory.return_value.__enter__.return_value = session
            result = read.inspect_workorder_progress(
                'https://example.invalid', {}, 722,
                username='synthetic', password='synthetic')
        session.post.assert_called_once_with(
            'https://example.invalid/capacity-planning/workorderlines',
            json={'onlyList': True, 'page': 1}, timeout=20, allow_redirects=False)
        json.dumps(result, allow_nan=False)
        return result

    def test_absent_fields(self):
        candidates = self.probe([self.row()])['completion_candidates']
        self.assertEqual(candidates['fields'], {})
        self.assertEqual(candidates['conflicts'], {})
        self.assertEqual(candidates['scope'], 'already_fetched_linked_records')
        self.assertEqual(candidates['source_endpoint'], '/capacity-planning/workorderlines')

    def test_consistent_values_and_exact_source_paths(self):
        row = self.row(completionDate='2026-09-12T10:15:00.000Z')
        row['workorder']['completed_at'] = '12/09/2026 10:15'
        candidates = self.probe([row, row])['completion_candidates']
        self.assertEqual(candidates['fields'], {
            '/completionDate': ['2026-09-12T10:15:00.000Z'],
            '/workorder/completed_at': ['12/09/2026 10:15']})
        self.assertEqual(candidates['conflicts'], {})

    def test_conflicting_linked_records(self):
        rows = [self.row(completionDate=value) for value in (
            '2026-09-12', '2026-09-13', '2026-09-12')]
        for row in rows:
            row['workorder']['completedAt'] = row['completionDate']
        result = self.probe(rows)
        expected = {'/completionDate': ['2026-09-12', '2026-09-13'],
                    '/workorder/completedAt': ['2026-09-12', '2026-09-13']}
        self.assertEqual(result['completion_candidates']['fields'], expected)
        self.assertEqual(result['completion_candidates']['conflicts'], expected)
        self.assertEqual(result['native_progress'], 11)
        self.assertTrue(result['native_progress_verified'])
        self.assertEqual(result['sources'][0]['status'], 'ok')

    def test_active_records_do_not_imply_completion(self):
        row = self.row(status='started', completionDate=None, completedAt='')
        row['workorder']['completionDate'] = '2026-09-13'
        result = self.probe([row])
        self.assertEqual(result['completion_candidates']['fields'], {
            '/completionDate': [None], '/completedAt': [''],
            '/workorder/completionDate': ['2026-09-13']})
        self.assertEqual(result['native_progress'], 11)
        self.assertTrue(result['native_progress_verified'])

    def test_null_and_empty_are_distinct_from_missing(self):
        candidates = self.probe([
            self.row(), self.row(completionDate=None),
            self.row(completionDate=''), self.row(completionDate='2026-09-13'),
        ])['completion_candidates']
        self.assertEqual(candidates['conflicts'], {
            '/completionDate': [None, '', '2026-09-13']})

    def test_allowlist_only_and_unlinked_records_ignored(self):
        row = self.row(**{name: '2026-09-13' for name in read.WORKORDER_COMPLETION_FIELDS})
        row['workorder'].update({name: 1720000000000 for name in read.WORKORDER_COMPLETION_FIELDS})
        row.update(description='private-marker', plannedCompletionDate='2026-09-14',
                   metadata={'completionDate': '2026-09-15'})
        row['workorder']['notes'] = 'private-marker'
        unrelated = self.row(completionDate='2026-09-16')
        unrelated['workorder']['id'] = 721
        result = self.probe([row, unrelated])
        self.assertEqual(result['completion_candidates']['fields'], {
            **{'/' + name: ['2026-09-13'] for name in read.WORKORDER_COMPLETION_FIELDS},
            **{'/workorder/' + name: [1720000000000] for name in read.WORKORDER_COMPLETION_FIELDS}})
        self.assertNotIn('private-marker', str(result))
        self.assertEqual(result['completion_candidates']['conflicts'], {})

    def test_unsafe_values_are_omitted(self):
        for value in ('private-marker', {'value': 'private-marker'}, ['2026-09-13'],
                      True, float('nan'), float('inf'), -1, 10**30,
                      '2026-09-13\nprivate-marker', 'x' * 129):
            with self.subTest(value_type=type(value).__name__):
                result = self.probe([self.row(completionDate=value)])
                self.assertEqual(result['completion_candidates']['fields'], {})
                self.assertEqual(result['completion_candidates']['omitted_unsafe_values'], 1)
                self.assertNotIn('private-marker', str(result))
                self.assertEqual(result['native_progress'], 11)

    def test_native_progress_failure_is_independent(self):
        for progress in (None, 12):
            row = self.row(completionDate='2026-09-13')
            other = self.row(completionDate='2026-09-13')
            other['workorder']['progress'] = progress
            result = self.probe([row, other])
            self.assertFalse(result['native_progress_verified'])
            self.assertIsNone(result['native_progress'])
            self.assertEqual(result['completion_candidates']['fields'], {
                '/completionDate': ['2026-09-13']})
            self.assertEqual(result['completion_candidates']['conflicts'], {})


if __name__ == '__main__':
    unittest.main()
