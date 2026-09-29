"""Shared Epoptia request guard: spacing, serialization and the 429/403 halt."""
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

import epoptia_throttle
from epoptia_throttle import EpoptiaHalted, Throttle


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(round(seconds, 3))
        self.now += seconds


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = directory.name
        self.clock = Clock()
        self.throttle = Throttle(self.dir, clock=self.clock, sleep=self.clock.sleep)

    def send(self, status=200, write=False, url='https://synthetic.invalid/api/3.03/workorderlines?page=1'):
        fn = Mock(return_value=Mock(status_code=status))
        return self.throttle.call(fn, url, timeout=5, write=write), fn

    def test_first_request_is_immediate_and_arguments_pass_through(self):
        response, fn = self.send()
        self.assertEqual(response.status_code, 200)
        fn.assert_called_once_with('https://synthetic.invalid/api/3.03/workorderlines?page=1', timeout=5)
        self.assertEqual(self.clock.slept, [])

    def test_reads_are_one_second_apart(self):
        self.send()
        self.clock.now += 0.25
        self.send()
        self.assertEqual(self.clock.slept, [0.75])
        self.clock.now += 5
        self.send()
        self.assertEqual(self.clock.slept, [0.75])

    def test_writes_are_two_seconds_apart_on_both_sides(self):
        self.send()
        self.send(write=True)
        self.send()
        self.assertEqual(self.clock.slept, [2.0, 2.0])

    def test_spacing_is_shared_between_throttle_instances(self):
        # Another process sees the same state directory and waits too.
        self.send()
        other = Throttle(self.dir, clock=self.clock, sleep=self.clock.sleep)
        other.call(Mock(return_value=Mock(status_code=200)), 'https://synthetic.invalid/x')
        self.assertEqual(self.clock.slept, [1.0])

    def test_429_and_403_halt_everything_without_retry(self):
        for status in (429, 403):
            with self.subTest(status=status):
                response = Mock(status_code=status)
                fn = Mock(return_value=response)
                with self.assertRaises(EpoptiaHalted):
                    self.throttle.call(fn, 'https://synthetic.invalid/api/3.03/workorders?token=PRIVATE',
                                       write=True)
                fn.assert_called_once()
                response.close.assert_called_once()
                record = self.throttle.halted()
                self.assertEqual(record['http_status'], status)
                self.assertEqual(record['path'], '/api/3.03/workorders')
                self.assertTrue(record['write'])
                self.assertNotIn('PRIVATE', json.dumps(record))
                blocked = Mock()
                with self.assertRaises(EpoptiaHalted):
                    self.throttle.call(blocked, 'https://synthetic.invalid/api/3.03/workorderlines')
                blocked.assert_not_called()
                self.assertTrue(self.throttle.clear())
                self.assertIsNone(self.throttle.halted())

    def test_other_errors_do_not_halt(self):
        for status in (401, 404, 500, 503):
            self.assertEqual(self.send(status)[0].status_code, status)
        self.assertIsNone(self.throttle.halted())

    def test_exception_still_records_the_request_time(self):
        with self.assertRaises(RuntimeError):
            self.throttle.call(Mock(side_effect=RuntimeError('boom')), 'https://synthetic.invalid/x')
        self.send()
        self.assertEqual(self.clock.slept, [1.0])

    def test_unreadable_halt_file_counts_as_halted(self):
        with open(self.throttle.halt_path, 'w') as handle:
            handle.write('not json')
        self.assertEqual(self.throttle.halted(), {'reason': 'halt_file_invalid'})
        with self.assertRaises(EpoptiaHalted):
            self.send()

    def test_requests_never_overlap_across_threads(self):
        throttle = Throttle(self.dir, sleep=lambda seconds: None)
        active, peak, lock = [0], [0], threading.Lock()

        def request(url):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.02)
            with lock:
                active[0] -= 1
            return Mock(status_code=200)

        threads = [threading.Thread(target=throttle.call, args=(request, 'https://synthetic.invalid/x'))
                   for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(peak[0], 1)

    def test_busy_lock_times_out(self):
        throttle = Throttle(self.dir, lock_timeout=0.1)
        entered, release = threading.Event(), threading.Event()

        def slow(url):
            entered.set()
            release.wait(2)
            return Mock(status_code=200)

        worker = threading.Thread(target=throttle.call, args=(slow, 'https://synthetic.invalid/x'))
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(release.set)
        entered.wait(1)
        with self.assertRaises(epoptia_throttle.EpoptiaBusy):
            throttle.call(Mock(), 'https://synthetic.invalid/y')

    def test_cli_status_and_clear_require_confirmation(self):
        original = epoptia_throttle._default
        self.addCleanup(setattr, epoptia_throttle, '_default', original)
        epoptia_throttle._default = self.throttle
        self.assertEqual(epoptia_throttle.main(['status']), 0)
        with self.assertRaises(EpoptiaHalted):
            self.send(429)
        self.assertEqual(epoptia_throttle.main(['status']), 1)
        self.assertEqual(epoptia_throttle.main(['clear']), 2)
        self.assertIsNotNone(self.throttle.halted())
        self.assertEqual(epoptia_throttle.main(['clear', '--confirm']), 0)
        self.assertIsNone(self.throttle.halted())


if __name__ == '__main__':
    unittest.main()
