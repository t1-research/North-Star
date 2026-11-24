"""SQLite storage (stdlib sqlite3). One short-lived connection per call, with
WAL mode, so the collector threads and the web server never step on each other.

Tables:
  devices    — one row per known device, keyed by MAC. Devices whose MAC
               cannot be learned (e.g. the CAX30, which sits on another
               subnet and never appears in this machine's ARP table) use a
               synthetic key "ip:<address>". The dashboard hides those keys.
               `name` is the manual name from config.yaml (always wins);
               `discovered_name` / `name_source` hold the auto-identified
               name and how it was found (mdns / netbios / rdns / oui /
               unknown) — see app/identify.py.
  latency    — one row per probe per target (rtt_ms NULL on failure).
  speedtests — one row per completed speed test.
  events     — outage windows (and, later, other event types). end_ts is
               NULL while the event is still open.
  service_checks — one row per service health check (down detector).
               up is 1/0, or NULL when the check was skipped because the
               internet itself was down (service state "unknown").
  service_status — latest official status-feed state per service (one row
               per service, overwritten on every successful feed fetch).
               Display only: the feed never drives up/down state.
               incidents_json holds a JSON list of {name, status, impact}.
"""
from __future__ import annotations

import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    mac       TEXT PRIMARY KEY,
    ip        TEXT,
    name      TEXT,
    online    INTEGER NOT NULL DEFAULT 0,
    last_seen INTEGER,
    first_seen INTEGER,
    wol       INTEGER NOT NULL DEFAULT 0,
    discovered_name TEXT,
    name_source TEXT
);
CREATE TABLE IF NOT EXISTS latency (
    ts      INTEGER NOT NULL,
    target  TEXT NOT NULL,
    rtt_ms  REAL,
    success INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_latency_ts ON latency(ts);
CREATE TABLE IF NOT EXISTS speedtests (
    ts            INTEGER NOT NULL,
    download_mbps REAL,
    upload_mbps   REAL,
    ping_ms       REAL,
    server        TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    type     TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts   INTEGER,
    note     TEXT
);
CREATE TABLE IF NOT EXISTS service_checks (
    ts          INTEGER NOT NULL,
    service     TEXT NOT NULL,
    up          INTEGER,
    status_code INTEGER,
    response_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_service_checks_ts ON service_checks(ts);
CREATE INDEX IF NOT EXISTS idx_service_checks_service ON service_checks(service);
CREATE TABLE IF NOT EXISTS service_status (
    service      TEXT PRIMARY KEY,
    ts           INTEGER NOT NULL,
    indicator    TEXT NOT NULL,
    description  TEXT NOT NULL,
    incidents_json TEXT NOT NULL DEFAULT '[]'
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that older databases predate. Safe to run on every
    startup: each ALTER runs only when its column is missing, and existing
    rows keep their data."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    if "discovered_name" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN discovered_name TEXT")
    if "name_source" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN name_source TEXT")


# ---------------------------------------------------------------- devices

def upsert_device(db_path, mac, ip, name, wol, now):
    """Insert or refresh a device row. `now` is a unix timestamp (seconds)."""
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO devices (mac, ip, name, online, last_seen, first_seen, wol)
               VALUES (?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(mac) DO UPDATE SET
                   ip = excluded.ip,
                   name = excluded.name,
                   online = 1,
                   last_seen = excluded.last_seen,
                   wol = excluded.wol""",
            (mac, ip, name, now, now, 1 if wol else 0),
        )


def delete_device(db_path, mac):
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM devices WHERE mac = ?", (mac,))


def sweep_offline(db_path, keep_macs, keep_ips):
    """Delete offline device rows, except rows matching keep_macs (MACs,
    case-insensitive) or keep_ips — those are config-defined devices and
    must survive even when offline. Online devices are never touched.
    Returns (removed, kept)."""
    keep_macs = {m.upper() for m in keep_macs}
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT mac, ip FROM devices WHERE online = 0").fetchall()
        removed = kept = 0
        for row in rows:
            mac, ip = row["mac"], row["ip"]
            if (mac and mac.upper() in keep_macs) or (ip and ip in keep_ips):
                kept += 1
                continue
            conn.execute("DELETE FROM devices WHERE mac = ?", (mac,))
            removed += 1
        return removed, kept


def update_discovered_name(db_path, mac, discovered_name, name_source):
    """Store an auto-identified name for a device. Never touches the manual
    `name` column — a config.yaml name always wins at display time."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE devices SET discovered_name = ?, name_source = ?"
            " WHERE mac = ?",
            (discovered_name, name_source, mac),
        )


def mark_offline_not_in(db_path, seen_macs):
    """Mark every device not in `seen_macs` as offline."""
    with _connect(db_path) as conn:
        if seen_macs:
            placeholders = ",".join("?" for _ in seen_macs)
            conn.execute(
                f"UPDATE devices SET online = 0 WHERE mac NOT IN ({placeholders})",
                tuple(seen_macs),
            )
        else:
            conn.execute("UPDATE devices SET online = 0")


def list_devices(db_path):
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM devices ORDER BY online DESC, name, ip"
        ).fetchall()
    return [dict(r) for r in rows]


def device_counts(db_path):
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(online), 0) AS online FROM devices"
        ).fetchone()
    return row["online"], row["total"]


