# Chromium dependency action

The project wrapper accepts exactly `browser install-chromium-dependencies`.
It accepts no package names, paths, flags, or shell strings. The existing generic
package allowlist remains empty. Root and Ubuntu 24.04 x86_64 are required.

The fixed tuple in `ermis-admin` matches the installed Playwright 1.63.0
`node_modules/playwright-core/lib/coreBundle.js` nativeDeps entry for
`ubuntu24.04-x64`: 21 Chromium libraries and 12 shared tools/font packages.
The shared set includes Xvfb for parity with Playwright's dependency installer;
Xvfb is not needed by the headless inspector itself. Apt may resolve additional
transitive dependencies. No Firefox or WebKit packages are requested.

The action uses fixed apt arguments with no removals, upgrades of installed
packages, or recommended packages. It does not refresh package indexes.
Existing packages too old for dependency resolution or stale indexes may cause
installation to fail. Package output and errors are discarded by the existing
runner; only its fixed failure message is returned. Package maintainer scripts
still run during installation, so this action must not be used under a blanket
prohibition on host changes or service operations.

## Gateway capability contract

After single-use gateway confirmation, the adapter invokes the fixed
`browser check-chromium-dependencies` request through the same noninteractive
sudo wrapper entry point. The root-only probe returns success without reading
files, running maintenance commands, or emitting output. Only success permits
the fixed install request; rejected/older wrappers and probe failures fail closed.

Previously the adapter read and parsed the installed wrapper as the application
user. Bootstrap publishes the wrapper with mode 0755, but does not guarantee
unprivileged traversal of its administrator-owned parent directories. Any read
failure therefore produced a missing-action result even if the wrapper
contained the action. The probe removes that source-read requirement without
changing package policy, installation permissions, or the sudo rule.

Local tests cover fresh bootstrap publication with privileged operations mocked.
They do not verify the installed host wrapper. An existing installation is still
not overwritten by bootstrap; host replacement and loading the updated adapter
require a separately authorized administrator operation.

## Current result

Installation was not attempted: this task prohibits sudo and operations outside
the project, and the process is not root. The installed host wrapper and host OS
were not inspected. Only the repository wrapper was extended; bootstrap,
sudoers, and installed files were not changed. Existing bootstrap refuses to
overwrite an installed wrapper, so running it again is not an upgrade path.
The repository change requires a separately authorized administrator review and
installation before the host wrapper can be assumed to support this action.

`CHROMIUM_DEPENDENCIES_BLOCKED.json` records the unresolved state. The local
Playwright smoke failed. The application launcher, with filesystem access
restricted to the project cache, returned `chromium_missing`. The previously
documented external cached Chromium and missing `libnspr4.so` were not rechecked:
that cache is outside this task's allowed directory. There is no evidence of a
successful repair, and application dependency errors must remain intact.

## Local validation

Run `python3 -m unittest admin_bootstrap/test_admin.py` with the project's Node
dependencies installed. Tests compare the fixed tuple against the actual
Playwright manifest, reject extra arguments and individual package operations,
and mock all privileged execution while checking root/platform guards and
output suppression. Runtime/policy checks:
`node --test tests/test_epoptia_browser_runtime.cjs tests/test_epoptia_browser.cjs`.

After a separately authorized installation, validate with both
`node tests/playwright_smoke.cjs` and `node tests/epoptia_browser_smoke.cjs` in an
execution scope allowing the approved browser cache. Both use synthetic content;
no production Epoptia inspection is required. No application policy or redaction
change, service restart, deployment, or commit was performed for this task.
