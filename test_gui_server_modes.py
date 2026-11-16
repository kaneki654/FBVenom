"""Offscreen LAN server dialog checks; no devices or network calls are made."""

import copy
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import aiconfig
with patch.object(aiconfig, "load", return_value={}):
    import gui
from PyQt6.QtWidgets import QApplication, QLineEdit, QWidget


LAN_KEY = "lan-test-secret-" + "k" * 40
LAN_URL = "http://192.168.100.129:7499/v1/chat/completions"
MODELS = ["configured-model", "second-model"]


class LANServerDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = dict(aiconfig.DEFAULTS)
        self.config.update({"provider": "custom", "url": "https://primary.invalid/v1/chat/completions",
                            "api_key": "primary-test-secret-" + "p" * 40,
                            "model": "primary-model", "fallback_model": "primary-fallback"})
        self.profile = {"provider": "lan", "url": LAN_URL, "api_key": LAN_KEY,
                        "model": MODELS[0], "fallback_model": MODELS[0],
                        "min_gap": 1.0, "timeout": 300}
        self._patch("gui.aiconfig.load", side_effect=lambda *a, **kw: copy.deepcopy(self.config))
        self._patch("gui.aiconfig.get_server_mode", return_value="primary")
        self._patch("gui.aiconfig.get_lan_profile", side_effect=lambda *a, **kw: dict(self.profile))
        self.local_key = self._patch("gui.aiconfig.get_local_lan_key", return_value="", create=True)
        self.validate = self._patch("gui.aiconfig.validate_lan_connection", return_value=list(MODELS))
        self.activate = self._patch("gui.aiconfig.set_server_mode", return_value=dict(self.config))
        self._patch("gui.aiconfig.is_ready", return_value=False)
        self._patch("gui.aiconfig.describe", return_value="Test AI settings")
        self._patch("gui.ensure_online", return_value={})
        self._patch("gui.fbcore.set_speed")
        self._patch("gui.stop_llama")
        self._patch("gui.fbcore.reset_ai_connection_state", create=True)
        self.dialogs = []
        self.windows = []
        self.workers = []
        self.addCleanup(self._close_windows)
        self.dialog = self._new_dialog()

    def _patch(self, target, **kwargs):
        item = patch(target, **kwargs)
        value = item.start()
        self.addCleanup(item.stop)
        return value

    def _new_dialog(self, parent=None):
        dialog = gui.BackUpServ(parent)
        dialog.url.setText(LAN_URL)
        dialog.key.setText(LAN_KEY)
        dialog.model.setCurrentText(MODELS[0])
        self.dialogs.append(dialog)
        return dialog

    def _close_windows(self):
        for event in self.workers:
            event.set()
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        for window in self.windows:
            window.running = False
            window.close()
            window.deleteLater()
        self.app.processEvents()

    def _wait(self, predicate, dialog=None):
        dialog = dialog or self.dialog
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app.processEvents()
            dialog._drain()
            if predicate():
                return
            time.sleep(0.002)
        self.fail("LAN dialog worker did not settle within three seconds")

    def _block_validation(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        self.workers.append(release)
        observed = []
        def validate(profile):
            observed.append((threading.get_ident(), copy.deepcopy(profile)))
            entered.set()
            try:
                if not release.wait(3):
                    raise RuntimeError("test worker was not released")
                return list(MODELS)
            finally:
                finished.set()
        self.validate.side_effect = validate
        return entered, release, finished, observed

    def test_controls_explain_actions_and_mask_lan_key(self):
        dialog = self.dialog
        self.assertEqual(dialog.key.echoMode(), QLineEdit.EchoMode.Password)
        self.assertTrue(dialog.model.isEditable())
        self.assertIn("Test connection", dialog.test_btn.text())
        self.assertIn("Use LAN server", dialog.apply_btn.text())
        self.assertIn("Use primary server", dialog.primary_btn.text())
        self.assertNotIn(LAN_KEY, dialog.status.text())
        self.activate.assert_not_called()
        self.validate.assert_not_called()

    def test_explicit_local_key_load_masks_secret_and_invalidates_previous_verification(self):
        loaded_key = "loaded-lan-test-key-" + "z" * 40
        self.local_key.return_value = loaded_key
        self.dialog._verified = True
        self.dialog.local_key_btn.click()
        self.local_key.assert_called_once_with(LAN_URL)
        self.assertEqual(self.dialog.key.text(), loaded_key)
        self.assertEqual(self.dialog.key.echoMode(), QLineEdit.EchoMode.Password)
        self.assertFalse(self.dialog._verified)
        self.assertIn("loaded", self.dialog.status.text().lower())
        self.assertNotIn(loaded_key, self.dialog.status.text())
        self.assertNotIn(loaded_key, self.dialog.current_mode.text())
        self.validate.assert_not_called()
        self.activate.assert_not_called()

    def test_unavailable_local_key_preserves_manual_key_and_gives_remote_setup_instruction(self):
        self.local_key.return_value = ""
        self.dialog.local_key_btn.click()
        self.local_key.assert_called_once_with(LAN_URL)
        self.assertEqual(self.dialog.key.text(), LAN_KEY)
        status = self.dialog.status.text().lower()
        self.assertIn("no local key", status)
        self.assertIn("another computer", status)
        self.assertIn("enter", status)
        self.assertNotIn(LAN_KEY, self.dialog.status.text())
        self.validate.assert_not_called()
        self.activate.assert_not_called()

    def test_local_key_load_is_disabled_and_ignored_while_connection_check_is_busy(self):
        entered, release, finished, _ = self._block_validation()
        self.dialog._start_check()
        self.assertTrue(entered.wait(1))
        self.assertFalse(self.dialog.local_key_btn.isEnabled())
        self.dialog.local_key_btn.click()
        self.dialog._load_local_key()
        self.local_key.assert_not_called()
        self.assertEqual(self.dialog.key.text(), LAN_KEY)
        release.set()
        self._wait(lambda: not self.dialog._busy)
        self.assertTrue(finished.is_set())
        self.assertTrue(self.dialog.local_key_btn.isEnabled())

    def test_changing_gateway_origin_clears_credential_before_another_request(self):
        for address in ("http://192.168.100.130:7499/v1", "http://192.168.100.129:7500/v1",
                        "https://192.168.100.129:7499/v1"):
            with self.subTest(address=address):
                self.dialog.url.setText(LAN_URL)
                self.dialog.key.setText(LAN_KEY)
                self.dialog._verified = True
                self.dialog.url.setText(address)
                self.assertEqual(self.dialog.key.text(), "")
                self.assertFalse(self.dialog._verified)
                self.dialog._start_check(activate=True)
                self.assertFalse(self.dialog._busy)
        self.local_key.assert_not_called()
        self.validate.assert_not_called()
        self.activate.assert_not_called()

    def test_same_origin_path_edits_keep_credential_but_require_revalidation(self):
        for address in ("http://192.168.100.129:7499", "http://192.168.100.129:7499/v1/",
                        LAN_URL):
            with self.subTest(address=address):
                self.dialog._verified = True
                self.dialog.url.setText(address)
                self.assertEqual(self.dialog.key.text(), LAN_KEY)
                self.assertFalse(self.dialog._verified)
        self.local_key.assert_not_called()
        self.validate.assert_not_called()
        self.activate.assert_not_called()

    def test_prefilled_profile_is_ready_without_manual_key_entry_or_secret_instructions(self):
        dialog = gui.BackUpServ()
        self.dialogs.append(dialog)
        self.assertEqual(dialog.key.text(), LAN_KEY)
        self.assertEqual(dialog.model.currentText(), MODELS[0])
        self.assertEqual(dialog.key.echoMode(), QLineEdit.EchoMode.Password)
        self.assertIn("ready", dialog.status.text().lower())
        self.assertNotIn("enter the lan api key", dialog.status.text().lower())
        self.assertNotIn(LAN_KEY, dialog.status.text())
        self.assertTrue(dialog.local_key_btn.isEnabled())
        self.assertTrue(dialog.apply_btn.isEnabled())
        self.local_key.assert_not_called()
        self.activate.assert_not_called()
        self.validate.assert_not_called()

    def test_test_only_fetches_models_without_persisting_or_switching(self):
        self.dialog._start_check()
        self._wait(lambda: not self.dialog._busy)
        self.validate.assert_called_once()
        self.activate.assert_not_called()
        self.assertEqual({self.dialog.model.itemText(i) for i in range(self.dialog.model.count())}, set(MODELS))
        self.assertEqual(self.dialog.model.currentText(), MODELS[0])
        self.assertNotIn(LAN_KEY, self.dialog.status.text())
        self.assertTrue(self.dialog._verified)

    def test_activation_requires_validation_and_saves_exact_verified_draft(self):
        self.dialog._start_check(activate=True)
        self._wait(lambda: not self.dialog._busy)
        self.validate.assert_called_once()
        self.activate.assert_called_once()
        args, kwargs = self.activate.call_args
        self.assertEqual(args[0], "lan")
        profile = args[1] if len(args) > 1 else kwargs["profile"]
        self.assertEqual(profile["url"], LAN_URL)
        self.assertEqual(profile["api_key"], LAN_KEY)
        self.assertEqual(profile["model"], MODELS[0])
        self.assertEqual(self.validate.call_args.args[0], profile)
        self.assertIn("LAN", self.dialog.current_mode.text())
        self.dialog._activate_primary()
        self.assertEqual(self.activate.call_count, 2)
        self.assertEqual(self.activate.call_args.args[0], "primary")
        self.assertIn("Primary", self.dialog.current_mode.text())
        self.assertEqual(self.dialog.url.text(), LAN_URL)
        self.validate.assert_called_once()

    def test_primary_restore_does_not_contact_lan_server(self):
        self.dialog._activate_primary()
        self.activate.assert_called_once()
        self.assertEqual(self.activate.call_args.args[0], "primary")
        self.validate.assert_not_called()
        self.assertNotIn(LAN_KEY, self.dialog.status.text())

    def test_validation_runs_off_gui_thread_and_fields_lock_until_completion(self):
        entered, release, finished, observed = self._block_validation()
        self.dialog._start_check(activate=True)
        self.assertTrue(entered.wait(1))
        self.assertTrue(self.dialog._busy)
        for widget in (self.dialog.url, self.dialog.key, self.dialog.model, self.dialog.local_key_btn,
                       self.dialog.test_btn, self.dialog.apply_btn, self.dialog.primary_btn):
            self.assertFalse(widget.isEnabled())
        self.assertNotEqual(observed[0][0], threading.get_ident())
        self.activate.assert_not_called()
        release.set()
        self._wait(lambda: not self.dialog._busy)
        self.assertTrue(finished.is_set())
        self.activate.assert_called_once()
        self.assertTrue(self.dialog.url.isEnabled())
        self.assertTrue(self.dialog.test_btn.isEnabled())

    def test_failed_connection_keeps_primary_and_sanitizes_error_text(self):
        self.validate.side_effect = RuntimeError("Connection refused; token=" + LAN_KEY)
        self.dialog._start_check(activate=True)
        self._wait(lambda: not self.dialog._busy)
        self.activate.assert_not_called()
        self.assertNotIn(LAN_KEY, self.dialog.status.text())
        self.assertTrue(self.dialog.status.text().strip())
        self.assertTrue(self.dialog.apply_btn.isEnabled())
        self.assertTrue(self.dialog.url.isEnabled())

    def test_unknown_selected_model_does_not_activate_or_persist(self):
        self.dialog.model.setCurrentText("not-on-this-server")
        self.dialog._start_check(activate=True)
        self._wait(lambda: not self.dialog._busy)
        self.activate.assert_not_called()
        self.assertIn("model", self.dialog.status.text().lower())
        self.assertNotIn(LAN_KEY, self.dialog.status.text())

    def test_invalid_draft_does_not_spawn_network_work_or_save(self):
        self.dialog.url.setText("http://8.8.8.8:7499/v1")
        self.dialog._start_check(activate=True)
        self.assertFalse(self.dialog._busy)
        self.validate.assert_not_called()
        self.activate.assert_not_called()
        self.assertTrue(self.dialog.status.text().strip())

    def test_changed_draft_discards_late_connection_result(self):
        entered, release, finished, _ = self._block_validation()
        self.dialog._start_check(activate=True)
        self.assertTrue(entered.wait(1))
        # Even programmatic changes during disabled controls invalidate results.
        self.dialog.url.setText("http://192.168.100.130:7499/v1")
        release.set()
        self._wait(lambda: not self.dialog._busy)
        self.assertTrue(finished.is_set())
        self.activate.assert_not_called()
        self.assertFalse(self.dialog._verified)

    def test_closing_dialog_discards_late_worker_result(self):
        for action in ("reject", "close"):
            with self.subTest(action=action):
                dialog = self._new_dialog()
                dialog.show()
                entered, release, finished, _ = self._block_validation()
                dialog._start_check(activate=True)
                self.assertTrue(entered.wait(1))
                getattr(dialog, action)()
                release.set()
                self.assertTrue(finished.wait(1))
                self.app.processEvents()
                dialog._drain()
                self.activate.assert_not_called()

    def test_session_started_during_check_blocks_late_server_switch(self):
        parent = QWidget()
        parent.running = False
        self.addCleanup(parent.deleteLater)
        dialog = self._new_dialog(parent)
        entered, release, finished, _ = self._block_validation()
        dialog._start_check(activate=True)
        self.assertTrue(entered.wait(1))
        parent.running = True
        release.set()
        self._wait(lambda: not dialog._busy, dialog)
        self.assertTrue(finished.is_set())
        self.activate.assert_not_called()
        self.assertIn("session", dialog.status.text().lower())
        dialog._activate_primary()
        self.activate.assert_not_called()

    def test_save_failure_is_visible_and_not_misreported_as_active(self):
        self.activate.side_effect = OSError("Read-only config file")
        self.dialog._start_check(activate=True)
        self._wait(lambda: not self.dialog._busy)
        self.activate.assert_called_once()
        self.assertTrue(any(word in self.dialog.status.text().lower()
                            for word in ("failed", "save", "could not", "error")))
        self.assertNotIn(LAN_KEY, self.dialog.status.text())
        self.assertTrue(self.dialog.apply_btn.isEnabled())
        self.activate.reset_mock()
        self.dialog._activate_primary()
        self.activate.assert_called_once()
        self.assertEqual(self.activate.call_args.args[0], "primary")
        self.assertTrue(any(word in self.dialog.status.text().lower()
                            for word in ("failed", "save", "could not", "error")))

    def test_config_load_error_is_visible_without_switching_or_exposing_secret(self):
        with patch.object(gui.aiconfig, "load", side_effect=OSError("config failed " + LAN_KEY)):
            dialog = self._new_dialog()
        self.activate.assert_not_called()
        self.validate.assert_not_called()
        self.assertTrue(any(word in dialog.status.text().lower()
                            for word in ("load", "settings", "config", "read")))
        self.assertNotIn(LAN_KEY, dialog.status.text())
        self.assertTrue(dialog.url.isEnabled())

    def test_status_refresh_error_after_save_keeps_successful_mode(self):
        parent = QWidget()
        parent.refresh_ai_status = Mock(side_effect=RuntimeError("callback failed " + LAN_KEY))
        self.addCleanup(parent.deleteLater)
        dialog = self._new_dialog(parent)
        dialog._save_mode("lan", dict(self.profile))
        self.activate.assert_called_once()
        parent.refresh_ai_status.assert_called_once()
        self.assertEqual(dialog._mode, "lan")
        self.assertIn("LAN", dialog.current_mode.text())
        self.assertNotIn(LAN_KEY, dialog.status.text())
        self.assertNotIn("has not changed", dialog.status.text())

    def _ai_settings(self):
        dialog = gui.AISettings()
        self.dialogs.append(dialog)
        return dialog

    def _set_active_lan_with_primary(self, provider="openai"):
        primary = {"provider": provider,
                   "url": "https://primary.invalid/custom/v1/chat/completions",
                   "api_key": "separate-primary-key-" + "p" * 40,
                   "model": "saved-primary-model", "fallback_model": "saved-primary-fallback",
                   "min_gap": 2.3, "timeout": 90}
        self.config.update(self.profile)
        self.config.update({"server_mode": "lan", "primary_profile": primary,
                            "lan_profile": dict(self.profile)})
        return primary

    def test_ai_settings_selecting_lan_uses_only_separate_lan_credentials(self):
        self.config["provider"] = "openai"
        dialog = self._ai_settings()
        primary_key = dialog.key.text()
        self.assertNotEqual(primary_key, LAN_KEY)
        with patch.object(gui.aiconfig, "save") as save:
            dialog.provider.setCurrentIndex(dialog._keys.index("lan"))
        save.assert_not_called()
        draft = dialog._collect()
        self.assertEqual(draft["provider"], "lan")
        self.assertEqual(draft["api_key"], LAN_KEY)
        self.assertNotEqual(draft["api_key"], primary_key)
        self.assertEqual(draft["url"], self.profile["url"])
        self.assertEqual(draft["model"], self.profile["model"])
        self.assertEqual(draft["fallback_model"], self.profile["fallback_model"])
        self.assertEqual(draft["min_gap"], self.profile["min_gap"])

    def test_ai_settings_empty_lan_key_never_inherits_primary_key(self):
        self.profile["api_key"] = ""
        self.config["provider"] = "openai"
        dialog = self._ai_settings()
        self.assertTrue(dialog.key.text())
        dialog.provider.setCurrentIndex(dialog._keys.index("lan"))
        self.assertEqual(dialog.key.text(), "")
        self.assertEqual(dialog._collect()["api_key"], "")

    def test_ai_settings_lan_to_saved_primary_restores_its_connection(self):
        primary = self._set_active_lan_with_primary()
        dialog = self._ai_settings()
        self.assertEqual(dialog.key.text(), LAN_KEY)
        for provider in ("openai", "lan", "openai"):
            dialog.provider.setCurrentIndex(dialog._keys.index(provider))
            expected = self.profile if provider == "lan" else primary
            draft = dialog._collect()
            self.assertEqual(draft["provider"], expected["provider"])
            for field in ("api_key", "url", "model", "fallback_model", "min_gap"):
                self.assertEqual(draft[field], expected[field])
        self.assertNotIn(LAN_KEY, dialog.result.text())

    def test_ai_settings_lan_to_another_provider_clears_both_saved_keys(self):
        primary = self._set_active_lan_with_primary()
        dialog = self._ai_settings()
        dialog.provider.setCurrentIndex(dialog._keys.index("groq"))
        draft = dialog._collect()
        self.assertEqual(draft["provider"], "groq")
        self.assertEqual(draft["url"], aiconfig.PRESETS["groq"]["url"])
        self.assertEqual(draft["api_key"], "")
        self.assertNotEqual(draft["api_key"], primary["api_key"])
        self.assertNotEqual(draft["api_key"], LAN_KEY)

    def test_main_backup_button_routes_to_dialog_and_refreshes_status(self):
        window = gui.ReactionGUI(workspace_path=Path(self.directory.name) / "workspaces.json")
        window.timer.stop()
        window.anim_timer.stop()
        self.windows.append(window)
        self.assertTrue(window.backup_btn.isEnabled())
        with patch.object(gui, "BackUpServ") as dialog_type, \
                patch.object(window, "refresh_ai_status") as refresh:
            window.backup_btn.click()
        dialog_type.assert_called_once_with(window)
        dialog_type.return_value.exec.assert_called_once()
        refresh.assert_called_once()
        window.running = True
        window._set_session_controls()
        self.assertFalse(window.backup_btn.isEnabled())
        with patch.object(gui, "BackUpServ") as dialog_type:
            window.open_backup_server()
        dialog_type.assert_not_called()
        window.running = False
        window._set_session_controls()
        self.assertTrue(window.backup_btn.isEnabled())


if __name__ == "__main__":
    unittest.main()
