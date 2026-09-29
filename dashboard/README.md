# Existing production dashboard

This directory retains the existing page, CSS and brand assets. The correctness
implementation and validation note (`CORRECTNESS_VALIDATION.md`) supersedes metric
claims in the earlier stage/live/deployment reports. No services were restarted.

`/api/dashboard` copies cached data only. A background scheduler starts independent
source workers; each source has a single-flight guard and bounded async read timeout.
The API returns source attempt counts, refresh sequence, successful generation,
latency, last success/failure, safe reason, consecutive failures and stale state.
Failed/partial sources preserve their last successful generation and observation.
The versioned display cache rejects older projections with unproven metrics.

The default provider calls import-safe `epoptia_queries` directly. Both existing
MCP wrappers and dashboard workers use its `production_overview` and
`workstation_census`, which reuse the existing `epoptia_read` paginated readers,
`OrderCensus` and `collect_stations`. Configuration uses the application's existing
dotenv/environment provisioning, loaded only during a read. There is no MCP
transport or fallback. Public tool contracts and authentication are unchanged.

Each source has a separate worker and a 90-second publication deadline. Cancellation
stops before the next data page; an in-flight request retains its existing HTTP
bound. A timed-out worker prevents overlapping retries until it exits. API GETs
only copy the latest snapshot. `read_attempts`, `http_requests`, `read_id` and
`successful_read_id` attest calls at the shared data-page HTTP boundary, separately
from scheduler attempts and snapshot generations. Login failure does not count as
a data read. The verifier requires three new successful direct reads after baseline;
repeated snapshots or changed generation numbers alone cannot pass.

The census contains one row per nested `workorder.id`. Display numbers use
`workorder.code`. Only consistent native `workorder.progress` supplies progress;
missing/conflicting values are null. Terminal parents and exclusively terminal
children are excluded from unfinished counts, while active children in mixed
lifecycles remain represented. Conflicting parent lifecycles are unknown.

Order deadlines use the existing WOL scan's normalized `target_day` values
linked by `workorder.id`, including archived/completed WOLs. Distinct valid dates
are sorted; each refresh selects the earliest date on or after today in
Europe/Athens, or the latest date if all are past (overdue). The canonical deadline
has `derived_from_rollforward_wol_dates` provenance. Multiple dates are valid;
missing/invalid dates produce a diagnostic only when no valid date exists.
ProductionData buckets and the
separate legacy deadline reader do not supply deadlines. Unavailable WOL date
evidence cannot reject a valid native production refresh. The earliest three
dated unfinished orders and overdue counts use these canonical deadlines.
The optional verified parent-field override remains available.

`stations.collect_stations` accepts a complete raw WOL scan, removes terminal WOLs,
deduplicates WOL identities, then counts routing steps using existing
`erp_routing` status semantics. Running (`started`, `in_progress`), paused,
waiting (`not_started`, `waiting`) and unknown are separate. Future routing is not
a verified executable queue. Distinct WOL counts and step counts have explicit
units. Legacy MCP station aggregates lack terminal filtering evidence and are
unavailable. The injectable read contract accepts `ok`, `complete`, `wol_rows`;
this is a dashboard adapter contract, not a claimed new upstream endpoint.

Capacity percentages remain null: required inputs are standard time per step,
remaining quantities, available station time and a defined capacity horizon.
The native order mean has its own truthful name. Updated JavaScript safely renders
the old API contract while hiding legacy false metrics, preserving layout/CSS.

Completion history is unverified, not absent upstream. `completion.completion_count`
accepts only a verified, complete whole-order status-transition history covering the
observation time. Its metric is distinct orders whose latest transition is completed
on the Athens date and which have not subsequently reopened. Duplicate events are
deduplicated; conflicting events, ambiguous simultaneous transitions, invalid or
missing timestamps and insufficient coverage make the count unknown. Recompletion
can count once. Archive/update/target timestamps cannot substitute for completion.
The `events` adapter contract is synthetic/injected until a real reader is verified.

Local validation: `sh dashboard/validate.sh`. Browser checks use the installed Node
VM/minimal DOM harness, not a real graphical browser. Tests perform no live upstream
reads or service mutations. Production adoption requires a separately approved
dashboard restart and live verification; this job performs neither.
`finalize_connection.py` retains interactive approval and `--verify-only` observation.
Only `ermis-dashboard.service` is required to activate the direct dashboard path;
MCP and tunnel services are not dependencies. The existing launcher fingerprints
the reviewed implementation and runs local connection tests before any activation.

### Factory display UI

The factory screen uses eight approved stations and counts pending routing steps
(running + paused + waiting/not_started/future), deduplicating WOL identities and
explicit step identities. Terminal/archive WOLs and completed routing steps do
not contribute. Unknown work states make the station's pending total unknown.
Missing source data remains `—`; an absent station in a complete census is zero.

`dashboard/station_activity.py:STATION_CAPACITY_TARGETS` is the single capacity
configuration mapping. A positive integer is the pending-step target at 100%.
`None` calibrates once from the first complete census to about 70% (minimum target
10), then retains the target across refreshes and restarts. These are display
work-count targets, not measured machine utilization. The local API and display
cache did not provide usable live counts during this change, so targets were
not fabricated from a claimed live distribution. Operators can override each
entry after reviewing the first calibrated census.

The existing server launch supplies `dashboard/.cache` as its cache directory.
`station-activity-v1.json` there stores only station names, fingerprints, observed
last-change timestamps, and calibrated targets using the existing atomic cache
writer. A baseline alone never awards gold. Only changes between successful,
complete station snapshots advance the observed change time; failures and
partial scans preserve it. This is an observation time, not an Epoptia audit
record. Stars are gold below 1 hour, gray from 1 through 2 hours (inclusive), and
red above 2 hours; missing history is gray. The browser ages stars every second,
including while disconnected. Cache write failure retains memory state and uses
the existing sanitized cache warning; durable history requires writable storage.

The preferred logo is `dashboard/static/saridis-logo-smooth.png`. Until that
asset is supplied, the existing PNG is used, with a text fallback if both fail.
The daily production KPI intentionally remains `—`.

Run `sh dashboard/validate.sh` for the synthetic Python suite, DOM checks, and
1920×1080 CSS layout budget. The optional Playwright branch checks real rendered
bounds when Playwright is installed; the DOM budget is not a browser rendering.
