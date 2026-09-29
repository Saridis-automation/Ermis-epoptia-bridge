#!/bin/sh
# Pure local checks: no installed commands, root operations, or network access.
set -eu
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$base"
/bin/sh -n bootstrap.sh
/bin/sh -n verify.sh
/usr/bin/python3 -I -B test_admin.py
/usr/bin/python3 -I -B test_bootstrap_sandbox.py
printf '%s\n' 'Local mocked tests passed; host installation has not been verified.'
