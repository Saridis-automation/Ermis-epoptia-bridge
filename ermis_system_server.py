"""Ermis_System infrastructure MCP; business tools remain in mcp_server.py.

Uses shared job, report and service modules without loading Epoptia credentials.
"""
import socket
import subprocess
from mcp.server.mcpserver import MCPServer
import codex_jobs
import technical_reports
import service_control
import git_housekeeping

mcp = MCPServer(
    "Ermis_System",
    description="Ermis infrastructure status, bounded jobs and service control",
    version="1.0.0",
)


PROJECT_DIR = "/home/ermis/projects/epoptia-bridge"
ALLOWED_SERVICES = service_control.ALLOWED_SERVICES


@mcp.tool()
def ermis_git_commit(message: str) -> dict:
    """Stage all current non-ignored project changes and create one commit.

    Requires existing Git identity and rejects known sensitive paths. Returns
    safe metadata only; clean=null means the final state could not be checked.
    """
    return git_housekeeping.commit(message)


@mcp.tool()
def ermis_git_status() -> dict:
    """Return branch and porcelain working-tree status without changing Git."""
    try:
        result = subprocess.run(
            ["/usr/bin/git", "--no-optional-locks", "-C", PROJECT_DIR,
             "status", "--porcelain=v1", "--branch", "--untracked-files=normal"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Git status timed out"}
    except (OSError, UnicodeError):
        return {"ok": False, "error": "Git status unavailable"}
    if result.returncode != 0:
        return {"ok": False, "error": "Git status failed"}
    lines = result.stdout.splitlines()
    if not lines or not lines[0].startswith("## "):
        return {"ok": False, "error": "Unexpected Git status response"}
    branch = lines[0][3:].split("...", 1)[0]
    for prefix in ("No commits yet on ", "Initial commit on "):
        if branch.startswith(prefix):
            branch = branch[len(prefix):]
    return {
        "ok": True,
        "branch": None if branch == "HEAD (no branch)" else branch,
        "detached": branch == "HEAD (no branch)",
        "clean": len(lines) == 1,
        "working_tree": lines[1:],
    }


@mcp.tool()
def ermis_service_control(service: str, operation: str = "status") -> dict:
    """Status or restart for a centrally approved service across Ermis projects.

    Restart queues a job using existing system permissions; accepted does not
    mean completed. A timeout has an unknown outcome. Restarting this MCP or
    its tunnel may interrupt the response. Check status after reconnecting.
    """
    return service_control.control(service, operation)


@mcp.tool()
def ermis_service_status(service: str) -> dict:
    """Read status for a centrally approved Ermis service (legacy interface)."""
    return service_control.control(service, "status")


@mcp.tool()
def ermis_health() -> dict:
    """Return service, repository, and local TCP listener status."""
    services = {service: ermis_service_status(service) for service in ALLOWED_SERVICES}
    git = ermis_git_status()
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=1):
            listening = True
    except OSError:
        listening = False
    return {
        "healthy": listening and git["ok"] and all(
            status.get("ok") and status.get("LoadState") == "loaded"
            and status.get("ActiveState") == "active"
            for status in services.values()
        ),
        "services": services,
        "git": git,
        "local_mcp_port_listening": listening,
    }


@mcp.tool()
def ermis_codex_start(task: str) -> dict:
    """Start a coding job in the fixed project; returns immediately.
    The exact prefix [[ERMIS_INSPECT]] selects a strictly read-only inspection
    eligible for a sanitized bounded final report. Other jobs withhold reports.
    Raw stdout/stderr are always withheld.
    """
    marker = "[[ERMIS_INSPECT]]"
    if isinstance(task, str) and task.startswith(marker):
        return codex_jobs.inspect(task[len(marker):].lstrip())
    return codex_jobs.start(task)


@mcp.tool()
def ermis_codex_inspect(task: str) -> dict:
    """Start a strictly read-only inspection in the fixed project.
    The read-only sandbox is mandatory regardless of task wording. Successful
    inspections may expose a sanitized bounded final report through status/logs;
    raw stdout/stderr are always withheld.
    """
    return codex_jobs.inspect(task)


@mcp.tool()
def ermis_codex_status(job_id: str) -> dict:
    """Read job state and optional sanitized inspection report; raw output is withheld."""
    return codex_jobs.status(job_id)


@mcp.tool()
async def ermis_codex_wait(job_id: str, timeout_seconds: int = 300) -> dict:
    """Wait server-side until completed/failed or timeout (integer 1–300 seconds).
    Returns safe job metadata and the existing sanitized final_report if eligible;
    raw stdout/stderr are always withheld. timed_out=true means still running;
    timeout does not cancel the job. No client-side status polling is needed.
    """
    return await codex_jobs.wait(job_id, timeout_seconds)


@mcp.tool()
def ermis_technical_report_read(report_id: str) -> dict:
    """Consume a temporary structured analysis report using its opaque ID.
    report_id must be exactly 64 lowercase hexadecimal characters, issued by
    the internal report producer. Returns only allowlisted integer findings
    and a redaction flag, at most 4096 report bytes, lifetime at most 300 seconds.
    Success deletes before delivery; a lost response cannot be retried.
    Invalid, unknown, expired or deletion-failed reports return no content.
    No paths, file contents, secrets, environment values or raw stdout/stderr.
    """
    if (type(report_id) is not str or len(report_id) != 64 or
            technical_reports.ID.fullmatch(report_id) is None):
        return dict(technical_reports.ERROR)
    return technical_reports.read(report_id)


@mcp.tool()
def ermis_codex_logs(job_id: str, tail_lines: int = 100) -> dict:
    """Read up to 500 safe progress lines; raw Codex text is withheld for secrecy."""
    return codex_jobs.logs(job_id, tail_lines)


if __name__ == "__main__":

    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=8001,
        json_response=True,
        stateless_http=True
    )
