"""Ephemeral structured analysis reports; never accept prose, logs or paths.

Internal producers call create({finding_code: count}); MCP exposes only read.
Files are private, ignored, and never used as the source of report content.
Unknown fields/values are redacted by omission, not heuristic secret matching.
One process owns the store. Restart discards orphaned reports; no ID recovery.
"""
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

ROOT = Path(__file__).resolve().parent / '.technical-reports'
MAX_BYTES = 4096
MAX_TTL = 300
MAX_REPORTS = 100
FINDINGS = frozenset({'files_checked', 'checks_passed', 'checks_failed',
                      'missing_metadata', 'unsupported_fields', 'warnings'})
ID = re.compile(r'[0-9a-f]{64}')
ERROR = {'ok': False, 'error': 'Report unavailable'}


class ReportStore:
    def __init__(self, root=ROOT):
        # root is internal configuration, never a bridge argument.
        root.mkdir(mode=0o700, exist_ok=True)
        self.fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(self.fd)
            raise OSError('Report storage unavailable')
        import fcntl
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            raise OSError('Report storage unavailable') from None
        self.lock = threading.Lock()
        self.entries = {}
        self.stop = threading.Event()
        # Discard orphan files without opening or following any of them.
        for name in os.listdir(self.fd):
            if ID.fullmatch(name):
                os.unlink(name, dir_fd=self.fd)
        self.worker = threading.Thread(target=self._reap, daemon=True)
        self.worker.start()

    def _reap(self):
        while not self.stop.wait(1):
            self.cleanup()

    def cleanup(self):
        with self.lock:
            for report_id, (deadline, _) in list(self.entries.items()):
                if time.monotonic() >= deadline:
                    self._delete(report_id)

    def _delete(self, report_id):
        try:
            os.unlink(report_id, dir_fd=self.fd)
        except FileNotFoundError:
            pass
        except OSError:
            return False  # Keep registered for the next cleanup attempt.
        del self.entries[report_id]
        return True

    def create(self, findings, ttl_seconds=MAX_TTL):
        if (type(findings) is not dict or len(findings) > 100 or
                type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_TTL):
            return dict(ERROR)
        safe = {key: value for key, value in findings.items()
                if type(key) is str and key in FINDINGS and
                type(value) is int and 0 <= value <= 1_000_000}
        payload = json.dumps({'findings': safe, 'redacted': len(safe) != len(findings)},
                             sort_keys=True).encode('ascii')
        if len(payload) > MAX_BYTES:
            return dict(ERROR)
        self.cleanup()
        with self.lock:
            if len(self.entries) >= MAX_REPORTS:
                return dict(ERROR)
            report_id = secrets.token_hex(32)
            try:
                fd = os.open(report_id, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
            except OSError:
                return dict(ERROR)
            self.entries[report_id] = (time.monotonic() + ttl_seconds, payload)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(payload)
            except OSError:
                self._delete(report_id)
                return dict(ERROR)
            return {'ok': True, 'report_id': report_id, 'expires_in_seconds': ttl_seconds}

    def read(self, report_id):
        if type(report_id) is not str or ID.fullmatch(report_id) is None:
            return dict(ERROR)
        with self.lock:
            entry = self.entries.get(report_id)
            if entry is None:
                return dict(ERROR)
            deadline, payload = entry
            if time.monotonic() >= deadline:
                self._delete(report_id)
                return dict(ERROR)
            result = {'ok': True, 'report': json.loads(payload)}
            # Success means consumed server-side, before transport delivery.
            # A lost response cannot be retried; unread/failed reads expire.
            if not self._delete(report_id):
                return dict(ERROR)
            return result

    def close(self):
        self.stop.set()
        self.worker.join()
        with self.lock:
            for report_id in list(self.entries):
                self._delete(report_id)
            os.close(self.fd)


_store = None
_store_lock = threading.Lock()


def create(findings, ttl_seconds=MAX_TTL):
    """Internal structured producer API. No remote creation or filesystem input."""
    global _store
    try:
        with _store_lock:
            if _store is None:
                _store = ReportStore()
        return _store.create(findings, ttl_seconds)
    except OSError:
        return dict(ERROR)


def read(report_id):
    return _store.read(report_id) if _store is not None else dict(ERROR)
