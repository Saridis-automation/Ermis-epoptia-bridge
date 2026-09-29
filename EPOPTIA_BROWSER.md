# Read-only Epoptia browser inspection

## Existing gateway entry point

Call the already exposed `ermis_gateway_execute` with:

```json
{"operation":"request","payload":{"session_id":"browser_inspection_0001","action":"epoptia_browser_inspect","arguments":{"scope":"dates"}}}
```

Only `scope` is accepted: `dates` (default when arguments is `{}`), `session`,
or `smoke`. Extra fields and other scope values are rejected before dispatch.
This is an internal gateway action, with no added top-level MCP registration,
schema refresh, confirmation, or HTTP call back to the System MCP service.
The existing gateway cache wrapper passes this action to the local worker.

- `dates` inspects the four fixed pages with separate ephemeral contexts and
  returns sanitized page/request metadata, DOM candidate names and JSON schema
  candidate locations such as `$.data[].due_date`. No field values are returned.
  Missing setup reports `auth_material_missing`; blocked requests mean incomplete
  coverage, even when page loading succeeds. The worker timeout is 150 seconds, including policy waits.
- `session` returns only `ok`, `usable`, and a fixed status. It fails closed:
  missing state is `auth_material_missing`; login/redirect responses are
  `login_required`. A successful page load is `session_unverified`
  with `usable: false`: the current policy has no reviewed positive evidence of
  authentication, so a public 200 page cannot be mistaken for an authenticated
  session. No authentication setup is changed by this action.
- `smoke` launches installed Chromium on `about:blank`, blocking page network
  traffic, without loading session state or contacting Epoptia.

The runtime change requires a later authorized restart of Ermis System MCP.
No restart, deployment, or authentication setup is performed by this change.

`ermis_epoptia_browser_inspect(page="production_report")` is registered only in
Ermis System MCP. Page choices are `workorders`, `workorderlines`,
`production_report`, and `daily_analysis`. The tool accepts no URLs, code,
commands, credentials, session paths, or browser options.

## Current readiness

The source-controlled policy in `epoptia_browser_policy.cjs` deliberately has
no origin or session file configured. No reusable Playwright session was
verified, and existing credential files were not read. The tool currently
returns `{"ok":false,"status":"auth_material_missing"}` without launching a
browser or contacting Epoptia. The existing HTTP login flow is not reused:
it performs a POST and requires credentials.

A separately authorized setup can set `sessionFile: 'private'` and the exact
HTTPS origin in the policy. New enrollment writes only to the private state
store described below. The old project-relative loader remains read-only for
compatibility; do not use it for new enrollment.
The inspector does not create or refresh authentication, operate on a live
profile, or save updated state. It requires an owner-only containing directory
and file, rejects symlinks and group/world-writable ancestor directories, and
accepts cookies scoped to the exact host and storage scoped to the exact origin.
It never searches for credentials. A login page, 401 or redirect returns `login_required`. A 403, 429 or 5xx
opens the persistent circuit and returns `circuit_open`. Other failures return
fixed diagnostic categories.

## Boundaries

- Uses a fixed Node worker as the service user, with a fresh environment and a
  120-second process-group timeout (150 seconds for the four-page `dates` scope).
  No shell, sudo or admin wrapper invocation.
  `admin_bootstrap/ermis-admin` was reviewed; its privileged maintenance verbs
  are unnecessary for browser inspection and were left unchanged.
- Only GET requests to exact approved routes are sent. Query names and value
  formats are restricted; URL credentials, encoded/ambiguous paths, arbitrary
  routes and origins are rejected. Redirects are never followed.
- Service workers, WebSockets, downloads, frames and form submissions are
  blocked. There are no click/fill/evaluate inputs exposed to MCP callers.
  A restrictive response CSP also blocks workers, frames and other resources.
- There are no approved external scripts/assets initially. Future additions
  need exact route review; some dynamic UI features will consequently be absent.
  POST-based report searches remain blocked even if Epoptia uses them for reads.
- Returns tag counts, recognized DOM field names, route aliases, method/type,
  status class, query **names**, and recognized JSON field names/types. Never
  returns page text, attribute values, IDs, raw URLs, headers, cookies, response
  values, screenshots, HTML, console output or exception text. Unknown field
  names are counted/omitted, including arbitrary dictionary keys.
- Response bodies must have an acceptable content type and declared length at
  most 512 KiB. Bodies without a length are withheld; decoded bodies are checked
  again. Playwright buffers responses, so this is not a streaming memory limit.
  Requests, traversal depth and output are bounded. State remains in an ephemeral
  browser context; the input session file is never modified.
- GET is not an absolute guarantee against a server with side-effecting GET
  handlers. Keep the exact route policy restricted to reviewed read endpoints.
  Counts and omitted fields indicate partial discovery, not proof that ETA/date
  fields do not exist. Authentication detection is heuristic for 200 responses.

