"""Packaging smoke tests use fake modules and never contact phones/providers."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import fbvenom
import tools_validate_windows_build as build_validator


class BuildCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "application"
        self.bundle = self.root / "bundle"
        self.app.mkdir()
        (self.bundle / "platform-tools").mkdir(parents=True)
        self.adb = self.bundle / "platform-tools" / "adb.exe"
        self.adb.write_bytes(b"mock adb")
        (self.bundle / "gnirehtet").mkdir()
        self.relay = self.bundle / "gnirehtet" / "gnirehtet.exe"
        self.relay.write_bytes(b"mock relay")
        (self.bundle / "gnirehtet" / "gnirehtet.apk").write_bytes(b"mock apk")
        self.verify = Mock(side_effect=lambda directory, platform=None: {
            "relay": str(Path(directory) / "gnirehtet.exe"),
            "apk": str(Path(directory) / "gnirehtet.apk"),
            "provenance": {"version": "2.5.1", "archive_sha256": "f" * 64}})
        self.relay_output = ("Syntax: gnirehtet (install|uninstall|reinstall|run|autorun|start|stop|"
                             "restart|tunnel|relay) ...\n\n  gnirehtet relay [-p PORT]\n")
        self.report_path = self.app / "smoke.json"
        self.require_key = Mock(side_effect=AssertionError("license UI must not run"))
        self.normalize = Mock(return_value="https://www.facebook.com/live/1234567890")
        pixmap = SimpleNamespace(isNull=lambda: False, width=lambda: 256, height=lambda: 256)
        gui = SimpleNamespace(
            app_icon_pixmap=Mock(return_value=pixmap), draw_symbiote=lambda: None,
            SymbioteBanner=object,
            BackUpServ=Mock(side_effect=AssertionError("packaging must not instantiate the server dialog")),
            USBInternetDialog=Mock(side_effect=AssertionError("packaging must not open USB internet")),
            ReactionGUI=SimpleNamespace(_build_diagnostics=object, open_backup_server=lambda: None,
                                        open_usb_internet=lambda: None))
        self.modules = {
            "version": SimpleNamespace(APP_VERSION="1.1.1"),
            "aiconfig": SimpleNamespace(CONFIG_PATH=str(self.app / "venom_ai.json"), **{
                name: Mock(side_effect=AssertionError("packaging must not access server settings"))
                for name in ("get_server_mode", "set_server_mode", "get_lan_profile",
                             "build_lan_profile", "validate_lan_connection", "get_local_lan_key")}),
            "core": SimpleNamespace(HERE=str(self.app), ADB=str(self.adb),
                                    normalize_url=self.normalize, report_error=lambda: None,
                                    reset_ai_connection_state=lambda: None),
            "PIL": SimpleNamespace(Image=SimpleNamespace(__version__="test")),
            "PyQt6.QtWidgets": SimpleNamespace(QApplication=SimpleNamespace(instance=lambda: object())),
            "gui": gui,
            "auth": SimpleNamespace(require_key=self.require_key),
            "usb_internet": SimpleNamespace(USBInternetManager=Mock(
                side_effect=AssertionError("packaging must not start USB internet"))),
            "tools_prepare_usb_internet": SimpleNamespace(verify_assets=self.verify),
        }
        self.patchers = [
            patch.dict(sys.modules, self.modules),
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(self.bundle), create=True),
            patch.object(sys, "platform", "win32"),
            patch.object(sys, "executable", str(self.app / "FBVenom.exe")),
            patch.dict("os.environ", {"FBVENOM_BUILD_CHECK_PATH": str(self.report_path)}),
        ]
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)
        self.adb_run = self._patch_run()

    def _patch_run(self):
        def run(command, **kwargs):
            if Path(command[0]).name == "gnirehtet.exe":
                # gnirehtet prints its syntax on stderr when given no command.
                return SimpleNamespace(returncode=0, stdout="", stderr=self.relay_output)
            return self.adb_result
        self.adb_result = SimpleNamespace(
            returncode=0, stdout="Android Debug Bridge version 1.0.41\nVersion test\n", stderr="")
        item = patch.object(subprocess, "run", side_effect=run)
        result = item.start()
        self.addCleanup(item.stop)
        return result

    def report(self):
        return json.loads(self.report_path.read_text(encoding="utf-8"))

    def test_frozen_windows_bundle_passes_without_license_or_settings_write(self):
        self.assertEqual(fbvenom.run_build_check(), 0)
        report = self.report()
        self.assertTrue(report["ok"])
        self.assertEqual(report["app_version"], "1.1.1")
        self.assertEqual(report["platform"], "win32")
        self.assertTrue(report["auth_imported"])
        self.assertTrue(all(report["gui_features"].values()))
        self.require_key.assert_not_called()
        self.modules["gui"].BackUpServ.assert_not_called()
        for name in ("get_server_mode", "set_server_mode", "get_lan_profile",
                     "build_lan_profile", "validate_lan_connection", "get_local_lan_key"):
            getattr(self.modules["aiconfig"], name).assert_not_called()
        self.assertFalse((self.app / "venom_ai.json").exists())
        (adb_call, relay_call) = self.adb_run.call_args_list
        self.assertEqual(adb_call.args[0], [str(self.adb), "version"])
        self.assertEqual(adb_call.kwargs["timeout"], 10)
        self.assertEqual(adb_call.kwargs["stdin"], subprocess.DEVNULL)
        # The relay is only asked for its syntax: no command, no ADB, no phones.
        self.assertEqual(relay_call.args[0], [str(self.relay)])
        self.assertEqual(relay_call.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(relay_call.kwargs["timeout"], 10)
        self.assertTrue(report["usb_internet_bundled"])
        self.assertTrue(report["usb_relay_cli_ok"])
        self.assertEqual(report["usb_internet"], {"version": "2.5.1", "archive_sha256": "f" * 64})
        self.verify.assert_called_once_with(str(self.bundle / "gnirehtet"), platform="windows")
        self.modules["usb_internet"].USBInternetManager.assert_not_called()
        self.modules["gui"].USBInternetDialog.assert_not_called()
        self.normalize.assert_called_once_with("https://m.facebook.com/live/1234567890/?fbclid=build-check")

    def test_relay_that_does_not_print_gnirehtet_syntax_fails_the_build(self):
        for output in ("", "usage: something else entirely", "Syntax: gnirehtet (install) ..."):
            with self.subTest(output=output):
                self.relay_output = output
                self.assertEqual(fbvenom.run_build_check(), 2)
                self.assertFalse(self.report()["usb_relay_cli_ok"])
                self.assertIn("usb_relay_cli_ok", self.report()["failed_checks"])

    def test_unverified_usb_internet_helpers_fail_the_build_without_running_them(self):
        self.verify.side_effect = ValueError("USB Internet helper hash or size mismatch: gnirehtet.exe")
        self.assertEqual(fbvenom.run_build_check(), 2)
        report = self.report()
        self.assertFalse(report["usb_internet_bundled"])
        self.assertIn("usb_internet_bundled", report["failed_checks"])
        self.assertTrue(any("hash or size mismatch" in error for error in report["errors"]))
        self.assertEqual([call.args[0][0] for call in self.adb_run.call_args_list], [str(self.adb)])

    def test_gui_without_usb_internet_button_is_rejected(self):
        with patch.object(self.modules["gui"], "USBInternetDialog", None):
            self.assertEqual(fbvenom.run_build_check(), 2)
        self.assertFalse(self.report()["gui_features"]["usb_internet"])
        self.assertIn("updated_features", self.report()["failed_checks"])

    def test_source_or_non_windows_cannot_pass_or_execute_adb(self):
        with patch.object(sys, "frozen", False), patch.object(sys, "platform", "linux"):
            self.assertEqual(fbvenom.run_build_check(), 2)
        report = self.report()
        self.assertFalse(report["ok"])
        self.assertIn("frozen", report["failed_checks"])
        self.assertIn("windows", report["failed_checks"])
        self.adb_run.assert_not_called()

    def test_host_installed_adb_is_rejected_without_executing_it(self):
        host_adb = self.app / "adb.exe"
        host_adb.write_bytes(b"not bundled")
        with patch.object(self.modules["core"], "ADB", str(host_adb)):
            self.assertEqual(fbvenom.run_build_check(), 2)
        self.assertTrue(self.report()["adb_exists"])
        self.assertFalse(self.report()["adb_bundled"])
        self.adb_run.assert_not_called()

    def test_adb_failure_and_timeout_are_reported_before_nonzero_exit(self):
        self.adb_result = SimpleNamespace(returncode=1, stdout="", stderr="Missing DLL")
        self.assertEqual(fbvenom.run_build_check(), 2)
        self.assertEqual(self.report()["adb_returncode"], 1)
        self.assertFalse(self.report()["adb_version_ok"])
        self.adb_run.side_effect = subprocess.TimeoutExpired([str(self.adb), "version"], 10)
        self.assertEqual(fbvenom.run_build_check(), 2)
        self.assertIn("TimeoutExpired", self.report()["errors"][0])

    def test_import_failure_still_writes_a_readable_failure_report(self):
        with patch.dict(sys.modules, {"auth": None}):
            self.assertEqual(fbvenom.run_build_check(), 2)
        self.assertFalse(self.report()["auth_imported"])
        self.assertIn("auth_imported", self.report()["failed_checks"])
        self.assertIn("auth import", self.report()["errors"][0])
        self.require_key.assert_not_called()

    def test_old_gui_without_updated_features_is_rejected(self):
        with patch.object(self.modules["gui"], "ReactionGUI", object):
            self.assertEqual(fbvenom.run_build_check(), 2)
        self.assertFalse(self.report()["gui_features"]["diagnostics_panel"])
        self.assertIn("updated_features", self.report()["failed_checks"])

    def test_backup_server_requires_dialog_handler_state_reset_and_mode_helpers(self):
        required = (("gui", "BackUpServ"), ("core", "reset_ai_connection_state"),
                    ("aiconfig", "get_server_mode"), ("aiconfig", "set_server_mode"))
        for module, name in required:
            with self.subTest(module=module, name=name), patch.object(self.modules[module], name, None):
                self.assertEqual(fbvenom.run_build_check(), 2)
                self.assertFalse(self.report()["gui_features"]["backup_server"])
                self.assertIn("updated_features", self.report()["failed_checks"])
        with patch.object(self.modules["gui"].ReactionGUI, "open_backup_server", None):
            self.assertEqual(fbvenom.run_build_check(), 2)
            self.assertFalse(self.report()["gui_features"]["backup_server"])


class WindowsBuildFeatureValidationTests(unittest.TestCase):
    def test_validator_rejects_missing_backup_feature_even_when_report_claims_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "FBVenom.exe"
            path.write_bytes(b"mock executable")
            report = {
                "ok": True, "app_version": "1.1.1", "adb_version": "test adb",
                **{name: True for name in ("frozen", "windows", "adb_bundled", "adb_version_ok",
                   "icon_loaded", "config_beside_exe", "app_beside_exe", "auth_imported",
                   "usb_internet_bundled", "usb_relay_cli_ok")},
                "gui_features": {name: True for name in ("live_link_normalization", "structured_errors",
                    "venom_theme", "diagnostics_panel", "usb_internet")},
                "usb_internet": {"version": "2.5.1", "archive_sha256": "f" * 64},
            }
            def frozen_check(*args, **kwargs):
                Path(kwargs["env"]["FBVENOM_BUILD_CHECK_PATH"]).write_text(json.dumps(report), encoding="utf-8")
                return SimpleNamespace(returncode=0)
            with patch.object(sys, "platform", "win32"), \
                    patch.object(build_validator, "windows_pe_info", return_value={}), \
                    patch.object(build_validator.subprocess, "run", side_effect=frozen_check), \
                    patch.dict(sys.modules, {"version": SimpleNamespace(APP_VERSION="1.1.1")}):
                with self.assertRaisesRegex(RuntimeError, "required application features"):
                    build_validator.validate(path, "source-commit")
                report["gui_features"]["backup_server"] = False
                with self.assertRaisesRegex(RuntimeError, "required application features"):
                    build_validator.validate(path, "source-commit")
                report["gui_features"]["backup_server"] = True
                validated = build_validator.validate(path, "source-commit")
                self.assertTrue(validated["gui_features"]["backup_server"])
                self.assertEqual(validated["source_commit"], "source-commit")
                self.assertEqual(validated["usb_internet"]["version"], "2.5.1")
                report["gui_features"]["usb_internet"] = False
                with self.assertRaisesRegex(RuntimeError, "required application features"):
                    build_validator.validate(path, "source-commit")
                report["gui_features"]["usb_internet"] = True
                report["usb_relay_cli_ok"] = False
                with self.assertRaisesRegex(RuntimeError, "required packaging checks"):
                    build_validator.validate(path, "source-commit")


if __name__ == "__main__":
    unittest.main()
