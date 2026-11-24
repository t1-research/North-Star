"""Automatic device identification for North Star Monitor.

Devices already broadcast identifying info on the LAN; this module harvests
it so the dashboard can name devices without manual config.yaml entries.

Priority (highest first):
  1. manual  — a name in config.yaml. Handled by the caller (collectors.py):
               manually-named devices are never probed and never overwritten.
  2. mdns    — mDNS / Bonjour service instance name, e.g. "Brayden's iPhone"
               (Apple devices, printers, some TVs and IoT gear broadcast
               these). Uses the `zeroconf` package; if the import fails,
               mDNS is skipped and the other methods still run.
  3. netbios — Windows computer name via an NBSTAT query on UDP 137
               (stdlib socket/struct only).
  4. rdns    — reverse DNS hostname (PTR lookup against the system resolver).
  5. oui     — vendor label from the MAC prefix, e.g. "Apple device"
               (offline oui.json lookup, no network calls).
  6. unknown — nothing answered.

All lookups have short timeouts (mDNS browse ~3s shared across devices,
NetBIOS/rDNS ~1s each) and run concurrently where sensible, so a scan never
waits long on a silent device.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time

# Re-probe a device that already has a discovered name at most this often.
# Devices still stuck at "unknown" are re-probed on every scan instead.
REPROBE_SECONDS = 3600

# mDNS service types to browse, in naming-preference order: the types that
# advertise friendly device names first, generic ones last. Only the first
# (highest-priority) name found per IP is kept.
MDNS_TYPES = [
    "_companion-link._tcp.local.",
    "_airplay._tcp.local.",
    "_raop._tcp.local.",
    "_homekit._tcp.local.",
    "_printer._tcp.local.",
    "_http._tcp.local.",
    "_ssh._tcp.local.",
    "_smb._tcp.local.",
    "_services._dns-sd._udp.local.",
]

_OUI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oui.json")
_oui_cache: dict[str, str] | None = None

try:
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    _ZEROCONF_AVAILABLE = True
except ImportError:  # noqa: F401 — mDNS is optional; everything else works
    _ZEROCONF_AVAILABLE = False
    print("[identify] zeroconf not installed — mDNS names disabled "
          "(pip install -r requirements.txt to enable)", flush=True)


# ---------------------------------------------------------------------- OUI

def load_oui() -> dict[str, str]:
    """Load the offline OUI table (cached after the first call)."""
    global _oui_cache
    if _oui_cache is None:
        try:
            with open(_OUI_PATH, "r", encoding="utf-8") as f:
                _oui_cache = json.load(f)
        except (OSError, ValueError):
            _oui_cache = {}
    return _oui_cache


def vendor_for_mac(mac: str | None) -> str | None:
    """Vendor name for a MAC address, e.g. 'A8:6E:84:F6:BB:3E' -> 'TP-Link'.
    Returns None for unknown prefixes (and for synthetic 'ip:' keys)."""
    if not mac or mac.startswith("ip:"):
        return None
    prefix = mac.strip().upper().replace("-", ":")[:8]
    if len(prefix) != 8:
        return None
    return load_oui().get(prefix)


# -------------------------------------------------------------- reverse DNS

def reverse_dns(ip: str, timeout: float = 1.0) -> str | None:
    """PTR lookup for an IP. Returns the hostname, or None on failure.

    socket.gethostbyaddr() has no timeout of its own, so it runs in a
    throwaway daemon thread and we give up after `timeout` seconds.
    A trailing '.local' / '.' is stripped for display ("Brayden-PC.local"
    becomes "Brayden-PC").
    """
    result: dict[str, str] = {}

    def _lookup() -> None:
        try:
            result["name"] = socket.gethostbyaddr(ip)[0]
        except Exception:  # noqa: BLE001 — no PTR record, DNS down, etc.
            pass

    thread = threading.Thread(target=_lookup, daemon=True)
    thread.start()
    thread.join(timeout)
    name = result.get("name")
    if not name or name == ip:
        return None
    name = name.rstrip(".")
    if name.endswith(".local"):
        name = name[: -len(".local")]
    return name or None


# ------------------------------------------------------------------ NetBIOS

def _encode_netbios_name(name16: bytes) -> bytes:
    """Encode a 16-byte NetBIOS name (15 chars + suffix) into the 32-byte
    wire form: each byte becomes two ASCII chars, 'A' + high nibble and
    'A' + low nibble."""
    out = bytearray()
    for b in name16:
        out.append(0x41 + (b >> 4))
        out.append(0x41 + (b & 0x0F))
    return bytes(out)


def build_nbstat_request(txid: int) -> bytes:
    """Build an NBSTAT request (RFC 1002) for the wildcard name '*'.

    Header: transaction id, flags 0, 1 question, no answers/authority/
    additional. Question: encoded '*' name, type 0x0021 (NBSTAT),
    class 0x0001 (IN).
    """
    name = b"*" + b" " * 14 + b"\x00"  # '*' + padding + 0x00 suffix
    qname = bytes([0x20]) + _encode_netbios_name(name) + b"\x00"
    header = struct.pack(">HHHHHH", txid & 0xFFFF, 0x0000, 1, 0, 0, 0)
    return header + qname + struct.pack(">HH", 0x0021, 0x0001)


def parse_nbstat_response(data: bytes, txid: int) -> str | None:
    """Parse an NBSTAT response, returning the computer (workstation) name.

    The response carries a list of 18-byte name entries (15-char name +
    suffix byte + 2 flag bytes). The computer name is the unique (non-group)
    entry with suffix 0x00; suffixes 0x20 (server) and 0x03 (messenger)
    carry the same name and are the fallback.
    """
    if len(data) < 12:
        return None
    r_txid = struct.unpack(">H", data[0:2])[0]
    if r_txid != (txid & 0xFFFF):
        return None
    qdcount, ancount = struct.unpack(">HH", data[4:8])
    if qdcount != 1 or ancount < 1:
        return None

    # Skip the question section: length-prefixed name labels + type/class.
    off = 12
    while off < len(data) and data[off] != 0:
        off += 1 + data[off]
    off += 1 + 4  # null terminator + qtype/qclass
    if off >= len(data):
        return None

    # Answer name: usually a compression pointer (0xC0 0x0C), else labels.
    if data[off] & 0xC0 == 0xC0:
        off += 2
    else:
        while off < len(data) and data[off] != 0:
            off += 1 + data[off]
        off += 1
    if off + 10 > len(data):
        return None
    rtype, _rclass, _ttl, _rdlen = struct.unpack(">HHIH", data[off:off + 10])
    off += 10
    if rtype != 0x0021 or off >= len(data):
        return None

    num_names = data[off]
    off += 1
    names: list[tuple[str, int, int]] = []
    for _ in range(num_names):
        if off + 18 > len(data):
            break
        raw = data[off:off + 15]
        suffix = data[off + 15]
        flags = struct.unpack(">H", data[off + 16:off + 18])[0]
        names.append((raw.decode("ascii", "replace").strip(), suffix, flags))
        off += 18

    for name, suffix, flags in names:
        if suffix == 0x00 and not (flags & 0x8000) and name:
            return name
    for name, suffix, flags in names:
        if suffix in (0x20, 0x03) and not (flags & 0x8000) and name:
            return name
    return None


def netbios_name(ip: str, timeout: float = 1.0) -> str | None:
    """Ask a host for its NetBIOS computer name (UDP 137 NBSTAT).
    Returns the name, or None if the host doesn't answer (most non-Windows
    devices don't run NetBIOS at all)."""
    txid = int(time.time() * 1000) & 0xFFFF
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(build_nbstat_request(txid), (ip, 137))
        data, _addr = sock.recvfrom(2048)
    except (OSError, socket.timeout):
        return None
    finally:
        sock.close()
    return parse_nbstat_response(data, txid)


