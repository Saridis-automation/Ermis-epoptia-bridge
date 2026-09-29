"""Bounded, unprivileged browser worker. No environment/credential loading."""
import asyncio
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAGES = frozenset({'workorders', 'workorderlines', 'production_report', 'daily_analysis'})
_lock = asyncio.Lock()


async def inspect(page: str = 'production_report') -> dict:
    if type(page) is not str or page not in PAGES:
        return {'ok': False, 'status': 'invalid_page'}
    return await _run(page)


async def inspect_scope(scope: str = 'dates') -> dict:
    if type(scope) is not str or scope not in {'session', 'dates', 'smoke'}:
        return {'ok': False, 'status': 'invalid_scope'}
    result = await _run(scope)
    if scope == 'session':
        status = result.get('status')
        if status not in {'session_unavailable', 'session_unverified', 'login_required', 'circuit_open',
                          'policy_unavailable', 'policy_state_invalid',
                          'authentication_required', 'inspection_incomplete',
                          'browser_unavailable', 'busy', 'auth_material_missing',
                          'session_policy_invalid', 'session_state_invalid',
                          'interactive_login_required', 'playwright_missing',
                          'chromium_missing', 'chromium_dependencies_missing',
                          'launch_failed', 'runtime_permission_denied'}:
            status = 'session_unverified'
        return {'ok': False, 'usable': False, 'status': status}
    return result


async def _run(page: str) -> dict:
    if _lock.locked():
        return {'ok': False, 'status': 'busy'}
    async with _lock:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                '/usr/bin/node', str(ROOT / 'epoptia_browser.cjs'), page,
                cwd=str(ROOT), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                     'HOME': '/home/ermis'}, start_new_session=True,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=150 if page == 'dates' else 120)
            if process.returncode != 0 or len(stdout) > 65536:
                return {'ok': False, 'status': 'launch_failed'}
            result = json.loads(stdout)
            if not isinstance(result, dict):
                raise ValueError('invalid result')
            return result
        except PermissionError:
            return {'ok': False, 'status': 'runtime_permission_denied'}
        except (OSError, ValueError, asyncio.TimeoutError):
            return {'ok': False, 'status': 'launch_failed'}
        finally:
            if process is not None and process.returncode is None:
                # Kill only this dedicated worker's process group, including Chromium.
                import os
                import signal
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()


LOGIN_BLOCKERS = (
    'approved_origin_missing',
    'authenticated_landing_signal_missing',
    'persistent_loopback_backend_missing',
    'fixed_bootstrap_installer_missing',
)


def validate_login_arguments(action, arguments):
    """No credentials, paths, bind addresses, or transport options accepted."""
    if type(action) is not str or action not in {'start', 'status', 'diagnose', 'stop', 'finalize', 'sandbox_probe'}:
        raise ValueError
    if type(arguments) is not dict or set(arguments) - ({'ttl_minutes'} if action == 'start' else set()):
        raise ValueError
    ttl = arguments.get('ttl_minutes', 5)
    if type(ttl) is not int or not 1 <= ttl <= 5:
        raise ValueError


