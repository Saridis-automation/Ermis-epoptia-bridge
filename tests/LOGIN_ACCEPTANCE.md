# Staged Chromium/AppArmor focused acceptance

Run from the project root:

```sh
python3 tests/run_login_acceptance.py all
```

Each group can also run separately using the names below. The `staging` alias
runs `source`, `permissions`, and `atomic`. Tests use project-local synthetic
files and mocked privileged operations; no browser or installed runtime is used.

Two consecutive full runs on 2026-09-21 passed after the final source digest
refresh: **218 test executions per run, 214 distinct tests, zero failures or
skips**. Four policy tests also run in the profile/manifest groups.

| Group | Run 1 | Run 2 |
| --- | ---: | ---: |
| source | 3 passed | 3 passed |
| permissions | 5 passed | 5 passed |
| atomic | 33 passed | 33 passed |
| launcher | 11 passed | 11 passed |
| profile | 13 passed | 13 passed |
| offline-probe | 53 passed | 53 passed |
| login-confirmation | 82 passed | 82 passed |
| manifest | 18 passed | 18 passed |

Detailed local logs: `tests/login_acceptance_pass1.log` and
`tests/login_acceptance_pass2.log`.

The previous three-group suite passed at baseline. Expanding confirmation
coverage reproduced an optional MCP-package import failure. Its fixture now
substitutes only MCP registration, while testing real system gateway dispatch,
confirmation, cancellation, replay, expiry, session binding and no-network
assertions. No assertions were weakened or skipped.

Pinned source metadata now rejects hardlinks and capabilities and rechecks path
identity after reading. Fixtures cover metadata replacement, unsafe helper and
directory modes, hardlinks, and capability-read errors. Publication tests inject
failures at browser rename and directory sync, profile/receipt replacement and
sync on fresh and existing installs, every managed artifact replacement and
sync during partial-install migration, and enclosing transaction failure. They
assert exact restoration, cleanup, and successful retry. Unloaded coherent
legacy state migrates; mismatched ownership is preserved and refused without
parser calls. Legacy cache attachments are never loaded by the fixture parser.

Files changed in this task (all already untracked at baseline):

- `admin_bootstrap/login_browser_stage.py`: stricter pinned metadata validation.
- `admin_bootstrap/login_manifest.json`: only the staging source digest changed;
  all 18 source digests verified after implementation and fixture edits.
- `tests/test_login_browser_stage.py`: source, permissions and browser rollback fixtures.
- `tests/test_login_apparmor.py`: partial/mismatch migration and policy publish rollback fixtures.
- `tests/test_login_install_sandbox.py`: rollback injection for every managed artifact publish stage.
- `tests/test_browser_login_gateway.py`: hermetic MCP registration fixture.
- `tests/run_login_acceptance.py`: explicit eight-group matrix retaining all previous coverage.
- `tests/LOGIN_ACCEPTANCE.md`: commands, results and scope.

Python syntax, tracked diff whitespace and untracked new-file diff whitespace
checks passed. New-file diffs were reviewed because these files are untracked.
The transient stale-manifest failure before digest regeneration and missing MCP
fixture dependency are resolved. Unrelated changes were preserved.

The exact /opt no-fallback launcher and exact userns-only profile remain unchanged
and passing. No bootstrap, installation, policy loading, service operation,
deployment, browser launch, Epoptia access or sudo was performed. No restart or
deployment is required for this source-only task. These results do not certify
the installed runtime; applying changes there requires a separate authorized task.
