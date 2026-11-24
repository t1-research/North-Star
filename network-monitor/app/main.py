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

from . import auth, db, services
from .collectors import Collectors
from .config import DB_PATH, PROJECT_ROOT, git_short_sha, load_config
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

collectors = Collectors(CFG, DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(DB_PATH)
    thread = threading.Thread(target=collectors.run_forever, daemon=True)
    thread.start()
    yield


app = FastAPI(title="North Star Monitor", lifespan=lifespan)


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
    }


@app.get("/api/devices")
def devices():
    return db.list_devices(DB_PATH)


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
