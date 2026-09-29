"""Central, fixed-purpose service control; never return raw systemctl output."""

import subprocess

# Add approved services here; callers cannot extend this whitelist.
ALLOWED_SERVICES = (
    "ermis-epoptia-mcp.service",
    "ermis-epoptia-tunnel.service",
    "ermis-system-mcp.service",
    "ermis-system-tunnel.service",
    "ermis-dashboard.service",
)
STATUS_VALUES = {
    "LoadState": frozenset(("stub", "loaded", "not-found", "bad-setting", "error", "merged", "masked")),
    "ActiveState": frozenset(("active", "reloading", "inactive", "failed", "activating", "deactivating", "maintenance", "refreshing")),
    "SubState": frozenset(("dead", "condition", "start-pre", "start", "start-post", "running", "exited", "reload", "reload-signal", "reload-notify", "stop", "stop-watchdog", "stop-sigterm", "stop-sigkill", "stop-post", "final-watchdog", "final-sigterm", "final-sigkill", "failed", "auto-restart", "auto-restart-queued", "cleaning")),
    "UnitFileState": frozenset(("", "enabled", "enabled-runtime", "linked", "linked-runtime", "alias", "masked", "masked-runtime", "static", "disabled", "indirect", "generated", "transient", "bad")),
}


def control(service: str, operation: str = "status") -> dict:
    """Read approved status or request a restart without interactive authorization."""
    if not isinstance(service, str) or service not in ALLOWED_SERVICES:
        return {"ok": False, "error": "Service is not allowed"}
    if not isinstance(operation, str) or operation not in ("status", "restart"):
        return {"ok": False, "error": "Operation is not allowed"}
    if operation == "status":
        command = ["/usr/bin/systemctl", "show", service, "--no-pager",
                   "--property=LoadState,ActiveState,SubState,UnitFileState"]
    else:
        command = ["sudo", "-n", "/usr/bin/systemctl", "restart", service]
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE if operation == "status" else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, text=True, timeout=5, check=False, shell=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "service": service, "error": f"Service {operation} timed out"}
    except (OSError, UnicodeError):
        return {"ok": False, "service": service, "error": f"Service {operation} unavailable"}
    if result.returncode != 0:
        return {"ok": False, "service": service, "error": f"Service {operation} failed"}
    if operation == "restart":
        return {"ok": True, "service": service, "operation": "restart", "accepted": True}
    if len(result.stdout) > 4096:
        return {"ok": False, "service": service, "error": "Unexpected service status response"}
    fields = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in STATUS_VALUES:
            # Unknown future states are withheld, never echoed as arbitrary text.
            fields[key] = value if value in STATUS_VALUES[key] else "unknown"
    if len(fields) != 4:
        return {"ok": False, "service": service, "error": "Incomplete service status"}
    return {"ok": True, "service": service, **fields}
