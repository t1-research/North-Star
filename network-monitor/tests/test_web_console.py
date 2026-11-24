"""Web console HTTP tests (POST /api/console/command, GET /api/console/log).

Starts the real server as a subprocess (same approach as
tests/smoke_test.py), with NORTHSTAR_MANAGED=1 so the trigger-file
endpoints behave as if a launcher supervises the process — no real
launcher runs, so written trigger files just sit in data/ until the
test removes them. Run from the network-monitor/ directory:

    python -m pytest tests/test_web_console.py -q
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "http://127.0.0.1:8080"
DATA_DIR = os.path.join(ROOT, "data")

TRIGGER_FILES = ("restart_requested", "shutdown_requested",
                 "update_check_requested")


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, json.loads(r.read())


def _post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _clean_triggers():
    for name in TRIGGER_FILES:
        try:
            os.remove(os.path.join(DATA_DIR, name))
        except OSError:
            pass


@pytest.fixture(scope="module")
def server():
    """One server for the whole file, managed-mode (no real launcher)."""
    _clean_triggers()
    env = dict(os.environ)
    env["NORTHSTAR_MANAGED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "run.py"], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 60
        while True:
            try:
                status, _ = _get("/api/status")
                if status == 200:
                    break
            except Exception:
                pass
            if time.time() > deadline:
                raise RuntimeError("server did not come up in time")
            time.sleep(1)
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        _clean_triggers()


@pytest.fixture(autouse=True)
def _no_leftover_triggers():
    _clean_triggers()
    yield
    _clean_triggers()


def test_console_log_returns_lines_list(server):
    status, body = _get("/api/console/log")
    assert status == 200
    assert isinstance(body.get("lines"), list)


def test_console_log_missing_file_is_empty_list(server):
    # No launcher runs in the sandbox, so no log file exists — the
    # endpoint must answer [] rather than erroring.
    try:
        os.remove(os.path.join(DATA_DIR, "launcher.log"))
    except OSError:
        pass
    status, body = _get("/api/console/log")
    assert status == 200
    assert body == {"lines": []}


def test_console_log_tail(server):
    path = os.path.join(DATA_DIR, "launcher.log")
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            for i in range(10):
                f.write(f"line {i}\n")
        status, body = _get("/api/console/log?lines=3")
        assert status == 200
        assert body["lines"] == ["line 7", "line 8", "line 9"]
        status, body = _get("/api/console/log?lines=0")
        assert status == 200
        assert body["lines"] == ["line 9"]  # clamped to 1
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def test_console_help(server):
    status, body = _post("/api/console/command", {"command": "help"})
    assert status == 200
    assert body.get("ok") is True
    assert "restart" in body["message"] and "quit" in body["message"]
    # Help writes no trigger file.
    for name in TRIGGER_FILES:
        assert not os.path.exists(os.path.join(DATA_DIR, name))


def test_console_unknown_command_400(server):
    status, body = _post("/api/console/command", {"command": "bogus"})
    assert status == 400
    assert "Unknown command" in body["detail"]


def test_console_missing_command_400(server):
    status, _ = _post("/api/console/command", {})
    assert status == 400
    status, _ = _post("/api/console/command", {"command": 42})
    assert status == 400


def test_console_command_case_insensitive(server):
    # "RESTART" parses like the physical console's parse_console_command.
    status, body = _post("/api/console/command", {"command": "RESTART"})
    assert status == 200
    assert os.path.exists(os.path.join(DATA_DIR, "restart_requested"))


def test_console_restart_writes_trigger(server):
    status, body = _post("/api/console/command", {"command": "restart"})
    assert status == 200
    assert body.get("ok") is True
    assert "Restart requested" in body["message"]
    assert os.path.exists(os.path.join(DATA_DIR, "restart_requested"))


def test_console_update_writes_trigger(server):
    status, body = _post("/api/console/command", {"command": "update"})
    assert status == 200
    assert body.get("ok") is True
    assert os.path.exists(os.path.join(DATA_DIR, "update_check_requested"))


def test_console_quit_writes_trigger(server):
    status, body = _post("/api/console/command", {"command": "quit"})
    assert status == 200
    assert body.get("ok") is True
    assert "run_launcher.bat" in body["message"]
    assert os.path.exists(os.path.join(DATA_DIR, "shutdown_requested"))


def test_console_restart_twice_reports_pending(server):
    status, _ = _post("/api/console/command", {"command": "restart"})
    assert status == 200
    status, body = _post("/api/console/command", {"command": "restart"})
    assert status == 200
    assert "already requested" in body["message"]
