# One-time admin bootstrap package

This directory is independent of the application. Nothing is installed by local
verification. `ERMIS_ADMIN_WRAPPER_READY` means the local mocked tests passed;
it does not mean host compatibility or a production policy has been approved.

## Review before installation

The wrapper has no caller-controlled shell, command paths, options, or policy
file. Review and edit `PACKAGES`, `SERVICES`, `PORTS`, and `FILES` in `ermis-admin`
before installation. They are empty by default: all named package, service,
port, journal, and file operations are denied until configured. The fixed
package-index update, nginx, certbot, and UFW status verbs are available.

Policy approval carries real root privileges:

- Package names must be exact repository package names, never local files.
  Apt uses the host's root-managed repositories, configuration and hooks.
  Installing packages can install dependencies and run maintainer scripts;
  removing packages can also remove dependents. Review those effects before
  approving a package. Install forbids removal; no upgrade/purge/autoremove verb
  is exposed.
- Service names must be exact unit names. Only approve units whose commands,
  scripts, configuration and executable search paths are administrator-owned,
  or whose processes run without root privileges. No Epoptia service or tunnel
  is preapproved.
- Ports are exact `number/tcp` or `number/udp` strings. There are no address,
  interface, reset, or broad firewall verbs.
- `FILES` maps simple logical names to exact absolute destinations. Approve
  only non-executable UTF-8 text, with existing root-owned parents that are not
  group/world writable. Never approve sudoers, SSH, authentication, systemd,
  tunnel, cron, executable files, credentials, or configuration that can load
  modules, run hooks, include caller-controlled files, or otherwise execute as
  root. Backups use the exact destination plus `.ermis-admin-backup`; review
  that derived destination too. No caller-supplied source or destination paths
  are accepted. Files are installed root-owned with mode 0600; preservation of
  ACLs, extended attributes, labels, or previous modes is not supported.
- Nginx configuration/modules and certbot configuration/plugins/hooks must
  already be administrator-controlled. Reload first runs `nginx -t`.
- Journal access is limited to configured units, 1–200 entries, a lookback of
  1–1440 minutes, and 64 KiB of output. Only approve units whose logs the user
  is allowed to read. Output is JSON encoded; truncation can cut a journal
  record. Other commands return status only, withholding their output.

The package assumes Debian/Ubuntu-style absolute executable paths, Python 3.8+
(`verify.sh` requires 3.8+), existing trusted `/usr/local/sbin` and
`/etc/sudoers.d` directories, and an existing sudoers include for
`/etc/sudoers.d`. It installs no dependencies. Ensure those prerequisites and
the `ermis` account exist before running the bootstrap. Existing unrelated sudo
grants are neither inspected nor revoked by this package.

## Local verification

```sh
sh admin_bootstrap/verify.sh
```

Tests mock privileged execution and use temporary fixtures in this directory.
They cover request validation, injection/traversal, bounds, clean subprocess
environment, nginx test failure, file symlinks and atomic replacement, backup /
restore, install modes, overwrite refusal, sudoers-validation failure cleanup,
and rollback tamper detection. No live apt/systemd/nginx/certbot/UFW/journal or
sudoers checks run. The verifier clears its previous marker before checking.

The fake-root regression harness exercises the install/publication/rollback
flow using temporary fixed destinations inside this project. It simulates root
ownership at the metadata boundary, retains the real trust and mode checks,
asserts the exact suppressed-output validator invocation, and loads the installed
fixture for the status-only action self-test. It also checks repeat-install
refusal, failure cleanup, and sanitized diagnostics. It does not execute visudo
or establish that the host sudoers configuration is valid.

Install accepts a different older wrapper only when all existing artifacts pass
exact root:root ownership, modes, regular-file/single-link/no-symlink checks,
wrapper/receipt agreement, and the fixed rule check. Partial or untrusted
installations and already-current installs are refused. Source is read without
following links, compiled, and checked against the reviewed fingerprint before
any publication or staging. Policy edits require review and a fingerprint update.
Upgrades stage synced root-owned candidates and rollback copies before atomic
replacement of the wrapper and receipt; the validated fixed rule stays in place.
Final sudoers and installed-artifact validation failures restore the prior files.
Sandbox regressions cover upgrades, rollback, invalid source, and trust failures.

Bootstrap errors now append only a fixed stage identifier:

| Identifier | Failed stage |
| --- | --- |
| `B_ARGUMENT` | Action validation |
| `B_PRIVILEGE` | Administrator invocation check |
| `B_PARENT_TRUST` | Installation directory trust checks |
| `B_LOCK` | Installer serialization |
| `B_EXISTING_INSTALL` | Existing artifact trust validation or already-current refusal |
| `B_SOURCE` | Wrapper source read, compilation, or reviewed fingerprint validation |
| `B_UPGRADE_STAGE`, `B_UPGRADE_REPLACE` | Upgrade staging or atomic replacement |
| `B_INSTALLED_VERIFY` | Final installed bytes, ownership, modes, and receipt validation |
| `B_SUDOERS_INITIAL` | Initial full configuration validation |
| `B_PUBLISH_WRAPPER`, `B_PUBLISH_RECEIPT`, `B_PUBLISH_RULE` | Fixed artifact publication |
| `B_SUDOERS_CANDIDATE` | Candidate rule validation |
| `B_SUDOERS_FINAL` | Final full configuration validation |
| `B_CLEANUP` | Failed-install cleanup or restoration of prior upgrade artifacts |
| `B_ROLLBACK_VERIFY`, `B_ROLLBACK_REMOVE` | Rollback verification or removal |
| `B_UNKNOWN` | Unclassified failure |

