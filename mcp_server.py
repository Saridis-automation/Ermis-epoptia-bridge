import os
import requests
import epoptia_throttle
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
import epoptia_read

# Load Epoptia credentials from .env
load_dotenv()

BASE_URL = os.getenv("EPOPTIA_BASE_URL")
API_KEY = os.getenv("EPOPTIA_API_KEY")
WEB_USERNAME = os.getenv("EPOPTIA_USERNAME")
WEB_PASSWORD = os.getenv("EPOPTIA_PASSWORD")

HEADERS = {
    "X-Auth-Token": API_KEY,
    "Accept": "application/json"
}

# Business-only MCP surface. Infrastructure tools belong in ermis_system_server.py.
mcp = MCPServer(
    "Epoptia MES",
    description="Read-only access to Epoptia MES production data",
    version="1.0.0"
)


def find_wol(wol_id: int):
    """
    Search Epoptia Work Order Lines, starting from the newest page.
    """

    first_response = epoptia_throttle.call(requests.get,
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

        response = epoptia_throttle.call(requests.get,
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
    - technical_details: normalized specifications, WOL/product source values,
      descriptions/notes and bounded sanitized extra fields; WOL values win
    - client
    - production status
    - target date
    - completed production steps
      with bounded completion_candidates at exact WOL source paths (unverified)
    - steps currently in progress
    - steps not yet started
    - progress: supplied routing completed-step percentage, independent of lifecycle;
      active excludes paused, unknown steps earn no credit, no steps means null
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

    routing = wol.get("erp_routing")
    for step_index, step in enumerate(routing if isinstance(routing, list) else []):
        item = epoptia_read.parse_routing_step(step)
        if item is None:
            continue
        status = item['status']

        if status == "completed":
            item.update(epoptia_read.routing_completion_details(step, step_index))
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
        "not_started": not_started,
        "technical_details": epoptia_read.technical_details(wol),
        **epoptia_read.native_progress_metadata(),
        "progress": epoptia_read.routing_progress(routing)
    }


def _read_query(query, **filters):
    try:
        # Validate query arguments before making any upstream request.
        query([], **filters)
        rows = epoptia_read.fetch_wols(BASE_URL, HEADERS)
        return {"ok": True, **query(rows, **filters)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except epoptia_read.ReadError:
        return {"ok": False, "error": "Epoptia read unavailable"}


@mcp.tool()
def inspect_workorder_progress(workorder_id: int,
                               include_actual_production_completion: bool = False) -> dict:
    """Read native overall workorder progress from capacity-planning WOLs.

    Matches nested workorder.id and returns nested workorder.progress after
    pagination and consistency checks. Root WOL progress is not overall progress.
    Missing, conflicting or incomplete results return a safe diagnostic.
    Always return actual_production_completion from the exact full label on the
    parent page, with a safe reason when unavailable. The include flag is retained
    for compatibility and no longer disables this read.
    Abbreviated labels and WOL completionDate/dbCompletionDate/
    displayCompletionDate are scheduled targets, never actual completion.
    """
    try:
        return {"ok": True, **epoptia_read.inspect_workorder_progress(
            BASE_URL, HEADERS, workorder_id,
            username=WEB_USERNAME, password=WEB_PASSWORD,
            include_actual_production_completion=include_actual_production_completion)}
    except ValueError:
        return {"ok": False, "error": "workorder_id must be an integer from 1 to 2147483647"}


@mcp.tool()
def calendar_target_dates(wol_ids: list[int] | None = None,
                          workorder_ids: list[int] | None = None, limit: int = 200) -> dict:
    """Read date-only calendar snapshot (60s TTL); never initiate login or writes.

    Existing authenticated progress reads populate it through GET /planning/calendar.
    Missing authentication and client rendering are independent date-source statuses.
    Filters contain at most 200 positive IDs; limit is 1–200.
    """
    from calendar_target_dates import calendar_target_dates as read_calendar
    try:
        return read_calendar(wol_ids, workorder_ids, limit)
    except ValueError:
        return {'ok': False, 'error': 'invalid_calendar_filters'}


@mcp.tool()
def get_wol_details(wol_id: int) -> dict:
    """Read technical details for one WOL (positive integer ID, e.g. 3168).

    Returns description, client name, quantity, target_day, status/state and
    embedded WOL/product dimensions, notes/remarks, custom fields and attributes.
    Source paths retain upstream values/units; WOL specifications override product
    defaults. The existing workorderlines read supplies details: absent
    product data is not fetched separately or invented. Unsafe values are omitted;
    missing metadata is null. Technical data is bounded to depth 8, 200 leaves,
    50 entries/container and 2,000 characters/value, with truncation reported.
    Temporary report_discovery adds bounded authenticated page-route and endpoint
    metadata only; diagnostic failures never discard the details result.
    """
    result = _read_query(epoptia_read.wol_details, wol_id=wol_id)
    if result.get('ok'):
        # TEMPORARY: metadata discovery runs in this service's reader context.
        # Keep all diagnostic failures separate from the working details read.
        try:
            from report_discovery import runtime_discovery
            result['report_discovery'] = runtime_discovery(BASE_URL, WEB_USERNAME, WEB_PASSWORD)
        except Exception:
            result['report_discovery'] = {
                'status': 'discovery_unavailable', 'landing_paths': [],
                'route_candidates': [], 'endpoint_candidates': [], 'notes': []}
    return result


@mcp.tool()
def list_wols(wol_id: int | None = None, client: str | None = None,
              product_text: str | None = None, status: str | None = None,
              state: str | None = None, target_from: str | None = None,
              target_to: str | None = None, limit: int = 50) -> dict:
    """List/search WOLs using local AND filters over all read endpoint pages.
    wol_id is exact; client and product_text (description) are case-insensitive
    substrings; status (production_status) and state are case-insensitive exact
    matches. State is available only when supplied upstream. Target bounds are
    inclusive YYYY-MM-DD; undated WOLs are excluded when bounds are set.
    limit: 1–200 WOLs, default 50, in upstream order. Returns selected fields,
    total_matches and truncation; never raw upstream records.
    Each item includes dimensions, model, code and routing progress. aggregate covers ALL matches before
    limit: total_wols, counts_by_status (lifecycle), unknown_routing_wols (no
    supplied steps), and step-weighted progress rounded to two decimals.
    Only completed steps earn credit; active (started/in_progress), paused,
    not_started and unknown steps earn zero. Active excludes paused. No steps
    means null percent. Archive does not imply routing completion. Use client
    and/or wol_id filters to scope these totals.
    """
    return _read_query(epoptia_read.list_wols, wol_id=wol_id, client=client,
                       product_text=product_text, status=status, state=state,
                       target_from=target_from, target_to=target_to, limit=limit)


@mcp.tool()
def production_overview(dashboard: bool = False) -> dict:
    """Aggregate all WOL pages: counts by production_status/state (missing =
    unknown), total WOLs, numeric quantity sum and quantity coverage, dated WOLs
    and past-target WOLs (all statuses; UTC today). Quantities are not converted
    between units. Includes mean native progress of distinct production/standby
    workorders; invalid or conflicting progress is excluded. Incomplete native
    scans return null aggregates. Includes the canonical whole-order census.
    Optional dashboard mode skips the separate WOL summary read.
    """
    # Dashboard needs only the native census; avoid a second all-history WOL scan.
    result = {"ok": True} if dashboard else _read_query(epoptia_read.overview)
    if not result.get('ok'):
        return result
    from epoptia_queries import production_overview as read_overview
    return read_overview(BASE_URL, username=WEB_USERNAME, password=WEB_PASSWORD, result=result, headers=HEADERS)


@mcp.tool()
def due_wols(mode: str = "due_soon", days: int = 7, as_of: str | None = None,
             target_from: str | None = None, target_to: str | None = None,
             limit: int = 50) -> dict:
    """Query dated WOLs, earliest target first. mode: due_soon includes as_of
    through as_of + days (0–3650); overdue means strictly before as_of, ignoring
    days. as_of defaults to UTC today. Dates use YYYY-MM-DD; optional target_from
    and target_to further restrict the inclusive window. Excludes production
    statuses completed/cancelled/canceled; other or unknown statuses remain.
    limit: 1–200 WOLs (default 50). Returns selected fields and match totals.
    """
    return _read_query(epoptia_read.due_wols, mode=mode, days=days, as_of=as_of,
                       target_from=target_from, target_to=target_to, limit=limit)


@mcp.tool()
def workstation_wip(workstation: str | None = None, step: str | None = None,
                    limit: int = 50, dashboard: bool = False) -> dict:
    """Optional dashboard mode returns a complete versioned station census.
    Legacy default: list routing steps with status started/paused/in_progress, including WOL
    summaries. workstation filters workstationName; step filters job_tag.name,
    both case-insensitive substrings. Only supplied routing data is used;
    missing stations group as unknown. Counts by workstation cover all matching
    steps before limit (1–200 steps, default 50, upstream order). A WOL may occur
    more than once. Returns selected fields, match totals and truncation.
    """
    if dashboard:
        # Complete compact census, using this process's existing authenticated reader.
        # Filter/limit arguments cannot silently narrow the dashboard population.
        if workstation is not None or step is not None:
            return {'ok': False, 'source_error': 'unsupported_dashboard_filter'}
        from epoptia_queries import workstation_census
        return workstation_census(BASE_URL, HEADERS)
    return _read_query(epoptia_read.workstation_wip, workstation=workstation,
                       step=step, limit=limit)


if __name__ == "__main__":

    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=8000,
        json_response=True,
        stateless_http=True
    )
