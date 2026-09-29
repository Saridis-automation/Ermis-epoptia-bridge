# Calendar target dates

Verified authenticated UI contract: same-origin **GET `/planning/calendar`**.
Cards use `.wolCardComponent[data-id][data-workorder]`. The nearest ancestor
`.workorderWol[data-date]` supplies the actual `YYYY-MM-DD` target date.
`.mainWolItem[data-id][data-workorder][data-date]` is a fallback only without the
inner date and with a valid ISO date. Its `past` value is an overdue grouping,
not a date. Visible text is never used to infer dates.

`calendar_target_dates.py` parses HTML without I/O. Positive 64-bit IDs and real
ISO calendar dates are required. Identical duplicates collapse; conflicting or
invalid cards reject the snapshot. Empty markup reports `render_required`;
login pages/redirects or absent authenticated sessions report
`calendar_auth_missing`. Failures contain fixed reason labels only.

The provider uses an existing protected session, never logs in, and requests
only the fixed route with GET, no body and zero redirects. Responses must be
200 HTML at the exact requested URL. The decoded response limit is 2 MiB;
records are capped at 10,000. Requests have a five-second transport timeout and
a five-second elapsed read budget checked per byte (an in-flight read can take
up to the transport timeout to return). Existing authenticated progress readers
publish a date-only in-memory snapshot with a 60-second TTL. No session or
credentials are retained by this module. The optional `rendered_calendar`
adapter reads only an existing authenticated Playwright page's DOM at the exact
route; it performs no navigation, login, submission, or runtime creation.

The Epoptia MCP tool and gateway action `calendar_target_dates` read that
snapshot, with optional `wol_ids` / `workorder_ids` integer lists (up to 200 each)
and `limit` (1–200). Empty lists match nothing; combined filters use AND.
Results report total matches and truncation. The action routes only to
`Epoptia_MES`, requires no confirmation, and cannot select a URL or service.
Until a protected progress read populates the process-local snapshot, the
standalone action and WOL details report `calendar_auth_missing`.

WOL details add `target_date`, `target_date_status`, and `target_date_reason`,
retaining all existing fields. Workorder progress and dashboard order records
add min/max dates; a single `target_date` is present as a date only when all
calendar-dated lines agree. Differing dates leave it null with
`calendar_dates_differ`. Missing dates remain null. These describe calendar
coverage, not proof that every order line is dated. Existing deadline/urgency
fields retain their separate semantics. Calendar status is independently exposed
in the dashboard and never determines whether production sources are offline.

WOL details also expose `effective_target_date`: the validated date component of
API `target_day`, with `effective_target_date_status` (`ok` or
`missing_or_invalid_target_day`) and `effective_target_date_provenance`
(`/api/3.03/workorderlines`, `target_day`, `date_component`). Raw valid `target_day`
is preserved, including its timestamp when present; no timezone conversion is
performed for this WOL field. Calendar dates never replace missing or conflicting
API values. Existing `target_date*` fields remain optional calendar diagnostics.
Dashboard order `deadline` retains its separate rollforward/Athens semantics and
has independent `field_status.deadline` coverage. No browser cookies are needed
to read API WOL dates; optional calendar failures do not determine global status.

Validation uses synthetic fixtures and blocked network access. No live Epoptia
verification or service restart is part of implementation. Loading the new MCP
tool requires a later authorized Epoptia MCP restart; gateway and dashboard
processes also need their changed Python code reloaded for their respective
integrations. The tunnel needs no change.
