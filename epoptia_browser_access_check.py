"""Bounded System-service access worker; only fixed sanitized states leave it."""
import asyncio
import json
import os
from pathlib import Path
import signal
import tempfile

ROOT = Path(__file__).resolve().parent
TIMEOUT = 100
STATUSES = frozenset({'authenticated', 'login_required', 'mfa_required',
    'session_missing', 'access_denied', 'navigation_failed', 'timeout',
    'busy', 'circuit_open', 'policy_unavailable', 'policy_state_invalid'})
_lock = asyncio.Lock()


def sanitize_result(result):
    status = result.get('status') if type(result) is dict else None
    if type(status) is not str or status not in STATUSES:
        status = 'navigation_failed'
    return {'ok': status == 'authenticated', 'status': status}


async def run():
    if _lock.locked():
        return sanitize_result({'status': 'navigation_failed'})
    async with _lock:
        process = None
        try:
            # All browser profile/download/cache writes are disposable and local.
            with tempfile.TemporaryDirectory(prefix='.epoptia-access-', dir=ROOT) as temp:
                try:
                    process = await asyncio.create_subprocess_exec(
                        '/usr/bin/node', str(ROOT / 'epoptia_browser_access_check.cjs'),
                        cwd=str(ROOT), stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                        env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                             'HOME': temp, 'TMPDIR': temp, 'XDG_CACHE_HOME': temp},
                        start_new_session=True, limit=1024)
                    async with asyncio.timeout(TIMEOUT):
                        # Pipe reads may split a valid result across chunks.
                        # Wait for EOF or the first byte beyond the output cap.
                        try:
                            output = await process.stdout.readexactly(257)
                        except asyncio.IncompleteReadError as error:
                            output = error.partial
                        if len(output) > 256:
                            return sanitize_result(None)
                        await process.wait()
                    if process.returncode != 0:
                        return sanitize_result(None)
                    return sanitize_result(json.loads(output))
                finally:
                    if process is not None:
                        # Also remove surviving descendants after normal worker exit.
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            return sanitize_result({'status': 'timeout'})
        except PermissionError:
            return sanitize_result({'status': 'access_denied'})
        except (OSError, ValueError):
            return sanitize_result(None)
