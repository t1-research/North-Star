"""North Star Monitor — auto-updating launcher.

The Minecraft-launcher pattern for the home monitor: run this file and it
takes care of everything from then on.

    python launcher.py        (or double-click run_launcher.bat on Windows)

What it does:
  1. Makes sure the venv exists and the dependencies are installed (same
     self-heal as run.bat).
  2. Starts the monitor (run.py) as a child process and supervises it.
  3. Every `update_check_interval_seconds` (default 300, see config.yaml)
     it asks GitHub for the latest commit on the configured branch. When a
     push lands, it pulls and restarts the monitor — no manual stop or
     restart, ever. The dashboard footer has a "Check for updates" button
     that drops a trigger file (data/update_check_requested) to run a
     check immediately instead of waiting for the next interval.
  4. If launcher.py itself changed in the pull, the launcher re-execs
     itself so it never goes stale.
  5. If the child crashes, it restarts it with backoff (5s, 15s, 30s cap).
     If the child fails to stay up 3 times in a row after an update, it
     logs a clear warning that the new version may be broken — and keeps
     retrying (no auto-rollback in v1).

Your settings live in config.local.yaml (untracked), so `git pull` never
conflicts with your edits. Rule: edit config.local.yaml on your PC, never
config.yaml.

Set `auto_update: false` in config.yaml or config.local.yaml to turn the
update checks off — the launcher still supervises the child.

Stdlib only. If git is missing or this isn't a git checkout, the launcher
logs a warning once and just runs the monitor without update checks.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Set on the first re-exec under the venv python, so we never re-exec twice.
LAUNCHER_REEXEC_ENV = "NORTHSTAR_LAUNCHER_VENV"

# Backoff (seconds) after a child crash: 1st, 2nd, 3rd+ quick failure.
BACKOFF = (5, 15, 30)
# A child that stays up this long counts as a healthy start; the quick-
# failure counter resets.
HEALTHY_UPTIME_SECONDS = 60
# Grace period for the child to stop on terminate() before we kill it.
STOP_TIMEOUT_SECONDS = 10
# Fastest allowed update-check interval (prevents an accidental hammering
# of the git remote from a bad config value).
MIN_UPDATE_INTERVAL_SECONDS = 10

# Set by the signal handler; the main loop checks it once a second.
stopping = False


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [launcher] {msg}", flush=True)


def _handle_signal(signum, frame):  # noqa: ARG001
    global stopping
    stopping = True


# Dashboard update checks: the web app drops a trigger file (in data/) to
# ask for an immediate update check instead of waiting for the next
# interval. The launcher consumes (deletes) it and runs the normal check
# path. After every check it records the last/next check times in
# data/update_status.json for the dashboard footer.
UPDATE_TRIGGER_NAME = "update_check_requested"
UPDATE_STATUS_NAME = "update_status.json"
MANAGED_ENV = "NORTHSTAR_MANAGED"


def trigger_path(app_dir: str) -> str:
    return os.path.join(app_dir, "data", UPDATE_TRIGGER_NAME)


def take_update_trigger(app_dir: str) -> bool:
    """True if the dashboard asked for an update check: consume the
    trigger file (delete it) and report it. Safe to call every second —
    a missing file is the normal case."""
    try:
        os.remove(trigger_path(app_dir))
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def write_update_status(app_dir: str, last_check_ts, next_check_ts,
                        interval: int) -> None:
    """Record when the last update check ran and when the next one is due,
    for the dashboard footer. Atomic (temp file + rename) so the app never
    reads a partial file. Failures only warn — a missing status file must
    never break the update loop."""
    path = os.path.join(app_dir, "data", UPDATE_STATUS_NAME)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"last_check_ts": last_check_ts,
                       "next_check_ts": next_check_ts,
                       "interval_seconds": interval}, f)
        os.replace(tmp, path)
    except OSError as exc:
        log(f"WARNING: could not write update status file ({exc}).")


# ------------------------------------------------------------------- venv

def venv_python(app_dir: str):
    """Path to the venv interpreter, if the venv exists."""
    if os.name == "nt":
        candidate = os.path.join(app_dir, "venv", "Scripts", "python.exe")
    else:
        candidate = os.path.join(app_dir, "venv", "bin", "python")
    return candidate if os.path.isfile(candidate) else None


def ensure_venv(app_dir: str) -> str:
    """Create the venv if missing; install deps if uvicorn can't be
    imported. Returns the python to run the monitor with (venv python
    preferred, sys.executable as fallback). Exits with a clear message if
    no working python with dependencies can be produced."""
    python = venv_python(app_dir)
    if python is None:
        venv_dir = os.path.join(app_dir, "venv")
        if os.path.isdir(venv_dir):
            # Partial venv (dir exists, interpreter missing) — start clean.
            log("Removing incomplete venv and recreating it...")
            shutil.rmtree(venv_dir)
        log("Creating virtual environment...")
        r = subprocess.run([sys.executable, "-m", "venv", venv_dir])
        if r.returncode != 0:
            log("WARNING: could not create venv; falling back to system python.")
            python = sys.executable
        else:
            python = venv_python(app_dir) or sys.executable
    check = subprocess.run([python, "-c", "import uvicorn"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode != 0:
        log("Installing dependencies (first run only)...")
        r = subprocess.run([python, "-m", "pip", "install", "-r",
                            os.path.join(app_dir, "requirements.txt")])
        if r.returncode != 0:
            log("ERROR: dependency install failed. Fix the error above, "
                "then re-run the launcher.")
            sys.exit(1)
    return python


# -------------------------------------------------------------------- git

def run_git(repo_root: str, args, timeout: int = 30):
    """Run a git command in the repo. Returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(["git", *args], cwd=repo_root, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", str(exc)


def local_head(repo_root: str):
    rc, out, _ = run_git(repo_root, ["rev-parse", "HEAD"])
    return out if rc == 0 and out else None


def remote_head(repo_root: str, remote: str, branch: str):
    """Latest commit SHA on the remote branch (lightweight: no download)."""
    rc, out, _ = run_git(repo_root, ["ls-remote", remote, f"refs/heads/{branch}"])
    if rc != 0 or not out:
        return None
    return out.split()[0]  # "<sha>\trefs/heads/<branch>"


def pull(repo_root: str, remote: str, branch: str):
    """Fast-forward pull. Returns (ok, stdout, stderr). --ff-only means we
    never create a merge commit — and if the working tree has local edits
    to tracked files, the pull fails instead of clobbering them."""
    rc, out, err = run_git(repo_root, ["pull", "--ff-only", remote, branch],
                           timeout=120)
    return rc == 0, out, err


def launcher_changed(repo_root: str, old_sha: str, new_sha: str) -> bool:
    """Did launcher.py itself change between two commits?"""
    rel = os.path.relpath(os.path.abspath(__file__), repo_root).replace(os.sep, "/")
    rc, out, _ = run_git(repo_root, ["diff", "--name-only", old_sha, new_sha])
    if rc != 0:
        return False
    return rel in out.splitlines()


# ------------------------------------------------------------------ child

def start_child(python: str, app_dir: str):
    """Start the monitor. Stdout/stderr are inherited, so its logs show in
    the launcher console — one window to watch. NORTHSTAR_MANAGED tells
    the app a launcher supervises it (enables the footer update button
    and the last/next check display)."""
    env = dict(os.environ)
    env[MANAGED_ENV] = "1"
    return subprocess.Popen([python, "run.py"], cwd=app_dir, env=env)


def stop_child(proc) -> None:
    """Terminate gracefully, kill after STOP_TIMEOUT_SECONDS."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        log("Child did not stop in time; killing it.")
        proc.kill()
        proc.wait()


# -------------------------------------------------------------- supervise

def supervise(python: str, cfg: dict) -> None:
    from app.config import find_repo_root, git_short_sha

    repo_root = find_repo_root(APP_DIR)
    git_ok = shutil.which("git") is not None
    auto_update = bool(cfg.get("auto_update", True))
    interval = max(MIN_UPDATE_INTERVAL_SECONDS,
                   int(cfg.get("update_check_interval_seconds", 300)))
    branch = cfg.get("update_branch", "main")
    remote = cfg.get("update_remote", "origin")

    if repo_root is None:
        log("WARNING: not inside a git repo — running without update checks.")
        log("To enable auto-updates, run the monitor from a clone of the "
            "North-Star repo.")
    elif not git_ok:
        log("WARNING: git is not installed — running without update checks.")
    elif not auto_update:
        log("Auto-update disabled (auto_update: false) — supervising only.")
    else:
        log(f"Auto-update on: checking {remote}/{branch} every {interval}s.")
    if repo_root and git_ok:
        log(f"Running version {git_short_sha()} (branch {branch}).")
    else:
        log("Running version dev (no git checkout).")
    if auto_update and repo_root and git_ok:
        # So the footer shows the next check time before the first check
        # runs. (When checks can't run at all, no file is written and the
        # footer line stays hidden.)
        now = int(time.time())
        write_update_status(APP_DIR, None, now + interval, interval)

    quick_failures = 0       # consecutive crashes within HEALTHY_UPTIME_SECONDS
    failures_since_update = 0  # quick failures since the last good update
    warned_broken = False    # already warned this version may be broken
    child = None
    try:
        while not stopping:
            child = start_child(python, APP_DIR)
            started = time.monotonic()
            next_check = time.monotonic() + interval
            log(f"Monitor started (pid {child.pid}).")
            while not stopping:
                rc = child.poll()
                if rc is not None:
                    # Child exited on its own (crash, or a bad push).
                    uptime = time.monotonic() - started
                    log(f"Monitor exited (code {rc}) after {uptime:.0f}s.")
                    if uptime < HEALTHY_UPTIME_SECONDS:
                        quick_failures += 1
                        failures_since_update += 1
                    else:
                        quick_failures = 0
                        failures_since_update = 0
                    if failures_since_update >= 3 and not warned_broken:
                        log("WARNING: the monitor has failed to stay up 3 times "
                            "in a row since the last update — the new version "
                            "may be broken. Check the log above; auto-rollback "
                            "is not implemented in v1, so it will keep retrying.")
                        warned_broken = True
                    if quick_failures:
                        delay = BACKOFF[min(quick_failures - 1, len(BACKOFF) - 1)]
                    else:
                        delay = BACKOFF[0]
                    log(f"Restarting in {delay}s...")
                    time.sleep(delay)
                    break  # restart the child
                # Dashboard "Check for updates" button: consume the trigger
                # file (if any) and run the normal check path right now
                # instead of waiting for the next interval. The loop
                # already polls every second, so no sleep restructuring
                # was needed — the trigger is just a second way in.
                triggered = take_update_trigger(APP_DIR)
                if triggered and not (auto_update and repo_root and git_ok):
                    log("Update check requested from the dashboard, but "
                        "auto-update is unavailable (no git repo, no git, "
                        "or auto_update: false).")
                if (auto_update and repo_root and git_ok
                        and (triggered or time.monotonic() >= next_check)):
                    next_check = time.monotonic() + interval
                    new_head = remote_head(repo_root, remote, branch)
                    now = int(time.time())
                    write_update_status(APP_DIR, now, now + interval, interval)
                    if new_head is None:
                        log("Update check failed (no network?); "
                            "will retry next round.")
                    else:
                        old_head = local_head(repo_root)
                        if old_head and new_head != old_head:
                            log(f"Update available: {old_head[:7]} -> "
                                f"{new_head[:7]}")
                            ok, _out, err = pull(repo_root, remote, branch)
                            if not ok:
                                log("Update skipped: git pull failed. If you "
                                    "edited tracked files, move your edits to "
                                    "config.local.yaml (see README) and undo "
                                    "the local changes.")
                                if err:
                                    log(f"git pull error: {err}")
                            else:
                                log(f"Updated to {new_head[:7]}. "
                                    "Restarting monitor...")
                                stop_child(child)
                                if launcher_changed(repo_root, old_head, new_head):
                                    log("launcher.py changed — restarting the "
                                        "launcher itself.")
                                    os.execv(sys.executable,
                                             [sys.executable,
                                              os.path.abspath(__file__),
                                              *sys.argv[1:]])
                                # Fresh version, fresh failure counters.
                                quick_failures = 0
                                failures_since_update = 0
                                warned_broken = False
                                break  # restart the child with the new code
                time.sleep(1)
    finally:
        if child is not None:
            stop_child(child)
        log("Launcher stopping.")


def main() -> None:
    log("North Star Monitor launcher starting.")
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (OSError, ValueError):
        pass  # SIGTERM handling isn't available everywhere (e.g. some Windows setups)
    python = ensure_venv(APP_DIR)
    # Re-exec under the venv python so `yaml` (a dependency) is importable
    # for reading config.yaml. No-op when already running under it.
    if (os.path.abspath(python) != os.path.abspath(sys.executable)
            and not os.environ.get(LAUNCHER_REEXEC_ENV)):
        os.environ[LAUNCHER_REEXEC_ENV] = "1"
        log(f"Switching to venv python: {python}")
        os.execv(python, [python, os.path.abspath(__file__), *sys.argv[1:]])
    try:
        sys.path.insert(0, APP_DIR)
        from app.config import load_config
        cfg = load_config()
    except Exception as exc:  # yaml missing, config broken, ...
        log(f"WARNING: could not read config ({exc}); using defaults and "
            "running without auto-update.")
        cfg = {"auto_update": False}
    supervise(python, cfg)


if __name__ == "__main__":
    main()
