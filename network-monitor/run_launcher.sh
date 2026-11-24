#!/bin/sh
# North Star Monitor — auto-updating launcher (Linux/macOS).
cd "$(dirname "$0")"
if [ -f venv/bin/activate ]; then . venv/bin/activate; fi
exec python3 launcher.py
