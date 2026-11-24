#!/bin/sh
# North Star Monitor — Linux/macOS launcher.
cd "$(dirname "$0")"
if [ -f venv/bin/activate ]; then . venv/bin/activate; fi
exec python3 run.py
