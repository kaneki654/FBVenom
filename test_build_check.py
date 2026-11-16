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
        self.report_path = self.app / "smoke.json"
        self.require_key = Mock(side_effect=AssertionError("license UI must not run"))
        self.normalize = Mock(return_value="https://www.facebook.com/live/1234567890")
        pixmap = SimpleNamespace(isNull=lambda: False, width=lambda: 256, height=lambda: 256)
        gui = SimpleNamespace(
            app_icon_pixmap=Mock(return_value=pixmap), draw_symbiote=lambda: None,
            SymbioteBanner=object,
            BackUpServ=Mock(side_effect=AssertionError("packaging must not instantiate the server dialog")),
            ReactionGUI=SimpleNamespace(_build_diagnostics=object, open_backup_server=lambda: None))
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
        item = patch.object(subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout="Android Debug Bridge version 1.0.41\nVersion test\n", stderr=""))
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
        args, kwargs = self.adb_run.call_args
        self.assertEqual(args[0], [str(self.adb), "version"])
        self.assertEqual(kwargs["timeout"], 10)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.normalize.assert_called_once_with("https://m.facebook.com/live/1234567890/?fbclid=build-check")

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
        self.adb_run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="Missing DLL")
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
                   "icon_loaded", "config_beside_exe", "app_beside_exe", "auth_imported")},
                "gui_features": {name: True for name in ("live_link_normalization", "structured_errors",
                    "venom_theme", "diagnostics_panel")},
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


if __name__ == "__main__":
    unittest.main()
