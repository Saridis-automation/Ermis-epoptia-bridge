Bootstrap/AppArmor offline verification, 2026-09-21

Run from the checkout as its non-root owner:

```sh
python3 -I -B tests/run_bootstrap_verification.py
```

Two consecutive complete runs exited zero, each with 333 tests, no failures,
errors, skips, or expected failures. Counts are test methods/Node tests;
parameterized subcases are additional coverage, not additional counted tests.

| Suite | Tests per run |
| --- | ---: |
| admin_bootstrap (21 admin + 18 sandbox) | 39 |
| test_login_*.py, including public shell entrypoint | 141 |
| browser login gateway | 6 |
| browser launch sandbox | 3 |
| Node AppArmor | 8 |
| Node login diagnostics | 66 |
| Node sandbox probe | 48 |
| Node socket protocol | 5 |
| Node login | 8 |
| Node browser runtime | 4 |
| Node Chromium smoke (mock browser) | 2 |
| Node browser access check (mock network/browser) | 3 |

All 22 production source manifest entries verified. The manifest and production
sources were not edited. Initial fingerprints covered 41 relevant files before
testing; only the six intentional existing harness/test edits below differ.
The final runner fingerprints 58 source/test/harness/marker files before testing
and verifies exact equality after each complete run. Combined fingerprint:
`e8ec3a1bffb7a75742f77f3829c85e9f05b6bc16d360857463fd86335faad88e`.

Files changed in this pass:

- `admin_bootstrap/test_bootstrap_sandbox.py`: isolate the source-preflight seam
  from fake installed-file metadata; test the legacy admin observer directly.
  Public CLI coverage remains in the dedicated entrypoint suite.
- `admin_bootstrap/verify.sh`: print the test result instead of deleting and
  rewriting the project readiness marker.
- `tests/test_login_acceptance_repair.py`: mock the current kernel inventory
  boundary, preserving exact tuple/duplicate/unknown-observation assertions.
- `tests/test_login_diagnose_entrypoint.py`: supply synthetic FD capability
  evidence, use current canonical receipt bindings, test foreign policy identity
  and missing bindings, and expand forbidden mutation/network calls.
- `tests/test_login_first_install.py`: add legacy-cache kernel conflicts at
  both post-load and final verification boundaries.
- `tests/test_login_socket.cjs`: use the test-only admission mock for protocol
  tests, matching the existing diagnostic harness.
- `tests/run_bootstrap_verification.py`: new non-root runner with synthetic child
  environments, workspace temporary fixtures, skip rejection, stable counts,
  manifest verification, and two-run fingerprint checks.
- `tests/BOOTSTRAP_VERIFICATION.md`: this report.

Coverage evidence:

| Required invariant | Explicit fixture coverage |
| --- | --- |
| Capability absence/presence/query failures | test_login_acceptance_repair.py; test_login_browser_stage.py |
| Source TOCTOU, inode swaps, unsafe ancestors | test_login_source_preflight.py; test_login_browser_stage.py |
| First-install-only gates before mutation | test_login_source_preflight.py; test_login_install_sandbox.py |
| NOREPLACE tree/profile/receipt/archive | test_login_browser_stage.py; test_login_first_install.py |
| Stale receipt binding and archival races | test_login_first_install.py; test_login_diagnose_entrypoint.py |
| Pre/post commit races and foreign artifact preservation | test_login_first_install.py; test_login_install_sandbox.py |
| Add-only, content-bound kernel proof; duplicate/legacy/same-attachment conflicts | test_login_acceptance_repair.py; test_login_first_install.py |
| Irreversible boundary and no automatic kernel removal | test_login_first_install.py; test_login_install_sandbox.py |
| Receipt last, no success on unknown/nonzero add or late failure | test_login_first_install.py; test_login_install_sandbox.py |
| Public diagnose zero side effects | test_login_diagnose_entrypoint.py; test_login_diagnose_readonly.py |

Initial failures were outdated harness boundaries/expectations, not demonstrated
production defects: synthetic metadata reached the newly added source preflight;
legacy diagnostic tests targeted the changed public route; a kernel mock targeted
the old reader (the attempted live inventory read was denied); the public fixture
lacked capability evidence and canonical receipt fields; and socket protocol tests
lacked an admission mock. Each was repaired at the test boundary. Production
security checks were not weakened. Direct Node file execution also fixes aggregate
reporting that counted files instead of individual tests.

Installer writes and race injections use project-local temporary trees and mocked
ownership, parser, kernel, and service boundaries. Public diagnose blocks filesystem
opens/mutations, network operations, and real subprocess creation after loading
approved source bytes, then asserts its in-memory fixtures are unchanged. Parser
confinement tests ran successfully against disposable workspace files without skips.
No live installation, rollback, AppArmor load/remove/reload, browser, service action,
network request, deployment, push, or commit was performed.

`git diff --check` passed; changed files were reviewed. Because the task's existing
bootstrap/test files are untracked, they also received explicit `git diff
--no-index --check` validation. Unrelated dirty changes were preserved.
No outstanding test errors. No service restart or deployment is required.
