# Private login socket bootstrap

The fixed login extension installs `ermis-epoptia-login.socket`, its matching
service, the supervisor launcher, four typed action clients and a public hash
receipt. It validates the reviewed source manifest and receipt ownership before
replacement. It accepts no caller-supplied paths, units, commands or listeners.

Install performs exactly `systemctl daemon-reload` and
`systemctl enable --now ermis-epoptia-login.socket`. It never restarts an existing
service. The service itself is not enabled. Socket activation starts it only
when a client connects. No browser, VNC or noVNC process starts until a typed
start request arrives through the confirmed gateway flow.

The socket has one AF_UNIX stream at
`/run/ermis-epoptia-login/control.sock`, mode 0600, owned by ermis:ermis, with
`Accept=no`. The standalone root-owned runtime helper creates its directory as
ermis:ermis mode 0700; unsafe existing ownership, modes or symlinks are refused.
The socket runs this preparation at boot as well, without executing project
source as root. The helper accepts no arguments and imports only isolated
standard-library modules. The service cannot create or
replace its listener: it verifies fd 3, the systemd activation PID/count, the
listening Unix-stream inode/path and private filesystem metadata before adopting
it. It refuses root execution and execution outside its fixed service cgroup.

Automatic installation is first-install-only. Under the exclusive lock it must
prove tree and disk profile absence, definite kernel absence, inactive login and
browser activity, idle transaction state, and all source/parser/destination gates.
Coherent prior/candidate, mixed, and unknown states require manual review without
mutation. A root-owned regular policy receipt alone may be replaced only when
independent live observations prove first-install absence. Other existing receipts
or installed artifacts are refused.

All publications use Linux renameat2(RENAME_NOREPLACE), without an overwrite
fallback. Candidates are bound to no-follow device/inode/type, ownership/mode and
validated content or full-tree manifests. All filesystem artifacts are committed
and checked before the explicit irreversible boundary immediately preceding the
add-only kernel load. The policy receipt and readiness marker are prepared early
and published last, after verification. A stale policy receipt is identity-checked
and archived with NOREPLACE to a unique transaction name only at receipt commit.

Failures retain artifacts for manual recovery, including pre-boundary temporary
files and private staging containers. No automatic filesystem deletion/restoration
or kernel removal/replacement occurs. Foreign replacements are never cleaned or
restored. Any ambiguous add, verification failure, or receipt race blocks success.
Retained transaction residue blocks a future automatic retry; do not infer cleanup
authority from a prefix or receipt. Diagnose remains zero-write; NOREPLACE symbol
availability is advisory, filesystem support is proven only by each atomic commit.

Diagnose verifies source/installed hashes, both unit files, dependencies, state
permissions, private runtime ownership/modes, an enabled active listening socket,
its Unix-stream entry and the fixed VNC/noVNC ports. The control listener must
be Unix-only; any VNC/noVNC listener must be IPv4 loopback-only. Diagnosis returns
fixed component codes only, never command output or session contents. Install
success means the private socket was enabled and no existing service restarted;
it is not proof of a usable saved session or successful authentication.

Confirmed start prepares private enrollment state before connecting. The client
sends only the typed action and bounded TTL. Launch checks the loopback VNC
banner and noVNC HTTP health before returning the fixed local URL and an SSH
local-forward template. No passwords or session material are returned. The
existing private VNC authentication remains required. Client disconnect during
launch cancels that enrollment; a launch watchdog, enrollment TTL and systemd
control-group cleanup bound failures. Disposable profiles live in the service
runtime subdirectory `enrollment`, which systemd removes even after a daemon
crash; the parent control socket remains socket-owned. Stop/finalize remain typed operations.
Saved-session promotion, conservative authentication proof, pacing and the
single-enrollment scheduler remain unchanged.

Policy rollback now fails closed for manual review: the kernel exposes no
transaction-unique profile identity authorizing automatic removal or restoration.
The surrounding admin-wrapper rollback is outside this policy transaction.

Offline validation uses only local synthetic fixtures and mocked host operations.
No host installation or running service is verified by these tests. After review,
the user must rerun the existing administrator bootstrap install command. No core
MCP or tunnel restart is performed by bootstrap. Already-running Python gateway
workers need the updated source loaded through a separately authorized rollout;
this task does not perform that rollout. A socket-activated daemon retires itself
on source revision changes so the next connection can activate the new version.

