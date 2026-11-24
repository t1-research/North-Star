"""Dashboard-triggered launcher actions (launcher only).

The web app never pulls code, restarts, or stops itself — it only drops
a trigger file that the launcher notices within seconds. The launcher
owns the actual update check, pull, restart, and shutdown (see
launcher.py), and records when checks ran in
data/update_status.json for the dashboard footer.
"""
from __future__ import annotations

import json
import os
import time

MANAGED_ENV = "NORTHSTAR_MANAGED"
TRIGGER_NAME = "update_check_requested"
RESTART_TRIGGER_NAME = "restart_requested"
SHUTDOWN_TRIGGER_NAME = "shutdown_requested"
STATUS_NAME = "update_status.json"
LAUNCHER_LOG_NAME = "launcher.log"


def trigger_path(app_dir: str, name: str = TRIGGER_NAME) -> str:
    """Where a trigger file lives: data/ next to monitor.db."""
    return os.path.join(app_dir, "data", name)


def status_path(app_dir: str) -> str:
    """Where the launcher's update-status file lives."""
    return os.path.join(app_dir, "data", STATUS_NAME)


def log_path(app_dir: str) -> str:
    """Where the launcher's log file lives (the web console's tail)."""
    return os.path.join(app_dir, "data", LAUNCHER_LOG_NAME)


def is_managed() -> bool:
    """True when a launcher supervises this process (it sets the env var
    when spawning the child)."""
    return bool(os.environ.get(MANAGED_ENV))


def _write_trigger(app_dir: str, name: str, ok_result: str) -> str:
    """Drop a trigger file for the launcher. Returns 'no_launcher'
    (standalone run — nothing would ever consume the file, so don't
    write a stale one), 'already_requested' (one is already pending),
    or ok_result (file written)."""
    if not is_managed():
        return "no_launcher"
    path = trigger_path(app_dir, name)
    if os.path.exists(path):
        return "already_requested"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(int(time.time())) + "\n")
    return ok_result


def request_update_check(app_dir: str) -> str:
    """Ask the launcher to check for updates now.

    Returns 'no_launcher' (standalone run, nothing supervising),
    'already_requested' (a request is already pending), or
    'check_requested' (trigger file written)."""
    return _write_trigger(app_dir, TRIGGER_NAME, "check_requested")


def request_restart(app_dir: str) -> str:
    """Ask the launcher to restart the monitor now (reloads config).

    Returns 'no_launcher', 'already_requested', or 'restart_requested'."""
    return _write_trigger(app_dir, RESTART_TRIGGER_NAME, "restart_requested")


def request_shutdown(app_dir: str) -> str:
    """Ask the launcher to stop the monitor (and itself) cleanly.

    Returns 'no_launcher', 'already_requested', or 'shutdown_requested'.
    The monitor stays stopped until someone runs run_launcher.bat on
    the PC — there is no remote way back up."""
    return _write_trigger(app_dir, SHUTDOWN_TRIGGER_NAME, "shutdown_requested")


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
