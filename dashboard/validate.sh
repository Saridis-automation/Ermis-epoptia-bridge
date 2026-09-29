#!/bin/sh
set -eu
cd /home/ermis/projects/epoptia-bridge
./venv/bin/python -B -m unittest discover -s tests -p 'test_dashboard*.py'
node --check dashboard/static/dashboard.js
./venv/bin/python -B -c 'from pathlib import Path; s=Path("tests/test_dashboard_browser.cjs").read_text(); exec(s.split("const fixtureSource = String.raw`",1)[1].split("`;",1)[0])' | node tests/test_dashboard_browser.cjs
node tests/test_dashboard_data_status_browser.cjs
node tests/test_dashboard_layout.cjs
