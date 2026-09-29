"""Fixed System-service diagnostic; no credentials or inherited environment reads."""
import asyncio
import json
import os
from pathlib import Path
import signal
import tempfile

ROOT = Path(__file__).resolve().parent
TIMEOUT = 20
STATUSES = frozenset({'completed', 'playwright_missing', 'chromium_missing',
    'chromium_dependencies_missing', 'runtime_permission_denied', 'launch_failed',
    'timeout', 'busy', 'service_context_required'})
_lock = asyncio.Lock()


def sanitize_result(result):
    status = result.get('status') if type(result) is dict else None
    if type(status) is not str or status not in STATUSES:
        status = 'launch_failed'
    return {'ok': status == 'completed', 'status': status}


async def run():
    if _lock.locked():
        return sanitize_result({'status': 'busy'})
    async with _lock:
        process = None
        try:
            # All browser profile/download/cache writes are disposable and local.
            with tempfile.TemporaryDirectory(prefix='.chromium-smoke-', dir=ROOT) as temp:
                try:
                    process = await asyncio.create_subprocess_exec(
                        '/usr/bin/node', str(ROOT / 'chromium_runtime_smoke.cjs'),
                        cwd=str(ROOT), stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                        env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                             'HOME': temp, 'TMPDIR': temp, 'XDG_CACHE_HOME': temp},
                        start_new_session=True, limit=1024)
                    async with asyncio.timeout(TIMEOUT):
                        output = await process.stdout.read(257)
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
            return sanitize_result({'status': 'runtime_permission_denied'})
        except (OSError, ValueError):
            return sanitize_result(None)
