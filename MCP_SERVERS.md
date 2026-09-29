MCP process split
=================

Implementation summary: `mcp_server.py` retains the six existing Epoptia business
tools and adds the generic `inspect_workorder_progress(workorder_id)` tool.
It preserves the existing server identity (`Epoptia MES`) and streamable HTTP on loopback
port 8000. `ermis_system_server.py` registers the existing infrastructure tools as
`Ermis_System`, on loopback port 8001, using the existing shared `codex_jobs`,
`technical_reports`, and `service_control` modules. It does not load `.env`.
Both use the SDK's default `/mcp` endpoint and existing requirements.txt.

Ermis_System additionally registers exactly one gateway tool,
`ermis_gateway_execute(operation, payload)`, using the shared Gateway allowlist
and confirmation policy. It adds no duplicate business/system registrations or
diagnostic tools. See [GATEWAY.md](GATEWAY.md) for request/confirmation examples
and the ChatGPT host's explicit-approval responsibility. Gateway upstream calls
use the existing endpoints, including port 8001 for system actions; the wrapper
is async so these calls can be served by the same process. The gateway itself
is not an allowlisted upstream action, preventing recursive gateway dispatch.
The existing protected Ermis_System connector/tunnel route exposes the new tool
after an authorized restart of that MCP process and discovery refresh. No tunnel
configuration or Epoptia restart is needed for this addition. Nothing is deployed
by the implementation task; live connector reachability is not locally verified.

Native progress uses nested `workorder.progress` from capacity-planning pages.
The single-workorder lookup stops at the first matching page and checks
consistency within that page; it does not establish consistency across later pages.
`production_overview` also reports the mean for distinct production/standby
workorders, with coverage and conflict counts; incomplete scans return null
aggregates. The existing web-session transport and its synthetic tests support
these reads. Routing completion remains separate from native progress.
Temporary fixed-record probes and heuristic progress-discovery tools are removed.

Deployment guidance (not performed)
-----------------------------------

Keep `ermis-epoptia-mcp.service` and its connector routing on port 8000.
For a separately managed `ermis-system-mcp.service`, use:

- User: `ermis`
- WorkingDirectory: `/home/ermis/projects/epoptia-bridge`
- ExecStart: the project's existing Python interpreter followed by
  `/home/ermis/projects/epoptia-bridge/ermis_system_server.py`
- Separate process on `127.0.0.1:8001`; no Epoptia environment file needed.

An operator must arrange a separate protected connector URL routed to
`http://127.0.0.1:8001/mcp`, preserving the current Epoptia URL and protection.
Do not expose the admin endpoint directly to the public network. After an
authorized deployment, refresh connector tool discovery: business tools remain
on Epoptia_MES; infrastructure callers must switch to Ermis_System. The running
Epoptia process needs an authorized restart to adopt the new registrations.

`ermis_service_control(service, operation="status")` accepts only the exact IDs
in `service_control.ALLOWED_SERVICES`: `ermis-epoptia-mcp.service` and
`ermis-epoptia-tunnel.service`, plus `ermis-system-mcp.service` and
`ermis-system-tunnel.service`, and `ermis-dashboard.service`.
Operations are exactly `status` or `restart`.
Adding another service requires a reviewed code change. Restart uses a fixed
noninteractive sudo command and requires existing OS permission. ChatGPT must ask
the user for approval before invoking restart; no conversational approval
parameter is embedded in the tool. Accepted means queued, not completed.

Health reports all five allowlisted services, repository status,
and the port 8000 listener, preserving the existing response structure. It does not claim to validate external connector routing.
Technical report producers must run in the Ermis_System process because the
report store is process-local; IDs created by another process are unavailable.
Existing Codex job storage and bounded result/report behavior are unchanged.

Local validation: run `-m unittest discover -s tests -q` with the project's
Python interpreter. Tests mock job execution and service control, and inspect
business registrations without loading credentials or opening listeners.

The shared gateway also defines the separately named write actions
`update_product_name(product_id, expected_current_name, new_name)` and
`update_wol_description(wol_id, expected_current_description, new_description)`.
Use the existing `ermis_gateway_execute` request/confirm envelope; there are no
standalone write tools that bypass confirmation. Exact argument validation and
limits are documented in [GATEWAY.md](GATEWAY.md). Proposals do not dispatch.
Confirmed execution currently returns `ok:false`, `status:auth_required`,
`write_performed:false` locally: no verified authenticated write transport exists.
Even the adapter's future read-only preflight cannot enable a write and returns
`write_transport_unverified` on a match. Existing Epoptia read tools are unchanged.
Activation of this code would require separately authorized gateway/System process
reloads; no restart, deployment or live write was performed for this foundation.

## User-assisted login readiness

System gateway allowlist: `epoptia_login_status` (read-only),
`epoptia_login_start`, `epoptia_login_finalize`, `epoptia_login_stop`
(each write requires separate confirmation). Start accepts only optional integer
`ttl_minutes` from 1 to 5, default 5; the other actions accept `{}` only.
Legacy `epoptia_browser_login_*` aliases remain. All currently return
`login_not_ready` without state access or runtime operations. See
[EPOPTIA_BROWSER.md](EPOPTIA_BROWSER.md) and
[EPOPTIA_LOGIN_READINESS.marker](EPOPTIA_LOGIN_READINESS.marker) for exact blockers.
Bootstrap installation is required eventually but its login installer is not
ready; reinstalling the current bootstrap will not enable enrollment.
