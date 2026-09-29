Ermis Gateway / Voice
====================

This extends commit `128af49f3298` with a local browser voice client on
`http://127.0.0.1:8002/` (also `http://localhost:8002/`). The gateway is a separate
process. Epoptia_MES on 8000 and Ermis_System on 8001 remain the source of truth;
all business/system actions call their existing MCP tools. Ermis_System also
exposes the single `ermis_gateway_execute` MCP entry point described below.
Existing services, tunnel settings and dependencies are unchanged on disk.

ChatGPT / Project MCP entry point
--------------------------------

Use `ermis_gateway_execute` on the existing Ermis_System connector (`/mcp` on
loopback port 8001). No second set of Epoptia or infrastructure tools is registered.
The wrapper calls `Gateway.request` or `Gateway.confirm`; dispatch stays in the
shared allowlist and uses the existing local MCP endpoints. The adapter accepts
structured dictionaries or a single JSON-text dictionary from existing tools.

Read example (tool arguments):

```json
{"operation":"request","payload":{"session_id":"chat_session_0001","action":"station_wip","arguments":{"workstation":"Strantza"}}}
```

Alternatively supply `text` instead of `action` and `arguments`, for example
`"text":"show production overview"`. Use a unique session ID per conversation,
16–128 letters, digits, underscores or hyphens. Supported actions and exact
argument names are defined by `ACTIONS` in `ermis_gateway.py` and advertised in
the tool description. Unknown actions, extra payload fields and invalid values
are rejected by the existing policy.

A write request only returns `confirmation_required`. ChatGPT must present the
exact proposal and wait for explicit user approval before calling the same tool
with `operation: "confirm"` and a payload containing only `session_id`, the
returned `confirmation_id`, and boolean `approved` (false cancels). The existing
120-second expiry, session binding and atomic single-use consumption apply.
The MCP host must enforce user approval: a caller-supplied boolean is not proof
of human consent. Session IDs are correlation IDs, not authentication.

This wrapper owns one process-local Gateway instance in Ermis_System. Its
confirmation IDs cannot be used in the separate browser gateway or survive a
process restart; run a single worker. Unlike the voice flow, MCP confirmation
IDs are returned to the tool caller. Existing voice approval handling is unchanged.
Writes include the allowlisted restart and `install_chromium_dependencies`.
The latter accepts exactly `arguments: {}` and, after confirmation, invokes only
the installed admin wrapper's fixed `browser install-chromium-dependencies`
action. It installs the fixed Playwright/Chromium Ubuntu runtime dependency set
and makes no Epoptia data changes. No package names, commands or extra arguments
are accepted. No standalone installer MCP tool is registered.

Installer responses contain only `ok` and a fixed `status` code. Missing, old,
unreadable or unrecognized wrapper source returns
`admin_wrapper_action_unavailable` without execution. The capability check reads
only the installed wrapper source and recognizes its reviewed fixed dispatch
branch; it never imports the source. Launch failures return
`admin_wrapper_execution_unavailable`; nonzero exit returns
`admin_wrapper_action_failed`. The wrapper does not distinguish authorization,
platform or package-manager failures. Output is discarded. A timeout returns
`outcome_unknown`, since a privileged child may still be running. Never
automatically retry. For other actions inspect nested `result.ok`/`accepted`.

`chromium_runtime_smoke` accepts exactly `arguments: {}` without confirmation.
Invoke it through `ermis_gateway_execute` in Ermis_System: only that server
dispatches the worker locally, inheriting its user and service restrictions.
Other gateway instances return `service_context_required` without launching it.
It reuses the project's Playwright executable resolver, opens only `about:blank`,
blocks browser requests and sockets, and loads no Epoptia session. Browser files
use a disposable project-local directory; the worker receives only fixed runtime
settings, not the service's credentials. A 20-second worker deadline plus a
2-second reap limit bounds execution; cleanup kills its dedicated process group
and removes temporary files, including on cancellation. No packages are installed.
Responses contain only `ok` and an allowlisted `status`: `completed`,
`playwright_missing`, `chromium_missing`, `chromium_dependencies_missing`,
`runtime_permission_denied`, `launch_failed`, `timeout`, `busy`, or
`service_context_required`. No worker output, paths, or exception text is exposed.

Activation requires a separately authorized Ermis_System restart and connector
tool-discovery refresh using its existing protected tunnel route. No tunnel,
authentication, listener or Epoptia registration changes are required. The local
browser port 8002 remains private. See [MCP_SERVERS.md](MCP_SERVERS.md).

Voice transport
---------------

The browser captures microphone audio and plays model audio over WebRTC. Start
creates an SDP offer and POSTs it to `/voice/session`. The server supplies the
reviewed model configuration and sends multipart `sdp` and `session` fields to
OpenAI's `POST /v1/realtime/calls`; only the SDP answer and a gateway session ID
return to the browser. Direct HTTPS uses the existing `requests` dependency,
fixed destinations, certificate verification, no redirects or environment proxy
configuration, bounded responses and connection/read timeouts. No SDK required.

