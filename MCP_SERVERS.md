MCP process split
=================

Implementation summary: `mcp_server.py` retains the six Epoptia business tools,
the existing server identity (`Epoptia MES`), and streamable HTTP on loopback
port 8000. `ermis_system_server.py` registers the ten infrastructure tools as
`Ermis_System`, on loopback port 8001, using the existing shared `codex_jobs`,
`technical_reports`, and `service_control` modules. It does not load `.env`.
Both use the SDK's default `/mcp` endpoint and existing requirements.txt.

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
`ermis-system-tunnel.service`. Operations are exactly `status` or `restart`.
Adding another service requires a reviewed code change. Restart uses a fixed
noninteractive sudo command and requires existing OS permission. ChatGPT must ask
the user for approval before invoking restart; no conversational approval
parameter is embedded in the tool. Accepted means queued, not completed.

Health reports all four allowlisted services, repository status,
and the port 8000 listener, preserving the existing response structure. It does not claim to validate external connector routing.
Technical report producers must run in the Ermis_System process because the
report store is process-local; IDs created by another process are unavailable.
Existing Codex job storage and bounded result/report behavior are unchanged.

Local validation: run `-m unittest discover -s tests -q` with the project's
Python interpreter. Tests mock job execution and service control, and inspect
business registrations without loading credentials or opening listeners.
