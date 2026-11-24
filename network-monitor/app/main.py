"""North Star Monitor — FastAPI backend + dashboard server.

LAN-only by design: no encryption, no internet exposure. Never
port-forward this. A dashboard password can be set with set_password.py
(the hash lives in config.local.yaml); without one the dashboard is open
to your LAN.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, db, deco, services, updates
from .collectors import Collectors
from .config import (DB_PATH, PROJECT_ROOT, build_device_lookups,
                   git_short_sha, load_config)
from .network import is_valid_mac, normalize_mac, subnet_broadcast
from .wol import send_magic_packet

CFG = load_config()
START_TIME = time.time()
VERSION = git_short_sha()  # short commit SHA at startup, or "dev"
STATIC_DIR = os.path.join(PROJECT_ROOT, "static")

# Dashboard password: the hash lives in config.local.yaml only. Without
# one, auth is disabled and the dashboard stays open — never invent a
# default password or lock the user out.
PASSWORD_HASH = CFG.get("dashboard_password_hash") or ""
AUTH_ENABLED = isinstance(PASSWORD_HASH, str) and bool(PASSWORD_HASH)
if not AUTH_ENABLED:
    print("WARNING: No dashboard password set — dashboard is open to your "
          "LAN. Run set_password.py to protect it.", flush=True)

# True when the launcher supervises this process (it sets
# NORTHSTAR_MANAGED=1 in the child's environment). The footer "Check for
# updates" button and the last/next check times only render when this is
# true — a standalone run.py has no launcher to ask.
MANAGED = updates.is_managed()

collectors = Collectors(CFG, DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(DB_PATH)
    thread = threading.Thread(target=collectors.run_forever, daemon=True)
    thread.start()
    yield


app = FastAPI(title="North Star Monitor", lifespan=lifespan)

# Shared dashboard assets (style.css, app.js). Served under /static and
# gated by the auth middleware like every other non-public path — with a
# password set, they only load once you're signed in.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------- auth gate

# Paths that work without a session (the login flow itself, plus favicon
# so browsers don't get a redirect loop on it).
PUBLIC_PATHS = {"/login", "/api/login", "/api/logout", "/favicon.ico"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Gate every request on a valid session cookie, unless auth is
    disabled (no password configured) or the path is public. API calls
    get a 401 JSON; page loads get a redirect to /login."""
    if not AUTH_ENABLED or request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    if auth.validate_session(request.cookies.get(auth.COOKIE_NAME)):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": "not authenticated"})
    return RedirectResponse(url="/login", status_code=302)