This follows the [OpenAI unified WebRTC interface](https://developers.openai.com/api/docs/guides/realtime-webrtc).
The server reads `OPENAI_API_KEY` from its runtime environment only when making an
OpenAI request. It does not load dotenv files or import the MCP server's startup
code. The key is never included in static assets, API responses or logs. Missing
configuration and provider failures return a generic `voice_unavailable` (503).
Tests inject synthetic transport/configuration and never read actual credentials.

The minimal implementation relays function calls only from `response.done` events
whose response status is `completed`; cancelled/failed/incomplete responses do not dispatch. Calls go from
the browser data channel to `/voice/tool`. Server code owns tool schemas,
validation, MCP dispatch, result caching, approvals and hangup. The client sends
`function_call_output` events back and requests the next response. It never
executes MCP business logic. The server treats every relay payload as untrusted.
The browser can alter its Realtime conversation, but cannot add gateway actions
or bypass approval by changing the model's tools or instructions.

A dedicated [Realtime sideband WebSocket](https://developers.openai.com/api/docs/guides/realtime-server-controls)
is not implemented: the current dependency set has no WebSocket client and the
bounded relay avoids adding one. Tool handling therefore depends on the browser
remaining connected; server-origin verification of model events is not claimed.
This is suitable for the existing trusted local boundary, not an authenticated
remote or multi-user deployment.

Try “What is running in Strantza now?” The model uses `station_wip` with
`workstation: "Strantza"`, mapped directly to Epoptia_MES `workstation_wip` with
that filter. The deterministic text router also understands this exact question.
Matching, pagination, aggregation and production interpretation stay upstream.
WIP includes started, in_progress **and paused** entries; instructions require the
model to distinguish them and explain missing/truncated data. Empty routing data
cannot establish that a physical machine is idle. No live production query was
performed during implementation.

HTTP contract and policy
------------------------

- `GET /`: browser UI; Start/Stop, audio controls and explicit approval buttons.
- `GET /health`: process liveness only; never contacts MCP/OpenAI or inspects keys.
- `POST /voice/session`: `{"sdp":"v=0..."}` → 201 with `sdp`, `session_id`,
  `expires_in_seconds`. Maximum 64 KiB request, at most four active sessions.
- `POST /voice/tool`: `session_id`, `call_id`, `name`, `arguments` → gateway result.
  Schemas come solely from the reviewed `ACTIONS` allowlist. Unknown actions,
  extra fields, invalid IDs/services and malformed arguments cannot dispatch.
- `POST /voice/confirm`: `session_id`, `confirmation_id`, boolean `approved`.
  This endpoint is **never** a model tool. Only the UI approval/cancel buttons use it.
- `POST /voice/stop`: `session_id`. Invalidates local session and approvals, then
  attempts OpenAI hangup; `stop_unverified` means remote hangup was not verified.
- Original non-browser `POST /request` and `/confirm` contracts are preserved.
  `/request` accepts `session_id` plus either `text` or `action` and `arguments`.
  These legacy endpoints continue to reject every Origin header.

Example legacy read request (send from a trusted local backend):

```json
{"session_id":"voice_session_0001","text":"what is running in Strantza now?"}
```

Automatic reads: overview, WOL status/details, workorder progress, list/due WOLs,
default WIP or workstation-filtered WIP, system health, Git status, service
status, job status and job logs. Gateway code calls existing MCP endpoints only.
There is no arbitrary shell, URL, job launch, commit, edit, comment or production
write tool. Approved service names reuse `service_control.ALLOWED_SERVICES`.

The existing service write action is `restart_service`. Requesting it creates a
proposal with the exact service and a 120-second, opaque, session-bound token;
nothing executes yet. In the voice flow, approval tokens never enter model messages. Explicit user
button approval consumes the pending record atomically before MCP execution.
Speech such as “yes”, model-supplied approval arguments, incorrect sessions,
expired tokens and replay cannot authorize an operation. Cancellation and Stop
invalidate proposals. At most 256 pending proposals are retained. A write failure
returns `outcome_unknown`, `retry_safe:false`; inspect service status before any
new request. Even `completed` means upstream returned: inspect nested `result.ok`
and `accepted`, and do not claim the restart finished merely because it was accepted.

The browser immediately releases media tracks and closes the peer on Stop,
connection failure or a ten-minute limit. A late microphone permission or SDP
response after Stop is cleaned up, including Stop during local/remote SDP setup.
Page exit clears client ownership and timers before attempting a best-effort hangup,
so returning through the browser back/forward cache allows a fresh Start.
The standalone server sweeps expired sessions every 30 seconds and also on voice
requests. Up to 128 unique tool call IDs per conversation are cached to prevent
duplicate dispatch; reusing an ID with different arguments is rejected. Operations
are serialized, so slow upstream requests can delay other conversations. Process
exit loses pending approvals safely. Multiple workers/replicas are unsupported.
A failed hangup or abrupt process loss may leave a provider session alive until
the browser disconnects or the provider expires it; the local TTL is not a
provider-side spending cap.

Boundary and observability
--------------------------

Only loopback peers and exact local Host values are accepted; proxy headers are
disabled. Voice POSTs accept matching local Origin headers; foreign/null Origins
and cross-site fetches are rejected. No CORS is enabled. JSON content types,
request size limits and same-origin static assets keep the browser boundary
narrow. Responses are no-store, nosniff, with a restrictive CSP and no framing.
The standalone ASGI wrapper enforces body limits before WSGI buffering, including
chunked requests and requests to health/static endpoints: 64 KiB for the session
path and 4 KiB elsewhere, with a ten-second total body-read deadline (408).
This is local trust, not user authentication: any local process can call the
original gateway endpoints. Do not expose port 8002 through a tunnel or proxy.
Remote access would require separately authorized identity/approval controls.

Structured application logs contain only fixed event/endpoint names, HTTP status
and duration. No request paths, query strings, SDP, audio, transcripts, arguments,
results, session/confirmation IDs, headers or raw exception messages are logged.
Uvicorn access logs are disabled. Existing MCP results retain their upstream
sanitization contract. Voice audio and requested tool results are transmitted to
OpenAI; the UI makes this visible before starting.

Validation and deployment
-------------------------

```sh
venv/bin/python -m unittest discover -s tests -p 'test_gateway*.py' -q
node tests/test_gateway_browser.cjs
```

Python tests exercise synthetic HTTPS and MCP dispatch, schemas, Strantza reads,
confirmation binding/replay/expiry/cancellation, duplicates/concurrency, lifecycle,
HTTP guards, size limits and safe logging/errors. Node tests mock browser media,
WebRTC and HTTP to check start/stop races, failures and approval UI behavior.
No credentials, live API calls, real microphone/browser or running service are
needed. A real browser/OpenAI/MCP end-to-end check remains a deployment-time task.

See [GATEWAY_DEPLOYMENT.md](GATEWAY_DEPLOYMENT.md) for the review-only proposed
separate service definition and acceptance steps. No service was started,
stopped, restarted, installed or deployed by this change.

Epoptia admin-write foundation
-----------------------------

Two distinct structured actions are available through the existing gateway/MCP
request and confirm flow: `update_product_name` takes exactly `product_id`,
`expected_current_name`, `new_name`; `update_wol_description` takes exactly
`wol_id`, `expected_current_description`, `new_description`. Both are writes,
with exact target/old/new arguments in the proposal and no request-stage dispatch.
IDs are integers 1–999999999999999 (not booleans); expected/new strings must be
nonblank and control-free, at most 255 characters for names or 2000 for descriptions.
These are local policy limits. Extra fields are rejected.

Confirmed execution is deliberately disabled: the local adapter returns
`ok:false`, `status:auth_required`, `write_performed:false`. It never contacts
upstream MCP or a browser. Its future read-only transport seam checks session/CSRF
readiness and exact current-value equality; even a match returns
`write_transport_unverified`. No authenticated write client exists yet. This is
not success, and confirmation does not enable a network write. Product master
names and WOL descriptions are independent. See [EPOPTIA_FUNCTION_MAP.md](EPOPTIA_FUNCTION_MAP.md).


## Private browser enrollment foundation

Use the public actions documented below. Legacy `epoptia_browser_login_*`
aliases use identical contracts. Login approval never authorizes Epoptia
business-data writes. All four actions return `login_not_ready` locally and
perform no state access, process launch or cleanup while prerequisites are missing.

Both existing browser site inspectors share persistent exclusive locking,
eight-second navigation spacing, a sixty-second cooldown after ten navigations,
and a stop circuit on HTTP/visible 403, 429 or 5xx. Login expiry/redirects return
`login_required`. Circuits and crash-left locks require deliberate operator
recovery; nothing retries or steals a lock automatically. Gateway outputs never
include console secrets or browser session contents. Deployment/reload is a
separate authorized operation and was not performed by this implementation.

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

### Offline login sandbox probe

`epoptia_login_sandbox_probe` (and `epoptia_browser_login_sandbox_probe`)
accepts exactly `{}` and runs without write confirmation. It uses the installed
private login socket/service with a fresh disposable profile, only `about:blank`,
network/DNS guards, sandbox enabled, a 10-second ready dwell and a 20-second
worker deadline. It cannot access saved authentication or Epoptia. Results are
closed booleans/enums; raw Chromium stderr is discarded in memory. Login and
probe concurrency is one. See [diagnostic details](EPOPTIA_BROWSER.md).
The source change requires a separately authorized trusted bootstrap update;
this task does not install, restart, activate or run the probe.
