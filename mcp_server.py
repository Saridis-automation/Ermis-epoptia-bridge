import os
import socket
import subprocess
import requests
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
import codex_jobs

# Load Epoptia credentials from .env
load_dotenv()

BASE_URL = os.getenv("EPOPTIA_BASE_URL")
API_KEY = os.getenv("EPOPTIA_API_KEY")

HEADERS = {
    "X-Auth-Token": API_KEY,
    "Accept": "application/json"
}

# MCP Server
mcp = MCPServer(
    "Epoptia MES",
    description="Read-only access to Epoptia MES production data",
    version="1.0.0"
)


def find_wol(wol_id: int):
    """
    Search Epoptia Work Order Lines, starting from the newest page.
    """

    first_response = requests.get(
        f"{BASE_URL}/api/3.03/workorderlines",
        headers=HEADERS,
        params={
            "page": 1,
            "limit": 100
        },
        timeout=20
    )

    first_response.raise_for_status()

    first_data = first_response.json()

    total_pages = first_data.get("numberOfPages", 0)

    # Search newest records first
    for page in range(total_pages, 0, -1):

        response = requests.get(
            f"{BASE_URL}/api/3.03/workorderlines",
            headers=HEADERS,
            params={
                "page": page,
                "limit": 100
            },
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        for wol in data.get("workorderLines", []):

            if str(wol.get("workorderline_id")) == str(wol_id):
                return wol

    return None


@mcp.tool()
def get_wol_status(wol_id: int) -> dict:
    """
    Get the live production status of a Work Order Line from Epoptia MES.

    Returns:
    - product description
    - client
    - production status
    - target date
    - completed production steps
    - steps currently in progress
    - steps not yet started
    """

    wol = find_wol(wol_id)

    if wol is None:
        return {
            "found": False,
            "workorderline_id": wol_id,
            "message": "Work Order Line not found"
        }

    completed = []
    in_progress = []
    not_started = []

    for step in wol.get("erp_routing", []):

        job_tag = step.get("job_tag")

        if isinstance(job_tag, dict):
            job_name = job_tag.get("name")
        else:
            job_name = None

        item = {
            "workstation": step.get("workstationName"),
            "job": job_name,
            "status": step.get("status"),
            "qty_done": step.get("qty_done")
        }

        status = step.get("status")

        if status == "completed":
            completed.append(item)

        elif status in ["started", "paused", "in_progress"]:
            in_progress.append(item)

        else:
            not_started.append(item)

    return {
        "found": True,
        "workorderline_id": wol.get("workorderline_id"),
        "description": wol.get("description"),
        "production_status": wol.get("production_status"),
        "quantity": wol.get("quantity"),
        "target_day": wol.get("target_day"),
        "client": (
            wol.get("client", {}).get("name")
            if isinstance(wol.get("client"), dict)
            else None
        ),
        "completed": completed,
        "in_progress": in_progress,
        "not_started": not_started
    }


PROJECT_DIR = "/home/ermis/projects/epoptia-bridge"
ALLOWED_SERVICES = (
    "ermis-epoptia-mcp.service",
    "ermis-epoptia-tunnel.service",
)


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
def ermis_service_status(service: str) -> dict:
    """Read status for one of the two explicitly allowed Ermis services."""
    if service not in ALLOWED_SERVICES:
        return {"ok": False, "error": "Service is not allowed"}
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "show", service, "--no-pager",
             "--property=LoadState,ActiveState,SubState,UnitFileState"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "service": service, "error": "Service status timed out"}
    except (OSError, UnicodeError):
        return {"ok": False, "service": service, "error": "Service status unavailable"}
    if result.returncode != 0:
        return {"ok": False, "service": service, "error": "Service status failed"}
    fields = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in ("LoadState", "ActiveState", "SubState", "UnitFileState"):
            fields[key] = value
    if len(fields) != 4:
        return {"ok": False, "service": service, "error": "Incomplete service status"}
    return {"ok": True, "service": service, **fields}


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
    """Start one sandboxed coding job in the fixed Ermis project; returns immediately."""
    return codex_jobs.start(task)


@mcp.tool()
def ermis_codex_status(job_id: str) -> dict:
    """Read a Codex job's persistent state and exit information."""
    return codex_jobs.status(job_id)


@mcp.tool()
def ermis_codex_logs(job_id: str, tail_lines: int = 100) -> dict:
    """Read up to 500 safe progress lines; raw Codex text is withheld for secrecy."""
    return codex_jobs.logs(job_id, tail_lines)


if __name__ == "__main__":

    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=8000,
        json_response=True,
        stateless_http=True
    )
