"""Wake-on-LAN magic packet sender (stdlib only, no extra dependency)."""
from __future__ import annotations

import socket

from .network import normalize_mac, is_valid_mac


def build_magic_packet(mac: str) -> bytes:
    """6 x 0xFF followed by the target MAC repeated 16 times."""
    clean = normalize_mac(mac)
    if not is_valid_mac(clean):
        raise ValueError(f"Invalid MAC address: {mac!r}")
    mac_bytes = bytes.fromhex(clean.replace(":", ""))
    return b"\xff" * 6 + mac_bytes * 16


def send_magic_packet(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    """Broadcast a WoL magic packet. Raises OSError on socket failure."""
    packet = build_magic_packet(mac)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.sendto(packet, (broadcast, port))
    finally:
        sock.close()
