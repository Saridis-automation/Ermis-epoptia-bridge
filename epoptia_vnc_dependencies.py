"""Fixed admin-wrapper adapter, invoked only after gateway confirmation."""
import subprocess

COMMAND = ("/usr/bin/sudo", "-n", "/usr/local/sbin/ermis-admin",
           "browser", "install-epoptia-vnc-dependencies")
STATUSES = frozenset(("completed", "already_installed", "failed"))


def sanitize_result(result):
    status = result.get("status") if type(result) is dict else None
    if type(status) is not str or status not in STATUSES:
        status = "failed"
    return {"ok": status != "failed", "status": status}


def install():
    """No caller options, output capture, automatic retries, or session access."""
    try:
        result = subprocess.run(
            COMMAND, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, shell=False, check=False, timeout=950,
            cwd="/home/ermis/projects/epoptia-bridge",
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"})
        status = {0: "completed", 10: "already_installed"}.get(result.returncode, "failed")
    except (OSError, subprocess.TimeoutExpired):
        # Failure can include an uncertain privileged child outcome; never retry.
        status = "failed"
    return sanitize_result({"status": status})