These identifiers omit exception details and runtime values. The marker
`BOOTSTRAP_NEEDS_SANITIZED_ROOT_DIAG.marker` records that host diagnosis is still
needed; it does not authorize another install attempt or any privileged action.

## Later administrator installation

### Read-only diagnosis

From a reviewed trusted copy, `sh bootstrap.sh diagnose` accepts no additional
arguments. It requires root to inspect the protected artifacts; insufficient
privilege returns `unknown`. It never invokes installation, rollback, installed
wrapper code or maintenance commands. Read-only subprocess checks include
`visudo -c -f`, fixed dependency probes and private login socket/listener status.
It does not validate the global sudoers configuration or effective grants.

Output is exactly two fixed code lines: the first detected diagnostic, followed
by the final state (`partial-install`, `installed-valid`, or `unknown`). Exit
status is zero only for `installed-valid`. Diagnostics are `source-invalid`,
`wrapper-missing`, `wrapper-type`, `wrapper-owner-mode`, `helper-missing`,
`helper-owner-mode`, `source-installed-mismatch`, `sudoers-missing`,
`sudoers-invalid`, `sudo-rule-mismatch`, `action-missing`, `installed-valid`,
or `unknown`. `partial-install` means some but not all three artifacts exist;
it may indicate interrupted installation or rollback, not a proven cause.
The helper codes refer to the rollback hash receipt: this bootstrap installs
no separate executable helper. Missing or changed receipts prevent a valid result.

Checks use bounded, no-follow, no-atime reads, exact root:root ownership and
modes, single-link regular files, trusted parents, and source/installed/receipt
fingerprint agreement. No fingerprints or other runtime details are printed.
The fixed action capability is recognized only for the reviewed immutable wrapper
fingerprint recorded in `bootstrap.py`; absent action literals produce
`action-missing`, and other unreviewed revisions produce `unknown`. This does not
execute a capability probe or establish host/package compatibility. Validation
tool absence, unreadable files, timeouts, and unexpected errors fail closed.
`installed-valid` describes the inspected artifacts, not global sudo access or
protection against changes after inspection. Local tests use synthetic ownership
and a mocked validator; they do not diagnose the live root-only failure.

Review all source and the policy, rerun verification, and take a trusted copy
of this directory in an administrator-owned location before invoking it. Do
not run root bootstrap code from a directory another user can change during
installation. The repository copy is a preparation artifact.

From that reviewed copy, the administrator can later run:

```sh
sudo sh bootstrap.sh install
```

The installer checks existing sudoers syntax, validates existing target files,
installs `/usr/local/sbin/ermis-admin` as root:root 0755, writes a root-only
hash receipt, and publishes `/etc/sudoers.d/ermis-admin` as root:root 0440 after
validating it separately. It validates the complete sudoers configuration
after publication. The only rule it adds is:

```sudoers
ermis ALL=(root) NOPASSWD: NOSETENV: /usr/local/sbin/ermis-admin
```

Arguments are deliberately validated by the wrapper. Python isolated mode
prevents caller import paths/Python environment settings from loading code.
Only root may invoke the wrapper successfully. Child commands receive a fixed
environment, closed stdin, and a 900-second timeout. Journal readers additionally
receive a 64 KiB output-file limit; maintenance command output is discarded.
Timeouts/failures do not roll back package/service operations; consult host
logs before retrying. Calls are serialized using an inode lock.

Examples of the structured interface (names must first be approved):

```text
ermis-admin package update
ermis-admin package install PACKAGE
ermis-admin package remove PACKAGE
ermis-admin service status|start|stop|restart|reload SERVICE
ermis-admin nginx test|reload
ermis-admin certbot renew|dry-run
ermis-admin ufw status
ermis-admin ufw allow|delete PORT/PROTOCOL
ermis-admin journal SERVICE LINES MINUTES
ermis-admin file backup|restore NAME
ermis-admin file install NAME < reviewed-text-file
```

Existing files require a backup before installation. A backup cannot be
overwritten through the wrapper; restore retains it. File replacement is
atomic and fsynced. Requests never print file contents. Syslog AUTHPRIV hooks
record begin/success/failure and validated requests, without rejected input,
file bodies, environment values, or child output. Host syslog routing and
retention must be configured separately; this is not a tamper-proof audit store.

## Rollback

From the same trusted copy, an administrator can later run:

```sh
sudo sh bootstrap.sh rollback
```

Rollback checks root ownership and the installed wrapper hash and exact rule,
revokes the rule first, removes the wrapper and receipt, then checks sudoers.
It refuses changed files for manual review. Fresh-install errors automatically
remove only files created by that attempt; upgrade errors restore the prior
wrapper and receipt. Power loss or forced termination can require manual review
of installation and staging paths; partial or inconsistent installs are refused.
Rollback does not reverse maintenance
operations, restore config files, or remove their backups. The admin-wrapper phase performs no service operations. The login extension
has the narrow socket policy described below.

The same `bootstrap.sh install` also installs/updates the fixed login supervisor
components described in [LOGIN_BOOTSTRAP.md](LOGIN_BOOTSTRAP.md). Diagnose includes
login component mismatches; rollback verifies and removes owned login components
before removing the admin wrapper. Login install reloads systemd and enables/starts
only `ermis-epoptia-login.socket`; it never restarts existing services. Login
rollback disables/stops only owned login units and preserves saved sessions.
