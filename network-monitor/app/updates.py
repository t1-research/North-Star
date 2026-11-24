"""Dashboard-triggered update checks (launcher only).

The web app never pulls code or restarts itself — it only drops a trigger
file that the launcher notices within seconds. The launcher owns the
actual update check, pull, and restart (see launcher.py), and records
when checks ran in data/update_status.json for the dashboard footer.
"""
from __future__ import annotations

import json
import os
import time

MANAGED_ENV = "NORTHSTAR_MANAGED"
TRIGGER_NAME = "update_check_requested"
STATUS_NAME = "update_status.json"


def trigger_path(app_dir: str) -> str:
    """Where the trigger file lives: data/ next to monitor.db."""
    return os.path.join(app_dir, "data", TRIGGER_NAME)


def status_path(app_dir: str) -> str:
    """Where the launcher's update-status file lives."""
    return os.path.join(app_dir, "data", STATUS_NAME)


def is_managed() -> bool:
    """True when a launcher supervises this process (it sets the env var
    when spawning the child)."""
    return bool(os.environ.get(MANAGED_ENV))


def request_update_check(app_dir: str) -> str:
    """Ask the launcher to check for updates now.

    Returns 'no_launcher' (standalone run, nothing supervising),
    'already_requested' (a request is already pending), or
    'check_requested' (trigger file written)."""
    if not is_managed():
        return "no_launcher"
    path = trigger_path(app_dir)
    if os.path.exists(path):
        return "already_requested"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(int(time.time())) + "\n")
    return "check_requested"


def read_update_status(app_dir: str):
    """Parse the launcher's update-status file. Returns the dict, or None
    when the file is missing or unparseable — never raises."""
    try:
        with open(status_path(app_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data
