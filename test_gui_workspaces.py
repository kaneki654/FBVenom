"""Offscreen workspace integration checks; no phones or AI calls are used.

Run explicitly with ``python3 -m unittest test_gui_workspaces -v``.
"""

import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import aiconfig

# core creates a default persona at import time. Keep even that initial read
# independent of the developer's saved AI keys and settings.
with patch.object(aiconfig, "load", return_value={}):
    import gui

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication, QListWidget


class WorkspaceGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "workspaces.json"
        self.devices = {"phone-a": "device", "phone-b": "device",
                        "phone-c": "device"}
        self.discovery = self._patch("gui.ensure_online",
                                     side_effect=lambda: dict(self.devices))
        self._patch("gui.aiconfig.is_ready", return_value=False)
        self._patch("gui.aiconfig.describe", return_value="Test AI settings")
        self._patch("gui.aiconfig.load", return_value={})
        self._patch("gui.fbcore.set_speed")
        # Construction and start validation are exercised, but a worker is
        # never started, so no ADB, provider, or posting operation can occur.
        self.threads = self._patch("gui.threading.Thread")
        self.windows = []
        self.addCleanup(self._close_windows)
        self.window = self._new_window()

    def _patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def _new_window(self):
        window = gui.ReactionGUI(workspace_path=self.path)
        window.timer.stop()
        window.anim_timer.stop()
        self.windows.append(window)
        return window

    def _close_windows(self):
        for window in self.windows:
            window.running = False
            window.close()
            window.deleteLater()
        self.app.processEvents()

    def _workspace(self, name="Alpha", count=1):
        self.window.feature_tabs.setCurrentIndex(1)
        workspace = self.window.create_workspace(name, count)
        self.assertIsNotNone(workspace)
        self.assertEqual(self.window.current_workspace_id, workspace["id"])
        return workspace

    def _draft(self, url="https://www.facebook.com/example/posts/123",
               comment="Example comment"):
        row = self.window.link_rows[0]
        row.url.setText(url)
        row.comment.setText(comment)
        return row

    def _assert_assigned_highlights(self):
        window = self.window
        highlighted = {item.data(Qt.ItemDataRole.UserRole)
                       for item in window.dev_list.selectedItems()}
        self.assertEqual(highlighted, set(window.get_selected_devices()))
        self.assertEqual(window.dev_list.selectionMode(),
                         QListWidget.SelectionMode.NoSelection)

    def test_named_drafts_and_original_tab_restore_independently(self):
        window = self.window
        self._draft(comment="Original draft")
        window.screenshot_check.setChecked(True)
        window.dose_rate.set_index(1)
        for serial, item in window.dev_items.items():
            item.setSelected(serial == "phone-b")

        alpha = self._workspace("Alpha", 1)
        self._draft(comment="Alpha draft")
        unfinished = window.add_link_row()
        unfinished.comment.setText("Unfinished row has no URL yet")
        window.screenshot_check.setChecked(False)
        window.dose_rate.set_index(0)

        beta = self._workspace("Beta", 1)
        self._draft(comment="Beta draft")
        self.assertTrue(window.select_workspace(alpha["id"]))
        self.assertEqual(window.link_rows[0].comment.text(), "Alpha draft")
        self.assertEqual(len(window.link_rows), 2)
        self.assertEqual(window.link_rows[1].url.text(), "")
        self.assertEqual(window.link_rows[1].comment.text(),
                         "Unfinished row has no URL yet")
        self.assertFalse(window.screenshot_check.isChecked())
        self.assertEqual(window.dose_rate.profile(), "safe")

        window.feature_tabs.setCurrentIndex(0)
        self.assertEqual(window.link_rows[0].comment.text(), "Original draft")
        self.assertEqual(len(window.link_rows), 1)
        self.assertEqual(window.get_selected_devices(), ["phone-b"])
        self.assertTrue(window.screenshot_check.isChecked())
        self.assertEqual(window.dose_rate.profile(), "fast")

        window.feature_tabs.setCurrentIndex(1)
        self.assertEqual(window.link_rows[0].comment.text(), "Alpha draft")
        self.assertTrue(window.select_workspace(beta["id"]))
        self.assertEqual(window.link_rows[0].comment.text(), "Beta draft")

    def test_names_and_device_counts_are_validated_before_mutating(self):
        window = self.window
        alpha = self._workspace("Alpha", 1)
        original_id = alpha["id"]
        for name, count in (("", 1), ("   ", 1), ("Alpha", 1),
                            ("alpha", 1), ("Bad count", 0),
                            ("Bad count", -1)):
            with self.subTest(name=name, count=count):
                self.assertIsNone(window.create_workspace(name, count))
                self.assertEqual(window.current_workspace_id, original_id)
                self.assertEqual(window.workspace_picker.count(), 1)
        self.assertFalse(window.edit_workspace("", 1))
        self.assertFalse(window.edit_workspace("Alpha", 0))
        self.assertTrue(window.edit_workspace("Renamed", 2))
        self.assertEqual(window.current_workspace_id, original_id)
        self.assertEqual(len(window.get_selected_devices()), 2)
        self.assertIn("Renamed", window.workspace_picker.currentText())

    def test_workspace_devices_are_distinct_and_survive_rescan(self):
        window = self.window
        alpha = self._workspace("Alpha", 2)
        alpha_devices = set(window.get_selected_devices())
        self._assert_assigned_highlights()
        beta = self._workspace("Beta", 1)
        beta_devices = set(window.get_selected_devices())
        self._assert_assigned_highlights()
        self.assertEqual(len(alpha_devices), 2)
        self.assertEqual(len(beta_devices), 1)
        self.assertFalse(alpha_devices & beta_devices)
        self.assertEqual(alpha_devices | beta_devices, set(self.devices))

        self.devices = dict(reversed(list(self.devices.items())))
        window.refresh_devices()
        self.assertEqual(set(window.get_selected_devices()), beta_devices)
        self._assert_assigned_highlights()
        self.assertTrue(window.select_workspace(alpha["id"]))
        self.assertEqual(set(window.get_selected_devices()), alpha_devices)
        self._assert_assigned_highlights()
        window.refresh_devices()
        self.assertEqual(set(window.get_selected_devices()), alpha_devices)
        self.assertTrue(window.select_workspace(beta["id"]))
        self.assertEqual(set(window.get_selected_devices()), beta_devices)
        self._assert_assigned_highlights()

    def test_failed_creation_rolls_back_metadata_and_keeps_existing_draft(self):
        window = self.window
        alpha = self._workspace("Alpha", 2)
        saved = self.path.read_bytes()
        self._draft(comment="Unsaved alpha draft")
        window._capture_workspace()
        previous = copy.deepcopy(window.workspace_store.workspaces)
        with patch.object(window.workspace_store, "save", side_effect=OSError("Disk full")):
            self.assertIsNone(window.create_workspace("Beta", 1))
        self.assertEqual(window.workspace_store.workspaces, previous)
        self.assertEqual(window.current_workspace_id, alpha["id"])
        self.assertEqual(window.link_rows[0].comment.text(), "Unsaved alpha draft")
        self.assertEqual(window.workspace_picker.count(), 1)
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertIn("Disk full", window.workspace_notice.text())
        self.assertFalse(window.workspace_save_error.isHidden())
        window._refresh_workspace_assignments()
        self.assertIn("Disk full", window.workspace_notice.text())
        self._assert_assigned_highlights()
        self.assertTrue(window._save_workspaces())
        self.assertTrue(window.workspace_save_error.isHidden())

    def test_failed_edit_rolls_back_name_count_allocations_and_keeps_draft(self):
        window = self.window
        alpha = self._workspace("Alpha", 2)
        self._workspace("Beta", 1)
        window.select_workspace(alpha["id"])
        saved = self.path.read_bytes()
        self._draft(comment="Unsaved before rename")
        window._capture_workspace()
        previous = copy.deepcopy(window.workspace_store.workspaces)
        with patch.object(window.workspace_store, "save", side_effect=OSError("Read-only disk")):
            self.assertFalse(window.edit_workspace("Renamed", 1))
        self.assertEqual(window.workspace_store.workspaces, previous)
        self.assertEqual(window.link_rows[0].comment.text(), "Unsaved before rename")
        self.assertIn("Alpha", window.workspace_picker.currentText())
        self.assertNotIn("Renamed", window.workspace_picker.currentText())
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertIn("Read-only disk", window.workspace_notice.text())
        self._assert_assigned_highlights()

    def test_workspace_dialog_stays_open_and_shows_save_failure(self):
        window = self.window
        window.feature_tabs.setCurrentIndex(1)
        dialog = gui.WorkspaceDialog(window)
        dialog.name.setText("Alpha")
        with patch.object(dialog, "accept") as accept, \
                patch.object(window.workspace_store, "save", side_effect=OSError("Disk full")):
            dialog._submit()
        accept.assert_not_called()
        self.assertEqual(dialog.name.text(), "Alpha")
        self.assertIn("Disk full", dialog.error.text())
        self.assertFalse(window.workspace_store.workspaces)
        dialog.deleteLater()

    def test_failed_close_preserves_draft_and_error_on_original_tab(self):
        window = self.window
        alpha = self._workspace("Alpha", 1)
        self._draft(comment="Keep this if the save fails")
        saved = self.path.read_bytes()
        with patch.object(window.workspace_store, "save", side_effect=OSError("Disk full")):
            window.feature_tabs.setCurrentIndex(0)
            event = QCloseEvent()
            window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertEqual(window.workspace_store.get(alpha["id"])["links"][0]["comment"],
                         "Keep this if the save fails")
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertTrue(window.workspace_bar.isHidden())
        self.assertFalse(window.workspace_save_error.isHidden())
        self.assertIn("Disk full", window.workspace_save_error.text())
        self.assertIn("still open", window.workspace_save_error.text())

        retry = QCloseEvent()
        window.closeEvent(retry)
        self.assertTrue(retry.isAccepted())
        self.assertTrue(window.workspace_save_error.isHidden())
        restored = self._new_window()
        restored.select_workspace(alpha["id"])
        self.assertEqual(restored.link_rows[0].comment.text(), "Keep this if the save fails")

    def test_clean_close_does_not_write_even_when_autosave_timer_is_active(self):
        window = self.window
        self._workspace("Alpha", 1)
        window._workspace_save_timer.start(400)
        with patch.object(window.workspace_store, "save", side_effect=OSError("Read-only disk")) as save:
            event = QCloseEvent()
            window.closeEvent(event)
        self.assertTrue(event.isAccepted())
        save.assert_not_called()

    def test_failed_creation_without_other_changes_does_not_block_close(self):
        window = self.window
        window.feature_tabs.setCurrentIndex(1)
        with patch.object(window.workspace_store, "save", side_effect=OSError("Read-only disk")) as save:
            self.assertIsNone(window.create_workspace("Alpha", 1))
            self.assertFalse(window.workspace_save_error.isHidden())
            save.reset_mock()
            self.assertTrue(window._save_workspaces())
            self.assertTrue(window.workspace_save_error.isHidden())
            event = QCloseEvent()
            window.closeEvent(event)
        self.assertTrue(event.isAccepted())
        save.assert_not_called()

    def test_insufficient_online_or_unmuted_devices_blocks_launch(self):
        window = self.window
        self._workspace("Needs two", 2)
        self._draft()
        self.devices = {"phone-a": "device", "phone-b": "offline",
                        "phone-c": "unauthorized"}
        window.refresh_devices()
        self.threads.reset_mock()
        window.start_reaction()
        self.assertFalse(window.running)
        self.threads.assert_not_called()
        self.assertTrue(set(window.get_selected_devices()) <= {"phone-a"})

        self.devices = {serial: "device" for serial in self.devices}
        window.refresh_devices()
        window._set_device_comment("phone-a", False)
        window._set_device_comment("phone-b", False)
        self.threads.reset_mock()
        window.start_reaction()
        self.assertFalse(window.running)
        self.threads.assert_not_called()
        self.assertTrue(set(window.get_selected_devices()) <= {"phone-c"})

    def test_assigned_session_uses_existing_runner_and_locks_workspace(self):
        window = self.window
        alpha = self._workspace("Alpha", 1)
        beta = self._workspace("Beta", 2)
        self._draft(comment="Preserve this example")
        window.screenshot_check.setChecked(True)
        assigned = window.get_selected_devices()
        self.threads.reset_mock()
        window.start_reaction()

        self.threads.assert_called_once()
        launch = self.threads.call_args.kwargs
        self.assertEqual(launch["target"], window.run_reactions)
        targets, reaction, devices, example, do_react, do_comment, shots = launch["args"]
        self.assertEqual(devices, assigned)
        self.assertEqual(len(devices), 2)
        self.assertEqual(example, "Preserve this example")
        self.assertEqual(targets[0]["comment"], example)
        self.assertFalse(do_react)
        self.assertTrue(do_comment)
        self.assertTrue(shots)
        self.assertTrue(window.running)
        self.assertFalse(window.feature_tabs.isEnabled())
        self.assertFalse(window.workspace_picker.isEnabled())
        self.assertFalse(window.select_workspace(alpha["id"]))
        self.assertFalse(window.edit_workspace("During run", 1))
        self.assertFalse(window.delete_workspace())
        self.assertIsNone(window.create_workspace("During run", 1))
        self.assertEqual(window.current_workspace_id, beta["id"])
        self.assertEqual(window.active_devices, assigned)

        window.reaction_finished()
        self.assertFalse(window.running)
        self.assertTrue(window.feature_tabs.isEnabled())
        self.assertTrue(window.workspace_picker.isEnabled())
        self.assertTrue(window.select_workspace(alpha["id"]))

    def test_deleting_workspaces_releases_devices_and_clears_final_draft(self):
        window = self.window
        alpha = self._workspace("Alpha", 2)
        beta = self._workspace("Beta", 1)
        self._draft(comment="Keep beta")
        self.assertTrue(window.select_workspace(alpha["id"]))
        self.assertTrue(window.delete_workspace())
        self.assertEqual(window.current_workspace_id, beta["id"])
        self.assertEqual(window.link_rows[0].comment.text(), "Keep beta")
        self.assertIsNone(window.workspace_store.get(alpha["id"]))
        self.assertTrue(window.edit_workspace("Beta", 3))
        self.assertEqual(set(window.get_selected_devices()), set(self.devices))

        self.assertTrue(window.delete_workspace())
        self.assertIsNone(window.current_workspace_id)
        self.assertFalse(window.workspace_store.workspaces)
        self.assertEqual(window.link_rows[0].comment.text(), "")
        self.assertEqual(window.get_selected_devices(), [])
        self.assertFalse(window.start_btn.isEnabled())
        self.assertFalse(window.cfg_panel.isEnabled())
        restored = self._new_window()
        self.assertFalse(restored.workspace_store.workspaces)

    def test_empty_workspace_tab_cannot_launch_a_session(self):
        window = self.window
        window.feature_tabs.setCurrentIndex(1)
        self.assertFalse(window.start_btn.isEnabled())
        self.assertFalse(window.cfg_panel.isEnabled())
        self._draft()
        self.threads.reset_mock()
        window.start_reaction()
        self.threads.assert_not_called()
        self.assertFalse(window.running)

    def test_runner_finished_event_unlocks_controls_after_early_return(self):
        window = self.window
        self._workspace("Alpha", 1)
        self._draft()
        self.threads.reset_mock()
        window.start_reaction()
        args = self.threads.call_args.kwargs["args"]
        window.process_queue()
        self.assertEqual(set(window.device_cards), set(args[2]))
        self.assertFalse(window.feature_tabs.isEnabled())

        # Exercise the real runner's early-return/finally path without URL
        # resolution, provider activity, or any phone operations.
        with patch.object(gui.fbcore, "normalize_targets_for_phones", return_value=[]), \
                patch.object(gui.fbcore, "STOP_REQUESTED", None), \
                patch.object(gui.fbcore, "PROGRESS_HOOK", None), \
                patch("gui.stop_llama"):
            window.run_reactions(*args)
        self.assertTrue(window.running)
        window.process_queue()
        self.assertFalse(window.running)
        self.assertTrue(window.feature_tabs.isEnabled())
        self.assertTrue(window.workspace_picker.isEnabled())
        self.assertTrue(window.start_btn.isEnabled())
        self.assertFalse(window.stop_btn.isEnabled())

    def test_saved_workspace_drafts_restore_after_restart(self):
        window = self.window
        alpha = self._workspace("Alpha", 2)
        self._draft(comment="Saved alpha")
        unfinished = window.add_link_row()
        unfinished.comment.setText("Saved unfinished example")
        window.screenshot_check.setChecked(True)
        window.dose_rate.set_index(1)
        beta = self._workspace("Beta", 1)
        self._draft(comment="Saved beta")
        window._capture_workspace()
        window._save_workspaces()
        self.assertTrue(self.path.exists())

        restored = self._new_window()
        restored.feature_tabs.setCurrentIndex(1)
        self.assertEqual(restored.workspace_picker.count(), 2)
        self.assertTrue(restored.select_workspace(alpha["id"]))
        self.assertEqual(restored.link_rows[0].comment.text(), "Saved alpha")
        self.assertEqual(restored.link_rows[1].comment.text(),
                         "Saved unfinished example")
        self.assertTrue(restored.screenshot_check.isChecked())
        self.assertEqual(restored.dose_rate.profile(), "fast")
        self.assertEqual(len(restored.get_selected_devices()), 2)
        self.assertTrue(restored.select_workspace(beta["id"]))
        self.assertEqual(restored.link_rows[0].comment.text(), "Saved beta")
        self.assertEqual(len(restored.get_selected_devices()), 1)

    def test_original_comments_tab_still_uses_manual_device_selection(self):
        window = self.window
        self._draft()
        for serial, item in window.dev_items.items():
            item.setSelected(serial in ("phone-a", "phone-c"))
        self.threads.reset_mock()
        window.start_reaction()
        self.threads.assert_called_once()
        launch = self.threads.call_args.kwargs
        self.assertEqual(launch["target"], window.run_reactions)
        self.assertEqual(set(launch["args"][2]), {"phone-a", "phone-c"})
        self.assertFalse(launch["args"][4])
        self.assertTrue(launch["args"][5])
        window.reaction_finished()


if __name__ == "__main__":
    unittest.main()
