# Factory display redesign — local implementation

Changed files in this task:

- `dashboard/static/index.html`: fixed viewport wrapper/canvas, metric title above semicircular gauge, native logo aspect ratio, requested orders title and a conditional notice for rows without verified urgency.
- `dashboard/static/dashboard.css`: fixed 1920×1080 industrial layout, seven cards, larger clock/logo/icons/numbers, integrated pending counts, SVG star colors, bottom-up segmented meters and three equal daily KPI columns.
- `dashboard/static/dashboard.js`: uniform centered viewport/fullscreen scaling, exact station order, legacy bending-station spelling alias, distinct machine/welding SVGs, always-rendered SVG stars, integrated pending count and explicit daily KPI dash. Small fallback logo remains capped at its native width.
- `dashboard/stations.py`: fingerprint complete normalized current routing-step content, including `qty_done` and nested progress; retain existing incomplete-work and archive/terminal filtering.
- `dashboard/station_activity.py`: seven-station target configuration and version-2 persistent observations. Migration preserves valid capacity targets and initializes neutral history rather than treating the signature change as an Epoptia update.
- `tests/test_dashboard_factory.py`: exact station config/exclusions, full-content changes, persistence and migration regressions.
- `tests/test_dashboard_correctness.py`: PUNCHING excluded from dashboard API expectations.
- `tests/test_dashboard_browser.cjs`: seven SVG icons/stars, distinct welding paths, aliases, pending counts, load cap, thresholds and reserved daily KPI.
- `tests/test_dashboard_layout.cjs`: fixed-canvas structure, resize/fullscreen scaling at six aspect ratios, and optional rendered-browser overflow checks.
- This report and `ERMISDASH_FACTORY2_PARTIAL_202_PASS_0_FAIL_1_SKIP.marker`.

Metric semantics remain the native mean progress of active orders in the top gauge. Station load remains incomplete current routing steps divided by the centralized target, capped at 100%; it is not measured machine utilization. An unset target retains the existing once-only calibration. Pending counts retain unknown values when the census is incomplete or statuses cannot be verified.

The tracker uses the existing dashboard cache directory and atomic cache writes. A new or migrated station stays neutral until a real subsequent change is observed. Unchanged polling does not advance `last_change_at`. Following an observed change: age <60 minutes is gold, 60–120 inclusive is neutral, and >120 is red. Incomplete/failed scans do not establish a new observation. API timestamps remain available; factory cards omit diagnostics.

Validation, synthetic/local only:

- `sh dashboard/validate.sh`: 108 Python tests, 15 DOM harness cases, 3 data-status cases and 1 canvas/layout contract passed; JavaScript syntax check passed.
- `test_completed_jobs_tracker.py`: 43 passed.
- `test_completed_today_persistence.py`: 7 passed.
- `test_active_production_progress.py`: 6 passed.
- `test_native_workorder_progress.py`: 19 passed.
- Total: 202 test cases passed, 0 failed, 1 rendered-browser case skipped. Syntax/whitespace checks are additional and not included in the count.

Limitation: Playwright and browser executables are unavailable. Actual rendered overflow, visual comparison and fullscreen presentation therefore remain unverified; this is partial validation, not full visual acceptance. The implementation follows the written mockup description; no mockup image was supplied. The existing larger local logo is 1047×236 and displayed below its native size on the design canvas; no image asset was modified.

Reviewed scoped git diffs and whitespace. These dashboard files already existed as untracked files, so git shows them as additions rather than a diff against their starting contents. Existing unrelated changes were preserved.

No services started, stopped or restarted; no deployment or push. The running dashboard will require a separately authorized restart to load the Python changes, then a browser reload for the new UI. No production data was changed.
