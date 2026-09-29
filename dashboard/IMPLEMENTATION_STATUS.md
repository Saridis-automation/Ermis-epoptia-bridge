# Dashboard implementation status

Core production overview and workstation WIP now use independent, bounded,
single-flight background refreshes with separate 45-second schedules. Each
publishes immediately and retains its own last successful values and timestamp.
Cold start is loading; failed reads are unavailable/cached; both failed reads
are offline; observations older than 120 seconds are stale. A successful sibling
remains available. Optional failures do not affect core freshness or availability.
The API waits at most 100 ms for newly started workers. Display cache persistence
is best-effort and does not hold the API snapshot lock.

Whole-order urgency and overdue counts consume the existing production_overview
`dashboard_orders` projection from the complete paginated native scan. Identities
are parent workorder IDs, never WOL IDs. Native parent progress, unambiguous
customer and parent code (when supplied) are exposed. The first three active,
not-fully-completed orders sort by earliest linked WOL target date, then parent ID;
missing dates sort last. This date is explicitly a line-derived urgency deadline,
not a verified parent deadline. Overdue counts distinct active whole orders before
the three-order limit, using dates before today in UTC. Incomplete scans or
indeterminate overdue membership yield unavailable values, never guessed totals.

Completed-today remains `unsupported_no_completion_history`: the current scan
has no verified whole-order completion timestamps/history. Routing completion,
updated-at timestamps and native progress of 100% cannot establish completion
on a particular day. Verified injected counter readers are supported and tested.
Missing native order codes/customer/progress/deadlines remain unknown.

Workstation load is 100 × station WIP / maximum station WIP (zero when all counts
are zero). Priority is red at >=85%, yellow at >=65%, gray otherwise. These are
relative WIP tiers, not measured capacity or dispatch priorities. Every live
station name is retained; known names keep their display order and additional
names sort deterministically.

Validation: 94 Python tests passed (52 dashboard, 42 existing Epoptia read/native
progress), plus browser rendering/API tests and JavaScript syntax. Git diff and
untracked dashboard-file whitespace checks passed. Tests use synthetic reads only. No live service or production data was accessed. Existing
MCP projection availability in the running process has not been verified; an
upstream response without that projection leaves optional order fields unavailable.
No MCP schemas, Voice Gateway behavior, service configuration or credentials
were changed by this task. Existing unrelated working-tree changes were preserved.

The exact single runtime step afterward is: **restart ermis-dashboard.service**.
It was not performed. No commit, push or deployment was performed.
