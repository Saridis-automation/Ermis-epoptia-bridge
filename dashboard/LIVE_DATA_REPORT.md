# Dashboard live-data inspection

The existing provider already uses the working gateway's `Epoptia_MES` read
path, `http://127.0.0.1:8000/mcp`, with the same MCP/httpx2 transport. The MCP
server owns API authentication and the authenticated native-progress scan.
No credential files or environment values were read, and no new dashboard
environment variables or configuration changes are required by this patch.

Changed `dashboard/provider.py` to sanitize transport failures and preserve
successful workstation WIP when native-progress source metadata is malformed.
Cancellation and the independent 25-second read deadlines remain intact.
Added regression tests in `tests/test_dashboard.py`.

Available metrics remain native active-production progress and coverage,
distinct active whole-order count, and workstation routing WIP counts from the
existing read tools. Urgent whole orders, overdue whole orders, completed-today,
station capacity and priority remain explicitly unavailable: the exposed reads
do not verify these metrics. WOL deadlines/counts are not whole-order metrics.

Validation: all 24 dashboard/deployment tests passed; JavaScript syntax and
`git diff --check` passed. Tests use synthetic data, including real MCP result
objects and the existing WIP aggregator. Both attempted live tool reads failed
with connection errors from this workspace. A bounded loopback TCP check
confirmed `PermissionError` (errno 1), so this workspace cannot test the endpoint.
Live Epoptia output is therefore
not verified, and this patch does not establish that the reported offline
deployment is fixed. An operator must verify reachability of the existing MCP
endpoint from the dashboard process and native-scan completeness on the host;
no evidence here justifies changing service credentials or configuration.

No services, systemd files, tunnels, voice gateway files, production data or
credentials were changed. No commit, push, deployment or restart was performed.
A running dashboard process would need a later authorized reload/restart to
pick up these Python changes; no MCP or gateway restart is required by them.
