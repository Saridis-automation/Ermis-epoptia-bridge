#!/bin/sh
# Includes the exact-executable AppArmor userns profile transaction (B_LOGIN_APPARMOR).
# Run only after reviewing this entire package. Only the private login socket is enabled by install.
set -eu
case "${1-}" in
  install|rollback|diagnose) ;;
  *) echo 'Usage: bootstrap.sh install|rollback|diagnose' >&2; exit 2 ;;
esac
if [ "$1" = diagnose ] && [ "$#" -ne 1 ] && { [ "$#" -ne 2 ] || [ "$2" != apparmor ]; }; then
  printf '%s\n' unknown unknown
  exit 1
fi
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$1" = diagnose ]; then
  exec /usr/bin/python3 -I -B "$base/bootstrap.py" "$@"
fi
exec /usr/bin/python3 -I -B "$base/bootstrap.py" "$1"
