"""Focused tests for the Deco restart (app/deco.py).

The Deco client is mocked — no real Deco is involved. Run from the
network-monitor/ directory:

    python tests/test_deco.py
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import deco  # noqa: E402
from requests.exceptions import ConnectionError as RequestsConnectionError  # noqa: E402
from tplinkrouterc6u.common.exception import ClientException  # noqa: E402

MAIN_MAC = "A8:6E:84:F6:BB:3D"
BED_MAC = "A8:6E:84:F6:BB:5B"
MBED_MAC = "A8:6E:84:F6:BB:51"

MESH = [{"mac": MAIN_MAC, "role": "master"},
        {"mac": BED_MAC, "role": "slave"},
        {"mac": MBED_MAC, "role": "slave"}]

TARGETS = [{"name": "Bedroom", "mac": BED_MAC},
           {"name": "Main Bedroom", "mac": MBED_MAC}]


def make_client(device_list=None, reboot_effect=None, authorize_effect=None):
    """A mock TPLinkDecoClient. request() dispatches on the endpoint:
    device_list returns the mesh, the reboot call records its payload
    (or raises reboot_effect)."""
    client = mock.MagicMock()
    client.device_list = device_list if device_list is not None else MESH
    client.reboot_payloads = []
    if authorize_effect is not None:
        client.authorize.side_effect = authorize_effect

    def request(path, data, *args, **kwargs):
        if "device_list" in path:
            return {"device_list": client.device_list}
        if "form=system" in path:
            client.reboot_payloads.append(json.loads(data))
            if isinstance(reboot_effect, Exception):
                raise reboot_effect
            return {}
        raise AssertionError(f"unexpected request path: {path}")

    client.request.side_effect = request
    return client


class TestConfiguredUnits(unittest.TestCase):
    def test_skips_macless_and_malformed(self):
        cfg = {"deco": {"units": [
            {"name": "Main", "mac": "a8-6e-84-f6-bb-3d", "main": True},
            {"name": "No MAC", "mac": "", "main": False},
            {"name": "Bad MAC", "mac": "not-a-mac", "main": False},
            {"name": "No MAC key", "main": False},
        ]}}
        units = deco.configured_units(cfg)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["name"], "Main")
        # Dashes in, uppercase colons out.
        self.assertEqual(units[0]["mac"], MAIN_MAC)
        self.assertTrue(units[0]["main"])

    def test_empty_config(self):
        self.assertEqual(deco.configured_units({}), [])
        self.assertEqual(deco.configured_units({"deco": {}}), [])


class TestRebootUnits(unittest.TestCase):
    def run_reboot(self, client, targets=None):
        with mock.patch("tplinkrouterc6u.TPLinkDecoClient",
                        return_value=client) as cls:
            results = deco.reboot_units("http://192.168.68.1", "pw",
                                        targets if targets is not None
                                        else TARGETS)
            # One login, to the main unit only — never one client per unit.
            cls.assert_called_once()
            self.assertEqual(cls.call_args.args[0], "http://192.168.68.1")
            return results

    def test_subset_reboots_only_selected_macs(self):
        """The core promise: the reboot call names ONLY the selected
        units' MACs — the main unit's MAC must not appear."""
        client = make_client()
        results = self.run_reboot(client)
        self.assertEqual(len(client.reboot_payloads), 1)
        payload = client.reboot_payloads[0]
        self.assertEqual(payload["operation"], "reboot")
        sent = {m["mac"] for m in payload["params"]["mac_list"]}
        self.assertEqual(sent, {BED_MAC, MBED_MAC})
        self.assertNotIn(MAIN_MAC, sent)
        self.assertEqual([r["status"] for r in results],
                         ["rebooting", "rebooting"])
        client.logout.assert_called_once()

    def test_mac_not_in_mesh_errors_only_that_unit(self):
        targets = ([{"name": "Bedroom", "mac": BED_MAC},
                    {"name": "Ghost", "mac": "AA:BB:CC:DD:EE:FF"}])
        client = make_client()
        results = self.run_reboot(client, targets)
        by_name = {r["name"]: r for r in results}
        self.assertEqual(by_name["Bedroom"]["status"], "rebooting")
        self.assertEqual(by_name["Ghost"]["status"], "error")
        self.assertIn("not found in the Deco mesh", by_name["Ghost"]["error"])
        # The reboot still went out for the unit that exists.
        sent = {m["mac"]
                for m in client.reboot_payloads[0]["params"]["mac_list"]}
        self.assertEqual(sent, {BED_MAC})

    def test_login_failure(self):
        client = make_client(
            authorize_effect=ClientException("Cannot authorize!"))
        with self.assertRaises(deco.DecoAuthError):
            self.run_reboot(client)
        # No reboot attempted after a failed login.
        self.assertEqual(client.reboot_payloads, [])

    def test_unreachable(self):
        client = make_client(
            authorize_effect=RequestsConnectionError("conn refused"))
        with self.assertRaises(deco.DecoUnreachable):
            self.run_reboot(client)

    def test_connection_drop_at_reboot_is_success(self):
        """The Deco drops its web server as it restarts, so the reboot
        call's reply never arrives. After a successful login +
        device-list fetch, that drop means the reboot was accepted."""
        client = make_client(
            reboot_effect=RequestsConnectionError("connection reset"))
        results = self.run_reboot(client)
        self.assertEqual([r["status"] for r in results],
                         ["rebooting", "rebooting"])

    def test_reboot_refused(self):
        client = make_client(
            reboot_effect=ClientException("Response with error"))
        results = self.run_reboot(client)
        self.assertTrue(all(r["status"] == "error" for r in results))
        self.assertIn("refused", results[0]["error"])

    def test_library_missing(self):
        with mock.patch.dict(sys.modules, {"tplinkrouterc6u": None}):
            with self.assertRaises(deco.DecoUnavailable):
                deco.reboot_units("http://192.168.68.1", "pw", TARGETS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
