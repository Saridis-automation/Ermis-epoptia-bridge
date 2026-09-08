Separate Ermis Gateway service proposal
======================================

Review-only deployment artifact. The following proposed `ermis-gateway.service`
definition is documentation, not an installed or modified systemd file. Applying
it, provisioning runtime configuration and starting the new service require a
separate authorized operations task. Nothing here requires restarting the
existing Epoptia, Ermis_System or tunnel services.

```ini
[Unit]
Description=Ermis local voice gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ermis
WorkingDirectory=/home/ermis/projects/epoptia-bridge
ExecStart=/home/ermis/projects/epoptia-bridge/venv/bin/python /home/ermis/projects/epoptia-bridge/ermis_gateway_server.py
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Runtime prerequisites: the existing repository virtualenv and requirements,
reachability of the two existing local MCP endpoints (ports 8000 and 8001), and
outbound HTTPS/WebRTC connectivity to OpenAI. No OpenAI SDK or added Python
package is required. Supply `OPENAI_API_KEY` through an independently managed
server-only runtime environment during the future operations task. Do not put a
key in the unit, repository, browser, command line or journal. The proposal
intentionally includes no credential file path or secret provisioning commands.
Without that runtime configuration, liveness and local MCP reads still work;
voice session creation returns 503. The model is configured as `gpt-realtime`
in `ermis_gateway_voice.py`; verify account access before activation.

Use one process, no reload and no replicas: session ownership, duplicate call
tracking and confirmations are in memory. The standalone entrypoint includes
the session expiry sweep and the ASGI request-body guard; use that entrypoint
rather than serving the Flask app directly. The guard limits bodies before WSGI
buffering and rejects body uploads exceeding ten seconds. The installed Uvicorn
WSGI adapter is deprecated; replacement is a future dependency change, not part
of this release. Keep the listener on loopback port 8002 with proxy
headers/access logs disabled. Do not reuse or modify an existing tunnel. This
release has no remote authentication; the browser must be local to the listener.
Localhost is a browser secure context for microphone permission. Audio playback
may require pressing the audio element's Play button depending on browser policy.

After a separately approved installation/start, acceptance should verify:

1. `/health` returns `alive` and static UI loads, without testing upstream readiness.
2. Start requests microphone permission; speech and model audio both work.
3. Ask “What is running in Strantza now?” and compare with the existing read-only
   `workstation_wip(workstation="Strantza")` MCP result, including paused/missing data.
4. Stop releases the microphone, closes the peer and invalidates the gateway session.
5. Review safe structured log metadata; do not enable payload/debug logging.

Confirmation behavior is covered with mocked writes in tests. Acceptance does
not authorize actually restarting any service or changing production data.
A release update would require deployment and restart of **only this new gateway**;
the current work performs neither. An authorized rollback can restore the prior
gateway code independently of both existing MCP servers.
