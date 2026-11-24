"""Down detector: HTTP health checks for the services this network uses.

Each configured service gets a plain HTTP GET (stdlib urllib only) every
`service_check_interval_seconds`. A 2xx/3xx final status counts as up;
4xx/5xx, a timeout, or a DNS/connection failure counts as down.

A service is only *declared* down after 2 consecutive failures, and it
recovers on the first success after that — see Collectors.service_check().
When the internet itself is down (all external latency targets failing),
checks are skipped and services report "unknown" instead of a pile of
false service outages.
"""
from __future__ import annotations

import ipaddress
import time
import urllib.error
import urllib.request

from . import db

# A browser-style User-Agent: some sites block urllib's default one.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NorthStarMonitor/1.0"
DOWN_EVENT_PREFIX = "service_down:"


def check_service(url: str, timeout: float = 10.0) -> tuple[bool, int | None, float | None]:
    """GET a URL once. Returns (up, status_code, response_ms).

    `status_code`/`response_ms` are None when the request never got an
    answer (DNS failure, timeout, connection refused). Only the first
    1 KiB of the body is read — we care about the status, not the page.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1024)
            code = resp.status
    except urllib.error.HTTPError as exc:
        # The server answered with an error status — record the code.
        return False, exc.code, (time.monotonic() - start) * 1000
    except Exception:  # noqa: BLE001 — DNS/timeout/connection: all "down"
        return False, None, None
    ms = (time.monotonic() - start) * 1000
    return 200 <= code < 400, code, ms


def external_latency_down(db_path: str, cfg: dict) -> bool:
    """True when every external latency target's latest probe failed.

    This is the same signal the outage detector uses, read at service-check
    time. With no latency data yet (fresh DB), returns False so checks run.
    """
    latest = db.latest_latency(db_path)
    external = [
        t for t in cfg.get("latency_targets", [])
        if not ipaddress.ip_address(t).is_private
    ]
    readings = [latest[t] for t in external if t in latest]
    if not readings:
        return False
    return all(not r["success"] for r in readings)


def down_event_type(service_name: str) -> str:
    """Event type for a service's down window (one open event per service)."""
    return f"{DOWN_EVENT_PREFIX}{service_name}"


def current_statuses(cfg: dict, db_path: str, failures: dict | None = None) -> list[dict]:
    """Current status per configured service, for GET /api/services.

    Status is one of:
      up      — latest check succeeded (or no down window is open)
      down    — an open service_down event exists (2 consecutive failures)
      unknown — the internet is down, or the service has never been checked
    """
    failures = failures or {}
    outage_open = db.get_open_event(db_path, "outage") is not None
    latest = db.latest_service_checks(db_path)
    since = int(time.time()) - 24 * 3600
    out = []
    for svc in cfg.get("services", []):
        name = svc["name"]
        row = latest.get(name)
        if outage_open:
            status = "unknown"
        elif db.get_open_event(db_path, down_event_type(name)):
            status = "down"
        elif row is None or row["up"] is None:
            status = "unknown"
        else:
            status = "up"
        out.append({
            "name": name,
            "url": svc["url"],
            "status": status,
            "last_check_ts": row["ts"] if row else None,
            "last_response_ms": row["response_ms"] if row else None,
            "last_status_code": row["status_code"] if row else None,
            "last_up": row["up"] if row else None,
            "uptime_24h": db.service_uptime_since(db_path, name, since),
            "consecutive_failures": failures.get(name, 0),
        })
    return out
