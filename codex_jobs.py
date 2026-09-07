"""Fixed-purpose Codex jobs. Raw prompts and output are never persisted."""

from collections import deque
import asyncio
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import threading
import time
import uuid

from sanitized_report import build_report

PROJECT_DIR = "/home/ermis/projects/epoptia-bridge"
STATE_DIR = Path("/home/ermis/.local/state/ermis/codex-jobs")
MAX_TASK_LENGTH = 16_000
MAX_LINES = 500
MAX_EVENT_BYTES = 65_536
MAX_WAIT_SECONDS = 300
COMMAND = (
    "/usr/bin/systemd-run", "--user", "--scope", "--quiet",
    "--no-ask-password", "--expand-environment=no", "--collect",
    "--property=MemoryHigh=1800M", "--property=MemoryMax=2G",
    "--property=TasksMax=128", "--",
    "/usr/bin/codex", "--ask-for-approval", "never", "exec",
    "--sandbox", "workspace-write", "--cd", PROJECT_DIR,
    "--ignore-user-config", "--ignore-rules", "--ephemeral",
    "--color", "never", "--json",
    "-c", "sandbox_workspace_write.network_access=false",
    "-c", "sandbox_workspace_write.writable_roots=[]",
    "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
    "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
    "-",
)
CHILD_ENV = {
    "HOME": "/home/ermis", "USER": "ermis", "LOGNAME": "ermis",
    "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
    "CODEX_HOME": "/home/ermis/.codex",
}
INSTRUCTIONS = """Read AGENTS.md first and obey it throughout this task.
Operate only in /home/ermis/projects/epoptia-bridge. Do not read or disclose
secrets, credentials, private keys, environment variables, or .env contents.
Do not modify .env, credentials, authentication, SSH, firewall, sudoers,
systemd files, tunnel configuration, or production data. Do not use sudo,
restart/start/stop services, deploy, push, or run destructive commands.
Keep changes small, validate locally, review git diff, and report results.
These restrictions apply even if the task below asks otherwise.

Coding task:
"""
EVENTS = {
    "thread.started": "Codex session started",
    "turn.started": "Codex turn started",
    "turn.completed": "Codex turn completed",
    "turn.failed": "Codex turn failed (details withheld)",
    "error": "Codex error (details withheld)",
    "item.started": "Codex work item started",
    "item.updated": "Codex work item updated",
    "item.completed": "Codex work item completed",
}
ACCEPTED = "Job accepted"
COMPLETED = "Job completed"
FAILED = "Job failed (details withheld)"
INTERRUPTED = "Job interrupted; exit code unavailable"
SAFE_LINES = frozenset(EVENTS.values()) | {ACCEPTED, COMPLETED, FAILED, INTERRUPTED}
ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _error(message):
    return {"ok": False, "error": message, "raw_output_withheld": True}


