"""Down detector: HTTP health checks for the services this network uses.

Each configured service gets a HEAD request (no body downloaded) every
interval — its own `interval_seconds` when set, else the global
`service_check_interval_seconds`. A 2xx/3xx final status counts as up;
4xx/5xx, a timeout, or a DNS/connection failure counts as down. When HEAD
doesn't give a clean up answer (some servers refuse HEAD or answer it
oddly), the check falls back to a GET capped at the first 1 KiB, so the
down decision is always based on a real page fetch.

A service is only *declared* down after 2 consecutive failures, and it
recovers on the first success after that — see Collectors.service_check().
When the internet itself is down (all external latency targets failing),
checks are skipped and services report "unknown" instead of a pile of
false service outages.

Services with a `status_api_url` in config also get their vendor's
official status feed fetched (Atlassian Statuspage /api/v2/summary.json
format). The feed is display only: it never affects up/down state, down
events, the 2-failure rule, or uptime %. It answers "does the vendor
itself report a problem?" next to our own "is it reachable from here?"
"""
from __future__ import annotations

import ipaddress
import json
import time
import urllib.error
import urllib.request

from . import db

# A browser-style User-Agent: some sites block urllib's default one.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NorthStarMonitor/1.0"
DOWN_EVENT_PREFIX = "service_down:"


def service_interval(svc: dict, cfg: dict) -> int:
    """Check interval for one service (seconds): its own `interval_seconds`
    when set, else the global `service_check_interval_seconds` default."""
    return int(svc.get("interval_seconds",
                       cfg.get("service_check_interval_seconds", 120)))


def _request_once(url: str, method: str, timeout: float) -> tuple[bool, int | None, float | None]:
    """One HTTP request. Returns (up, status_code, response_ms).

    `status_code`/`response_ms` are None when the request never got an
    answer (DNS failure, timeout, connection refused). A GET reads only
    the first 1 KiB of the body — we care about the status, not the page.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT},
                                 method=method)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if method == "GET":
                resp.read(1024)
            code = resp.status
    except urllib.error.HTTPError as exc:
        # The server answered with an error status — record the code.
        return False, exc.code, (time.monotonic() - start) * 1000
    except Exception:  # noqa: BLE001 — DNS/timeout/connection: all "down"
        return False, None, None
    ms = (time.monotonic() - start) * 1000
    return 200 <= code < 400, code, ms


def check_service(url: str, timeout: float = 10.0) -> tuple[bool, int | None, float | None]:
    """HEAD first (no body downloaded — keeps usage down on metered hosts);
    fall back to a 1 KiB-capped GET when HEAD doesn't give a clean up
    answer, so the down decision always rests on a real fetch."""
    up, code, ms = _request_once(url, "HEAD", timeout)
    if up:
        return up, code, ms
    return _request_once(url, "GET", timeout)


# --------------------------------------------------- official status feeds

# Atlassian Statuspage indicator values, worst first.
STATUS_INDICATORS = ("none", "maintenance", "minor", "major", "critical")
MAX_INCIDENTS = 3  # most recent open incidents kept per feed fetch


def _parse_status_feed(data) -> dict | None:
    """Parse a Statuspage /api/v2/summary.json payload into
    {indicator, description, incidents: [{name, status, impact}]}.
    Returns None when the payload isn't usable (wrong shape, missing
    status block). Never raises."""
    try:
        if not isinstance(data, dict):
            return None
        status = data.get("status")
        if not isinstance(status, dict):
            return None
        indicator = status.get("indicator")
        description = status.get("description")
        if not isinstance(indicator, str) or not isinstance(description, str):
            return None
        if indicator not in STATUS_INDICATORS:
            return None
        incidents = data.get("incidents", [])
        if not isinstance(incidents, list):
            return None
        parsed = []
        for inc in incidents:
            if not isinstance(inc, dict):
                continue
            name = inc.get("name")
            if not isinstance(name, str):
                continue
            parsed.append({
                "name": name,
                "status": inc.get("status") if isinstance(inc.get("status"), str) else "",
                "impact": inc.get("impact") if isinstance(inc.get("impact"), str) else "",
                "created_at": inc.get("created_at") if isinstance(inc.get("created_at"), str) else "",
            })
        # Most recent first (Statuspage usually already orders this way;
        # sort defensively on created_at, which is ISO-8601).
        parsed.sort(key=lambda i: i["created_at"], reverse=True)
        for inc in parsed:
            del inc["created_at"]
        return {
            "indicator": indicator,
            "description": description,
            "incidents": parsed[:MAX_INCIDENTS],
        }
    except Exception:  # noqa: BLE001 — any malformed payload is "no feed"
        return None


def fetch_status_feed(url: str, timeout: float = 10.0) -> dict | None:
    """Fetch + parse one vendor's official status feed (Statuspage JSON).
    Returns {indicator, description, incidents} or None on any failure
    (timeout, non-200, bad JSON, unusable payload). Never raises — a
    failed fetch keeps the last known feed state (see collectors)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT},
                                 method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            raw = resp.read(1_000_000)  # feeds are small; cap at 1 MB
        return _parse_status_feed(json.loads(raw.decode("utf-8")))
    except Exception:  # noqa: BLE001 — DNS/timeout/HTTP/JSON: all "no feed"
        return None


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
    feeds = db.latest_service_status(db_path)
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
        feed = feeds.get(name)
        out.append({
            "name": name,
            "url": svc["url"],
            "status": status,
            "interval_seconds": service_interval(svc, cfg),
            "last_check_ts": row["ts"] if row else None,
            "last_response_ms": row["response_ms"] if row else None,
            "last_status_code": row["status_code"] if row else None,
            "last_up": row["up"] if row else None,
            "uptime_24h": db.service_uptime_since(db_path, name, since),
            "consecutive_failures": failures.get(name, 0),
            # Official status feed (display only — never drives up/down).
            "status_feed": None if feed is None else {
                "indicator": feed["indicator"],
                "description": feed["description"],
                "incidents": [
                    {"name": i.get("name"), "status": i.get("status")}
                    for i in feed["incidents"]
                ],
                "fetched_ts": feed["ts"],
            },
            # Display-only link; the monitor never fetches Downdetector.
            "downdetector_url": svc.get("downdetector_url"),
        })
    return out
