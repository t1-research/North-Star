"""North Star Monitor — auto-updating launcher.

The Minecraft-launcher pattern for the home monitor: run this file and it
takes care of everything from then on.

    python launcher.py        (or double-click run_launcher.bat on Windows)

What it does:
  1. Makes sure the venv exists and the dependencies are installed (same
     self-heal as run.bat). The check runs before EVERY child start, not
     just at launcher startup — so a pull that adds a new requirement
     installs it before the new child starts, instead of the child
     crashing on a missing import.
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
  6. Console commands: while it runs, type `restart` in the launcher
     window to restart the monitor (reloads config — no need to kill
     the console), `update` to check for updates now, `quit` to stop
     cleanly, `help` for the list. Headless runs (Task Scheduler) have
     no console, so no commands — everything else works the same.
  7. Web console: the System page in the dashboard sends the same
     commands (restart / update / quit / help) through trigger files
     (data/restart_requested, data/shutdown_requested, plus the update
     trigger from point 3) — the app never talks to the launcher any
     other way. The launcher's own log lines are also written to
     data/launcher.log (bounded to the last 500 lines) so the web
     console can show them; the child's output stays on the console.

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
import threading
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

# The launcher's own log file (data/launcher.log) — the web console's
# tail source. Bounded: when it passes MAX lines, only the last KEEP
# lines survive. The child's stdout/stderr are NOT redirected here
# (its access logs would drown the launcher's own lines).
LAUNCHER_LOG_NAME = "launcher.log"
LAUNCHER_LOG_MAX_LINES = 1000
LAUNCHER_LOG_KEEP_LINES = 500
_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [launcher] {msg}"
    print(line, flush=True)
    _append_log_line(line)


def _append_log_line(line: str, app_dir: str = None) -> None:
    """Append one line to the launcher log file. Best-effort: logging
    must never break the launcher, so every failure is swallowed.
    The lock keeps the console-reader thread's log lines from
    interleaving with the main loop's (and from racing the trim)."""
    if app_dir is None:
        app_dir = APP_DIR
    path = os.path.join(app_dir, "data", LAUNCHER_LOG_NAME)
    with _log_lock:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            return
        _trim_log_file(path)