def login_listener_blocker():
    """Inspect only the fixed login TCP ports; never return listener details."""
    try:
        result = subprocess.run(
            ['/usr/bin/ss', '-H', '-ltn', '( sport = :5991 or sport = :6091 )'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=5, check=False, shell=False,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C'})
        if result.returncode or len(result.stdout) > 4096:
            return 'login-listener-check-unavailable'
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 5 or fields[0] != 'LISTEN':
                return 'login-listener-check-unavailable'
            if fields[3] not in {'127.0.0.1:5991', '127.0.0.1:6091'}:
                return 'login-unsafe-listener'
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return 'login-listener-check-unavailable'
    return None


def sanitize_login_diagnostic(value):
    value = value if type(value) is dict else {}
    result = {key: value.get(key) is True for key in ('command_accepted', 'child_launch_attempted', 'spawn_returned', 'error_event', 'exit_event', 'close_event', 'readiness_timeout', 'cleanup_started')}
    for key, allowed in {
        'failure_stage': {'none', 'prepare', 'display', 'vnc', 'websocket', 'executable', 'browser', 'navigation', 'health', 'controller'},
        'failure_class': {'none', 'B_LOGIN_APPARMOR', 'unknown', 'executable_missing', 'executable_not_executable', 'shared_library_missing', 'sandbox_denied', 'display_unavailable', 'profile_locked', 'profile_permission', 'cache_permission', 'spawn_error', 'immediate_nonzero_exit', 'signal_exit', 'readiness_timeout', 'bind_conflict', 'browser_crash', 'protocol_error', 'cancelled'},
        'signal_class': {'sigabrt', 'sigbus', 'sigill', 'sigkill', 'sigsegv', 'sigsys', 'sigtrap', 'sighup', 'sigterm', 'other', 'none', 'unknown'},
        'child_exit_class': {'none', 'spawn_error', 'nonzero', 'signal', 'clean', 'unknown'},
    }.items():
        item = value.get(key)
        result[key] = item if type(item) is str and item in allowed else 'none' if key == 'failure_stage' else 'unknown'
    return result


SANDBOX_ENUMS = {
    'cleanup_class': ['none', 'completed', 'refused', 'failed'],
    'failure_class': ['none', 'B_LOGIN_APPARMOR', 'deadline', 'cancelled', 'spawn_error', 'readiness_failure', 'classifier_exception', 'shutdown_failure', 'unknown'],
    'sandbox_class': [
        'userns_lsm_denied', 'unit_namespace_denied', 'kernel_userns_disabled',
        'seccomp_clone_denied', 'setuid_helper_unusable', 'root_identity_refused',
        'proc_namespace_incompatible', 'runtime_fs_denied', 'sandbox_check_other', 'none',
        'unknown',
    ],
    'user_namespace': [
        'success', 'permission_denied', 'unavailable', 'unknown',
    ],
    'apparmor_restriction': [
        'enabled', 'disabled', 'unavailable', 'unknown',
    ],
    'kernel_userns': [
        'enabled', 'disabled', 'unavailable', 'unknown',
    ],
    'apparmor_confinement': [
        'confined', 'unconfined', 'unavailable', 'unknown',
    ],
    'setuid_helper': [
        'valid', 'invalid', 'absent', 'unavailable', 'unknown',
    ],
    'child_exit_class': [
        'none', 'spawn_error', 'nonzero', 'signal', 'clean', 'unknown',
    ],
    'signal_class': [
        'sigabrt', 'sigbus', 'sigill', 'sigkill', 'sigsegv', 'sigsys', 'sigtrap', 'sighup',
        'sigterm', 'other', 'none', 'unknown',
    ],
    'elapsed_bucket': [
        'under_5s', '5_to_10s', '10_to_15s', '15_to_20s', 'timeout', 'unknown',
    ],
}
SANDBOX_BOOLS = ['command_accepted', 'spawn_returned', 'ready', 'clean_close', 'nonroot', 'timed_out']


def sanitize_sandbox_probe(value):
    value = value if type(value) is dict else {}
    result = {key: value.get(key) is True for key in SANDBOX_BOOLS}
    for key, allowed in SANDBOX_ENUMS.items():
        item = value.get(key)
        result[key] = item if type(item) is str and item in allowed else ('none' if key == 'cleanup_class' else 'unknown')
    return result


async def login_command(action: str, **arguments) -> dict:
    result = await _login_command(action, **arguments)
    if action == 'sandbox_probe':
        return {'ok': result.get('ok') is True,
                'status': result.get('status') if result.get('status') in {'sandbox_probe_complete', 'busy', 'login_not_ready', 'login_unavailable', 'invalid_action'} else 'login_unavailable',
                **sanitize_sandbox_probe(result.get('sandbox_probe'))}
    if action != 'diagnose':
        return result
    # Deliberately omit readiness blocker arrays and all transport identifiers.
    return {
        'ok': result.get('ok') is True,
        'status': result.get('status', 'login_unavailable'),
        'source_wiring_ready': result.get('source_wiring_ready') is True,
        'bootstrap_install_required': result.get('bootstrap_install_required') is True,
        'socket_activatable': result.get('socket_activatable') is True,
        'ipc_phase': result.get('ipc_phase', 'preflight'),
        'installed_source_match': result.get('installed_source_match', 'unknown'),
        'runtime_generation_match': result.get('runtime_generation_match', 'unknown'),
        **sanitize_login_diagnostic(result.get('diagnostic')),
        **({'sandbox_probe': sanitize_sandbox_probe(result['sandbox_probe'])} if 'sandbox_probe' in result else {}),
    }


async def _login_command(action: str, **arguments) -> dict:
    """Source readiness is separate from an installed, managed runtime."""
    try:
        validate_login_arguments(action, arguments)
    except ValueError:
        return {'ok': False, 'status': 'invalid_action'}
    from admin_bootstrap.login_bootstrap import source_blockers, installed_blockers, dependency_blockers, configuration_blockers, fingerprint, runtime_blockers
    blockers = source_blockers(ROOT)
    readiness = {'source_wiring_ready': not blockers, 'operational_ready': False,
                 'bootstrap_install_required': True, 'bootstrap_install_ready': not blockers,
                 'blockers': blockers}
    if blockers:
        return {'ok': False, 'status': 'login_not_ready', **readiness}
    installation = installed_blockers()
    dependencies = (configuration_blockers() or dependency_blockers()) if not installation else []
    readiness.update(bootstrap_install_required=bool(installation), installed_source_match='mismatch' if installation else 'match',
                     installation_blockers=installation, dependency_blockers=dependencies)
    if installation or dependencies:
        readiness['blockers'] = installation + dependencies
        return {'ok': False, 'status': 'login_not_ready', **readiness}
    # Do not prevent cleanup/finalization when a listener check fails.
    listener_blocker = await asyncio.to_thread(login_listener_blocker) if action in {'status', 'start'} else None
    runtime = runtime_blockers()
    blocker = listener_blocker or (runtime[0] if runtime else None)
    if blocker:
        readiness['blockers'] = [blocker]
        return {'ok': False, 'status': 'login_not_ready', **readiness}
    readiness['socket_activatable'] = True
    # Connecting activates the fixed service via systemd's inherited Unix socket.
    try:
        process = await asyncio.create_subprocess_exec(
            '/usr/bin/node', str(ROOT / 'epoptia_browser_login.cjs'), action,
            json.dumps(arguments), cwd=str(ROOT), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/home/ermis'})
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 65)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        value = json.loads(output) if len(output) <= 2048 and process.returncode == 0 else {}
        statuses = {'login_not_ready', 'stopped', 'enrolled', 'awaiting_login',
                    'ready_not_enrolled', 'busy', 'policy_unavailable', 'login_required',
                    'login_unavailable', 'invalid_action', 'sandbox_probe_complete'}
        if (not isinstance(value, dict) or not isinstance(value.get('status'), str)
                or value['status'] not in statuses):
            raise ValueError()
        readiness['ipc_phase'] = value.get('ipc_phase') if value.get('ipc_phase') in ('response', 'timeout', 'connect_failed') else 'invalid_response'
        readiness['diagnostic'] = sanitize_login_diagnostic(value.get('diagnostic'))
        if 'sandbox_probe' in value:
            readiness['sandbox_probe'] = sanitize_sandbox_probe(value['sandbox_probe'])
        readiness['runtime_generation_match'] = ('match' if value.get('source_revision') == fingerprint() else 'mismatch') if value.get('source_revision') else 'unknown'
        active = value.get('operational_ready') is True and value.get('source_revision') == fingerprint()
        readiness.update(operational_ready=active)
        if not active:
            readiness['blockers'] = [
                'login-supervisor-restart-required'
                if value.get('operational_ready') is True else
                'login-backend-ipc-failure'
            ]
            return {'ok': False, 'status': 'login_not_ready', **readiness}
        response = {'ok': value.get('ok') is True, 'status': value['status'], **readiness}
        if action == 'start' and value['status'] == 'awaiting_login':
            response.update(local_url='http://127.0.0.1:6091/vnc.html',
                            ssh_forward='ssh -N -L 127.0.0.1:6091:127.0.0.1:6091 ermis@<server>',
                            ttl_minutes=arguments.get('ttl_minutes', 5))
        return response
    except (OSError, ValueError, asyncio.TimeoutError):
        readiness['ipc_phase'] = 'unavailable'
        readiness['blockers'] = ['login-backend-ipc-failure']
        return {'ok': False, 'status': 'login_not_ready', **readiness}
