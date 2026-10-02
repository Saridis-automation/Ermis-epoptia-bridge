# ERMIS / Epoptia bridge — project memory

Speak to the user in Greek, addressing them in the singular (ενικός — "εσύ", not "εσείς"). Work as a partner: do the technical work yourself (shell, code, tests)
instead of handing the user commands to run. Ask for a one-line "yes" only in the three
cases listed in Safety rules.

## What ERMIS is
Always-on integration server for SARIDIS on Ubuntu host `Ermis-server`, user `ermis`.
Repo: `/home/ermis/projects/epoptia-bridge` (venv: `./venv`). Secrets in `.env` (never print it).
`EPOPTIA_BASE_URL=https://saridis.epoptia.io`, API key in `.env`.

Services (systemd):
- `ermis-dashboard` (port 8010, `python -m dashboard.server`) — reads Epoptia directly (no MCP).
- user units: `claude-remote-control` (this Claude session), `ermis-epoptia-backup.timer` (nightly).
- DISABLED 1 Oct 2026 by the user's decision (ChatGPT access no longer used; files kept, re-enable
  with `sudo systemctl enable --now …`): `ermis-epoptia-mcp` (8000) + `ermis-epoptia-tunnel`,
  `ermis-system-mcp` (8001) + `ermis-system-tunnel`. Keep the MES / system-admin split if revived.
- Voice/ERMIS Gateway (`ermis_gateway_server.py`, port 8002) is NOT deployed; it depends on the
  disabled MCP servers. To be discussed with the user.

## Quick start for a new session (read this before searching anything)
Everything below already exists and works. Do not rediscover it, and do not ask the user for API addresses or keys.
- **Look up Epoptia data FIRST in the local copy, without any request to Epoptia:**
  `~/epoptia-backup/epoptia.sqlite`, table `records`.
  - `kind` is one of: product, client, workstation, workflow, customfield, tag, workorderline.
  - `data` is the full JSON of each record.
  - The copy is refreshed nightly at 23:30. Example:
    `venv/bin/python -c "import sqlite3,json,os; ..."` or `dashboard.load_model._lines_from_backup()`.
  - Workflow template pages are kept in `~/epoptia-backup/raw/<date>/workflow_<id>.html.gz`.
- **Need fresh data during the day?** Only if the user asks:
  - all work order lines: `venv/bin/python epoptia_backup.py --now --only workorderline` (32 GETs, ~3 min)
  - one page: `epoptia_write.WebWriter` (`_writer_from_env()._get_page('/products/1427')`)
  - All of it goes through the throttle.
- **Writes** (orders, products, clients, workflows, name-checked deletes): `epoptia_write.py`.
  Endpoints and conventions are in `docs/epoptia_form_map.md`.
  For a PDF order: `create-order --plan inbox/<x>.plan.json` (preview first, then `--confirm`).
- **Workflows (read/create/edit/delete):** `epoptia_workflows.py`; format and ids in `docs/epoptia_form_map.md`.
- **Station load in the dashboard:** `dashboard/load_model.py`. Model and parameters: `docs/dashboard_load_model.md`.
- **Terminology:** a SARIDIS "κωδικός προϊόντος" = work order LINE id (e.g. 2380).
- **The user's open requests and dates** live in Claude's auto-memory for this folder. It is loaded automatically.

## Confirmed working (historical, re-verify)
- Runs 24/7 without the developer Mac (since 6 Sep 2026).
- Epoptia API reads: `GET /api/3.03/workorderlines` (32 pages / 3,131 records),
  `POST /capacity-planning/workorderlines` (native progress; `workorder.progress` = order level,
  WOL progress = line level). production_overview can take ~67 s; occasional timeouts ≠ IP block.

