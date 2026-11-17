"""Offscreen USB internet window; a fake manager stands in for ADB and phones."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import aiconfig
with patch.object(aiconfig, "load", return_value={}):
    import gui
from PyQt6.QtWidgets import QApplication


class FakeManager:
    def __init__(self, *args, on_event=None, log_dir=None, auto_approve_vpn=True, **kwargs):
        self.args, self.on_event, self.log_dir = args, on_event, log_dir
        self.auto_approve_vpn = auto_approve_vpn
        self.calls = []
        self.devices = []
        self.running = False
        self.closed = threading.Event()
        self.log_path = str(Path(log_dir) / "venom_usb_internet.log") if log_dir else ""

    def snapshot(self):
        return {"running": self.running, "busy": False, "relay_port": 4242 if self.running else None,
                "devices": [dict(row) for row in self.devices], "last_error": "",
                "network": {"ok": True, "kind": "wifi", "interface": "Wi-Fi", "message": "ready"}}

    def discover_usb(self):
        self.calls.append(("scan", self.auto_approve_vpn))
        self.devices = [{"serial": "R58M1", "model": "A50", "state": "ready", "detail": ""},
                        {"serial": "R58M2", "model": "A50", "state": "ready", "detail": ""}]
        return self.devices

    def start_all(self):
        self.calls.append(("start", self.auto_approve_vpn))
        self.running = True
        self.devices = [{"serial": "R58M1", "model": "A50", "state": "vpn_active", "detail": ""},
                        {"serial": "R58M2", "model": "A50", "state": "failed", "detail": "install failed"}]
        return self.snapshot()

    def stop_all(self):
        self.calls.append(("stop", self.auto_approve_vpn))
        self.running = False
        return self.snapshot()

    def close(self):
        self.calls.append(("close", None))
        self.closed.set()


class USBInternetGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.managers = []

        def make_manager(*args, **kwargs):
            manager = FakeManager(*args, **kwargs)
            self.managers.append(manager)
            return manager
        self.patches = [
            patch.object(gui, "ensure_online", return_value={}),
            patch.object(gui.aiconfig, "load", return_value={}),
            patch.object(gui.aiconfig, "is_ready", return_value=False),
            patch.object(gui.aiconfig, "describe", return_value="Test AI settings"),
            patch.object(gui.fbcore, "set_speed"),
            patch.object(gui.fbcore, "DEBUG_LOG", str(Path(self.directory.name) / "debug.log")),
            patch.object(gui, "stop_llama"),
            patch.object(gui, "HERE", self.directory.name),
            patch.object(gui.usb_internet, "find_helpers", return_value=("/x/gnirehtet.exe", "/x/gnirehtet.apk")),
            patch.object(gui.usb_internet, "USBInternetManager", side_effect=make_manager),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.window = gui.ReactionGUI(workspace_path=Path(self.directory.name) / "workspaces.json")
        self.window.timer.stop()
        self.window.anim_timer.stop()
        self.addCleanup(self.window.close)

    def dialog(self):
        dialog = gui.USBInternetDialog(self.window)
        self.window._usb_dialog = dialog
        self.addCleanup(dialog.close)
        return dialog

    def settle(self, dialog, timeout=3.0):
        deadline = time.monotonic() + timeout
        while dialog._busy and time.monotonic() < deadline:
            dialog._drain()
            time.sleep(0.01)
        dialog._drain()
        self.assertFalse(dialog._busy)

    def test_header_has_a_usb_internet_button(self):
        self.assertEqual(self.window.usb_btn.text(), "USB internet")
        self.assertIn("USB", self.window.usb_btn.toolTip())
        self.assertTrue(callable(gui.ReactionGUI.open_usb_internet))

    def test_opening_scans_and_start_brings_phones_online(self):
        dialog = self.dialog()
        self.settle(dialog)
        manager = self.managers[0]
        self.assertEqual(manager.calls, [("scan", True)])
        self.assertEqual(manager.args[0], gui.fbcore.ADB)
        self.assertEqual(Path(manager.log_dir), Path(self.directory.name) / "logs")
        self.assertIn("2 USB phones", dialog.status.text())
        dialog.auto_vpn.setChecked(False)
        dialog.start_btn.click()
        self.settle(dialog)
        self.assertEqual(manager.calls[-1], ("start", False))
        rows = [dialog.devices.item(i).text() for i in range(dialog.devices.count())]
        self.assertIn("Online via PC", rows[0])
        self.assertIn("Failed", rows[1])
        self.assertIn("install failed", rows[1])
        self.assertIn("1 online via PC", dialog.status.text())
        self.assertIn("1 need attention", dialog.status.text())
        self.assertIn("Computer internet ready", dialog.network.text())

    def test_events_reach_the_dialog_log_and_errors_reach_diagnostics(self):
        dialog = self.dialog()
        self.settle(dialog)
        manager = self.managers[0]
        manager.start_all()
        for event in ({"time": "12:00:00", "severity": "info", "code": "TETHER_DEVICE_ONLINE",
                       "serial": "R58M1", "message": "Phone is using the PC's internet over USB"},
                      {"time": "12:00:01", "severity": "error", "code": "TETHER_INSTALL_FAILED",
                       "serial": "R58M2", "message": "The USB internet client could not be installed",
                       "detail": "INSTALL_FAILED_INSUFFICIENT_STORAGE", "hint": "Check the phone's storage"}):
            manager.on_event(event)            # arrives on a worker thread in real use
        self.assertNotIn("TETHER_DEVICE_ONLINE", dialog.log.toPlainText())   # only via the queue
        self.window.process_queue()
        log = dialog.log.toPlainText()
        self.assertIn("TETHER_DEVICE_ONLINE · R58M1", log)
        self.assertIn("Next step: Check the phone's storage", log)
        codes = [event["code"] for event in self.window._diagnostic_events]
        self.assertEqual(codes, ["TETHER_INSTALL_FAILED"])
        self.assertTrue(self.window._diagnostic_events[0]["message"].startswith("USB internet:"))
        self.assertEqual(self.window.usb_btn.text(), "USB internet · 1")
        # A reopened dialog shows the earlier history too.
        again = gui.USBInternetDialog(self.window)
        self.addCleanup(again.close)
        self.assertIn("TETHER_INSTALL_FAILED", again.log.toPlainText())

    def test_missing_helpers_disable_the_controls_with_a_reason(self):
        with patch.object(gui.usb_internet, "find_helpers",
                          side_effect=FileNotFoundError("helpers not found")):
            dialog = self.dialog()
        self.assertFalse(dialog.start_btn.isEnabled())
        self.assertIn("unavailable", dialog.status.text())
        self.assertIn("helpers not found", dialog.status.text())
        self.assertEqual(self.managers, [])

    def test_closing_the_app_stops_usb_internet(self):
        dialog = self.dialog()
        self.settle(dialog)
        manager = self.managers[0]
        dialog.close()
        self.window._shutdown_usb_internet(wait=2)
        self.assertTrue(manager.closed.is_set())
        self.assertIsNone(self.window.usb_internet)

    def test_core_is_told_while_usb_internet_runs_so_it_never_restarts_adb(self):
        self.assertIs(gui.fbcore.USB_INTERNET_ACTIVE.__self__, self.window)
        self.assertFalse(gui.fbcore.usb_internet_active())        # nothing started yet
        manager = self.window.usb_manager()
        manager.start_all()
        self.assertTrue(gui.fbcore.usb_internet_active())
        manager.stop_all()
        self.assertFalse(gui.fbcore.usb_internet_active())

    def test_info_notices_are_not_counted_as_warnings(self):
        self.window._append_diagnostic({"code": "AI_RECOVERED", "severity": "info", "message": "back"})
        self.window._append_diagnostic({"code": "AI_SLOW", "severity": "warning", "message": "slow"})
        self.assertEqual(self.window._diagnostic_warnings, 1)
        self.assertEqual(self.window._diagnostic_errors, 0)


if __name__ == "__main__":
    unittest.main()
