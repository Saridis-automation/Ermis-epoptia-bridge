# Dashboard revision 3 — PARTIAL

## Evidenced-endpoint follow-up (2026-09-14)

Result: PARTIAL, exact commercial-flow join still unverified. This is an access
limitation, not evidence that the label is absent upstream. Retained
`ERMISDASH_REV3_PARTIAL_131PASS_0FAIL.marker` as the prior revision marker; no PASS
marker is warranted. Current validation totals are recorded below.

Re-inspected the existing reader, query composition, technical WOL projections,
report discovery, station census, and their tests for workflow/flow/routing/
template/process resources. Evidenced reads relevant to this investigation:

- GET `/api/3.03/workorderlines`: `epoptia_read.fetch_wols` /
  `_workorderlines_page`; raw rows contain the existing `erp_routing` relation.
- POST `/capacity-planning/workorderlines` with `onlyList: true` and `page`:
  `epoptia_read._scan_production_pages`; existing production census and parent
  `workorder.id` relation. No verified flow/template ID-to-name relation.
- GET `/workorders/{id}`: existing parent completion helper. Report discovery
  also evidences GET `/workorders` and `/reports/factory/productiondata`.
  These are parent/report reads, not established flow/template lookup contracts.
  Fixed speculative report probes and synthetic test URLs are not evidence for
  new workflow endpoints. No guessed endpoint was requested.

Attempted connected `list_wols(limit=200)` and
`workstation_wip(dashboard=true)` through the existing authorized connector.
Both returned tool errors before providing any Epoptia payload:
`MCP tool call requires approval, but approval policy is never`.
Thus no response rows were available to search for exact `Παραγγελία εμπορίου`
or a stable join. No upstream HTTP success or complete scan is claimed. Further
calls requiring the same approval were not attempted. Local session helpers
require prohibited credential/environment loading, so they were not invoked.
The connected tools' selected-field projections also cannot establish an absent
raw workflow field. No raw payloads or secrets were saved or disclosed.

Changes in this follow-up:
- `dashboard/static/dashboard.js`: removed the unavailable-reason tooltip;
  preserved the metric title and `—`. Reason remains in API/code diagnostics.
- `tests/test_dashboard_browser.cjs`: added a DOM case proving unsupported
  counts stay hidden and diagnostic text is not displayed, including tooltips.
- `tests/test_dashboard_rev3.py`: added a case rejecting unattested zero/nonzero
  totals even with caller-supplied availability and exclusion flags.
- This report records endpoint evidence, blocked probes, and validation.

Validation: `sh dashboard/validate.sh` passed 112 Python tests, 17 DOM cases,
3 data-status cases, and 1 layout contract case: **133 passed, 0 failed**.
JavaScript syntax passed. One rendered-browser layout case skipped because
Playwright is unavailable. REV3 station budgets, archive filtering, stars,
five urgent orders, clock, dial, and layout contract checks remain passing.
Scoped changes and `git diff --check` reviewed. No service restart or deployment
is required for this static-only runtime change; browser refresh loads it when
served from this working tree. No service operations or Voice Gateway changes.

## Earlier commercial-flow follow-up (historical)

Raw live verification remains blocked under this task's explicit restrictions:
`epoptia_read.fetch_wols` preserves raw `workorderLines` rows, but the existing
local `epoptia_queries.application_settings()` initialization loads `.env` and
reads environment variables containing credentials. It was not invoked. The
available connected Epoptia tools expose projected results, which cannot verify
the raw field contract. No raw live payload was fetched or saved, and no exact
workflow path was established. This is an access limitation, not evidence that
the label or field is absent from the upstream payload.

The metric remains unavailable and runtime code is unchanged. Added
`ERMIS_COMMERCIAL_FLOW_FIELD_NOT_FOUND.marker` with that distinction; retained
the prior PARTIAL marker and did not issue a PASS marker. An eventual verified
metric must count current production/standby WOL/product rows, not quantity
units, exclude archive/terminal rows, and exclude only the verified workflow
path's exact `Παραγγελία εμπορίου` value.

Validation rerun: `sh dashboard/validate.sh` passed 111 Python tests, 16 DOM
cases, 3 data-status cases, and 1 layout contract case (131 passed, 0 failed).
JavaScript syntax passed. One rendered-browser case skipped because Playwright
is unavailable. Separate REV3 rerun passed all 3 tests, including rejection of
guessed commercial-flow fields and unattested counts (already in the 131 total).
No service restart or deployment is required for this documentation-only update.

Changed files:
- dashboard/static/index.html, dashboard.css, dashboard.js: larger semicircular red/amber/green dial with a native-progress needle; HH:MM clock; smaller logo/icons; warning/calendar icons; five urgent rows; narrower ID/wider customer column; production-WOL title and unavailable value; retained 1920×1080 auto-fit canvas.
- dashboard/orders.py and dashboard/adapter.py: five urgent orders through census/API; explicit unavailable production-WOL contract.
- dashboard/station_activity.py: single provisional pending-step target map, replacing count / 0.7 calibration. LASER 100, scissors 60, bending 80, assembly 1/2 50 each, glass 40, refrigeration 30. These are tunable queue budgets, not measured utilization or empirically validated capacities. Existing v2 star history remains intact; old calibrated capacities no longer override configuration.
- dashboard/stations.py: explicit coverage flag identifying unverified commercial-flow exclusion. Existing active lifecycle, archive filtering, routing-step deduplication and star fingerprints preserved.
- tests/test_dashboard.py, test_dashboard_connection.py, test_dashboard_factory.py, test_dashboard_orders.py, test_dashboard_browser.cjs, test_dashboard_layout.cjs, test_dashboard_rev3.py: updated expected loads, urgency limit, clock/dial/icons, station config, unavailable total and layout regressions.
- dashboard/REV3_REPORT.md and root revision marker.

Workflow limitation: inspected the existing epoptia_read.fetch_wols REST scan, epoptia_queries.read_wol_snapshot composition, summary projection and dashboard collectors. No verified exact WOL workflow-name path (or workflow-ID/name mapping) is established for /api/3.03/workorderlines. The exact exclusion value is `Παραγγελία εμπορίου`. No customer, description or guessed flow key is used as a substitute. The production-WOL total remains null/— even if an unattested count is supplied. Station counts retain known active pending work, but commercial exclusion is explicitly unverified in coverage metadata. Completing this requires a verified workflow field/reader contract; no live credentials or production reads were used to invent one.

Validation: sh dashboard/validate.sh: 111 Python tests, 16 JS DOM cases, 3 JS data-status cases, 1 canvas/layout contract case passed (131 total), 0 failures. JS syntax check passed. After final CSS spacing adjustment, layout contract rerun passed. One real-browser layout case skipped because Playwright is unavailable. Real rendered overflow and reference-image comparison remain unverified; no reference image was attached in this task. Initial three failures were obsolete expectations for the replaced 70% calibration; updated expectations pass.

Reviewed scoped git diff/check and before/after diffs for pre-existing untracked dashboard files. Existing unrelated work was preserved. No services restarted, deployed, started or stopped. Python changes need a separately authorized dashboard process reload/restart to reach the running instance; static assets need browser refresh. No systemd/tunnel or Voice Gateway changes.
