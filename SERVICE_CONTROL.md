# Service restart permissions

The previously documented sudoers.d rules for user `ermis` are:

```sudoers
ermis ALL=(root) NOPASSWD: /usr/bin/systemctl restart ermis-epoptia-mcp.service
ermis ALL=(root) NOPASSWD: /usr/bin/systemctl restart ermis-epoptia-tunnel.service
ermis ALL=(root) NOPASSWD: /usr/bin/systemctl restart ermis-system-mcp.service
ermis ALL=(root) NOPASSWD: /usr/bin/systemctl restart ermis-system-tunnel.service
```

These rules allow only the four exact restart commands, without wildcards or
additional flags. This file is documentation only; no sudoers configuration is
installed or modified by the project.

Restart invokes `sudo -n /usr/bin/systemctl restart <approved-service>` and fails
without prompting if permission is unavailable. Status uses read-only
`/usr/bin/systemctl show` without sudo. Restart waits for systemctl with a
five-second timeout; a timeout does not cancel a restart already submitted to
systemd. Restarting the MCP service may interrupt its own response.

The central allowlist also includes `ermis-dashboard.service`, using the same
status and restart operations. No start, stop, enable, install, wildcard or
arbitrary-command operation is exposed. Gateway restart requests still require
confirmation. Dashboard restart requires existing OS permission for the exact
command `sudo -n /usr/bin/systemctl restart ermis-dashboard.service`; it fails
closed if permission is absent. Dashboard permissions have not been inspected
or changed, and the rules above do not grant that permission.

Ermis System health now includes the dashboard: a missing or inactive dashboard
makes aggregate health false. Running processes must reload this Python code in
a separately authorized activation before the new allowlist takes effect;
no services were restarted for this change.

Login enrollment uses the bootstrap-owned private Unix socket described in
[LOGIN_BOOTSTRAP.md](admin_bootstrap/LOGIN_BOOTSTRAP.md). The login service is
excluded from this generic service-control allowlist. The gateway invokes no
sudo or systemctl operation: a confirmed typed start connects to the fixed
socket, triggering activation. Unsafe listener/runtime state fails closed;
listener inspection failures do not prevent typed stop/finalize cleanup.