## Open workstreams (separate — do not conflate)
1. **Epoptia writes — WORKING (since 1 Oct 2026).** The documented API commands
   (`products`, `clients`, `workorders`, …) answer 400 "command not found"; writes go through the
   web UI endpoints with a web session. Tool: `epoptia_write.py` (`WebWriter`, CLI; preview by
   default, `--confirm` sends; re-reads each form and compares with `docs/epoptia_form_map.md`;
   name-checked deletes). Whole order from a PDF: `create-order --plan inbox/<x>.plan.json`
   (client → new products + workflow → one WO with all lines). PDFs live in `inbox/` (gitignored).
   Endpoint/field map and conventions: `docs/epoptia_form_map.md`. Next: an app where the
   secretary drops the PDF and approves the table.
   Terminology: SARIDIS "κωδικός προϊόντος" = Epoptia work-order-LINE id (e.g. 2380).
   **Backup:** `epoptia_backup.py` copies Epoptia into `~/epoptia-backup/epoptia.sqlite` once per
   night (user timer `ermis-epoptia-backup.timer`, 23:30 Europe/Athens, never during the day).
   Change history only until 2026-11-01, then ask the user before `--purge-history`.
2. **Dashboard station load.** The current "%" = pending routing steps ÷ the busiest station.
   That is NOT what the user asked for. The user's model (products with size weights, capacity in
   products/day, backward per-station deadlines from delivery dates) is specified in
   `docs/dashboard_load_model.md`. Waiting on the user for the daily capacities and the working days.
   Build and show it offline first; the dashboard restart needs the user's sudo.
3. **Voice Gateway.** Not running (no unit, port 8002 closed); old symptom `upstream_unavailable`.
   The MCP servers it calls are disabled. User wants to discuss its future.
4. ~~Connector schema (ChatGPT saw 14 of 15 tools)~~ — moot: ChatGPT connectors disabled.
5. **Browser/AppArmor installer — ABANDONED, leftovers REMOVED 1 Oct 2026** (by the user with sudo:
   profile `ermis-epoptia-login-chromium` unloaded + deleted, `ermis-epoptia-login.{service,socket}`,
   `/usr/local/libexec/ermis-epoptia-login-*`, `/opt/ermis`; config backup in
   `/root/ermis-login-leftovers-2026-10-01.tgz`). History: `admin_bootstrap/bootstrap.sh` grew into an
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
- Ask the user ONLY before: (1) any write to real Epoptia data, (2) rebooting the server or
  upgrading the OS (apt upgrade / release upgrade), (3) git push.
- Decide yourself, then tell the user in one line afterwards: restarting ERMIS services, deleting
  temporary files, installing Python packages into `./venv`, git commit.
- Before any restart, verify the change won't leave the service down: syntax/import check
  (`venv/bin/python -m py_compile …` / import the module), `scripts/run_offline_tests.sh`, and for
  unit-file changes `systemd-analyze verify`. After the restart confirm `systemctl is-active` and
  the service's health endpoint / port (`ss -ltnp`, curl 127.0.0.1); if it fails, roll back the
  change and restart again, then report.
- Still off-limits regardless (standing prohibitions, not ask-rules): disabling AppArmor, running
  `bootstrap.sh install`/recovery, removing `/root/ermis-login-recovery-err_ra_7`, clearing the
  Epoptia halt file.
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
  password covers only `systemctl restart` of the two (now disabled) MCP and tunnel units — not the
  dashboard; dashboard restarts need the user.
- Tests: run `scripts/run_offline_tests.sh` (offline guard, temp throttle state). Never run tests
  directly with the real `state/` dir — a mocked 403 would halt production.

## First session checklist
1. Read-only audit: host, `git status/log/diff --stat`, service states, `ss -ltnp`, dashboard HTTP
   + logs, `aa-status` / kernel profiles filtered for ermis|chrom|login, files in
   `/etc/apparmor.d` changed since 2026-09-01, grep `admin_bootstrap` for the error string.
2. Create `.claude/settings.json` allowing read-only commands (git status/log/diff, systemctl
   status/is-active, journalctl, ss, curl to 127.0.0.1) and denying `Read(./.env)`.
3. Report findings to the user in Greek, then start on Epoptia writes.
