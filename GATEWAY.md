Ermis Gateway / Voice
====================

This extends commit `128af49f3298` with a local browser voice client on
`http://127.0.0.1:8002/` (also `http://localhost:8002/`). The gateway is a separate
process. Epoptia_MES on 8000 and Ermis_System on 8001 remain the source of truth;
all business/system actions call their existing MCP tools. No MCP registrations,
existing services, tunnel settings or dependencies change.

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

The only existing write action is `restart_service`. Requesting it creates a
proposal with the exact service and a 120-second, opaque, session-bound token;
nothing executes yet. Approval tokens never enter model messages. Explicit user
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
