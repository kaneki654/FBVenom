"""USB internet (reverse tethering) without phones, ADB, or network changes.

A fake ADB/gnirehtet answers every command the manager issues, and the fake
relay process really listens on its loopback port so readiness checks are the
real ones. Nothing here runs a binary or touches a phone.
"""

import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

import usb_internet
import usb_network
import tools_prepare_usb_internet as prep

ADB, RELAY, APK = "/fake/adb", "/fake/gnirehtet", "/fake/gnirehtet.apk"
SERVICE = ("ServiceRecord{1a2b u0 com.genymobile.gnirehtet/.GnirehtetService}\n")
VPN = "NetworkAgentInfo{ ni{[type: VPN[], state: CONNECTED/CONNECTED]} Transports: VPN }"


class FakePhone:
    def __init__(self, serial, model="Pixel", state="device", installed=False, sdk="30"):
        self.serial, self.model, self.state = serial, model, state
        self.installed, self.sdk = installed, sdk
        self.reverse = None           # e.g. "tcp:31416"
        self.vpn = False              # gnirehtet's VPN up
        self.foreign_vpn = False      # someone else's VPN up
        self.always_on = "null"
        self.appops = None
        self.approve_on_start = True  # the VPN comes up after start (consent given)


class FakeRelayProcess:
    """Listens on the port it was started with, like the real relay."""

    def __init__(self, port):
        self.port = port
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(16)
        self.returncode = None
        self.stdout = io.StringIO("INFO Relay: started\nWARN Relay: client lost\n")
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0
        self.sock.close()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode


class FakeTools:
    def __init__(self, phones):
        self.phones = {phone.serial: phone for phone in phones}
        self.calls = []
        self.relays = []
        self.fail_install = False
        self.lock = threading.Lock()

    # subprocess.run replacement: returns (code, stdout, stderr)
    def run(self, args, **kwargs):
        with self.lock:
            self.calls.append(list(args))
        if args[0] == ADB and args[1:3] == ["devices", "-l"]:
            lines = ["List of devices attached"]
            for phone in self.phones.values():
                lines.append(f"{phone.serial}\t{phone.state} product:x model:{phone.model} transport_id:1")
            return 0, "\n".join(lines) + "\n", ""
        if args[0] == ADB and args[1] == "-s":
            return self._adb(self.phones[args[2]], args[3:])
        if args[0] == RELAY:
            return self._relay(args[1], self.phones.get(args[2]) if len(args) > 2 else None, args)
        raise AssertionError(f"unexpected command {args}")

    def _adb(self, phone, rest):
        if rest == ["get-devpath"]:
            return 0, "unknown\n", ""           # what Windows ADB reports
        if rest == ["shell", "getprop", "ro.build.version.sdk"]:
            return 0, phone.sdk + "\n", ""
        if rest == ["shell", "settings", "get", "secure", "always_on_vpn_app"]:
            return 0, phone.always_on + "\n", ""
        if rest == ["reverse", "--list"]:
            return 0, (f"UsbFfs localabstract:gnirehtet {phone.reverse}\n" if phone.reverse else ""), ""
        if rest == ["reverse", "--remove", "localabstract:gnirehtet"]:
            phone.reverse = None
            return 0, "", ""
        if rest == ["shell", "dumpsys", "connectivity"]:
            return 0, (VPN if (phone.vpn or phone.foreign_vpn) else "no vpn here"), ""
        if rest == ["shell", "dumpsys", "activity", "services", usb_internet.PACKAGE]:
            return 0, (SERVICE if phone.vpn else "(nothing)"), ""
        if rest == ["shell", "pm", "path", usb_internet.PACKAGE]:
            return (0, "package:/data/app/gnirehtet/base.apk\n", "") if phone.installed else (1, "", "")
        if rest[:3] == ["shell", "appops", "set"]:
            phone.appops = rest[3:]
            return 0, "", ""
        raise AssertionError(f"unexpected adb command {rest}")

    def _relay(self, command, phone, args):
        if command == "install":
            if self.fail_install:
                return 1, "", "adb: failed to install: INSTALL_FAILED_INSUFFICIENT_STORAGE"
            phone.installed = True
            return 0, "", ""
        if command == "start":
            phone.installed = True
            phone.reverse = "tcp:" + args[args.index("-p") + 1]
            phone.vpn = phone.approve_on_start
            return 0, "", ""
        if command == "stop":
            phone.vpn = False
            return 0, "", ""
        if command == "tunnel":
            phone.reverse = "tcp:" + args[args.index("-p") + 1]
            return 0, "", ""
        raise AssertionError(f"unexpected relay command {args}")

    def popen(self, args, **kwargs):
        assert args[0] == RELAY and args[1] == "relay", args
        process = FakeRelayProcess(int(args[3]))
        self.relays.append((args, kwargs, process))
        return process


