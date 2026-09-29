# Granular audit 7d0736a72771445d90e3ebf9fb6bc77d

Source fix completed and validated locally on 2026-09-21.

All browser launch paths now use a central validator requiring explicit
`chromiumSandbox: true`. It validates the exact copied options passed to
Playwright, allows only reviewed arguments and environment keys, and rejects
sandbox-disabling switches, unknown config/extra arguments, default-argument
replacement, preload overrides, and inherited sandbox options. Ambient browser
flag variables are not inherited. The actual production runtime regression
captures Playwright-generated argv at a mocked process-spawn boundary; it proves
absence of `--no-sandbox` without executing a browser.

The outer bootstrap checks the source manifest before admin mutation. Install
stages and validates browser contents, profile, both receipts, artifact backups,
and the final ready marker before invalidating old readiness or publishing the
tree. Checks include dry-parse, exact bytes/hash bindings, ownership, modes and
capability rejection. Candidates are rechecked after dry-parse. The ready marker
is published last. Failure recovery restores exact prior receipt/ready bytes only
when prior installed artifact, tree, profile and kernel policy bindings are
restored. A failed source upgrade preserves the prior commit bytes but cannot
admit the new source revision. Drift leaves the installation not-ready.

Runtime Python readiness and daemon IPC admission recompute installed bindings;
status/start/probe reject drift before dispatch. Runtime tree verification avoids
privileged O_NOATIME opens while retaining the same link, owner, mode, capability,
ELF and hash checks. Privileged commit/recovery also checks loaded kernel policy.
The exact /opt path, userns-only profile, passwd-home selection, no fallback,
NNP/socket/systemd hardening, offline probe and confirmation logic are preserved.

Files changed for this task:

- `epoptia_browser_launch.cjs`: new central options/argv validator and launcher.
- `epoptia_browser_runtime.cjs`, `epoptia_browser_access_check.cjs`,
  `chromium_runtime_smoke.cjs`, `epoptia_login_backend.cjs`: validated sandbox launch.
- `epoptia_login_daemon.cjs`: runtime binding admission at the IPC boundary.
- `admin_bootstrap/bootstrap.py`: source gate before privileged bootstrap work.
- `admin_bootstrap/login_bootstrap.py`: staged candidate validation, exact ready
  recovery, installed-binding verification and final staged-marker commit.
- `admin_bootstrap/login_apparmor.py`: complete policy/receipt/ready staging and
  prepublication validation coordinated with the artifact transaction.
- `admin_bootstrap/login_browser_stage.py`: final staged-tree rehash and
  unprivileged installed-tree verification.
- `admin_bootstrap/login_manifest.json`: 21 source digests refreshed after source
  and test edits. Generated service/wrapper assets did not change.
- `tests/test_browser_launch_sandbox.cjs`: production argv, override injection,
  and daemon drift regressions.
- `tests/test_login_install_sandbox.py`: actual-production transaction fault
  injection, source gate, exact recovery, staged corruption, runtime drift,
  unprivileged readiness and prepublication success-marker coverage.
- `tests/test_login_apparmor.py`, `tests/test_login_readiness.py`,
  `tests/test_login_browser_stage.py`, `tests/test_login_diagnostics.cjs`,
  `tests/test_login_sandbox_probe.cjs`: local fixture seams updated for the new
  validation calls. Existing assertions retained except the old not-ready-after-
  exact-rollback expectation, replaced by exact ready-byte restoration plus
  readiness validation as explicitly requested by this audit.
- `tests/run_login_acceptance.py`: includes the new production launcher tests
  and existing smoke/access-check regressions.
- `tests/LOGIN_AUDIT_FIX.md`: this report.

Validation after final source/test edits and digest regeneration:

| Check | Result |
| --- | --- |
| Targeted atomic group | 47 passed |
| Targeted launcher group | 20 passed |
| Complete acceptance run 1 | 241 passed, 0 failure/error/skip |
| Complete acceptance run 2 | 241 passed, 0 failure/error/skip |
| Source manifest | All 21 digests match |
| Python AST / Node syntax | Passed |
| Tracked and changed-untracked diff whitespace | Passed |

Commands: `python3 tests/run_login_acceptance.py atomic`, then `launcher`, then
`all` twice. Each invocation allocates a writable temporary root under `tests/`
and supplies a minimal synthetic environment. Logs: `login_fix_atomic.log`,
`login_fix_launcher.log`, `login_fix_pass1.log`, `login_fix_pass2.log`, all under
`tests/`. Source/test files were already untracked at task entry except the new
validator/test/report; untracked diffs and relevant source were reviewed alongside
`git diff --check`. Unrelated tracked and untracked changes were preserved.

Initial validation exposed stale fixture seams and manifests, subsequently fixed.
One early standalone artifact fixture attempted to unlink the host ready marker;
the read-only filesystem rejected it. No host marker was modified. Readiness
invalidation is now explicitly supplied only by the enclosing installer, and the
final fixtures operate on local paths. Final runs have no outstanding errors.

No bootstrap execution, installation, AppArmor load, browser execution, Epoptia
access, service operation, deployment, push or sudo was performed. No systemd,
authentication, credentials, tunnel or environment files were changed. These are
source-only results, not certification of the installed production runtime.
Production activation requires a separately authorized bootstrap/deployment and
login supervisor refresh; none was performed here.
