# Dashboard correctness implementation / validation — 2026-09-09

Local implementation completed; live verification blocked. No restart, reload,
service mutation, deployment, push, staging, commit or process replacement occurred.
The running port-8010 backend was not changed. CSS, logo and layout were preserved;
only metric labels/bindings changed. Existing unrelated working-tree edits remain.

## Changed relative files

- `dashboard/orders.py`: canonical parent-ID census, native-only progress,
  lifecycle classification, strict verified parent deadlines/provenance, Athens
  dates, earliest-three and overdue/undated counts from the same census. Shared
  `epoptia_read._scan_production_pages` is imported, not modified.
- `dashboard/stations.py` and `dashboard/completion.py`: terminal-filtered,
  deduplicated routing state census and verified transition-history reducer.
- `dashboard/provider.py`, `dashboard/adapter.py`, `dashboard/server.py`: independent
  bounded source refreshes, single-flight publication, per-source diagnostics,
  atomic order generations, last-good preservation, versioned cache, GET without
  upstream reads. Source latency excludes display-cache persistence.
- `dashboard/static/dashboard.js`, `dashboard/static/index.html`: truthful native
  mean label, unavailable capacity, factual step counts, legacy-contract guards.
- `tests/test_dashboard.py`, `tests/test_dashboard_orders.py`,
  `tests/test_dashboard_refresh.py`, `tests/test_dashboard_correctness.py`,
  `tests/test_dashboard_browser.cjs`, `tests/test_dashboard_deployment.py`: updated
  dashboard-only regression tests; deployment entrypoint remains mocked.
- `dashboard/README.md`, this note, `dashboard/protected_hashes_before.json`,
  `dashboard/protected_hashes_after.json`, `dashboard/validation_live_attempt.json`:
  contract, validation and bounded non-sensitive evidence.

## Local simulated evidence

Final exact validation command: `sh dashboard/validate.sh` (exit 0). It executes:

- `./venv/bin/python -m unittest discover -s tests -p 'test_dashboard*.py'`:
  **38 tests passed**, final run 0.681 seconds, zero failures/errors.
- `node --check dashboard/static/dashboard.js`: passed.
- `node tests/test_dashboard_browser.cjs`: render, API/last-good and legacy-backend
  guards passed. This is the installed Node VM/minimal-DOM harness, not a graphical
  browser or live backend test.

Coverage includes duplicate orders/WOLs, inconsistent/missing native progress,
parent-versus-child deadline separation, invalid/conflicting/undated deadlines,
Athens midnight, terminal and mixed lifecycles, shared read-only POST pagination,
archived station steps, running/paused/waiting/unknown states, explicit units,
false capacity removal, completion evidence/timestamps, duplicates and
reopen/recomplete, incomplete scans, stale last-good data, cancellation/timeouts,
independent repeated refreshes and no overlapping same-source refresh. Five cached
Flask test-client reads each passed the 100 ms bound; these are simulated local
reads, not port-8010 latency measurements. GET does not schedule upstream work.

An initial run against the previous test assertions produced failures and was
interrupted without a final total. Those assertions included child-date deadlines,
relative station percentages and GET-triggered refresh behavior, all intentionally
removed by this task. Dashboard tests were updated to the new contract; transport,
read-only HTTP and deployment checks remain covered. One asyncio slow-task notice
occurred during mocked MCP import (~0.52 seconds); it was not a test failure.

`git diff --check` passed for existing tracked work. Because dashboard files were
already untracked, scoped additions were also reviewed using
`git diff --no-index /dev/null dashboard/server.py` and the analogous provider
command, plus `git diff --no-index --check /dev/null <changed-file>` checks for all
15 changed source/test/README files. A trailing blank line was fixed before final
checks. No staging or commit was used to obtain these diffs.

## Live evidence and precise limits

Actual endpoints located in `dashboard/server.py`: `/`, `/health`,
`/api/dashboard`. An in-process Python socket probe to 127.0.0.1:8010 was blocked
before connection: **PermissionError: [Errno 1] Operation not permitted**.
No workaround was attempted. Therefore the service's current health and actual
cached-API latency could not be independently measured here.

A separate `./venv/bin/python -` invocation constructed the CHANGED
`LocalEpoptiaProvider`, awaited `provider.snapshot()` once, and stored only source
metadata/coverage in `validation_live_attempt.json`. Both existing MCP read attempts
failed safely (`read_unavailable`); successful live cycles: **0**, not three.
The JSON records attempt timestamps, measured durations, sequences, null last
successes, generations and reasons. No raw upstream payload is stored in this note.
No live canonical counts, deadline coverage, or sample native matches were obtained.
The caller's 21-order/3100-WOL baseline was not independently reproduced and is not
presented as this implementation's measured result.

Discovery inspected the existing reader's capacity-planning parent projection,
WOL API pagination, routing semantics, existing MCP overview integration, and
reader tests/documentation for history/completion paths. Authenticated live payload
schema and relevant events could not be inspected because sockets were blocked and
credential access was prohibited. This is NOT evidence that upstream lacks parent
deadlines or completion history. No optional endpoint or field is claimed verified.

Default parent deadline mapping remains unverified/null. The default existing MCP
station summary cannot prove terminal filtering or separate routing states; its
counts are rejected. The raw-WOL station input and verified completion-event input
are injected adapter contracts exercised with fixtures, not newly discovered API
endpoints. `collect_orders` can use a supplied authorized session and the existing
scanner; this job did not obtain or load such a session. The unchanged running MCP
may still return its old canonical projection, which the changed provider rejects.
Thus full live data integration remains unverified/unavailable under these access
constraints. A dashboard restart alone does not supply missing verified readers.

Completion is defined as distinct whole orders whose latest verified transition is
completed on the Europe/Athens date and not subsequently reopened. History must be
complete through observation time; archive/update/target dates are not substitutes.
Station utilization remains null without standard times, remaining quantities,
available station time and a defined horizon. Undated/uncertain orders are exposed
and excluded from ranking/overdue counts; unknown is not a valid zero.

## Protected files and deployment

Before/after SHA-256 manifests match **9/9**: Voice Gateway Python/JS/CSS,
Voice Gateway tests, service-control code/tests, MCP code, shared Epoptia reader,
and dashboard service template. Manifests contain paths/hashes only. No protected
file was changed by this task; previously modified files remain modified as found.

Deployment status: **not deployed; not restarted**. Backend adoption requires an
explicitly approved restart outside this job plus verified reader access for missing
sources. No production services, configurations, credentials or data were mutated.