## Local validation

```sh
node --test tests/test_epoptia_browser.cjs
venv/bin/python -m unittest tests.test_epoptia_browser tests.test_server_separation
venv/bin/python -m unittest tests.test_gateway_browser_inspect tests.test_gateway tests.test_gateway_mcp
node tests/epoptia_browser_smoke.cjs
```

The smoke test launches installed Chromium and substitutes synthetic upstream
responses, exercising browser-generated XHR/fetch, DOM extraction, write blocking,
redaction, login detection and redirect rejection without visiting Epoptia.
On this host, Chromium currently cannot launch because `libnspr4.so` is missing.
No packages were installed and no privileged setup was attempted. The smoke test
fails explicitly rather than treating a missing runtime dependency as a pass.

Gateway integration validation: 48 targeted Python tests and the Node policy /
scope suite passed. A direct local call through `ermis_gateway_execute` with
`scope: smoke` reached the worker and returned the sanitized `browser_unavailable`
result; the Chromium smoke harness reported `library_missing` during launch.
The broader voice suite has one unrelated error-envelope expectation failure
(`test_voice_preserves_missing_data_and_upstream_failures`); existing gateway
diagnostic fields exceed that test's expected response. No dependency installation
or service operation was attempted.

No service was restarted. The new MCP registration becomes available only after
a later authorized reload/restart of Ermis System MCP. Epoptia MCP and tunnel
changes are not required. Live authenticated inspection remains unvalidated.


## Runtime resolution and local validation

The worker uses a fresh, fixed environment and inherits the caller's OS user;
setting HOME does not change that user. It resolves the executable from the
installed Playwright package, then checks the exact matching Chromium revision
in the project cache and the ermis user's Playwright cache. It passes the full
Chromium executable explicitly, so a separate headless-shell installation is
not required. No arbitrary revision, caller-supplied path, shell or sudo is used.
Executable files must be regular files, not symlinks or group/world-writable.
Temporary browser files remain in a private project directory.

Smoke emits only fixed statuses: `ok`, `playwright_missing`, `chromium_missing`,
`chromium_dependencies_missing`, `runtime_permission_denied`, or `launch_failed`
(with `busy` when another inspection is running). Exceptions and browser logs
are never returned. The Python worker also sanitizes process-launch failures.
Session setup distinguishes `auth_material_missing`, `session_policy_invalid`,
and `session_state_invalid`. Here `auth_material_missing` means no approved
browser state is configured; it does not claim that API credentials are absent.

Local validation found an installed Chromium executable, but launch failed due
to the missing host library libnspr4.so. Installing host libraries is outside
this task's project-only permissions. The read-only systemd metadata query was
blocked by the sandbox, so the actual running service user and confinement
remain unverified. Project documentation specifies ermis, and the local worker
ran as UID 1000. A restart alone is not verified to resolve the host dependency.

No secret or environment contents were inspected. Source review confirms the
system MCP does not load Epoptia credentials and the existing HTTP login needs
a credential-bearing POST. That flow cannot bootstrap this GET-only inspector.
The null origin/session policy is unchanged; session scope therefore returns
`auth_material_missing` without reading secrets or contacting Epoptia.


## Login lifecycle readiness (fail-closed)

Public actions through System MCP `ermis_gateway_execute`:

| Action | Arguments | Confirmation |
| --- | --- | --- |
| `epoptia_login_status` | `{}` | Read-only |
| `epoptia_login_start` | optional `ttl_minutes`, integer 1–5, default 5 | Required |
| `epoptia_login_finalize` | `{}` | Required |
| `epoptia_login_stop` | `{}` | Required |

All reject extra fields, including credentials, URLs, bind addresses and paths.
The old `epoptia_browser_login_*` names remain aliases. Request a write, present
its proposal, then confirm using the same conversation session ID and the
returned confirmation ID. Each write needs its own unexpired approval; rejected,
expired, cancelled or replayed confirmations cannot execute.

**Enrollment is not operational.** All four actions return `login_not_ready`
with `ok: false`. The gateway envelope means dispatch completed, not successful
enrollment. They perform no process launch, network access, state read/write,
permission change or cleanup. Stop cannot claim to stop an unowned surface.
The standalone Node readiness command also fails closed, including stop.

`EPOPTIA_LOGIN_READINESS.marker` and Python action results report these exact
source blockers: `approved_origin_missing`, `authenticated_landing_signal_missing`,
`persistent_loopback_backend_missing`, and `fixed_bootstrap_installer_missing`.
`source_wiring_ready=true` means the contracts and dispatch are wired only;
`operational_ready=false` is authoritative for availability. Bootstrap installation
will be required, but `bootstrap_install_ready=false`: reinstalling the existing
bootstrap cannot enable login. No installed-host state was inspected.

