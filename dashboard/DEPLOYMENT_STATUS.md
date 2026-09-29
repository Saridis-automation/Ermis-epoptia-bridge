# Dashboard deployment status — 2026-09-08

The repository service allowlist now includes `ermis-dashboard.service`.
Ermis System status/control, aggregate health, and gateway service validation
share that allowlist. Only existing status and restart operations are allowed;
gateway restarts retain explicit confirmation. No runtime permissions were
granted. Missing dashboard restart permission fails closed.

Existing deployment artifacts were inspected and preserved: the unit template,
foreground launcher, Waitress entrypoint, requirements and validation script.
No units, authentication, tunnels, dependencies or production data were changed.
No service was installed, enabled, started, stopped or restarted.

Read-only checks from this workspace:

- Project venv: Waitress is unavailable.
- Dashboard systemd properties: query denied (`Operation not permitted` while
  connecting to the system bus). Installation, enablement and active state remain
  unverified; this denial does not establish that the service is absent.
- HTTP `/health` and `/` on port 8010: socket access denied (`PermissionError`,
  errno 1). Neither route could be verified against a live listener.

`http://127.0.0.1:8010/` is the configured server-local browser address, **not a
verified live or remote URL**. The entrypoint binds only to loopback. The
inspected dashboard artifacts provide no external route; existing tunnel
configuration was not inspected or changed. An externally accessible URL cannot
be established from this workspace.

Local validation passed: 45 tests across service control (9), MCP separation
and health (8), gateway routing/confirmation (10), and dashboard routes and
deployment contracts (18). Dashboard JavaScript syntax also passed. Service
operations and upstream calls were mocked; these checks do not prove runtime
activation. The dashboard suite emitted an asyncio slow-task diagnostic but
completed without failures.

Remaining activation work requires separate authorization: provide Waitress in
the project venv, verify port availability and the unit on the host, install and
activate the dashboard if needed, and verify live health and HTML responses.
The running Ermis System process must reload the changed allowlist to expose
the dashboard controls; this task prohibits that restart. Gateway processes
likewise retain their previous imported allowlist until reloaded. Dashboard
restart also depends on OS permission for its exact restart command, which
was not inspected. External browser access needs a verified, separately
authorized access route. Existing integrations and tunnels remain untouched.