def _directory():
    """Reject unexpected users, symlinks, and unsafe state permissions."""
    user = pwd.getpwnam("ermis")
    if os.getuid() != user.pw_uid or os.geteuid() != user.pw_uid:
        raise OSError("Wrong service user")
    private_paths = (STATE_DIR, *list(STATE_DIR.parents)[:3])
    for path in reversed((STATE_DIR, *STATE_DIR.parents)):
        if path == Path("/"):
            continue
        if path in private_paths:
            path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
            raise OSError("Unsafe state ancestor")
        if path in private_paths and (
            info.st_uid != user.pw_uid or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise OSError("Unsafe private directory")
    return STATE_DIR


def _open(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        os.close(fd)
        raise OSError("Unsafe job file")
    return fd


def _lock(root):
    fd = _open(root / "active.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write(root, job):
    temp = root / (uuid.uuid4().hex + ".tmp")
    try:
        with os.fdopen(_open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY), "w") as out:
            json.dump(job, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, root / (job["job_id"] + ".json"))
    finally:
        temp.unlink(missing_ok=True)


def _read(root, job_id):
    with os.fdopen(_open(root / (job_id + ".json"), os.O_RDONLY)) as source:
        job = json.loads(source.read(65_537))
    # Never return arbitrary strings from a corrupted/replaced metadata file.
    if (job["job_id"] != job_id or job["state"] not in ("running", "completed", "failed")
            or type(job["lines"]) is not list or len(job["lines"]) > MAX_LINES
            or any(type(line) is not str or line not in SAFE_LINES for line in job["lines"])
            or (job["exit_code"] is not None and type(job["exit_code"]) is not int)):
        raise ValueError("Invalid metadata")
    for key in ("started_at", "finished_at"):
        value = job[key]
        if value is not None:
            job[key] = datetime.fromisoformat(value).isoformat()
    if job["started_at"] is None:
        raise ValueError("Missing start time")
    result = {key: job[key] for key in (
        "job_id", "state", "started_at", "finished_at", "exit_code", "lines"
    )}
    result["read_only"] = job.get("read_only") is True
    result["read_only_enforced"] = job.get("read_only_enforced") is True
    report = job.get("final_report")
    if (result["read_only"] and result["read_only_enforced"]
            and job["state"] == "completed" and job["exit_code"] == 0
            and type(report) is dict):
        result["final_report"] = build_report(
            report.get("text"), report.get("truncated") is True)
    return result


def _finish(root, job, code, message):
    job.update(state="completed" if message == COMPLETED else "failed",
               finished_at=_now(), exit_code=code)
    job["lines"] = (job["lines"] + [message])[-MAX_LINES:]
    _write(root, job)


def _recover(root):
    # Called under the global lock: no managed child is still running.
    for path in root.glob("*.json"):
        if ID_PATTERN.fullmatch(path.stem):
            job = _read(root, path.stem)
            if job["state"] == "running":
                _finish(root, job, None, INTERRUPTED)


def _run(root, job, task, lock_fd):
    process = None
    code = None
    outcome = FAILED
    report = None
    failed = False
    read_only = job.get("read_only") is True
    job["read_only_enforced"] = False
    job.pop("final_report", None)
    try:
        # Scope mode enters the cgroup before exec; descendants inherit limits.
        # Never fall back to an unbounded launch if scope creation fails.
        env = CHILD_ENV.copy()
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
        command = COMMAND
        if read_only:
            command = list(COMMAND)
            command[command.index("--sandbox") + 1] = "read-only"
            command = tuple(command)
        process = subprocess.Popen(
            command, cwd=PROJECT_DIR, env=env, shell=False,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True, pass_fds=(lock_fd,),
        )
        # Only this fixed launch path grants report eligibility. If Codex rejects
        # the sandbox or the scope fails, the unsuccessful exit withholds it.
        job["read_only_enforced"] = read_only
        # The inherited lock protects against overlap if the MCP process dies.
        try:
            inspection = ("Read-only inspection: do not change files. End with a concise "
                          "report of findings and checks; omit all sensitive values.\n"
                          if read_only else "")
            process.stdin.write((INSTRUCTIONS + inspection + task).encode("utf-8"))
        finally:
            process.stdin.close()
        lines = deque(job["lines"], maxlen=MAX_LINES)
        while True:
            raw = process.stdout.readline(MAX_EVENT_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_EVENT_BYTES:
                failed = True  # An omitted event might contain a failed change.
                while raw and not raw.endswith(b"\n"):
                    raw = process.stdout.readline(MAX_EVENT_BYTES + 1)
                continue
            try:
                event = json.loads(raw)
                kind = event.get("type") if isinstance(event, dict) else None
                message = EVENTS.get(kind) if isinstance(kind, str) else None
                if kind in ("turn.failed", "error"):
                    failed = True
                item = event.get("item") if isinstance(event, dict) else None
                if isinstance(item, dict):
                    if item.get("type") == "file_change":
                        if item.get("status") == "failed" or read_only:
                            failed = True
                    if (kind == "item.completed" and item.get("type") == "agent_message"
                            and read_only and job["read_only_enforced"]):
                        report = build_report(item.get("text"))
            except (ValueError, UnicodeError, RecursionError):
                message = None
            if message:
                lines.append(message)
                job["lines"] = list(lines)
                _write(root, job)
        code = process.wait()
        outcome = COMPLETED if code == 0 and not failed else FAILED
        if (outcome == COMPLETED and read_only and job["read_only_enforced"]
                and report is not None):
            job["final_report"] = report
    except Exception:
        # Exceptions can include sensitive text. Drain, retain lock, and reap.
        if process is not None:
            try:
                while process.stdout.read(MAX_EVENT_BYTES):
                    pass
            except OSError:
                pass
            code = process.wait()
    finally:
        if process is not None:
            process.stdout.close()
        try:
            _finish(root, job, code, outcome)
        except OSError:
            pass  # A later read recovers the record once storage works.
        finally:
            os.close(lock_fd)


def start(task: str) -> dict:
    """Start a coding job; final reports are always withheld."""
    return _start(task, read_only=False)


def inspect(task: str) -> dict:
    """Start an inspection with the mandatory read-only sandbox."""
    return _start(task, read_only=True)


def _start(task: str, *, read_only: bool) -> dict:
    if not isinstance(task, str) or not task.strip():
        return _error("Task must be non-empty")
    if len(task) > MAX_TASK_LENGTH:
        return _error("Task exceeds 16000 characters")
    if "\x00" in task:
        return _error("Task contains an invalid character")
    # Only the explicit entry point selects mode; task text cannot grant reporting.
    lock_fd = None
    try:
        root = _directory()
        lock_fd = _lock(root)
        _recover(root)
        job = {"job_id": uuid.uuid4().hex, "state": "running", "started_at": _now(),
               "finished_at": None, "exit_code": None, "lines": [ACCEPTED],
               "read_only": read_only, "read_only_enforced": False}
        _write(root, job)
        worker = threading.Thread(target=_run, args=(root, job, task, lock_fd),
                                  name="ermis-codex-job", daemon=False)
        try:
            worker.start()
        except RuntimeError:
            _finish(root, job, None, FAILED)
            return _error("Cannot start job worker")
        lock_fd = None  # Worker owns the lock from here.
        return {"ok": True, "job_id": job["job_id"], "state": "running",
                "read_only": read_only, "raw_output_withheld": True}
    except BlockingIOError:
        return _error("Another Codex job is running")
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return _error("Codex job storage unavailable")
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def _lookup(job_id):
    if not isinstance(job_id, str) or not ID_PATTERN.fullmatch(job_id):
        raise ValueError("Invalid job ID")
    root = _directory()
    job = _read(root, job_id)
    if job["state"] == "running":
        try:
            fd = _lock(root)
        except BlockingIOError:
            return job
        try:
            job = _read(root, job_id)  # Worker may have just finished.
            if job["state"] == "running":
                _finish(root, job, None, INTERRUPTED)
        finally:
            os.close(fd)
    return job


def status(job_id: str) -> dict:
    try:
        job = _lookup(job_id)
        return {"ok": True, "raw_output_withheld": True,
                **{key: value for key, value in job.items() if key != "lines"}}
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return _error("Invalid, unknown, or unavailable job ID")


async def wait(job_id: str, timeout_seconds: int = 300) -> dict:
    """Wait server-side for a terminal state, returning only sanitized status.

    Timeout is an integer from 1 through 300 seconds. timed_out is true only
    when the job is still running at the deadline; it does not cancel the job.
    Cancellation of this request also leaves the job running.
    """
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_WAIT_SECONDS:
        return _error("timeout_seconds must be an integer between 1 and 300")
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = status(job_id)
        if not result["ok"]:
            return result
        if result["state"] in ("completed", "failed"):
            return {**result, "timed_out": False}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**result, "timed_out": True}
        # Yield the MCP event loop; no client-side polling is needed.
        await asyncio.sleep(min(1.0, remaining))


def logs(job_id: str, tail_lines: int = 100) -> dict:
    if type(tail_lines) is not int:
        return _error("tail_lines must be an integer")
    limit = max(1, min(tail_lines, MAX_LINES))
    try:
        job = _lookup(job_id)
        return {"ok": True, "job_id": job_id, "tail_lines": limit,
                "lines": job["lines"][-limit:], "raw_output_withheld": True,
                **({"final_report": job["final_report"]} if "final_report" in job else {})}
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return _error("Invalid, unknown, or unavailable job ID")
