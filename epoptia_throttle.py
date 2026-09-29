"""Shared guard for every HTTP request to Epoptia, across all Ermis processes.

Epoptia has blocked this server's IP before because of rapid requests. Every
call site that talks to Epoptia goes through ``call()``, which:

* holds one exclusive file lock (state/epoptia.lock) for the whole request, so
  only one request is in flight across the dashboard, both MCP servers and CLI
  tools;
* keeps at least READ_INTERVAL seconds between requests, and WRITE_INTERVAL
  seconds before and after a write;
* on HTTP 429 or 403 writes the halt switch (state/epoptia_halt.json) and raises
  EpoptiaHalted. While the switch exists no process sends anything. There are no
  automatic retries; only a person clears the switch:

    venv/bin/python epoptia_throttle.py status
    venv/bin/python epoptia_throttle.py clear --confirm
"""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

import requests

STATE_DIR = Path(__file__).resolve().parent / "state"
READ_INTERVAL = 1.0
WRITE_INTERVAL = 2.0
LOCK_TIMEOUT = 300
HALT_STATUSES = frozenset((403, 429))


class EpoptiaHalted(requests.RequestException):
    """Epoptia answered 429/403 (now or earlier); nothing is sent until reset."""


class EpoptiaBusy(requests.Timeout):
    """The shared request lock could not be acquired in time."""


class Throttle:
    def __init__(self, state_dir=STATE_DIR, *, clock=time.time, sleep=time.sleep,
                 lock_timeout=LOCK_TIMEOUT):
        self.state_dir = Path(state_dir)
        self.lock_path = self.state_dir / "epoptia.lock"
        self.halt_path = self.state_dir / "epoptia_halt.json"
        self.last_path = self.state_dir / "epoptia_last_request.json"
        self.clock = clock
        self.sleep = sleep
        self.lock_timeout = lock_timeout

    def halted(self):
        """Return the halt record, or None when requests are allowed."""
        try:
            text = self.halt_path.read_text()
        except FileNotFoundError:
            return None
        except OSError:
            return {"reason": "halt_file_unreadable"}
        try:
            record = json.loads(text)
        except ValueError:
            record = None
        return record if isinstance(record, dict) else {"reason": "halt_file_invalid"}

    def call(self, fn, *args, write=False, **kwargs):
        """Send one request through the shared lock; returns fn's response."""
        self._raise_if_halted()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+") as handle:
            self._acquire(handle)
            try:
                self._raise_if_halted()
                self._wait_for_slot(write)
                try:
                    response = fn(*args, **kwargs)
                finally:
                    self._record(write)
                status = getattr(response, "status_code", None)
                if status in HALT_STATUSES:
                    self._halt(status, args, kwargs, write)
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
                    raise EpoptiaHalted(f"Epoptia answered HTTP {status}; all requests halted")
                return response
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def clear(self):
        try:
            self.halt_path.unlink()
            return True
        except FileNotFoundError:
            return False

    def _raise_if_halted(self):
        record = self.halted()
        if record is not None:
            raise EpoptiaHalted("Epoptia requests are halted: " + json.dumps(record, sort_keys=True))

    def _acquire(self, handle):
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise EpoptiaBusy("Epoptia request lock busy") from None
                time.sleep(0.05)

    def _wait_for_slot(self, write):
        previous = self._last()
        if previous is None:
            return
        gap = WRITE_INTERVAL if write or previous.get("write") else READ_INTERVAL
        wait = previous["at"] + gap - self.clock()
        if wait > 0:
            self.sleep(min(wait, gap))

    def _last(self):
        try:
            record = json.loads(self.last_path.read_text())
            return record if isinstance(record.get("at"), (int, float)) else None
        except (OSError, ValueError, AttributeError):
            return None

    def _record(self, write):
        self._write_json(self.last_path, {"at": self.clock(), "write": bool(write)})

    def _halt(self, status, args, kwargs, write):
        url = kwargs.get("url", args[0] if args else "")
        path = urlsplit(url).path if isinstance(url, str) else ""
        self._write_json(self.halt_path, {
            "halted_at": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
            "http_status": status,
            "path": path,
            "write": bool(write),
            "process": os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "python",
            "pid": os.getpid(),
        })

    @staticmethod
    def _write_json(path, record):
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(record, sort_keys=True))
        os.replace(tmp, path)


_default = None


def default():
    global _default
    if _default is None:
        _default = Throttle()
    return _default


def call(fn, *args, write=False, **kwargs):
    return default().call(fn, *args, write=write, **kwargs)


def halted():
    return default().halted()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    throttle = default()
    if argv == ["status"]:
        record = throttle.halted()
        print("halted: " + json.dumps(record, sort_keys=True) if record else "running")
        return 1 if record else 0
    if argv == ["clear", "--confirm"]:
        print("halt cleared" if throttle.clear() else "no halt to clear")
        return 0
    print("usage: epoptia_throttle.py status | clear --confirm", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
