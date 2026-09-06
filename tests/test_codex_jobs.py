"""No real Codex calls, dotenv loading, services, or production state access."""
import ast
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

import codex_jobs as jobs


class Input(io.BytesIO):
    def close(self):
        self.sent = self.getvalue()
        super().close()


class FakeProcess:
    def __init__(self, output=b'', code=0, gate=None):
        self.stdin = Input()
        self.stdout = io.BytesIO(output)
        self.code = code
        self.gate = gate

    def wait(self):
        if self.gate is not None and not self.gate.wait(5):
            raise AssertionError('Test worker timed out')
        return self.code


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        directory = patch.object(jobs, '_directory', return_value=self.root)
        directory.start()
        self.addCleanup(directory.stop)
        self.workers = []
        original_thread = threading.Thread
        self.thread_class = original_thread

        def thread(*args, **kwargs):
            worker = original_thread(*args, **kwargs)
            self.workers.append(worker)
            return worker

        factory = patch.object(jobs.threading, 'Thread', side_effect=thread)
        factory.start()
        self.addCleanup(factory.stop)
        self.popen = patch.object(jobs.subprocess, 'Popen')
        self.launch = self.popen.start()
        # An unconfigured MagicMock stream never reaches EOF.
        self.launch.return_value = FakeProcess()
        self.addCleanup(self.popen.stop)
        self.addCleanup(self.join_workers)

    def join_workers(self):
        for worker in self.workers:
            if worker.ident is not None:
                worker.join(6)
                self.assertFalse(worker.is_alive())

    def record(self, state='completed'):
        job = {'job_id': 'a' * 32, 'state': state, 'started_at': jobs._now(),
               'finished_at': jobs._now() if state != 'running' else None,
               'exit_code': 0 if state == 'completed' else None,
               'lines': [jobs.ACCEPTED, jobs.COMPLETED]}
        jobs._write(self.root, job)
        return job

    def test_task_validation_never_spawns(self):
        for task in ('', ' \n\t', None, 1, 'x' * 16001, 'a\x00b'):
            with self.subTest(task_type=type(task).__name__):
                self.assertFalse(jobs.start(task)['ok'])
        self.launch.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_and_unknown_ids(self):
        for job_id in ('', '../anything', '/etc/passwd', 'A' * 32, 'a' * 32 + '\n', None, 42, 'b' * 32):
            self.assertFalse(jobs.status(job_id)['ok'])
            self.assertFalse(jobs.logs(job_id)['ok'])
        self.launch.assert_not_called()

    def test_completed_persistence_and_tail_clamping(self):
        job = self.record()
        self.assertEqual(jobs.status(job['job_id'])['state'], 'completed')
        self.assertIsNotNone(jobs.status(job['job_id'])['finished_at'])
        self.assertEqual(jobs.logs(job['job_id'], -9)['lines'], [jobs.COMPLETED])
        self.assertEqual(jobs.logs(job['job_id'], 999999)['tail_lines'], 500)
        for limit in (True, '100', None, 1.5):
            self.assertFalse(jobs.logs(job['job_id'], limit)['ok'])
        self.assertEqual(stat.S_IMODE((self.root / (job['job_id'] + '.json')).stat().st_mode), 0o600)
        self.launch.assert_not_called()

    def test_async_single_job_and_fixed_invocation(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        process = FakeProcess(b'{"type":"turn.started"}\n', gate=gate)
        self.launch.return_value = process
        task = '--help; $(touch forbidden)\nRead AGENTS.md'
        with patch.dict(os.environ, {'EPOPTIA_API_KEY': 'synthetic-test-value',
                                     'XDG_RUNTIME_DIR': '/tmp/untrusted',
                                     'DBUS_SESSION_BUS_ADDRESS': 'synthetic-test-value'}):
            result = jobs.start(task)
            self.assertTrue(result['ok'])
            self.assertEqual(jobs.status(result['job_id'])['state'], 'running')
            self.assertEqual(jobs.start('second')['error'], 'Another Codex job is running')
            gate.set()
            self.join_workers()
        args, kwargs = self.launch.call_args
        self.assertEqual(args, (jobs.COMMAND,))
        self.assertEqual(args[0][:11], (
            '/usr/bin/systemd-run', '--user', '--scope', '--quiet',
            '--no-ask-password', '--expand-environment=no', '--collect',
            '--property=MemoryHigh=1800M', '--property=MemoryMax=2G',
            '--property=TasksMax=128', '--',
        ))
        self.assertEqual(args[0][11], '/usr/bin/codex')
        self.assertEqual(kwargs['cwd'], '/home/ermis/projects/epoptia-bridge')
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['env'], {
            **jobs.CHILD_ENV, 'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}',
        })
        self.assertNotIn('EPOPTIA_API_KEY', kwargs['env'])
        self.assertNotIn('DBUS_SESSION_BUS_ADDRESS', kwargs['env'])
        self.assertEqual(kwargs['stderr'], jobs.subprocess.DEVNULL)
        self.assertEqual(len(kwargs['pass_fds']), 1)
        self.assertEqual(process.stdin.sent.decode(), jobs.INSTRUCTIONS + task)
        self.assertNotIn(task, str(args))
        self.assertEqual(jobs.status(result['job_id'])['exit_code'], 0)
        self.assertEqual(jobs.status(result['job_id'])['state'], 'completed')
        self.launch.assert_called_once()

    def test_output_allowlist_and_bound(self):
        events = b'{"type":"item.completed","text":"synthetic-sensitive-text"}\n' * 600
        events += b'{"type":["bad"]}\nnot-json\n'
        events += b'x' * (jobs.MAX_EVENT_BYTES + 100) + b'\n'
        events += b'{"type":"turn.completed","secret":"synthetic-sensitive-text"}\n'
        self.launch.return_value = FakeProcess(events)
        result = jobs.start('test')
        self.join_workers()
        log = jobs.logs(result['job_id'], 500)
        self.assertEqual(len(log['lines']), 500)
        self.assertEqual(log['lines'][-2:], ['Codex turn completed', jobs.COMPLETED])
        self.assertTrue(log['raw_output_withheld'])
        for path in self.root.glob('*.json'):
            self.assertNotIn('synthetic-sensitive-text', path.read_text())

    def test_launch_failure_and_nonzero_exit(self):
        self.launch.side_effect = OSError('synthetic-sensitive-error')
        result = jobs.start('test')
        self.join_workers()
        status = jobs.status(result['job_id'])
        self.assertEqual(status['state'], 'failed')
        self.assertIsNone(status['exit_code'])
        self.assertNotIn('synthetic-sensitive-error', json.dumps(jobs.logs(result['job_id'])))
        self.launch.side_effect = None
        self.launch.return_value = FakeProcess(code=7)
        result = jobs.start('another test')
        self.join_workers()
        self.assertEqual(jobs.status(result['job_id'])['exit_code'], 7)
        self.assertEqual(jobs.status(result['job_id'])['state'], 'failed')

    def test_thread_start_failure_releases_lock(self):
        with patch.object(self.thread_class, 'start', side_effect=RuntimeError):
            self.assertFalse(jobs.start('test')['ok'])
        fd = jobs._lock(self.root)
        os.close(fd)
        self.launch.assert_not_called()

    def test_interrupted_record_recovered_and_lock_respected(self):
        job = self.record('running')
        fd = jobs._lock(self.root)
        try:
            self.assertEqual(jobs.status(job['job_id'])['state'], 'running')
            self.assertFalse(jobs.start('test')['ok'])
        finally:
            os.close(fd)
        status = jobs.status(job['job_id'])
        self.assertEqual(status['state'], 'failed')
        self.assertIsNone(status['exit_code'])
        self.assertIsNotNone(status['finished_at'])
        self.assertEqual(jobs.logs(job['job_id'])['lines'][-1], jobs.INTERRUPTED)

    def test_recovery_before_next_job(self):
        old = self.record('running')
        self.launch.return_value = FakeProcess()
        self.assertTrue(jobs.start('test')['ok'])
        self.join_workers()
        self.assertEqual(jobs.status(old['job_id'])['state'], 'failed')
        self.assertEqual(jobs.logs(old['job_id'])['lines'][-1], jobs.INTERRUPTED)
        self.launch.assert_called_once()

    def test_unsafe_files_rejected(self):
        job_id = 'a' * 32
        target = self.root / 'unrelated'
        target.write_text('synthetic-sensitive-text')
        path = self.root / (job_id + '.json')
        path.symlink_to(target)
        self.assertFalse(jobs.logs(job_id)['ok'])
        path.unlink()
        os.link(target, path)
        self.assertFalse(jobs.logs(job_id)['ok'])
        path.unlink()
        path.mkdir()
        self.assertFalse(jobs.status(job_id)['ok'])
        lock = self.root / 'active.lock'
        lock.symlink_to(target)
        self.assertFalse(jobs.start('test')['ok'])
        self.launch.assert_not_called()

    def test_corrupt_metadata_fails_closed(self):
        job = self.record()
        job['lines'] = ['synthetic-sensitive-text']
        jobs._write(self.root, job)
        self.assertFalse(jobs.logs(job['job_id'])['ok'])
        self.assertFalse(jobs.start('test')['ok'])


class StructureTest(unittest.TestCase):
    def test_expected_mcp_tools_remain_registered(self):
        tree = ast.parse(Path('mcp_server.py').read_text())
        names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)
                 and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                         and d.func.attr == 'tool' for d in node.decorator_list)}
        self.assertEqual(names, {'get_wol_status', 'ermis_git_status', 'ermis_service_status',
                                'ermis_health', 'ermis_codex_start', 'ermis_codex_status',
                                'ermis_codex_logs'})

    def test_wrong_user_rejected_before_filesystem_access(self):
        with patch.object(jobs.os, 'getuid', return_value=-1):
            with self.assertRaises(OSError):
                jobs._directory()


if __name__ == '__main__':
    unittest.main()
