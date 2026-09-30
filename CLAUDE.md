# ERMIS / Epoptia bridge — project memory

Speak to the user in Greek, addressing them in the singular (ενικός — "εσύ", not "εσείς"). Work as a partner: do the technical work yourself (shell, code, tests)
instead of handing the user commands to run. Ask for a one-line "yes" only before irreversible or
production-affecting actions (see Safety rules).

## What ERMIS is
Always-on integration server for SARIDIS on Ubuntu host `Ermis-server`, user `ermis`.
Repo: `/home/ermis/projects/epoptia-bridge` (venv: `./venv`). Secrets in `.env` (never print it).
`EPOPTIA_BASE_URL=https://saridis.epoptia.io`, API key in `.env`.

Services (systemd):
- `ermis-epoptia-mcp` (127.0.0.1:8000, `mcp_server.py`) + `ermis-epoptia-tunnel` — MES reads
- `ermis-system-mcp` (127.0.0.1:8001, `ermis_system_server.py`, `service_control.py`) + `ermis-system-tunnel` — host admin
- `ermis-dashboard` (port 8010, `python -m dashboard.server`)
- a Voice/ERMIS Gateway service (exact unit name unknown — find it)
Keep the MES / system-admin split.

## Confirmed working (historical, re-verify)
- Runs 24/7 without the developer Mac (since 6 Sep 2026).
- Epoptia API reads: `GET /api/3.03/workorderlines` (32 pages / 3,131 records),
  `POST /capacity-planning/workorderlines` (native progress; `workorder.progress` = order level,
  WOL progress = line level). production_overview can take ~67 s; occasional timeouts ≠ IP block.

## Open workstreams (separate — do not conflate)
1. **Epoptia writes (TOP PRIORITY for the user).** Goal: create products, create/modify orders,
   change descriptions, routings/workflows, workstations. Epoptia's official API (v3.0.3,
   https://epoptia.tawk.help/article/api-version-303) supports: create/update WO & WOL, delete
   un-launched WO/WOL, archive WOLs, add clients, add products, remote workstation control
   (start/pause/cancel/complete), GET workflow templates. Not documented: editing workflows,
   workstations, existing product descriptions → discover via the Epoptia web UI network calls
   (done by Claude in the browser on the user's Mac, user logs in once). Prefer official API;
   UI-internal endpoints are unofficial and may change.
2. **Dashboard.** Capacity % was relative pending counts (not utilization); unknown capacity was
   rendered as 100%. Intended: unknown → "—". After that change the page went blank while the
   backend still served data. Unresolved. True utilization needs standard_time_per_step,
   remaining_quantity, available_station_time, capacity_horizon.
3. **Voice Gateway.** Health OK, but returns `upstream_unavailable` while MCP answers directly →
   Gateway↔MCP protocol/session issue.
4. **Connector schema:** server had 15 tools, ChatGPT saw 14 — likely client-side caching.
5. **Browser/AppArmor installer — PAUSED.** `admin_bootstrap/bootstrap.sh` grew into an
   over-engineered transactional installer. 24 Sep: recovery OK (10 items archived to
   `/root/ermis-login-recovery-err_ra_7`, TRANSACTION_STATE idle, ATOMIC_PREFLIGHT_OK), then install
   failed with `B_LOGIN_APPARMOR-MANUAL_RECOVERY_BEFORE_KERNEL_BOUNDARY_PROFILE_COMMIT`.
   Unknown whether it changed anything. Decision: no more install attempts. First a read-only
   check for leftovers; later, if a server-side browser is still needed, replace with a simple
   design (one plain AppArmor profile via apparmor_parser, or a sandboxed container/systemd unit).
   Browser-based discovery does NOT need this — it's done from the Mac.

## Safety rules
- Never print `.env` or API keys. Never disable AppArmor. No broad sudo.
- Read-only inspection is always fine.
- Ask the user first before: any write to Epoptia production data, installs, `apparmor_parser`,
  running `bootstrap.sh install`/recovery, deleting files, restarting services, git push/reset.
- Epoptia writes: test on test/dummy records first; show a preview of the exact change; log every
  write (timestamp, payload, response) to `logs/epoptia_writes.log`; keep write tools separate
  from read tools.
- Do not remove `/root/ermis-login-recovery-err_ra_7`.
- Epoptia once blocked our IP for rapid requests. Every HTTP call to Epoptia goes through
  `epoptia_throttle.call()` (one request at a time across all processes, ≥1 s between reads,
  ≥2 s around writes). HTTP 429/403 writes `state/epoptia_halt.json` and stops everything — no
  automatic retries; only the user clears it (`venv/bin/python epoptia_throttle.py clear --confirm`).
- `ermis-dashboard` runs with `ProtectSystem=strict` + `ProtectHome=read-only`; only
  `state/` is writable via `/etc/systemd/system/ermis-dashboard.service.d/epoptia-state.conf`
  (`ReadWritePaths=`). Before any change that writes files, check each unit's sandbox
  (`systemctl show <unit> -p ProtectHome -p ProtectSystem -p ReadWritePaths`). The MCP units
  have no sandbox. Restarting an MCP unit also restarts its tunnel (`Requires=`). sudo without
  password covers only `systemctl restart` of the two MCP and two tunnel units, not the dashboard.
- Tests: run `scripts/run_offline_tests.sh` (offline guard, temp throttle state). Never run tests
  directly with the real `state/` dir — a mocked 403 would halt production.

## First session checklist
1. Read-only audit: host, `git status/log/diff --stat`, service states, `ss -ltnp`, dashboard HTTP
   + logs, `aa-status` / kernel profiles filtered for ermis|chrom|login, files in
   `/etc/apparmor.d` changed since 2026-09-01, grep `admin_bootstrap` for the error string.
2. Create `.claude/settings.json` allowing read-only commands (git status/log/diff, systemctl
   status/is-active, journalctl, ss, curl to 127.0.0.1) and denying `Read(./.env)`.
3. Report findings to the user in Greek, then start on Epoptia writes.