The Chromium AppArmor fix stages the complete pinned Playwright 1.63.0 /
Chromium revision 1243 tree at
`/opt/ermis/epoptia-browser/chromium-1243/chrome-linux64/chrome`.
Bootstrap reads static package metadata and resolves the service home with passwd
information. It never executes package JavaScript or trusts the administrator's
HOME. Exactly one matching full browser tree must exist in the project cache or
the service user's cache. Every source entry must belong to that user, have safe
permissions and contain no symlinks, special files, setuid/setgid bits or file
capabilities. Symlinks are deliberately unsupported, including internal links.

Copying uses directory file descriptors and no-follow opens, checks for source
races, and preserves the complete tree. The root-owned candidate uses 0755
directories/executables and 0644 data files; chrome_sandbox never gains setuid.
A same-filesystem rename publishes only a fully validated tree. An invalid or
different existing tree is never overwritten. The receipt pins the full-tree
manifest hash (sorted compact JSON records of relative path, type, normalized
mode and file SHA-256), executable hash, exact layout and package/browser versions.
A revision upgrade requires reviewed source changes and bootstrap; it cannot
silently select a different cache installation.

The generated unit explicitly selects this executable. Both real login and the
offline worker require it and verify the root-owned ancestors, literal real path,
complete tree, modes, capabilities, profile and receipt before launch. Package
executable resolution and PATH are never fallbacks. Drift returns only the bounded
`B_LOGIN_APPARMOR` blocker. Managed home, NNP, private socket, offline restrictions,
real-login confirmation and Chromium sandbox settings remain unchanged.

The version-named AppArmor profile attaches to the exact staged executable and
contains only `userns,` with `flags=(unconfined)`. Bootstrap dry-parses with
skip-kernel-load/skip-cache, loads only that profile, checks its kernel name,
attachment and mode, and publishes the matching receipt last. No global policy
or sysctl changes are made. Diagnose is read-only/non-loading and returns bounded
source, staging, profile and receipt states without paths or hashes.

Legacy partial installs and coherent installed states require manual review.
Automatic migration and continuation are disabled. Transaction errors report a
bounded stage with MANUAL_RECOVERY or MANUAL_REVIEW; no raw parser output is shown.

These are source changes for a later explicitly authorized administrator bootstrap.
Local tests use synthetic trees and mocked policy/service operations. They do not
establish live AppArmor attachment, browser startup or authentication success.
Bootstrap retains its no-existing-service-restart contract.

Read-only AppArmor diagnosis
---------------------------

`bash admin_bootstrap/bootstrap.sh diagnose` requires administrator execution.
It does not enter the install/rollback implementation. It preserves seven leading compatibility
code lines: two legacy `unknown` placeholders (general wrapper/service health
is deliberately not inspected), the AppArmor compatibility code, the legacy
recovery code, `substage-*`, `state-*`, and `retry-*`. Exit zero means the
AppArmor tree/profile/kernel/receipt bindings agree, not general service health.
No file contents, paths, hashes, subprocess output, or exception text are printed.

Substages are `apparmor-source-parse`, `apparmor-parser-options`,
`apparmor-profile-publish`, `apparmor-profile-load`, `apparmor-postload-verify`,
`apparmor-rollback`, `tree-stage`, `receipt-binding`, and `other`. They identify
an observed failed check, not historical proof of where a previous install
failed: the installer does not persist a transaction journal. Random shared
transaction prefixes are treated as unowned residue and always forbid retry.
The diagnostic neither reads nor cleans those candidates/backups.

Both forms append bounded source-preflight fields after the compatibility prefix.
The optional scoped form
`diagnose apparmor` adds five bounded phase enums for original trigger, load,
post-load verification, rollback file, and rollback kernel. Raw ledger fields
never reach output. Current observations do not establish historical phases.

Missing, unsafe, stale, or incomplete transaction evidence emits
`state-INDETERMINATE` and `retry=blocked:insufficient-transaction-evidence`.
The existing installer publishes no complete phase/prestate ledger, so this is
expected even when the observed candidate appears ready. Install behavior is
unchanged. A root-owned ledger, if available, must match the source and contain
all five phase outcomes and complete disk/kernel baselines. Rollback equality
includes metadata, fingerprints, kernel policy identity, and cache/disable/
complain state; successful commands alone never prove restoration.