The existing bootstrap installs the fixed admin wrapper, receipt and sudo rule;
it has no login supervisor installer. It remains unchanged because this task
forbids modifying authentication, systemd and sudoers. Enabling login requires a
reviewed positive landing-page signal and persistent supervisor implementation,
then a separately authorized fixed installer for its unit/wrapper/permissions.
Do not substitute a 200 response, cookie presence or user assertion for proof.

The injectable supervisor is a test foundation, not an enabled runtime. A future
backend must enforce private disposable profiles, owner-only state, literal
loopback binds, authenticated console access, no TCP X11/CDP, shared scheduler
ownership, bounded lifetime and cleanup of all owned children. Finalize must
verify only the approved landing-page signal before saving cookie-only state.
Any access URL must be delivered only in the confirmed start result, with its
secret excluded from logging and ordinary status results. No access URL or SSH
instruction is currently returned because no secure surface exists.

## Shared scheduling and storage contract

`epoptia_browser_scheduler.cjs` is shared by both existing site inspectors.
Future site operations must hold `Scheduler.run` for their entire browser
lifetime and use `navigate`, `observe`, and (for separately confirmed writes)
`click`. Production defaults/floors are eight seconds between navigation starts,
three seconds after clicks, and sixty seconds of cooldown after navigation ten
finishes. Timing overrides can only be slower. Reservations, batch counters and
circuits persist across worker invocations. Duplicate page document navigations
are blocked; the current request/GET allowlists remain in place.

An in-process guard and atomic exclusive `operation.lock` file serialize workers.
A crash leaves the lock in place: automatic stale-lock stealing is deliberately
unsupported. Operator recovery must first verify that every prior worker and
browser child is gone, then remove only the stale lock. A circuit likewise has
no timer-based reset or automatic retry; separately reviewed operator recovery
is required. A worker timeout during a cooldown can therefore leave a stale lock.
This favors stopping over risking overlapping site activity. Default worker budgets
include a cooldown: inspection uses 120/150 seconds and access-check uses 100
seconds. Slower future timing configuration may require separately reviewing
these deadlines.

HTTP and visible 403/429/5xx open the circuit. Login forms and redirects stop with
`login_required` and invalidate private state. Visible detection is heuristic,
not authentication proof. No browser/site request is retried. The separate local
IPC helper permits at most three attempts with one- and two-second backoff, only
for explicitly marked EPIPE/ECONNRESET errors, never status pages or general
Playwright timeouts. Already in-flight requests cannot be recalled; new requests
are blocked once a stop is observed.

The default storage directory is the OS account home plus
`.local/state/epoptia-browser` (for ermis,
`/home/ermis/.local/state/epoptia-browser`), independent of environment variables.
It must be 0700; files must be regular, owner-owned, single-link 0600 files.
Symlinks and writable ancestors are rejected. Atomic replacement uses a private
exclusive temporary file, fsync, rename, and directory fsync. Nothing was created
there during implementation or tests.

`session.json` contains exact-host secure cookies only, an origin, and an explicit
expiry no later than 24 hours (finalization uses one hour). Username/password
fields, localStorage and IndexedDB are not persisted. This deliberately may not
support an application that requires localStorage authentication. `invalid.json`
forces `login_required`; successful verified enrollment removes it. Expiry does
not trigger refresh or credential login. Existing session/credential artifacts
are untouched; precise ignore entries prevent the listed legacy artifacts from
being newly added to git. Ignore rules do not untrack already tracked files.

No bodies, form values, raw URLs, exception strings, cookies, or console secrets
are logged. Existing outputs retain only fixed statuses and approved metadata.
All Epoptia mapping remains **read-only until separate action-time confirmation
for writes**; enrollment approval never authorizes business-data changes.

Offline foundation validation uses synthetic state beneath the checkout:

```sh
node tests/test_epoptia_browser_foundation.cjs
node tests/test_epoptia_browser.cjs
node tests/test_epoptia_browser_access_check.cjs
node tests/test_epoptia_browser_runtime.cjs
node tests/test_chromium_runtime_smoke.cjs
venv/bin/python -m unittest tests.test_browser_login_gateway tests.test_epoptia_browser tests.test_gateway_browser_inspect tests.test_gateway tests.test_gateway_mcp
```

No live authentication, actual VNC surface, production state, service restart,
or network access is part of these checks. Test storage explicitly trusts its
synthetic checkout boundary because the checkout itself is group-writable;
production directory validation retains the stricter ancestor checks.

## Fixed local login source readiness

The login policy is pinned to `https://app.epoptia.com`. Alternate authorities,
explicit ports (including `:443`), schemes and off-origin redirects are refused.
HTTP redirects are inspected before following them; service workers, popups and
WebSockets cannot bypass the enrollment request policy.

