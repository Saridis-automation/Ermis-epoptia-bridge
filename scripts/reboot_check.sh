#!/usr/bin/env bash
# Local-only health snapshot (no Epoptia calls, no sudo).
#   scripts/reboot_check.sh baseline   -> writes state/reboot_baseline.txt
#   scripts/reboot_check.sh check      -> compares current state against it
set -u
cd "$(dirname "$0")/.."
UNITS="ermis-epoptia-mcp ermis-epoptia-tunnel ermis-system-mcp ermis-system-tunnel ermis-dashboard"
PORTS="22 80 443 8000 8001 8010 8080 8081"

snapshot() {
  echo "kernel $(uname -r)"
  for u in $UNITS; do echo "unit $u $(systemctl is-active "$u.service")"; done
  echo "userunit claude-remote-control $(systemctl --user is-active claude-remote-control.service 2>/dev/null)"
  for p in $PORTS; do
    ss -ltnH "( sport = :$p )" | grep -q . && echo "port $p listen" || echo "port $p DOWN"
  done
  echo "dashboard_http $(curl -s -o /dev/null -m 10 -w '%{http_code}' http://127.0.0.1:8010/)"
  [ -e state/epoptia_halt.json ] && echo "epoptia_halt PRESENT" || echo "epoptia_halt none"
  [ -e /var/run/reboot-required ] && echo "reboot_required yes" || echo "reboot_required no"
}

case "${1:-check}" in
  baseline) snapshot | tee state/reboot_baseline.txt ;;
  check)
    snapshot > state/reboot_after.txt
    cat state/reboot_after.txt
    echo "---- diff vs baseline (kernel/reboot_required/userunit are expected to change) ----"
    diff state/reboot_baseline.txt state/reboot_after.txt && echo "no differences" ;;
  *) echo "usage: $0 baseline|check" >&2; exit 2 ;;
esac