# ---------------------------------------------------------------- latency

def insert_latency(db_path, ts, target, rtt_ms, success):
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO latency (ts, target, rtt_ms, success) VALUES (?, ?, ?, ?)",
            (ts, target, rtt_ms, 1 if success else 0),
        )


def latency_since(db_path, since_ts, limit=20000):
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ts, target, rtt_ms, success FROM latency"
            " WHERE ts >= ? ORDER BY ts LIMIT ?",
            (since_ts, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_latency(db_path):
    """Latest reading per target: {target: {rtt_ms, ts, success}}."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT l.target, l.rtt_ms, l.ts, l.success FROM latency l
               JOIN (SELECT target, MAX(ts) AS m FROM latency GROUP BY target) m
                 ON l.target = m.target AND l.ts = m.m"""
        ).fetchall()
    return {
        r["target"]: {"rtt_ms": r["rtt_ms"], "ts": r["ts"], "success": bool(r["success"])}
        for r in rows
    }


# -------------------------------------------------------------- speedtests

def insert_speedtest(db_path, ts, download_mbps, upload_mbps, ping_ms, server):
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO speedtests (ts, download_mbps, upload_mbps, ping_ms, server)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, download_mbps, upload_mbps, ping_ms, server),
        )


def speedtests_since(db_path, since_ts):
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM speedtests WHERE ts >= ? ORDER BY ts",
            (since_ts,),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_speedtest(db_path):
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM speedtests ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------ events

def get_open_event(db_path, event_type):
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE type = ? AND end_ts IS NULL"
            " ORDER BY start_ts DESC LIMIT 1",
            (event_type,),
        ).fetchone()
    return dict(row) if row else None


def open_event(db_path, event_type, start_ts, note=None):
    """Open an event of this type if one is not already open."""
    if get_open_event(db_path, event_type):
        return
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO events (type, start_ts, end_ts, note) VALUES (?, ?, NULL, ?)",
            (event_type, start_ts, note),
        )


def close_open_event(db_path, event_type, end_ts):
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET end_ts = ? WHERE type = ? AND end_ts IS NULL",
            (end_ts, event_type),
        )


def list_events(db_path, limit=50):
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY start_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------- service_checks

def insert_service_check(db_path, ts, service, up, status_code, response_ms):
    """Record one health check. `up` is 1/0, or None when the check was
    skipped because the internet itself was down."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO service_checks (ts, service, up, status_code, response_ms)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, service, up, status_code, response_ms),
        )


def latest_service_checks(db_path):
    """Latest reading per service: {name: {ts, up, status_code, response_ms}}."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT s.service, s.ts, s.up, s.status_code, s.response_ms
               FROM service_checks s
               JOIN (SELECT service, MAX(ts) AS m FROM service_checks GROUP BY service) m
                 ON s.service = m.service AND s.ts = m.m"""
        ).fetchall()
    return {r["service"]: dict(r) for r in rows}


def service_uptime_since(db_path, service, since_ts):
    """Uptime % for one service since `since_ts`, over measured checks only
    (rows skipped during an internet outage carry up=NULL and don't count).
    Returns None when there are no measured checks."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(up), 0) AS up_count"
            " FROM service_checks WHERE service = ? AND ts >= ? AND up IS NOT NULL",
            (service, since_ts),
        ).fetchone()
    if not row["total"]:
        return None
    return round(100.0 * row["up_count"] / row["total"], 1)


def service_checks_since(db_path, since_ts, limit=20000):
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ts, service, up, status_code, response_ms FROM service_checks"
            " WHERE ts >= ? ORDER BY ts LIMIT ?",
            (since_ts, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------- service_status

def upsert_service_status(db_path, service, ts, indicator, description, incidents):
    """Store the latest official status-feed state for one service.
    Overwrites the previous row — latest state only, no history."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO service_status (service, ts, indicator, description, incidents_json)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(service) DO UPDATE SET"
            " ts = excluded.ts, indicator = excluded.indicator,"
            " description = excluded.description,"
            " incidents_json = excluded.incidents_json",
            (service, ts, indicator, description, json.dumps(incidents)),
        )


def latest_service_status(db_path):
    """Latest feed state per service:
    {name: {ts, indicator, description, incidents: [{name, status, impact}]}}.
    Services with no successful feed fetch yet are absent."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM service_status").fetchall()
    out = {}
    for r in rows:
        try:
            incidents = json.loads(r["incidents_json"])
            if not isinstance(incidents, list):
                incidents = []
        except (ValueError, TypeError):
            incidents = []
        out[r["service"]] = {
            "ts": r["ts"],
            "indicator": r["indicator"],
            "description": r["description"],
            "incidents": incidents,
        }
    return out
