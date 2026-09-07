# Service restart permissions

The minimal sudoers.d rules for user `ermis`, matching the central whitelist in
`service_control.ALLOWED_SERVICES`, are:

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
