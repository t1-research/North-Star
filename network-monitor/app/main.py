"""North Star Monitor — FastAPI backend + dashboard server.

LAN-only by design: no auth, no internet exposure. Never port-forward this.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from . import db, services
from .collectors import Collectors
from .config import DB_PATH, PROJECT_ROOT, git_short_sha, load_config
from .network import is_valid_mac, normalize_mac, subnet_broadcast
from .wol import send_magic_packet

CFG = load_config()
START_TIME = time.time()
VERSION = git_short_sha()  # short commit SHA at startup, or "dev"
STATIC_DIR = os.path.join(PROJECT_ROOT, "static")

collectors = Collectors(CFG, DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(DB_PATH)
    thread = threading.Thread(target=collectors.run_forever, daemon=True)
    thread.start()
    yield


app = FastAPI(title="North Star Monitor", lifespan=lifespan)


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