def _trim_log_file(path: str) -> None:
    """Keep the launcher log bounded: when it passes
    LAUNCHER_LOG_MAX_LINES, drop everything but the last
    LAUNCHER_LOG_KEEP_LINES. Runs on every append — cheap at this log
    volume (a handful of lines per minute at most), and a read failure
    just leaves the file alone. Callers hold _log_lock."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= LAUNCHER_LOG_MAX_LINES:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-LAUNCHER_LOG_KEEP_LINES:])
    except OSError:
        return


def _handle_signal(signum, frame):  # noqa: ARG001
    global stopping
    stopping = True


# ------------------------------------------------------- console commands

# Console commands (typed in the launcher's console window): the reader
# thread sets these events and the main loop acts on them once a second —
# the same consume-once-a-second pattern as the dashboard's update-check
# trigger file. The reader is a daemon thread: it never blocks shutdown,
# and it simply doesn't start when there is no console (Task Scheduler,
# piped stdin), so headless runs are unaffected.
restart_requested = threading.Event()
update_requested = threading.Event()

CONSOLE_COMMANDS = ("restart", "update", "quit", "help")


def parse_console_command(line: str):
    """Map one typed line to a command name, or None for blank/unknown
    input. Case-insensitive; extra words are ignored ('restart now'
    restarts)."""
    words = line.strip().lower().split()
    if not words:
        return None
    return words[0] if words[0] in CONSOLE_COMMANDS else None


def console_help_text() -> str:
    return ("Console commands: restart (restart the monitor, reloading "
            "config), update (check for updates now), quit (stop the "
            "monitor and launcher), help (this list).")


def console_reader_loop(stream, log_fn=log) -> None:
    """Read command lines until EOF and dispatch them. Runs in a daemon
    thread; never raises — a dead stdin just ends the loop quietly."""
    global stopping
    try:
        while True:
            try:
                line = stream.readline()
            except (OSError, ValueError):
                return
            if not line:  # EOF / stdin closed
                return
            cmd = parse_console_command(line)
            if cmd is None:
                if line.strip():
                    log_fn(f"Unknown console command: {line.strip()!r}. "
                           f"Type 'help' for the list.")
                continue
            if cmd == "restart":
                restart_requested.set()
            elif cmd == "update":
                update_requested.set()
            elif cmd == "quit":
                stopping = True
                return
            elif cmd == "help":
                log_fn(console_help_text())
    except Exception as exc:  # never let the reader thread die loudly
        log_fn(f"Console reader stopped ({exc}).")


def start_console_reader() -> bool:
    """Start the console command thread when stdin is an interactive
    console. Returns True when the thread is running. Headless runs
    (no stdin, piped stdin, Task Scheduler) get no thread and no error."""
    stdin = sys.stdin
    try:
        if stdin is None or not stdin.isatty():
            return False
    except (OSError, ValueError):
        return False
    thread = threading.Thread(target=console_reader_loop, args=(stdin,),
                              name="console-reader", daemon=True)
    thread.start()
    return True


# Dashboard trigger files: the web app drops a file in data/ to ask the
# launcher to do something, and the launcher consumes (deletes) it in
# its 1s loop. The update trigger asks for an immediate update check
# instead of waiting for the next interval; the restart/shutdown
# triggers are the web console's restart/quit commands. After every
# update check the launcher records the last/next check times in
# data/update_status.json for the dashboard footer.
UPDATE_TRIGGER_NAME = "update_check_requested"
RESTART_TRIGGER_NAME = "restart_requested"
SHUTDOWN_TRIGGER_NAME = "shutdown_requested"
UPDATE_STATUS_NAME = "update_status.json"
MANAGED_ENV = "NORTHSTAR_MANAGED"


def trigger_path(app_dir: str, name: str = UPDATE_TRIGGER_NAME) -> str:
    return os.path.join(app_dir, "data", name)


def take_trigger(app_dir: str, name: str) -> bool:
    """Consume a trigger file (delete it). True if it was there. Safe
    to call every second — a missing file is the normal case."""
    try:
        os.remove(trigger_path(app_dir, name))
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def take_update_trigger(app_dir: str) -> bool:
    """True if the dashboard asked for an update check."""
    return take_trigger(app_dir, UPDATE_TRIGGER_NAME)


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
    """Create the venv if missing; install deps if any required module
    can't be imported. Returns the python to run the monitor with (venv
    python preferred, sys.executable as fallback). Exits with a clear
    message only if the CORE modules (fastapi/uvicorn) still can't be
    imported after an install attempt — without those the monitor can't
    run at all. A missing optional module (e.g. tplinkrouterc6u) only
    warns: the monitor starts, and the feature that needs it reports
    the missing library instead of the whole app dying."""
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
    ensure_deps(python, app_dir)
    if not _modules_importable(python, CORE_MODULES):
        log("ERROR: dependency install failed, and the monitor cannot run "
            "without fastapi/uvicorn. Fix the error above, then re-run "
            "the launcher.")
        sys.exit(1)
    return python


# Every module requirements.txt must provide, by import name (which
# differs from the package name for speedtest-cli -> speedtest and
# PyYAML -> yaml). Checked before every child start: a pull that adds a
# requirement installs it before the new child starts.
REQUIRED_MODULES = ("fastapi", "uvicorn", "speedtest", "yaml", "zeroconf",
                    "tplinkrouterc6u")
# Without these the monitor can't start at all (see ensure_venv).
CORE_MODULES = ("fastapi", "uvicorn")


def _modules_importable(python: str, modules) -> bool:
    """True if every module in `modules` imports under `python`."""
    code = "import " + ", ".join(modules)
    check = subprocess.run([python, "-c", code],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return check.returncode == 0


def ensure_deps(python: str, app_dir: str) -> bool:
    """Make sure every required module imports; pip-install from
    requirements.txt when one doesn't. Returns True when everything
    imports afterwards. NON-FATAL: a failed install (no network, PyPI
    down) only logs a warning and the launcher carries on — the child
    may still run, and the feature needing the missing module reports
    it cleanly. Callers that can't run without the core modules check
    that separately (see ensure_venv)."""
    if _modules_importable(python, REQUIRED_MODULES):
        return True
    log("Installing dependencies (a required module is missing)...")
    r = subprocess.run([python, "-m", "pip", "install", "-r",
                        os.path.join(app_dir, "requirements.txt")])
    if r.returncode != 0:
        log("WARNING: dependency install failed. The monitor will start "
            "anyway; features needing the missing module will report it. "
            "Re-run the launcher once the network is back to retry.")
        return False
    if not _modules_importable(python, REQUIRED_MODULES):
        log("WARNING: pip reported success but a required module still "
            "does not import. The monitor will start anyway; the "
            "affected feature will report the missing module.")
        return False
    return True


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
    global stopping
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
    if start_console_reader():
        log("Console commands enabled — type 'help' for the list "
            "(restart / update / quit).")

    quick_failures = 0       # consecutive crashes within HEALTHY_UPTIME_SECONDS
    failures_since_update = 0  # quick failures since the last good update
    warned_broken = False    # already warned this version may be broken
    child = None
    try:
        while not stopping:
            # A pull may have added a requirement since the last start —
            # install it BEFORE the new child starts, or the child
            # crashes on a missing import. Cheap when everything is
            # already installed (one import check, no network).
            ensure_deps(python, APP_DIR)
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
                # Console "restart": the same graceful restart an update
                # pull does, minus the pull — reloads config without
                # killing the launcher (or the console window). A manual
                # restart is intentional, so the quick-failure counters
                # reset instead of counting it as a crash.
                if restart_requested.is_set():
                    restart_requested.clear()
                    log("Restart requested from the console.")
                    stop_child(child)
                    quick_failures = 0
                    failures_since_update = 0
                    warned_broken = False
                    break  # outer loop re-checks deps and starts fresh
                # Web console (System page) "quit": a shutdown trigger
                # file stops the launcher cleanly, the same as the
                # console `quit` / SIGINT — the main loop exits and the
                # finally block stops the child. Checked before restart
                # so a quit always wins; a pending restart trigger is
                # discarded with it (we're stopping anyway, and leaving
                # it would restart the child once on the next launch).
                if take_trigger(APP_DIR, SHUTDOWN_TRIGGER_NAME):
                    log("Shutdown requested from the dashboard.")
                    take_trigger(APP_DIR, RESTART_TRIGGER_NAME)
                    stopping = True
                    break
                # Web console "restart": the same graceful restart as
                # the console command above, triggered by a file.
                if take_trigger(APP_DIR, RESTART_TRIGGER_NAME):
                    log("Restart requested from the dashboard.")
                    stop_child(child)
                    quick_failures = 0
                    failures_since_update = 0
                    warned_broken = False
                    break  # outer loop re-checks deps and starts fresh
                # Dashboard "Check for updates" button: consume the trigger
                # file (if any) and run the normal check path right now
                # instead of waiting for the next interval. The loop
                # already polls every second, so no sleep restructuring
                # was needed — the trigger is just a second way in.
                # The console "update" command is a third way in.
                triggered = take_update_trigger(APP_DIR)
                console_update = update_requested.is_set()
                if console_update:
                    update_requested.clear()
                    triggered = True
                if triggered and not (auto_update and repo_root and git_ok):
                    source = "console" if console_update else "dashboard"
                    log(f"Update check requested from the {source}, but "
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
