import os
import requests
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

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


if __name__ == "__main__":

    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=8000,
        json_response=True,
        stateless_http=True
    )