def wifi_ok():
    return {"ok": True, "code": "TETHER_PC_WIFI_READY", "message": "The PC Wi-Fi Internet connection is ready (wlan0).",
            "detail": "", "hint": "", "interface": "Wi-Fi", "kind": "wifi"}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.logs = Path(temporary.name) / "logs"
        self.events = []
        patcher = patch.object(usb_internet, "_read_adb_proto", side_effect=OSError("old adb server"))
        patcher.start()
        self.addCleanup(patcher.stop)
        verify = patch.object(usb_internet.USBInternetManager, "_verify_helpers", return_value=None)
        verify.start()
        self.addCleanup(verify.stop)

    def manager(self, tools, network=wifi_ok, **kwargs):
        manager = usb_internet.USBInternetManager(
            ADB, RELAY, APK, on_event=self.events.append, log_dir=self.logs,
            network_check=network, run=tools.run, popen=tools.popen, vpn_wait=0.0, **kwargs)
        self.addCleanup(manager.close)
        return manager

    def wait_events(self, manager):
        manager._events.join()

    def codes(self, manager):
        self.wait_events(manager)
        return [event["code"] for event in self.events]

    def test_one_click_brings_every_usb_phone_online_and_preapproves_vpn(self):
        tools = FakeTools([FakePhone("R58M1"), FakePhone("R58M2", installed=True)])
        manager = self.manager(tools)
        snap = manager.start_all()
        self.assertTrue(snap["running"])
        self.assertEqual({row["serial"]: row["state"] for row in snap["devices"]},
                         {"R58M1": "vpn_active", "R58M2": "vpn_active"})
        port = snap["relay_port"]
        for phone in tools.phones.values():
            self.assertEqual(phone.reverse, f"tcp:{port}")
            self.assertEqual(phone.appops, [usb_internet.PACKAGE, "ACTIVATE_VPN", "allow"])
        # The uninstalled phone got the client before its VPN was pre-approved.
        install = [RELAY, "install", "R58M1"]
        approve = [ADB, "-s", "R58M1", "shell", "appops", "set", usb_internet.PACKAGE, "ACTIVATE_VPN", "allow"]
        self.assertLess(tools.calls.index(install), tools.calls.index(approve))
        self.assertNotIn([RELAY, "install", "R58M2"], tools.calls)
        args, kwargs, _ = tools.relays[0]
        self.assertEqual(args, [RELAY, "relay", "-p", str(port)])
        self.assertEqual(kwargs["env"]["ADB"], ADB)
        self.assertEqual(kwargs["env"]["GNIREHTET_APK"], APK)
        codes = self.codes(manager)
        self.assertIn("TETHER_RELAY_READY", codes)
        self.assertEqual(codes.count("TETHER_DEVICE_ONLINE"), 2)

    def test_windows_style_serial_without_transport_info_is_still_treated_as_usb(self):
        tools = FakeTools([FakePhone("0123456789ABCDEF")])
        manager = self.manager(tools)
        rows = manager.discover_usb()
        self.assertEqual([row["serial"] for row in rows], ["0123456789ABCDEF"])

    def test_wireless_adb_and_emulators_are_never_tethered(self):
        tools = FakeTools([FakePhone("192.168.1.20:5555"), FakePhone("emulator-5554"),
                           FakePhone("adb-R58M-abc._adb-tls-connect._tcp")])
        manager = self.manager(tools)
        self.assertEqual(manager.discover_usb(), [])
        self.assertEqual(manager.start_all()["relay_port"], None)
        self.assertIn("TETHER_NO_USB_DEVICES", self.codes(manager))

    def test_unauthorized_phone_is_reported_and_skipped(self):
        tools = FakeTools([FakePhone("R58M1", state="unauthorized"), FakePhone("R58M2")])
        manager = self.manager(tools)
        snap = manager.start_all()
        states = {row["serial"]: row["state"] for row in snap["devices"]}
        self.assertEqual(states, {"R58M1": "unauthorized", "R58M2": "vpn_active"})
        self.assertIn("TETHER_DEVICE_UNAUTHORIZED", self.codes(manager))

    def test_ethernet_is_shared_with_a_warning_instead_of_refused(self):
        def ethernet():
            return {**wifi_ok(), "kind": "ethernet", "interface": "Ethernet",
                    "code": "TETHER_PC_INTERNET_READY",
                    "message": "The PC Internet connection is ready (Ethernet: Ethernet).",
                    "hint": "To share Wi-Fi instead, make Wi-Fi the PC's preferred connection."}
        tools = FakeTools([FakePhone("R58M1")])
        manager = self.manager(tools, network=ethernet)
        snap = manager.start_all()
        self.assertEqual(snap["devices"][0]["state"], "vpn_active")
        self.wait_events(manager)
        event = next(e for e in self.events if e["code"] == "TETHER_PC_INTERNET_READY")
        self.assertEqual(event["severity"], "warning")
        self.assertIn("Wi-Fi", event["hint"])

    def test_no_pc_internet_stops_before_touching_phones(self):
        def offline():
            return {"ok": False, "code": "TETHER_PC_DNS_FAILED", "message": "DNS failed", "detail": "",
                    "hint": "Check the Wi-Fi connection.", "interface": "Wi-Fi", "kind": "wifi"}
        tools = FakeTools([FakePhone("R58M1")])
        manager = self.manager(tools, network=offline)
        snap = manager.start_all()
        self.assertFalse(snap["running"])
        self.assertEqual(tools.relays, [])
        self.assertFalse(any(call[:2] == [RELAY, "start"] for call in tools.calls))
        self.assertIn("TETHER_PC_DNS_FAILED", self.codes(manager))

    def test_stale_tunnel_and_vpn_from_a_crashed_session_are_cleaned_up(self):
        phone = FakePhone("R58M1", installed=True)
        phone.reverse = "tcp:1"       # nothing listens on port 1
        phone.vpn = True              # gnirehtet's own VPN, relay long gone
        tools = FakeTools([phone])
        manager = self.manager(tools)
        snap = manager.start_all()
        self.assertEqual(snap["devices"][0]["state"], "vpn_active")
        self.assertEqual(phone.reverse, f"tcp:{snap['relay_port']}")
        codes = self.codes(manager)
        self.assertIn("TETHER_STALE_TUNNEL_REMOVED", codes)
        self.assertIn("TETHER_STALE_VPN_STOPPED", codes)

    def test_tunnel_owned_by_a_live_relay_is_left_alone(self):
        live = socket.socket()
        live.bind(("127.0.0.1", 0))
        live.listen(1)
        self.addCleanup(live.close)
        phone = FakePhone("R58M1", installed=True)
        phone.reverse = f"tcp:{live.getsockname()[1]}"
        tools = FakeTools([phone])
        manager = self.manager(tools)
        snap = manager.start_all()
        self.assertEqual(snap["devices"][0]["state"], "failed")
        self.assertEqual(phone.reverse, f"tcp:{live.getsockname()[1]}")
        self.assertIn("TETHER_MAPPING_IN_USE", self.codes(manager))

    def test_someone_elses_vpn_is_never_replaced(self):
        phone = FakePhone("R58M1", installed=True)
        phone.foreign_vpn = True
        tools = FakeTools([phone])
        manager = self.manager(tools)
        snap = manager.start_all()
        self.assertEqual(snap["devices"][0]["state"], "failed")
        self.assertFalse(any(call[:2] == [RELAY, "start"] for call in tools.calls))
        self.assertIn("TETHER_VPN_CONFLICT", self.codes(manager))

    def test_without_auto_approval_the_phone_waits_for_the_prompt(self):
        phone = FakePhone("R58M1", installed=True)
        phone.approve_on_start = False
        tools = FakeTools([phone])
        manager = self.manager(tools, auto_approve_vpn=False)
        snap = manager.start_all()
        self.assertEqual(snap["devices"][0]["state"], "approval_required")
        self.assertIsNone(phone.appops)
        self.assertIn("TETHER_VPN_APPROVAL_REQUIRED", self.codes(manager))

    def test_failed_install_is_logged_with_its_reason_and_hint(self):
        tools = FakeTools([FakePhone("R58M1")])
        tools.fail_install = True
        manager = self.manager(tools)
        snap = manager.start_all()
        self.assertEqual(snap["devices"][0]["state"], "failed")
        self.wait_events(manager)
        event = next(e for e in self.events if e["code"] == "TETHER_INSTALL_FAILED")
        self.assertEqual(event["severity"], "error")
        self.assertIn("INSUFFICIENT_STORAGE", event["detail"])
        self.assertIn("storage", event["hint"])
        self.assertFalse(snap["running"])            # nothing owned: relay stopped again

    def test_stop_all_restores_phones_and_stops_the_relay(self):
        tools = FakeTools([FakePhone("R58M1"), FakePhone("R58M2")])
        manager = self.manager(tools)
        manager.start_all()
        process = tools.relays[0][2]
        snap = manager.stop_all()
        self.assertFalse(snap["running"])
        self.assertTrue(process.terminated)
        for phone in tools.phones.values():
            self.assertFalse(phone.vpn)
            self.assertIsNone(phone.reverse)
        self.assertEqual({row["state"] for row in snap["devices"]}, {"stopped"})

    def test_monitor_checks_the_vpn_on_the_phone_only_every_20s_or_after_a_repair(self):
        tools = FakeTools([FakePhone("R58M1", installed=True)])
        with patch.object(usb_internet.USBInternetManager, "_start_monitor"):
            manager = self.manager(tools)
            manager.start_all()

        def phone_dumps():
            return sum(call[3:6] == ["shell", "dumpsys", "activity"] for call in tools.calls)

        before = phone_dumps()
        clock = [5000.0]
        with patch.object(usb_internet.time, "monotonic", side_effect=lambda: clock[0]):
            manager._monitor_once()                       # first pass: checked
            self.assertEqual(phone_dumps(), before + 1)
            clock[0] += 5
            manager._monitor_once()                       # 5 s later: tunnel only
            self.assertEqual(phone_dumps(), before + 1)
            clock[0] += usb_internet.VPN_RECHECK_SECONDS
            manager._monitor_once()                       # due again
            self.assertEqual(phone_dumps(), before + 2)
            tools.phones["R58M1"].reverse = None           # e.g. the ADB server restarted
            clock[0] += 1
            manager._monitor_once()                       # restored -> checked at once
            self.assertEqual(phone_dumps(), before + 3)
        self.assertEqual(tools.phones["R58M1"].reverse, f"tcp:{manager.snapshot()['relay_port']}")
        self.assertEqual(manager.snapshot()["devices"][0]["state"], "vpn_active")

    def test_every_event_is_persisted_as_redacted_json_lines(self):
        tools = FakeTools([FakePhone("R58M1")])
        tools.fail_install = True
        manager = self.manager(tools)
        manager._emit("TETHER_START_FAILED", "boom", "R58M1", "Authorization: Bearer sk-secret-123 api_key=abc",
                      severity="error")
        manager.start_all()
        manager.close()
        log = Path(manager.log_path)
        self.assertEqual(log.parent, self.logs)
        lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        events = [line for line in lines if "code" in line]
        self.assertTrue({"time", "timestamp", "severity", "code", "serial", "message", "detail", "hint",
                         "session"} <= set(events[0]))
        text = log.read_text(encoding="utf-8")
        self.assertNotIn("sk-secret-123", text)
        self.assertNotIn("api_key=abc", text)
        self.assertIn("TETHER_INSTALL_FAILED", text)

    def test_relay_warnings_reach_the_log_but_not_packet_chatter(self):
        tools = FakeTools([FakePhone("R58M1")])
        manager = self.manager(tools)
        manager.start_all()
        manager._reader.join(timeout=2)
        lines = [json.loads(line) for line in Path(manager.log_path).read_text(encoding="utf-8").splitlines()]
        relay = [line["detail"] for line in lines if line.get("kind") == "relay"]
        self.assertEqual(relay, ["WARN Relay: client lost"])

    def test_unwritable_log_folder_falls_back_to_the_profile_folder(self):
        self.logs.parent.mkdir(parents=True, exist_ok=True)
        blocker = self.logs.parent / "not-a-folder"
        blocker.write_text("file")
        home = self.logs.parent / "home"
        with patch.object(Path, "home", return_value=home):
            manager = usb_internet.USBInternetManager(ADB, RELAY, APK, log_dir=blocker / "logs")
            self.addCleanup(manager.close)
        self.assertEqual(Path(manager.log_path).parent, home / ".fbvenom" / "logs")