Finalize requires the final same-origin path `/dashboard`, no visible password
field, and exactly one visible `nav a[href="/logout"]` authenticated shell link.
This selector is a **candidate contract, not an observation of the live UI**.
It must match at action time or enrollment fails closed. Only a reviewed source
change to `epoptia_login_policy.cjs` may add a verified authenticated path or
replace the selector after authorized UI observation. No live verification was
performed for this change. A URL alone, cookies alone, or absence of a login
form never proves authentication. No page text or credential values are returned.

The persistent owner is `epoptia_login_daemon.cjs`, controlled through a mode-0700
runtime directory and private Unix control socket. Its fixed wrappers accept no
arguments. The MCP start action additionally accepts a TTL of 1–5 minutes.
Xvfb uses a private Xauthority file and disables TCP X11. VNC and noVNC bind only
`127.0.0.1:5991` and `127.0.0.1:6091`. VNC requires a disposable random password
held only in the private runtime surface directory; no tool returns it. Access
requires a separately authorized private transport and operator handling of that
file. This change creates no public endpoint or tunnel configuration.

The owner holds the existing scheduler lock until cleanup. Stop, failed launch,
failed finalize and expiry close Chromium, terminate owned child processes and
remove the disposable profile and console material. A hard deadline exits the
managed supervisor if graceful TTL cleanup stalls; systemd control-group cleanup
then terminates descendants. An abnormal exit may leave the existing scheduler's
fail-closed stale lock; it is never automatically stolen. Successful enrollment
persists only validated cookies in the private store. Reader session activation
remains under its existing separate policy.

Source readiness does not mean operational readiness. The fixed bootstrap must
be installed and its unit active before the managed runtime reports operational
readiness. See `admin_bootstrap/LOGIN_BOOTSTRAP.md`. Nothing was installed or
started as part of this source change.

### Offline same-service sandbox diagnostic

`epoptia_login_sandbox_probe` (legacy alias
`epoptia_browser_login_sandbox_probe`) takes an empty argument object. It is a
read-only gateway action and requires no write confirmation: it cannot navigate
to Epoptia, enroll, or read/write the persisted auth session. It uses the existing
installed login control socket, source-revision checks, and service identity.
No independent shell command or caller-supplied executable/target is exposed.

The daemon excludes concurrent login/probe operations. A disposable worker uses
the real Backend's executable resolver, headed Chromium, sandbox enabled,
managed HOME/XDG/profile preparation and Xvfb launch. VNC/websocket helpers and
login navigation are skipped. A fresh `sandbox-*` directory under the managed
runtime root is removed after every outcome. The worker inherits the service's
hardening; its descendants stay in one dedicated process group for forced
cleanup. No unit, LSM, NNP, userns, or sandbox setting is loosened.

The only target is `about:blank`. Chromium background networking, sync, component
updates, pings and QUIC are disabled; a fixed unresolvable proxy with no loopback
bypass and `MAP * ~NOTFOUND` DNS rules apply from spawn. Playwright starts offline,
blocks service workers, aborts all routes and closes WebSockets/popups. A ready
browser dwells for 10 seconds and closes cleanly. The daemon's independent
20-second watchdog kills the worker group even if launch, dwell or close hangs;
forced termination is never reported as a clean close. If group termination or
disposable-storage cleanup fails, further starts/probes remain blocked in this
daemon rather than releasing exclusivity.

Child stderr is intercepted before Playwright's collector, classified in bounded
8 KiB fragments, zeroed and discarded. Raw text is neither logged, persisted nor
returned. Only closed boolean/enum schemas cross worker IPC, the service socket,
and the Python gateway. `status`/`diagnose` retain existing fields and optionally
include the last sanitized `sandbox_probe` result, held only in daemon memory.
Results include acceptance/spawn/ready/clean-close booleans, exit/signal classes,
elapsed bucket, sandbox class, and primitive user-namespace outcome. The latter
runs a fixed `unshare(CLONE_NEWUSER)` syscall in a short-lived child, without
network, mount, auth-state, or persistent writes.

Effective facts are restricted to nonroot identity, bounded fixed sysctl and
current AppArmor reads, and adjacent setuid-helper metadata. Helper `valid`
means expected metadata only, not proof that NNP/LSM permits its use. A generic
namespace permission error or SIGTRAP does not identify a cause: it remains
`unknown` or `sandbox_check_other`; the primitive's `permission_denied` does not
by itself distinguish AppArmor, seccomp or a unit restriction. Fragmented or
unrecognized stderr can also remain unknown. No production fix is attempted.

Changed owned sources are covered by the trusted login manifest; stale or tampered
sources remain rejected. This source update requires the separately authorized
trusted bootstrap/update workflow before use in the installed service. The
implementation job does not install, restart, activate, or run the live probe.