Exact duplicate names or attachments are definite conflicts. AARE/glob or
unrecognized disk policy syntax that cannot be proven disjoint is a possible
conflict. Both block retry. No unconditional retry eligibility is emitted.
Strict retry reporting permits only `eligible_after_atomic_recheck` for a proven
first-install baseline, supported NOREPLACE symbol, inactive browser/login,
idle transaction, successful parsing, and no definite or possible conflict.
Filesystem NOREPLACE support and the irreversible boundary remain pending until
fresh installer verification. Coherent prior/candidate states never qualify.

Both dry parse stages pipe trusted, bounded profile bytes through anonymous stdin
into `/usr/sbin/apparmor_parser --config-file=/dev/null -Q -K --abort-on-error`.
A fixed Bash runner immediately captures `PIPESTATUS[1]`, with errexit suspended
only for that pipeline and restored afterward. Producer failure cannot override
parser success. Output is discarded without files; exit status alone determines
success. There is no timeout or pre-exec sandbox in this proven command path.
The flags bypass host configuration and prohibit kernel loading and cache use.
A successful fixed-profile probe is required before exact candidate compilation;
nonzero probe status blocks environment validation, and nonzero candidate status
blocks compile validation. Exec, signal and timeout status codes are distinct.
No mounts, system profiles, services, browser, network, or Epoptia operations are
used. Python diagnostic file/directory traversal uses
anchored descriptors with `O_NOFOLLOW`, `O_NONBLOCK`, and `O_NOATIME`; the
installed-tree check explicitly avoids the runtime helper that drops NOATIME.

The reviewed bootstrap pins the diagnostic module, which contains the pure
source verifier shared with install preflight. Its explicit sorted 22-file
allowlist, byte hashing and no-follow reader also drive manifest generation.
Source trust does not depend on root ownership or installed receipts; installed
binding and candidate checks run afterward. This assumes administrator review
of the bootstrap entry point and source manifest, as with installation.
After edits, run `python3 -B admin_bootstrap/generate_login_manifest.py` to
refresh the diagnostic pin followed by the manifest; repeat to check stability.

Source-preflight output
-----------------------

The fields are observations of this invocation. Checks not reached after a
failure are omitted; none of these enums has a `not-run` value. In particular,
manifest rejection never runs the option probe or candidate compiler. If the
pinned diagnostic module cannot be loaded, bootstrap emits
`diagnostic-module=invalid|unreadable` and omits `source-manifest`, since the
source verifier has not run.

- `source-manifest=ok|invalid|unreadable`
- `source-manifest-reason=verified|unsafe-path|entry-set|digest-mismatch|permission-denied|missing|unsafe-type-mode|io-error|manifest-format|changed-during-read`
- `candidate-file=ok|missing|unsafe-type|unreadable`
- `candidate-name=ok|mismatch`
- `candidate-attachment=ok|mismatch`
- `candidate-rules=ok|unexpected|missing-userns`
- `parser-options=ok|unsupported|failed`
- `candidate-compile=ok|syntax-error|include-error|feature-unsupported|other-error`
- `expected-binding=source-pinned|receipt-mismatch|indeterminate`
- `primary=SOURCE_MANIFEST_FAILURE|PROFILE_IDENTITY_FAILURE|PROFILE_RULE_FAILURE|PARSER_OPTION_FAILURE|PROFILE_PARSE_FAILURE|INDETERMINATE`
- `retry=blocked:<specific-code>` (future eligibility remains
  `retry=eligible_after_atomic_recheck`, never emitted by this implementation).

`candidate-file` describes the browser source used to generate the in-memory
profile; no temporary policy file is written or stale transaction file read.
Unsafe file types/links are distinct from unreadable or unverifiable tree data.
The exact profile name and attachment are checked against pinned source
constants. Installed receipts never supply the candidate's expected identity;
a stale policy receipt reports `expected-binding=receipt-mismatch` separately.
The compiler receives exactly the validated generated bytes. Compiler stderr is
never printed, persisted, or used as an output value; unrecognized failures,
signals, and timeouts classify as `other-error`.

The final retry field identifies the first source-preflight failure (for example,
`blocked:source-manifest-invalid` or `blocked:candidate-attachment-mismatch`).
The earlier compatibility retry line retains its historical meaning. Source
preflight success alone cannot prove retry eligibility; transaction evidence,
conflict checks and atomic recheck requirements continue to block it.
