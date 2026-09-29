# Factory dashboard UI result

Implemented locally; no commit, push, deployment, service control, Voice Gateway,
protected configuration, or Epoptia production-data changes.

## Files changed

- `dashboard/static/index.html`: semicircular mean-progress gauge, preferred smooth-logo path with fallback, and three equal daily metrics including the production KPI placeholder.
- `dashboard/static/dashboard.css`: factory-first 1920×1080 grid, larger hierarchy, station icons/meters, warm high-load colors, discreet status, urgent-order bars and red deadlines, responsive layouts.
- `dashboard/static/dashboard.js`: eight-station allowlist, icons, ten bottom-up segments, pending percentages/counts, independently aged stars, logo fallback, Athens clock, urgent-order bars/date formatting, three-order limit.
- `dashboard/station_activity.py` (new): one capacity-target mapping, persistent calibration and per-station observed-change history, exact star thresholds, safe corrupt-history recovery.
- `dashboard/stations.py`: pending totals, explicit routing-identity deduplication, future status, stable per-station state fingerprints; archive/terminal filtering retained.
- `dashboard/adapter.py`: safe display mapping for load, pending totals, observation timestamps and star state; excluded stations filtered.
- `dashboard/provider.py`: observe only validated complete station publications; publish counts and star metadata together; restore durable history through the existing cache directory.
- `dashboard/verify_live.py`: local consistency checks now validate the pending/target load formula.
- `dashboard/README.md`: target configuration, persistence, metric semantics, asset fallback and validation instructions.
- `tests/test_dashboard_factory.py` (new): pending counts, alternate-ID deduplication, fingerprints, swaps between named jobs, thresholds, calibration, corruption, partial snapshots, atomic publication and restart coverage.
- `tests/test_dashboard.py`: updated load contract.
- `tests/test_dashboard_connection.py`: preserved direct-reader provenance checks and updated station semantics.
- `tests/test_dashboard_browser.cjs`: real serializer plus DOM rendering assertions for all factory requirements.
- `tests/test_dashboard_data_status_browser.cjs`: current observation timestamps for truthful status checks.
- `tests/test_dashboard_layout.cjs`: 1920×1080 CSS budget, eight-card constraints, and optional rendered checks with three orders and long customer names.

Artifacts: this report, `.factory-suite-results.txt` (final validation log), and
`../ERMISDASH_FACTORY_UI_PASS_121_0.marker`.

## Validation

`sh dashboard/validate.sh` exited 0: **121 passed, 0 failed** (104 Python tests,
13 DOM cases, 3 data-status cases, 1 CSS layout contract). JavaScript syntax also
passed. Scoped `git diff` and `git diff --no-index --check` reviews were clean.
The dashboard and its existing tests were untracked before this task, so the
no-index checks were necessary; nothing was staged.

## Limitations and warnings

- Playwright is unavailable. Rendered viewport checks were skipped; the passing
  1920×1080 check is a DOM/CSS budget contract, not a browser measurement.
- No approved mockup image was available in the supplied assets. Layout follows
  the supplied proportions and requirements; visual matching still needs review.
- `static/saridis-logo-smooth.png` is absent; the existing logo remains the fallback.
- The local API was unavailable and the display cache contained no usable live
  counts. Targets therefore seed from the first complete live census, persist,
  and can be overridden in `STATION_CAPACITY_TARGETS`; no live distribution was
  fabricated. Station change observations are not upstream audit timestamps.
- An initial edit helper attempted `python`, which is unavailable. It was rerun
  successfully with the repository virtualenv. No unresolved test errors remain.

## Activation

The backend changes require a later authorized dashboard service restart (or
normal deployment) to take effect. None was performed. The UI assets are served
without caching and become visible on the next page load.
