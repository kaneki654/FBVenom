"""Offscreen diagnostics integration; no phones, AI calls, or posts are used."""
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import aiconfig
with patch.object(aiconfig, "load", return_value={}):
    import gui
from PyQt6.QtWidgets import QApplication


class DiagnosticsGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patches = [
            patch.object(gui, "ensure_online", return_value={}),
            patch.object(gui.aiconfig, "load", return_value={}),
            patch.object(gui.aiconfig, "is_ready", return_value=False),
            patch.object(gui.aiconfig, "describe", return_value="Test AI settings"),
            patch.object(gui.fbcore, "set_speed"),
            patch.object(gui.fbcore, "DEBUG_LOG", str(Path(self.directory.name) / "debug.log")),
            patch.object(gui, "stop_llama"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.window = gui.ReactionGUI(workspace_path=Path(self.directory.name) / "workspaces.json")
        self.window.timer.stop()
        self.window.anim_timer.stop()
        self.addCleanup(self.window.close)

    def test_worker_failure_is_printable_queued_and_restores_previous_hook(self):
        received = []
        old_hook = received.append
        with patch.object(gui.fbcore, "ERROR_HOOK", old_hook), \
                patch.object(gui.fbcore, "normalize_targets_for_phones", side_effect=RuntimeError("Network unavailable")), \
                patch.object(sys, "stdout", io.StringIO()) as terminal:
            self.window.run_reactions([], "like", [])
            self.assertIs(sys.stdout, terminal)
            self.assertIs(gui.fbcore.ERROR_HOOK, old_hook)
            self.assertIn("SESSION_FAILED", terminal.getvalue())
        # Qt widgets are only updated when the main thread drains the queue.
        self.assertEqual(self.window._diagnostic_events, [])
        self.window.process_queue()
        self.assertEqual(received[0]["code"], "SESSION_FAILED")
        text = self.window.diagnostics_log.toPlainText()
        self.assertIn("SESSION_FAILED", text)
        self.assertIn("Network unavailable", text)
        self.assertIn("Next step:", text)
        self.assertEqual(self.window._diagnostic_errors, 1)
        self.assertFalse(self.window.running)

    def test_diagnostics_keep_context_copy_and_clear(self):
        self.window.handle_cmd({"action": "diagnostic", "event": {
            "code": "COMMENT_NO_BOX", "time": "10:20:30", "serial": "phone-a",
            "account": 2, "link": 3, "url": "https://www.facebook.com/live/123",
            "message": "Comment composer unavailable.", "hint": "Check that comments are enabled.",
        }})
        self.window._copy_diagnostics()
        copied = QApplication.clipboard().text()
        for value in ("10:20:30", "COMMENT_NO_BOX", "phone-a", "account 2", "link 3", "Next step:", "/live/123"):
            self.assertIn(value, copied)
        self.window._clear_diagnostics()
        self.assertEqual(self.window.diagnostics_log.toPlainText(), "")
        self.assertEqual(self.window._diagnostic_events, [])
        self.assertIn("no errors", self.window.diagnostics_toggle.text())

    def test_invalid_link_is_printed_and_retained_in_diagnostics_before_start(self):
        self.window.link_rows[0].url.setText("https://example.com/posts/123")
        with patch.object(gui.threading, "Thread") as worker, \
                patch.object(sys, "stdout", io.StringIO()) as terminal:
            self.window.start_reaction()
        worker.assert_not_called()
        self.assertIn("INVALID_URL", terminal.getvalue())
        self.window.process_queue()
        event = self.window._diagnostic_events[0]
        self.assertEqual(event["code"], "INVALID_URL")
        self.assertEqual(event["link"], 1)
        self.assertIn("Paste a Facebook", event["hint"])

    def test_diagnostics_are_bounded_and_validation_notice_survives_toast(self):
        self.window.handle_cmd({"action": "toast", "text": "Enter a Facebook post link"})
        self.assertEqual(self.window._diagnostic_warnings, 1)
        for n in range(505):
            self.window._append_diagnostic({"code": f"ERROR_{n}", "message": f"Failure {n}"})
        self.assertEqual(len(self.window._diagnostic_events), 500)
        self.assertEqual(self.window._diagnostic_events[0]["code"], "ERROR_5")
        self.window._copy_diagnostics()
        self.assertNotIn("Enter a Facebook post link", QApplication.clipboard().text())
        self.assertIn("ERROR_504", QApplication.clipboard().text())

    def test_missing_app_and_failed_install_have_device_diagnostics(self):
        for apk, expected in ((None, "FACEBOOK_NOT_INSTALLED"),
                              ("/tmp/facebook-lite.apk", "FACEBOOK_INSTALL_FAILED")):
            with self.subTest(code=expected), \
                    patch.object(gui.fbcore, "normalize_targets_for_phones", return_value=[{"url": "https://www.facebook.com/posts/123"}]), \
                    patch.object(gui, "find_apk", return_value=apk), \
                    patch.object(gui, "reset_comment_pool"), \
                    patch.object(gui, "wake_and_unlock"), \
                    patch.object(gui, "wait_online"), \
                    patch.object(gui, "has_fb", return_value=False), \
                    patch.object(gui, "install_fb", return_value=False), \
                    patch.object(sys, "stdout", io.StringIO()):
                self.window._clear_diagnostics()
                self.window.run_reactions([], "like", ["phone-a"])
                self.window.process_queue()
                self.assertEqual(self.window._diagnostic_events[0]["code"], expected)
                self.assertEqual(self.window._diagnostic_events[0]["serial"], "phone-a")

    def test_options_and_diagnostics_stay_accessible_at_minimum_size(self):
        self.window.resize(self.window.minimumSize())
        self.window.show()
        self.app.processEvents()
        self.assertTrue(self.window.cmdbar.isVisible())
        self.assertTrue(self.window.diagnostics_toggle.isVisible())
        self.assertTrue(self.window.session_options.isHidden())
        self.window.session_options_btn.click()
        self.assertFalse(self.window.session_options.isHidden())
        self.window.diagnostics_toggle.click()
        self.assertFalse(self.window.diagnostics_log.isHidden())
        self.assertLessEqual(self.window.diagnostics.geometry().bottom(), self.window.canvas.height())


if __name__ == "__main__":
    unittest.main()
