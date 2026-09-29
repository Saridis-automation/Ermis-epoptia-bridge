#!/usr/bin/env bash
# Run only the offline (mocked) unit tests under the offline guard.
#
# The guard (tests/offline_guard/sitecustomize.py) blocks non-loopback network,
# the local service ports, and sudo/systemctl/apparmor/bootstrap/git commands.
# Dummy EPOPTIA_* values keep load_dotenv() from picking up real credentials
# (python-dotenv never overrides variables already present in the environment).
#
# Excluded: login/apparmor/bootstrap/browser/chromium/vnc/sandbox/playwright
# tests (paused workstream; some execute bootstrap.sh) and all .cjs tests.
#
# Usage: scripts/run_offline_tests.sh [test_module ...]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXCLUDE='login|apparmor|bootstrap|browser|chromium|vnc|sandbox|playwright'

if [ "$#" -gt 0 ]; then
    modules=("$@")
else
    mapfile -t modules < <(find "$ROOT/tests" -maxdepth 1 -name 'test_*.py' -printf '%f\n' \
        | sed 's/\.py$//' | grep -v -E "$EXCLUDE" | sort)
fi
for m in "${modules[@]}"; do
    if grep -q -E "$EXCLUDE" <<<"$m"; then
        echo "refusing excluded module: $m" >&2
        exit 2
    fi
done

guard_log="$(mktemp)"
before="$(git -C "$ROOT" status --porcelain)"
trap 'rm -f "$guard_log"' EXIT

set +e
( cd "$ROOT" && env \
    EPOPTIA_BASE_URL=https://blocked.invalid EPOPTIA_API_KEY=dummy \
    EPOPTIA_USERNAME=dummy EPOPTIA_PASSWORD=dummy \
    ERMIS_GUARD_LOG="$guard_log" PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="$ROOT/tests/offline_guard:$ROOT:$ROOT/tests" \
    "$ROOT/venv/bin/python" -m unittest "${modules[@]}" )
status=$?
set -e

echo "--- offline guard: ${#modules[@]} modules"
if [ -s "$guard_log" ]; then
    echo "blocked escape attempts:"
    cut -d' ' -f2- "$guard_log" | sort | uniq -c
else
    echo "no escape attempts"
fi
if [ "$before" != "$(git -C "$ROOT" status --porcelain)" ]; then
    echo "WARNING: git working tree changed during the test run" >&2
    status=1
fi
exit "$status"
