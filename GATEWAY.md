Ermis Gateway / Voice
====================

`ermis_gateway.py` owns routing and confirmation policy. The separate
`ermis_gateway_server.py` process serves loopback port 8002, calling the existing
Epoptia_MES (8000) and Ermis_System (8001) MCP endpoints. It adds no MCP tools,
loads no credentials or environment configuration, and uses existing dependencies.
Existing bridges and connector registrations are unchanged.

After separately authorized deployment, the entrypoint is
`venv/bin/python ermis_gateway_server.py` (Uvicorn, one process, no reload).
No deployment, service changes, or live upstream calls are part of local tests.

Client contract
---------------

A trusted local voice backend creates a fresh session ID (16–128 ASCII letters,
digits, underscores or hyphens) per conversation and POSTs JSON to `/request`:

```json
{"session_id":"voice_session_0001","text":"show production overview"}
```

Alternatively, a future voice/Realtime client can expose ONE application function
to its model, passing a structured action to the same endpoint:

```json
{"session_id":"voice_session_0001","action":"wol_details","arguments":{"wol_id":123}}
```

The backend supplies session_id itself. Model output is untrusted: exact action,
argument names, types, limits and service names are revalidated by the gateway.
The deterministic text router is intentionally bounded English, not a general
language model. Unknown/compound requests return `unsupported_request`; ask the
user to clarify. Supported text examples include `git status`, `system health`,
`show WOL status 123`, `show workorder progress 123`, `list work orders`,
`show due work orders`, `show workstation work in progress`,
`status service ermis-system-mcp.service`, and `show job logs <32-hex-ID>`.

`ACTIONS` is the authoritative allowlist: production overview, WOL status/details,
workorder progress, default list/due/WIP queries, system health, Git status,
service status, job status/logs, and service restart. Query filters are not yet
exposed. Every declared argument is required; extra arguments are rejected.
Service names reuse `service_control.ALLOWED_SERVICES`. No shell, arbitrary URLs,
job launch, report consumption, commits, edits or comments are available.
New actions require a code review and explicit read/write classification.

Confirmation boundary
---------------------

`restart service ermis-system-mcp.service` (or action `restart_service` with
`service`) returns `confirmation_required`, an opaque confirmation_id, the exact
action/arguments, a prompt and a 120-second lifetime. Nothing executes yet.
The trusted client must display/read that exact proposal and obtain an explicit
user approval, then POST to `/confirm`:

```json
{"session_id":"voice_session_0001","confirmation_id":"<returned ID>","approved":true}
```

Use `approved:false` to cancel. Do not expose `/confirm` as a model function or
let model-generated text establish approval. A generic “yes” sent to `/request`
cannot authorize anything. Confirmations are session-bound, immutable, expiring,
and consumed atomically before execution; duplicate/racing confirmations cannot
repeat an operation. At most 256 proposals are retained. Process restart loses
pending approvals safely; multiple workers/replicas are not supported.

This is a trusted-local-backend boundary, not user authentication: session IDs
are correlation values, and any local process able to call the gateway is trusted.
The listener rejects non-loopback peers, unexpected Host headers and browser
Origin headers; proxy headers are disabled. Do not expose or tunnel it directly.
Remote voice clients need an independently authorized authenticated backend that
owns identity and approval; this change adds no authentication or external API.
Existing MCP callers bypass this gateway's policy and retain existing behavior.

Responses distinguish `completed` (upstream returned; inspect nested `result.ok`
and `accepted`), `confirmation_required`, `cancelled`, validation failures and
`upstream_unavailable`. A write transport failure returns `outcome_unknown` and
`retry_safe:false`: do not automatically resubmit; inspect service status first.
Even a successful restart response means accepted, not verified completion.
Upstream calls have a 30-second deadline and are never automatically retried.
Raw exception text is withheld; normal structured upstream results retain their
existing sanitization contracts. Request/response bodies are not logged.

Local validation: `venv/bin/python -m unittest discover -s tests -q`.
Gateway tests use synthetic MCP results and mock dispatch, including every read
route, blocked inputs, replay/races, expiry/cancellation, capacity and HTTP guards.
