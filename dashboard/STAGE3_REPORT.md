# Stage 3 preparation report

Status: standalone code and deployment assets prepared; activation prerequisites
remain blocked by this sandbox. No service was installed or started.

## Files changed in this task

- `dashboard/server.py`: replaced the loopback Flask preview launcher with a
  Waitress production entrypoint, all-interface binding, validated `--port`,
  bounded connections/request sizes/idle timeouts and an actionable missing
  dependency error. Existing UI, health and JSON routes/provider remain intact.
- `dashboard/requirements.txt`: pinned additional Waitress dependency.
- `dashboard/run.sh`: foreground launcher using the existing project venv.
- `dashboard/validate.sh`: local synthetic dashboard tests and JavaScript check.
- `dashboard/ermis-dashboard.service.in`: new repo-local, uninstalled template
  running as ermis with filesystem hardening and bounded restart/shutdown policy.
- `dashboard/README.md`: deployment preparation, port selection evidence,
  timeout behavior, trust assumptions and next-step verification instructions.
- `tests/test_dashboard_deployment.py`: four additional offline tests covering
  production configuration/routes, port validation, missing dependency and unit
  entrypoint contract.
- `dashboard/STAGE3_REPORT.md`: this report.

The pre-existing dashboard directory and `tests/test_dashboard.py` were already
untracked. Existing changes to `ermis_gateway_voice.py`,
`tests/test_gateway_voice.py` and `tests/test_gateway_browser.cjs` were preserved;
this task did not edit them or run voice tests. Frontend files were not changed;
no credentials or raw upstream diagnostics were introduced into frontend data.

## Validation

- `sh dashboard/validate.sh`: PASS, all 18 dashboard tests plus JavaScript syntax.
  Tests use synthetic data and mocked transport/server; no live Epoptia reads.
- `sh -n dashboard/run.sh dashboard/validate.sh`: PASS.
- `git diff --check`: PASS. Reviewed git status/stat and scoped diffs; because
  dashboard assets are untracked, also reviewed no-index diffs for the server,
  unit template and new tests. No files were staged or committed.
- Unit contract tests passed; actual host systemd verification was not performed.

## Errors, warnings and remaining prerequisites

- Existing project documentation assigns ports 8000, 8001 and 8002 to other
  components. Retained the dashboard's existing 8010 as a candidate default.
  `ss -ltn` could not open its netlink socket (Operation not permitted); a
  bind-only check was also denied. **8010 availability is unverified.** An
  operator must inspect host listeners before accepting this port.
- The project venv contains Flask and existing MCP dependencies but not Waitress.
  An isolated pip install attempt failed to resolve waitress==3.0.2 in this
  environment; no dependency was installed. Pip also reported an inaccessible
  cache and disabled it. The README's preparation command disables caching.
  Dependency installation and real server/socket smoke tests remain required.
- The async transport test emitted a slow-task diagnostic (~0.59 seconds);
  all tests passed.
- Final logo, live-data latency, LAN reachability and target-TV appearance remain
  activation checks. The dashboard has no login and serves operational data;
  its all-interface listener assumes an approved trusted LAN.

## Deployment/restart requirement

No existing service needs a restart. To activate later, first install the pinned
WSGI dependency in the existing venv, confirm a free port, verify the template on
the host, then install/start the new dashboard service under separate operator
authorization. No system unit, authentication, tunnel, production data or
existing integration was modified. No sudo, deployment, push or destructive
operation was performed.