@app.get("/login")
def login_page(request: Request):
    """Login page. No password configured, or already signed in, means
    there is nothing to do here — go to the dashboard."""
    if not AUTH_ENABLED:
        return RedirectResponse(url="/", status_code=302)
    if auth.validate_session(request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse(url="/", status_code=302)
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


@app.post("/api/login")
async def login(request: Request):
    """Sign in with JSON {"password": "..."}. On success sets the session
    cookie (7 days, HttpOnly); wrong passwords count toward the per-IP
    lockout."""
    if not AUTH_ENABLED:
        raise HTTPException(status_code=400, detail="no dashboard password configured")
    ip = request.client.host if request.client else "unknown"
    if auth.is_locked_out(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many failed logins — try again in a few minutes")
    try:
        body = await request.json()
    except Exception:
        body = {}
    password = body.get("password") if isinstance(body, dict) else None
    if not isinstance(password, str) or not auth.verify_password(password, PASSWORD_HASH):
        auth.record_failed_login(ip)
        raise HTTPException(status_code=401, detail="wrong password")
    auth.record_successful_login(ip)
    response = JSONResponse(content={"ok": True})
    # No Secure flag: the dashboard is plain HTTP on the LAN, and Secure
    # would stop the browser sending the cookie at all. Remote access
    # goes over Tailscale (encrypted), never a raw port forward.
    response.set_cookie(
        key=auth.COOKIE_NAME, value=auth.create_session(),
        httponly=True, samesite="lax", path="/",
        max_age=auth.SESSION_TTL_SECONDS,
    )
    return response


@app.post("/api/logout")
def logout(request: Request):
    """Sign out: revoke the session and clear the cookie."""
    auth.revoke_session(request.cookies.get(auth.COOKIE_NAME))
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key=auth.COOKIE_NAME, path="/")
    return response


# -------------------------------------------------------------- dashboard

@app.get("/")
def dashboard():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/devices")
def devices_page():
    return FileResponse(os.path.join(STATIC_DIR, "devices.html"))


@app.get("/speed-tests")
def speed_tests_page():
    return FileResponse(os.path.join(STATIC_DIR, "speed-tests.html"))


@app.get("/statistics")
def statistics_page():
    return FileResponse(os.path.join(STATIC_DIR, "statistics.html"))


@app.get("/services")
def services_page():
    return FileResponse(os.path.join(STATIC_DIR, "services.html"))


@app.get("/system")
def system_page():
    return FileResponse(os.path.join(STATIC_DIR, "system.html"))


# ------------------------------------------------------------------- API

@app.get("/api/status")
def status():
    """Current snapshot: internet state, latest latency, last speed test,
    device counts, and whether a speed test is running."""
    outage = db.get_open_event(DB_PATH, "outage")
    online, total = db.device_counts(DB_PATH)
    return {
        "internet_up": outage is None,
        "outage": outage,
        "latency": db.latest_latency(DB_PATH),
        "last_speedtest": db.latest_speedtest(DB_PATH),
        "speedtest_running": collectors.speedtest_running,
        "devices_online": online,
        "devices_total": total,
        "server_time": int(time.time()),
        "started_at": int(START_TIME),
        "uptime_seconds": int(time.time() - START_TIME),
        "version": VERSION,
        "managed": MANAGED,
        # Deco restart (System page): whether the Deco password is set,
        # and the units that can be restarted (config entries with a
        # usable MAC — MAC-less entries are skipped).
        "deco_configured": bool((CFG.get("deco") or {}).get("password")),
        "deco_units": deco.configured_units(CFG),
        # Last/next update check, from the launcher's status file; null
        # when standalone, or when the launcher hasn't written one yet.
        "update_check": (updates.read_update_status(PROJECT_ROOT)
                         if MANAGED else None),
    }


@app.get("/api/devices")
def devices():
    return db.list_devices(DB_PATH)


@app.post("/api/devices/sweep-offline")
def sweep_offline():
    """Delete offline device rows, except devices defined in config.yaml
    (matched by MAC, then IP) — config entries are intentional (notably
    the PC WoL entries) and must survive even when the device is offline.
    Online devices are never touched. The frontend confirm dialog is the
    only guard; there is no confirmation on the backend."""
    by_mac, by_ip = build_device_lookups(CFG)
    removed, kept = db.sweep_offline(DB_PATH, set(by_mac), set(by_ip))
    return {"removed": removed, "kept_config": kept}


@app.get("/api/latency")
def latency(hours: int = 24):
    """Latency history. `hours` caps the window (default 24)."""
    hours = max(1, min(hours, 24 * 30))
    return db.latency_since(DB_PATH, int(time.time()) - hours * 3600)


@app.get("/api/speedtests")
def speedtests(days: int = 7):
    """Speed test history. `days` caps the window (default 7)."""
    days = max(1, min(days, 90))
    return db.speedtests_since(DB_PATH, int(time.time()) - days * 86400)


@app.get("/api/events")
def events(limit: int = 50):
    """Outage/event log, most recent first."""
    return db.list_events(DB_PATH, limit=max(1, min(limit, 200)))


@app.get("/api/services")
def services_status():
    """Current status per configured service: name, url, status
    (up/down/unknown), last check, last response time, and 24h uptime %."""
    return services.current_statuses(CFG, DB_PATH, collectors.svc_failures)


@app.get("/api/services/history")
def services_history(hours: int = 24):
    """Per-service check history. `hours` caps the window (default 24)."""
    hours = max(1, min(hours, 24 * 30))
    return db.service_checks_since(DB_PATH, int(time.time()) - hours * 3600)


@app.post("/api/check-updates")
def check_updates():
    """Ask the launcher to check GitHub for updates now instead of
    waiting for the next periodic check. Gated by the auth middleware
    like every other /api/* route (only /api/login and /api/logout are
    public). 409 when no launcher supervises this process."""
    result = updates.request_update_check(PROJECT_ROOT)
    if result == "no_launcher":
        return JSONResponse(status_code=409, content={"status": "no_launcher"})
    return {"status": result}


# ------------------------------------------------------------- web console

# The web console (System page) sends the launcher's own console
# commands — the same four the launcher accepts from its console window
# (see launcher.py CONSOLE_COMMANDS). Keep the two lists in sync.
CONSOLE_COMMANDS = ("restart", "update", "quit", "help")
CONSOLE_HELP = (
    "Commands: restart — restart the monitor (reloads config); "
    "update — check for updates now; "
    "quit — stop the monitor completely (restart it on the PC with "
    "run_launcher.bat); help — this list.")


@app.post("/api/console/command")
async def console_command(request: Request):
    """Run a launcher console command from the web console (System page).

    SECURITY — HARD RULE: the command is an allowlist lookup ONLY. The
    input is never passed to a shell, subprocess, eval, or exec — there
    is no freeform shell access from the web, ever. restart/update/quit
    only drop a trigger file for the launcher (the same mechanism as
    the footer update button); help just returns the command list.
    restart/update/quit return 409 when no launcher supervises this
    process (a trigger file with no launcher would sit there stale —
    a stale shutdown file would kill the next launcher start)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw = body.get("command") if isinstance(body, dict) else None
    if not isinstance(raw, str):
        raise HTTPException(
            status_code=400,
            detail='Send {"command": "restart" | "update" | "quit" | "help"}.')
    words = raw.strip().lower().split()
    cmd = words[0] if words else None
    if cmd not in CONSOLE_COMMANDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown command {raw.strip()!r} — try restart, update, "
                   "quit, or help.")
    if cmd == "help":
        return {"ok": True, "message": CONSOLE_HELP}
    if cmd == "update":
        result = updates.request_update_check(PROJECT_ROOT)
        if result == "no_launcher":
            raise HTTPException(
                status_code=409,
                detail="No launcher is supervising this monitor — run it "
                       "with run_launcher.bat on the PC.")
        if result == "already_requested":
            return {"ok": True,
                    "message": "Update check already requested — the "
                               "launcher will check shortly."}
        return {"ok": True,
                "message": "Update check requested — the launcher will "
                           "check GitHub now."}
    if cmd == "restart":
        result = updates.request_restart(PROJECT_ROOT)
        if result == "no_launcher":
            raise HTTPException(
                status_code=409,
                detail="No launcher is supervising this monitor — run it "
                       "with run_launcher.bat on the PC.")
        if result == "already_requested":
            return {"ok": True,
                    "message": "Restart already requested — waiting for "
                               "the launcher."}
        return {"ok": True,
                "message": "Restart requested — the monitor will restart "
                           "in a few seconds."}
    # cmd == "quit"
    result = updates.request_shutdown(PROJECT_ROOT)
    if result == "no_launcher":
        raise HTTPException(
            status_code=409,
            detail="No launcher is supervising this monitor — run it "
                   "with run_launcher.bat on the PC.")
    if result == "already_requested":
        return {"ok": True,
                "message": "Shutdown already requested — the monitor is "
                           "stopping."}
    return {"ok": True,
            "message": "Shutdown requested — the monitor is stopping. "
                       "Restart it on the PC with run_launcher.bat."}


@app.get("/api/console/log")
def console_log(lines: int = 100):
    """Tail of the launcher's log file (data/launcher.log) for the web
    console. `lines` caps the tail (default 100, clamped 1–500). A
    missing file (standalone run, or the launcher hasn't logged yet) is
    an empty list, not an error."""
    lines = max(1, min(lines, 500))
    try:
        with open(updates.log_path(PROJECT_ROOT), "r", encoding="utf-8",
                  errors="replace") as f:
            all_lines = f.read().splitlines()
    except OSError:
        return {"lines": []}
    return {"lines": all_lines[-lines:]}


@app.post("/api/deco/reboot")
async def reboot_deco(request: Request):
    """Restart Deco mesh units, individually or in any combination.
    Body: {"units": ["Living Room", "Bedroom"]}. Only the named units
    reboot — the rest of the mesh stays up. Restarting the main unit
    still drops the whole network for ~1-2 min (it's the router); the
    frontend warns about that before sending. Sync endpoint (runs in a
    threadpool): the Deco calls block for a few seconds."""
    cfg_deco = CFG.get("deco") or {}
    password = cfg_deco.get("password") or ""
    if not password:
        return JSONResponse(
            status_code=503,
            content={"error": "deco_not_configured",
                     "detail": "No Deco password set — add deco.password "
                               "(your Deco app password) to "
                               "config.local.yaml, then restart the monitor."})
    try:
        body = await request.json()
    except Exception:
        body = {}
    names = body.get("units") if isinstance(body, dict) else None
    if not isinstance(names, list) or not names:
        raise HTTPException(status_code=400,
                            detail="No units selected — send "
                                   '{"units": ["<name>", ...]}.')
    # Dedupe, keeping the requested order.
    wanted = list(dict.fromkeys(str(n) for n in names))
    units = {u["name"]: u for u in deco.configured_units(CFG)}
    unknown = [n for n in wanted if n not in units]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown Deco unit(s): {', '.join(unknown)}. "
                   f"Configured units: {', '.join(units) or 'none'}.")
    try:
        results = deco.reboot_units(
            cfg_deco.get("host") or "http://192.168.68.1", password,
            [units[n] for n in wanted])
    except deco.DecoUnavailable as exc:
        return JSONResponse(status_code=503,
                            content={"error": "deco_library_missing",
                                     "detail": str(exc)})
    except deco.DecoAuthError as exc:
        return JSONResponse(status_code=502,
                            content={"error": "deco_login_failed",
                                     "detail": str(exc)})
    except deco.DecoUnreachable as exc:
        return JSONResponse(status_code=502,
                            content={"error": "deco_unreachable",
                                     "detail": str(exc)})
    except deco.DecoApiError as exc:
        return JSONResponse(status_code=502,
                            content={"error": "deco_api_error",
                                     "detail": str(exc)})
    return {"results": results}


@app.post("/api/speedtest/run", status_code=202)
def run_speedtest():
    """Start a speed test in the background. Returns immediately; poll
    /api/status (speedtest_running) or /api/speedtests for the result."""
    if not collectors.run_speedtest_async():
        raise HTTPException(status_code=409, detail="Speed test already running")
    return {"status": "started"}


@app.post("/api/wol/{mac}")
def wake_on_lan(mac: str):
    """Send a Wake-on-LAN magic packet for the given MAC address, to both the
    global broadcast address and the subnet broadcast address."""
    clean = normalize_mac(mac)
    if not is_valid_mac(clean):
        raise HTTPException(status_code=400, detail=f"Invalid MAC address: {mac!r}")
    try:
        send_magic_packet(clean, "255.255.255.255")
        send_magic_packet(clean, subnet_broadcast(CFG["subnet"]))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to send magic packet: {exc}")
    return {"status": "sent", "mac": clean}
