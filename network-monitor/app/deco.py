"""Restart Deco mesh units — individually, or in any combination.

How it works: the Deco web API reboots by MAC list. One login to the
MAIN unit, one call naming only the selected units' MACs:

    POST admin/device?form=system
    {"operation": "reboot", "params": {"mac_list": [{"mac": ...}, ...]}}

Units not in the list are untouched — restart both satellites and the
main never reboots. (tplinkrouterc6u's own reboot() sends the whole
mesh's MAC list, which is why it restarts everything; we call the same
endpoint with a subset instead. This is the same mechanism the Deco
app's own "Select Deco" reboot screen uses.)

The library (tplinkrouterc6u) is imported lazily, inside reboot_units —
a missing install must never take the monitor down; the endpoint just
reports "library not installed". The Deco web UI allows only ONE
logged-in session at a time, so we always logout in a finally block.

Caveat: the WB10800 is not on tplinkrouterc6u's tested list. If its
firmware answers these endpoints differently, the errors below say so
(login failed vs unreachable vs unexpected response) instead of
failing silently.
"""
from __future__ import annotations

import json

from .network import is_valid_mac, normalize_mac

# The Deco kills its own web server as it restarts, so the reboot call
# may never get a reply. 15s is plenty on a LAN and keeps a hung call
# from blocking the dashboard for the library's 30s default.
DECO_TIMEOUT_SECONDS = 15


class DecoError(Exception):
    """Base class; `code` is the short error id the API returns."""

    code = "deco_error"


class DecoUnavailable(DecoError):
    """tplinkrouterc6u isn't installed."""

    code = "deco_library_missing"


class DecoAuthError(DecoError):
    """The Deco rejected the password."""

    code = "deco_login_failed"


class DecoUnreachable(DecoError):
    """The main Deco didn't answer at all (down? wrong host?)."""

    code = "deco_unreachable"


class DecoApiError(DecoError):
    """The Deco answered, but not the way the library expects — the
    firmware may differ from the models tplinkrouterc6u was built for."""

    code = "deco_api_error"


def configured_units(cfg: dict) -> list[dict]:
    """Deco units from config with a usable MAC, for /api/status and the
    System page checkbox list. Units with no MAC (or a malformed one)
    are skipped — they can't be addressed for a reboot. MACs are
    normalized to uppercase colon form."""
    units = []
    for entry in (cfg.get("deco") or {}).get("units", []) or []:
        if not isinstance(entry, dict):
            continue
        mac = str(entry.get("mac") or "")
        if not mac or not is_valid_mac(mac):
            continue
        units.append({
            "name": str(entry.get("name") or mac),
            "mac": normalize_mac(mac),
            "main": bool(entry.get("main")),
        })
    return units


def reboot_units(host: str, password: str, targets: list[dict]) -> list[dict]:
    """Reboot the given units (each {"name", "mac"}) via the main unit's
    web API. Returns one result dict per target: {"name", "mac",
    "status": "rebooting" | "error", "error"?}.

    Raises DecoUnavailable / DecoAuthError / DecoUnreachable for
    whole-operation failures (before any unit is attempted). A target
    whose MAC isn't in the mesh gets a per-unit error and the rest still
    reboot.
    """
    try:
        from tplinkrouterc6u import TPLinkDecoClient
        from tplinkrouterc6u.common.exception import ClientException
        from requests.exceptions import ConnectionError as RequestsConnectionError
        from requests.exceptions import Timeout as RequestsTimeout
    except ImportError:
        raise DecoUnavailable(
            "tplinkrouterc6u is not installed — the launcher installs it "
            "from requirements.txt on the next child start; restart the "
            "launcher if this persists.")

    results = {normalize_mac(t["mac"]): {"name": t["name"],
                                         "mac": normalize_mac(t["mac"]),
                                         "status": "error",
                                         "error": "not attempted"}
               for t in targets}
    client = TPLinkDecoClient(host, password, timeout=DECO_TIMEOUT_SECONDS)
    try:
        try:
            client.authorize()
        except (RequestsConnectionError, RequestsTimeout) as exc:
            raise DecoUnreachable(
                f"Could not reach the Deco at {host} ({exc}).") from exc
        except ClientException as exc:
            raise DecoAuthError(
                "The Deco rejected the password — check deco.password in "
                f"config.local.yaml (it is the Deco app password). ({exc})"
            ) from exc

        # Which MACs are actually in this mesh? Validating first means a
        # typo'd MAC errors clearly instead of silently rebooting nothing,
        # and it confirms the WB10800 speaks this API before we send a
        # reboot at all. Use the Deco's own MAC strings in the reboot
        # call — its formatting, not ours.
        try:
            device_list = client.request(
                "admin/device?form=device_list",
                json.dumps({"operation": "read"})).get("device_list", [])
        except (RequestsConnectionError, RequestsTimeout) as exc:
            raise DecoUnreachable(
                f"Lost contact with the Deco at {host} ({exc}).") from exc
        except ClientException as exc:
            raise DecoApiError(
                "The Deco answered, but the device list came back in an "
                f"unexpected shape ({exc}) — this firmware may differ "
                "from the models tplinkrouterc6u supports.") from exc
        mesh_macs = {}
        for item in device_list:
            mac = str(item.get("mac") or "")
            if mac and is_valid_mac(mac):
                mesh_macs[normalize_mac(mac)] = mac

        to_reboot = []  # (normalized mac, Deco's own mac string)
        for norm in results:
            if norm in mesh_macs:
                to_reboot.append((norm, mesh_macs[norm]))
            else:
                results[norm]["error"] = (
                    "not found in the Deco mesh — check the MAC in "
                    "config.yaml")
        if not to_reboot:
            return list(results.values())

        try:
            client.request(
                "admin/device?form=system",
                json.dumps({"operation": "reboot",
                            "params": {"mac_list": [{"mac": m}
                                                    for _, m in to_reboot]}}))
        except (RequestsConnectionError, RequestsTimeout):
            # Expected when the MAIN unit is among the rebooted: the Deco
            # drops its web server as it restarts, so the reply never
            # arrives. The authorize + device-list calls just succeeded,
            # so the unit was reachable a moment ago — a drop at exactly
            # this call means the reboot was accepted, not that the
            # network failed. Treat it as success.
            pass
        except ClientException as exc:
            for norm, _ in to_reboot:
                results[norm]["error"] = f"the Deco refused the reboot ({exc})"
            return list(results.values())
        for norm, _ in to_reboot:
            results[norm]["status"] = "rebooting"
            results[norm].pop("error", None)
        return list(results.values())
    finally:
        # Best-effort: the Deco may already be rebooting (logout then
        # fails), and a failed logout must never mask the real result.
        # The web UI allows one session at a time, so a clean logout
        # when possible keeps the next login from tripping over ours.
        try:
            client.logout()
        except Exception:
            pass