class FindHelpersTests(unittest.TestCase):
    def test_environment_directory_is_used_and_missing_files_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"FBVENOM_GNIREHTET_DIR": directory}), \
                    patch.object(usb_internet, "__file__", str(Path(directory) / "nowhere" / "usb_internet.py")):
                with self.assertRaises(FileNotFoundError):
                    usb_internet.find_helpers(extra_dirs=())
                (Path(directory) / usb_internet.RELAY_NAME).write_bytes(b"relay")
                (Path(directory) / "gnirehtet.apk").write_bytes(b"apk")
                relay, apk = usb_internet.find_helpers()
            self.assertEqual(Path(relay).parent, Path(directory))
            self.assertEqual(Path(apk).name, "gnirehtet.apk")


class PcNetworkTests(unittest.TestCase):
    def check(self, routes, probe):
        with patch.object(usb_network, "_route_snapshot", return_value={"platform": "windows", "routes": routes}), \
                patch.object(usb_network, "_internet_probe", return_value=probe):
            return usb_network.check_pc_internet()

    def test_wifi_route_with_internet_is_ready(self):
        result = self.check([{"interface": "Wi-Fi", "kind": "wifi", "active": True, "metric": 30}],
                            usb_network._result(True, "TETHER_PC_WIFI_READY", "ready"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "wifi")
        self.assertIn("Wi-Fi", result["message"])

    def test_ethernet_route_is_usable_and_says_so(self):
        result = self.check([{"interface": "Ethernet", "kind": "ethernet", "active": True, "metric": 10},
                             {"interface": "Wi-Fi", "kind": "wifi", "active": True, "metric": 50}],
                            usb_network._result(True, "TETHER_PC_WIFI_READY", "ready"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "ethernet")
        self.assertIn("Ethernet", result["message"])
        self.assertIn("Wi-Fi", result["hint"])

    def test_no_route_or_failed_probe_is_an_error(self):
        result = self.check([], usb_network._result(True, "x", "y"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "TETHER_PC_NO_DEFAULT_ROUTE")
        result = self.check([{"interface": "Wi-Fi", "kind": "wifi", "active": True, "metric": 30}],
                            usb_network._result(False, "TETHER_PC_CAPTIVE_PORTAL", "sign in"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "TETHER_PC_CAPTIVE_PORTAL")


class HelperVerificationTests(unittest.TestCase):
    def test_missing_folder_and_wrong_archive_are_rejected_without_running_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "missing or unsafe"):
                prep.verify_assets(Path(directory) / "absent", platform="windows")
            with self.assertRaisesRegex(ValueError, "SHA-256 verification failed"):
                prep.prepare("windows", Path(directory) / "out", downloader=lambda url, limit: b"not the release")

    def test_pinned_release_is_the_official_project(self):
        self.assertEqual(prep.PROJECT, "Genymobile/gnirehtet")
        self.assertTrue(prep._archive_url("windows").startswith(
            "https://github.com/Genymobile/gnirehtet/releases/download/v2.5.1/"))


if __name__ == "__main__":
    unittest.main()