# ---------------------------------------------------------------------- mDNS

if _ZEROCONF_AVAILABLE:

    class _MdnsListener(ServiceListener):
        """Collects {ip: (type_priority, instance_name)} from browsers."""

        def __init__(self, type_priority: dict[str, int]):
            self.type_priority = type_priority
            self.found: dict[str, tuple[int, str]] = {}
            self._lock = threading.Lock()

        def _record(self, zc: "Zeroconf", type_: str, name: str) -> None:
            try:
                info = zc.get_service_info(type_, name, timeout=2000)
            except Exception:  # noqa: BLE001 — a flaky service must not break the browse
                return
            if not info or not info.addresses:
                return
            # "Brayden's iPhone._airplay._tcp.local." -> "Brayden's iPhone"
            instance = name
            suffix = "." + type_
            if instance.endswith(suffix):
                instance = instance[: -len(suffix)]
            instance = instance.rstrip(".").strip()
            if not instance:
                return
            prio = self.type_priority.get(type_, len(self.type_priority))
            with self._lock:
                for addr in info.addresses:
                    if len(addr) != 4:  # IPv4 only — this monitor is IPv4
                        continue
                    ip = socket.inet_ntoa(addr)
                    prev = self.found.get(ip)
                    if prev is None or prio < prev[0]:
                        self.found[ip] = (prio, instance)

        def add_service(self, zc: "Zeroconf", type_: str, name: str) -> None:
            self._record(zc, type_, name)

        def remove_service(self, zc: "Zeroconf", type_: str, name: str) -> None:
            pass

        def update_service(self, zc: "Zeroconf", type_: str, name: str) -> None:
            self._record(zc, type_, name)


def browse_mdns(timeout: float = 3.0, service_types: list[str] | None = None) -> dict[str, str]:
    """Browse mDNS for `timeout` seconds. Returns {ip: instance_name}.

    One browse covers the whole LAN, so the caller runs this once per scan
    round and shares the result across devices. Returns {} when zeroconf
    is unavailable or the browse fails — callers treat that as "no mDNS
    names this round" and fall through to the other methods.
    """
    if not _ZEROCONF_AVAILABLE:
        return {}
    types = service_types or MDNS_TYPES
    priority = {t: i for i, t in enumerate(types)}
    listener = _MdnsListener(priority)
    try:
        zc = Zeroconf()
    except Exception:  # noqa: BLE001 — no multicast-capable interface, etc.
        return {}
    browsers = []
    try:
        for type_ in types:
            try:
                browsers.append(ServiceBrowser(zc, type_, listener))
            except Exception:  # noqa: BLE001 — one bad type must not stop the rest
                continue
        time.sleep(timeout)
    finally:
        for browser in browsers:
            try:
                browser.cancel()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
        try:
            zc.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
    return {ip: name for ip, (_prio, name) in listener.found.items()}


# ------------------------------------------------------------ identification

def identify_device(ip: str, mac: str | None, mdns_names: dict[str, str] | None = None) -> tuple[str | None, str]:
    """Identify one device. Returns (discovered_name, source) where source
    is one of mdns / netbios / rdns / oui / unknown. The name is None when
    the source is unknown.

    `mdns_names` is the shared browse result from browse_mdns() for this
    scan round (pass None to skip mDNS for this device).
    """
    if mdns_names and ip in mdns_names:
        return mdns_names[ip], "mdns"
    nb = netbios_name(ip)
    if nb:
        return nb, "netbios"
    rd = reverse_dns(ip)
    if rd:
        return rd, "rdns"
    vendor = vendor_for_mac(mac)
    if vendor:
        return f"{vendor} device", "oui"
    return None, "unknown"
