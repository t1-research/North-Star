"""Cross-platform network helpers (Windows + Linux).

Everything here uses subprocess (ping / arp / ip) instead of raw sockets, so
no admin privileges are required. MAC addresses are normalized to uppercase
colon form, e.g. "F8:3D:C6:9B:CA:5A".
"""
from __future__ import annotations

import ipaddress
import platform
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

IS_WINDOWS = platform.system() == "Windows"

# Matches "time=12ms", "time=12.3 ms" and "time<1ms" (Windows + Linux ping).
_RTT_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)

# Windows `arp -a`: "  192.168.68.1          a8-6e-84-f6-bb-3e     dynamic"
_ARP_WIN_RE = re.compile(r"^\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F-]{17})\s+", re.MULTILINE)
# Linux `ip neigh`: "192.168.68.1 dev eth0 lladdr a8:6e:84:f6:bb:3e REACHABLE"
_IP_NEIGH_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+).*lladdr\s+([0-9a-fA-F:]{17})", re.MULTILINE)
# Linux `arp -n`: "192.168.68.1             ether   a8:6e:84:f6:bb:3e   C ..."
_ARP_LIN_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+)\s+\S+\s+([0-9a-fA-F:]{17})", re.MULTILINE)

_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


def normalize_mac(mac: str) -> str:
    """'a8-6e-84-f6-bb-3e' -> 'A8:6E:84:F6:BB:3E'."""
    return mac.strip().upper().replace("-", ":")


def is_valid_mac(mac: str) -> bool:
    return bool(_MAC_RE.match(normalize_mac(mac)))


def ping_once(host: str, timeout_ms: int = 1000) -> float | None:
    """Ping once. Returns RTT in ms, or None if the host did not respond."""
    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
    else:
        # Linux -W is whole seconds, minimum 1.
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), host]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=(timeout_ms / 1000) + 5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    match = _RTT_RE.search(proc.stdout)
    if not match:
        return None
    return float(match.group(1))


def ping_sweep(subnet: str, timeout_ms: int = 400, workers: int = 32) -> set[str]:
    """Ping every host in the subnet concurrently. Returns the set of live IPs.

    The sweep also refreshes the OS ARP cache, which read_arp_table() then
    reads to resolve live IPs to MAC addresses.
    """
    net = ipaddress.ip_network(subnet, strict=False)
    hosts = [str(h) for h in net.hosts()]

    def _try(host: str) -> str | None:
        return host if ping_once(host, timeout_ms) is not None else None

    live: set[str] = set()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(_try, hosts):
            if result:
                live.add(result)
    return live


def _run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return proc.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def read_arp_table() -> dict[str, str]:
    """Return {ip: mac} from the OS ARP / neighbour table."""
    if IS_WINDOWS:
        out = _run(["arp", "-a"])
        return {
            ip: normalize_mac(mac)
            for ip, mac in _ARP_WIN_RE.findall(out)
        }
    # Linux: prefer `ip neigh`, fall back to `arp -n`.
    out = _run(["ip", "neigh"])
    table = {
        ip: normalize_mac(mac)
        for ip, mac in _IP_NEIGH_RE.findall(out)
    }
    if not table:
        out = _run(["arp", "-n"])
        table = {
            ip: normalize_mac(mac)
            for ip, mac in _ARP_LIN_RE.findall(out)
        }
    return table


def ip_in_subnet(ip: str, subnet: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return False


def subnet_broadcast(subnet: str) -> str:
    return str(ipaddress.ip_network(subnet, strict=False).broadcast_address)
