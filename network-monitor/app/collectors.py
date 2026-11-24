"""Background collectors: device scans, latency probes, speed tests, service
health checks, and outage detection. Runs in a plain daemon thread with a
simple schedule loop (no extra scheduler dependency).

An outage opens when ALL external latency targets fail in the same probe
round, and closes when any external target succeeds again. A service is
declared down after 2 consecutive failed health checks, and recovers on the
first success after that; while the internet is down, service checks are
skipped and services report "unknown".

Each device scan also auto-identifies devices that have no manual name in
config.yaml (mDNS / NetBIOS / reverse DNS / OUI vendor — see
app/identify.py). Results are cached in memory; a device with a discovered
name is re-probed at most once an hour, while a still-unnamed device is
re-probed on every scan.
"""
from __future__ import annotations

import ipaddress
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import db, identify, notifiers, services
from .config import build_device_lookups
from .network import (
    ip_in_subnet,
    ping_once,
    ping_sweep,
    read_arp_table,
)


class Collectors:
    def __init__(self, cfg: dict, db_path: str):
        self.cfg = cfg
        self.db_path = db_path
        self.by_mac, self.by_ip = build_device_lookups(cfg)
        self._speed_lock = threading.Lock()
        self._speed_running = False
        # Consecutive failed health checks per service (in-memory only; a
        # restart resets the counters, worst case delaying a down alert by
        # one check cycle). Down state itself lives in the events table.
        self._svc_failures: dict[str, int] = {}
        # Auto-identification cache: device key ->
        # (discovered_name, name_source, last_probe_unix_ts). Names rarely
        # change, so a cached non-unknown result is trusted for an hour;
        # "unknown" results are re-probed on every scan (the device may
        # answer next time, e.g. a phone waking up).
        self._identify_cache: dict[str, tuple[str | None, str, float]] = {}

    # ------------------------------------------------------------ helpers

    @property
    def speedtest_running(self) -> bool:
        with self._speed_lock:
            return self._speed_running

    @property
    def svc_failures(self) -> dict[str, int]:
        """Copy of the consecutive-failure counters (for /api/services)."""
        return dict(self._svc_failures)

    def _resolve(self, ip: str, mac: str | None) -> tuple[str | None, bool]:
        """Look up a discovered device in the config. MAC wins over IP."""
        entry = None
        if mac:
            entry = self.by_mac.get(mac)
        if entry is None:
            entry = self.by_ip.get(ip)
        if entry is None:
            return None, False
        return entry.get("name"), bool(entry.get("wol"))

    # ------------------------------------------------------- device scan

    def device_scan(self) -> int:
        """Ping-sweep the subnet, resolve MACs via the ARP table, and update
        the devices table. Returns the number of devices seen online."""
        now = int(time.time())
        live = ping_sweep(self.cfg["subnet"], timeout_ms=400)

        # Also ping named devices that live outside the scanned subnet
        # (e.g. the CAX30 on 192.168.1.1) so they still show up.
        for entry in self.cfg.get("devices", []):
            ip = entry.get("ip")
            if ip and ip not in live and not ip_in_subnet(ip, self.cfg["subnet"]):
                if ping_once(ip, 1000) is not None:
                    live.add(ip)

        arp = read_arp_table()
        seen: set[str] = set()
        candidates: list[tuple[str, str, str | None, str | None]] = []
        for ip in sorted(live):
            mac = arp.get(ip)
            # Devices on another subnet never appear in this machine's ARP
            # table, so key them by IP until/unless a real MAC is learned.
            key = mac if mac else f"ip:{ip}"
            seen.add(key)
            name, wol = self._resolve(ip, mac)
            db.upsert_device(self.db_path, mac=key, ip=ip, name=name, wol=wol, now=now)
            if mac:
                # A real MAC supersedes any earlier synthetic ip: row.
                db.delete_device(self.db_path, f"ip:{ip}")
            candidates.append((key, ip, mac, name))
        db.mark_offline_not_in(self.db_path, seen)
        print(f"[scan] {len(seen)} device(s) online", flush=True)
        self._identify_devices(candidates)
        return len(seen)

    # ---------------------------------------------- auto-identification

    def _identify_devices(self, candidates: list[tuple[str, str, str | None, str | None]]) -> None:
        """Auto-identify devices that have no manual name in config.yaml.

        Manually-named devices are never probed — a config name always wins.
        A cached non-unknown result is trusted for identify.REPROBE_SECONDS;
        still-unnamed devices are re-probed on every scan. One mDNS browse
        is shared across all devices needing a probe; NetBIOS/rDNS run
        concurrently per device. Failures here never break the scan.
        """
        try:
            now = time.time()
            to_probe: list[tuple[str, str, str | None]] = []
            for key, ip, mac, manual_name in candidates:
                if manual_name:
                    continue
                cached = self._identify_cache.get(key)
                if cached and cached[1] != "unknown" and now - cached[2] < identify.REPROBE_SECONDS:
                    continue
                to_probe.append((key, ip, mac))
            if not to_probe:
                return
            mdns_names = identify.browse_mdns(timeout=3.0)
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {
                    pool.submit(identify.identify_device, ip, mac, mdns_names): key
                    for key, ip, mac in to_probe
                }
                for fut, key in futures.items():
                    try:
                        name, source = fut.result()
                    except Exception:  # noqa: BLE001 — one bad probe must not stop the rest
                        name, source = None, "unknown"
                    self._identify_cache[key] = (name, source, now)
                    db.update_discovered_name(self.db_path, key, name, source)
            print(f"[identify] probed {len(to_probe)} device(s)", flush=True)
        except Exception:  # noqa: BLE001 — identification is best-effort
            print("[identify] error:", flush=True)
            traceback.print_exc()

    # ------------------------------------------------------ latency probe

    def latency_probe(self) -> dict[str, float | None]:
        """Ping each target once, store the results, and update outage state."""
        now = int(time.time())
        results: dict[str, float | None] = {}
        for target in self.cfg["latency_targets"]:
            rtt = ping_once(target, 2000)
            results[target] = rtt
            db.insert_latency(self.db_path, now, target, rtt, rtt is not None)

        external = [
            t for t in self.cfg["latency_targets"]
            if not ipaddress.ip_address(t).is_private
        ]
        if external and all(results[t] is None for t in external):
            already_open = db.get_open_event(self.db_path, "outage") is not None
            db.open_event(
                self.db_path, "outage", now,
                "All external latency targets unreachable",
            )
            if not already_open:
                notifiers.notify({
                    "kind": "outage_open", "service": None, "url": None,
                    "ts": now, "note": "All external latency targets unreachable",
                })
            print("[latency] OUTAGE — all external targets down", flush=True)
        else:
            was_open = db.get_open_event(self.db_path, "outage") is not None
            db.close_open_event(self.db_path, "outage", now)
            if was_open:
                notifiers.notify({
                    "kind": "outage_closed", "service": None, "url": None,
                    "ts": now, "note": "An external latency target is responding again",
                })
        return results

    # ----------------------------------------------------- service checks

    def service_check(self) -> None:
        """HTTP health-check every configured service (the down detector).

        A service is declared down after 2 consecutive failures, and
        recovers on the first success after that. Down windows are recorded
        in the events table (one open event per service) and fanned out to
        notifiers. When the internet itself is down, checks are skipped
        (recorded as unknown) — no false service outages.
        """
        svc_list = self.cfg.get("services", [])
        if not svc_list:
            return
        now = int(time.time())
        if services.external_latency_down(self.db_path, self.cfg):
            for svc in svc_list:
                db.insert_service_check(self.db_path, now, svc["name"], None, None, None)
            print("[services] skipped — internet down, services unknown", flush=True)
            return
        for svc in svc_list:
            name, url = svc["name"], svc["url"]
            up, code, ms = services.check_service(url)
            db.insert_service_check(self.db_path, now, name, 1 if up else 0, code, ms)
            etype = services.down_event_type(name)
            if up:
                self._svc_failures[name] = 0
                if db.get_open_event(self.db_path, etype):
                    db.close_open_event(self.db_path, etype, now)
                    notifiers.notify({
                        "kind": "service_recovered", "service": name,
                        "url": url, "ts": now,
                        "note": f"{name} is reachable again",
                    })
                    print(f"[services] RECOVERED: {name}", flush=True)
            else:
                fails = self._svc_failures.get(name, 0) + 1
                self._svc_failures[name] = fails
                if fails >= 2 and not db.get_open_event(self.db_path, etype):
                    db.open_event(self.db_path, etype, now, f"{name} ({url}) unreachable")
                    notifiers.notify({
                        "kind": "service_down", "service": name,
                        "url": url, "ts": now,
                        "note": f"{name} failed {fails} checks in a row"
                        + (f" (HTTP {code})" if code else ""),
                    })
                    print(f"[services] DOWN: {name} ({fails} failures)", flush=True)

    # --------------------------------------------------------- speed test

    def run_speedtest_async(self) -> bool:
        """Start a speed test in a background thread. Returns False if one is
        already running."""
        with self._speed_lock:
            if self._speed_running:
                return False
            self._speed_running = True
        threading.Thread(target=self._speedtest_worker, daemon=True).start()
        return True

    def _speedtest_worker(self) -> None:
        try:
            import speedtest  # provided by the speedtest-cli package

            print("[speedtest] running...", flush=True)
            s = speedtest.Speedtest()
            s.get_best_server()
            s.download()
            s.upload()
            r = s.results.dict()
            server = r.get("server") or {}
            label = f"{server.get('sponsor', '?')} ({server.get('name', '?')})"
            db.insert_speedtest(
                self.db_path,
                int(time.time()),
                r["download"] / 1e6,
                r["upload"] / 1e6,
                r["ping"],
                label,
            )
            print(f"[speedtest] done: {r['download']/1e6:.1f} down / "
                  f"{r['upload']/1e6:.1f} up Mbps, ping {r['ping']:.0f} ms", flush=True)
        except Exception as exc:  # noqa: BLE001 — log and keep the loop alive
            print(f"[speedtest] failed: {exc}", flush=True)
        finally:
            with self._speed_lock:
                self._speed_running = False

    # ------------------------------------------------------- main loop

    def run_forever(self) -> None:
        """Simple scheduler loop. Each job is guarded so one failure never
        kills the loop."""
        # Immediate first run so the dashboard is not empty on startup.
        # Latency runs before the service check so the internet signal exists.
        self._safe(self.device_scan)
        self._safe(self.latency_probe)
        self._safe(self.service_check)

        next_scan = time.time() + self.cfg["scan_interval_seconds"]
        next_lat = time.time() + self.cfg["latency_interval_seconds"]
        next_speed = time.time() + self.cfg["speedtest_interval_seconds"]
        next_svc = time.time() + self.cfg["service_check_interval_seconds"]
        while True:
            now = time.time()
            if now >= next_lat:
                self._safe(self.latency_probe)
                next_lat = now + self.cfg["latency_interval_seconds"]
            if now >= next_scan:
                self._safe(self.device_scan)
                next_scan = now + self.cfg["scan_interval_seconds"]
            if now >= next_svc:
                self._safe(self.service_check)
                next_svc = now + self.cfg["service_check_interval_seconds"]
            if now >= next_speed:
                self.run_speedtest_async()
                next_speed = now + self.cfg["speedtest_interval_seconds"]
            time.sleep(5)

    @staticmethod
    def _safe(fn) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 — log and keep the loop alive
            print(f"[{fn.__name__}] error:", flush=True)
            traceback.print_exc()
