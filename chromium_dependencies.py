"""Gateway-only adapter for one installed admin-wrapper action.

Capability is checked through the same fixed privileged entry point as the
install: an unprivileged caller need not be able to read the wrapper source.
Older wrappers reject the exact probe, so missing support still fails closed.
"""
import subprocess


WRAPPER = "/usr/local/sbin/ermis-admin"
COMMAND = ("/usr/bin/sudo", "-n", WRAPPER, "browser", "install-chromium-dependencies")
PROBE_COMMAND = ("/usr/bin/sudo", "-n", WRAPPER, "browser", "check-chromium-dependencies")
STATUSES = frozenset(("completed", "admin_wrapper_action_unavailable",
                      "admin_wrapper_execution_unavailable", "admin_wrapper_action_failed",
                      "outcome_unknown"))


def sanitize_result(result):
    status = result.get("status") if type(result) is dict else None
    if type(status) is not str or status not in STATUSES:
        status = "outcome_unknown"
    return {"ok": status == "completed", "status": status}


def _installed_action_available():
    try:
        result = subprocess.run(
            PROBE_COMMAND, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, shell=False, check=False, timeout=10,
            cwd="/home/ermis/projects/epoptia-bridge",
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def install():
    """Called only after gateway confirmation; accepts no caller arguments."""
    if not _installed_action_available():
        return sanitize_result({"status": "admin_wrapper_action_unavailable"})
    try:
        result = subprocess.run(
            COMMAND, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, shell=False, check=False, timeout=930,
            cwd="/home/ermis/projects/epoptia-bridge",
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
    except subprocess.TimeoutExpired:
        # A privileged child may still be running. Never automatically retry.
        status = "outcome_unknown"
    except OSError:
        status = "admin_wrapper_execution_unavailable"
    else:
        status = "completed" if result.returncode == 0 else "admin_wrapper_action_failed"
    return sanitize_result({"status": status})
