"""Smoke test for North Star Monitor (used by GitHub Actions CI).

Starts the server, hits the dashboard and every API endpoint, asserts 200s,
then shuts the server down. Run from the network-monitor/ directory:

    python tests/smoke_test.py
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"
TIMEOUT = 60


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, r.read()


def post_json(path, payload):
    """POST a JSON body. Returns (status, parsed body) — error statuses
    come back as data, not exceptions, so tests can assert on them."""
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main():
    proc = subprocess.Popen(
        [sys.executable, "run.py"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.time() + TIMEOUT
        while True:
            try:
                status, _ = get("/api/status")
                if status == 200:
                    break
            except Exception:
                pass
            if time.time() > deadline:
                print("FAIL: server did not come up in time")
                return 1
            time.sleep(1)
        # /api/status returns a JSON object with version info
        status, body = get("/api/status")
        data = json.loads(body)
        assert isinstance(data, dict), "status is not a JSON object"
        assert "version" in data and isinstance(data["version"], str), \
            "status missing version"
        assert "started_at" in data and isinstance(data["started_at"], int), \
            "status missing started_at"
        # Dashboard loads and carries the title
        status, body = get("/")
        assert status == 200 and b"North Star" in body, "dashboard missing"
        # All five dashboard pages load with their own content
        pages = {
            "/": b"Services at a glance",
            "/devices": b"Sweep offline",
            "/speed-tests": b"Recent speed tests",
            "/statistics": b"Service uptime",
            "/services": b"Vercel sites once a day",
            "/system": b"Restart Deco",
        }
        for path, marker in pages.items():
            status, body = get(path)
            assert status == 200 and marker in body, \
                f"{path} missing marker {marker!r} (status {status})"
        # Shared assets serve (the pages link /static/style.css + app.js)
        for path in ("/static/style.css", "/static/app.js"):
            status, body = get(path)
            assert status == 200 and len(body) > 100, f"{path} -> {status}"
        # No Events page (the Outages & events display was removed in v1.8)
        # and no Network page (split into Speed Tests + Statistics).
        # The /api/events endpoint still exists; only the pages are gone.
        for path in ("/events", "/network"):
            try:
                get(path)
                raise AssertionError(f"{path} should not exist")
            except urllib.error.HTTPError as e:
                assert e.code == 404, f"{path} -> {e.code}, expected 404"
        # Remaining endpoints answer 200
        for path in ("/api/devices", "/api/latency",
                     "/api/speedtests", "/api/events",
                     "/api/services", "/api/services/history"):
            status, _ = get(path)
            assert status == 200, f"{path} -> {status}"
        # /api/services lists the configured services with a status each
        status, body = get("/api/services")
        svcs = json.loads(body)
        assert isinstance(svcs, list) and len(svcs) >= 8, "services list short"
        names = {s["name"] for s in svcs}
        assert "GitHub" in names and "T1 Research" in names, "seeded services missing"
        assert all(s["status"] in ("up", "down", "unknown") for s in svcs), \
            "bad service status"
        # /api/devices rows carry the auto-identification fields
        status, body = get("/api/devices")
        devices = json.loads(body)
        assert isinstance(devices, list), "devices is not a list"
        for d in devices:
            assert "discovered_name" in d and "name_source" in d, \
                "device row missing identification fields"
            assert d["name_source"] in (None, "mdns", "netbios", "rdns", "oui", "unknown"), \
                f"bad name_source: {d['name_source']!r}"
        # /api/status carries the Deco restart data (System page)
        status, body = get("/api/status")
        data = json.loads(body)
        assert "deco_configured" in data and "deco_units" in data, \
            "status missing Deco fields"
        assert data["deco_configured"] is False, \
            "sandbox has no Deco password — deco_configured must be False"
        assert isinstance(data["deco_units"], list) and len(data["deco_units"]) == 3, \
            f"expected 3 Deco units in config, got {data['deco_units']!r}"
        assert {u["name"] for u in data["deco_units"]} == \
            {"Living Room", "Bedroom", "Main Bedroom"}, "Deco unit names wrong"
        assert [u["main"] for u in data["deco_units"]] == [True, False, False], \
            "only Living Room may be main"
        # Deco reboot with no password configured -> clean 503 JSON, no crash
        status, body = post_json("/api/deco/reboot", {"units": ["Bedroom"]})
        assert status == 503, f"deco reboot unconfigured -> {status}, expected 503"
        assert body.get("error") == "deco_not_configured", \
            f"wrong deco error: {body!r}"
        # Empty selection and unknown unit are 400s — but only reachable
        # with a password configured, so the sandbox (no password) gets
        # the 503 first. Those paths are covered in tests/test_deco.py.
        print("Smoke test passed.")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
