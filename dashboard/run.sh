#!/bin/sh
set -eu
cd /home/ermis/projects/epoptia-bridge
exec ./venv/bin/python -m dashboard.server "$@"
