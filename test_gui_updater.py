"""Offscreen updater lifecycle checks; network, installation and quitting are mocked."""

import copy
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
from PyQt6.QtWidgets import QApplication, QMessageBox


MANIFEST = {"version": "999.0.0", "url": "https://updates.example/FBVenom.exe",
            "sha256": "a" * 64, "notes": "A tested update"}


class UpdaterDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.directory_path = Path(self.directory.name)
        self._patch("gui.updater.APP_DIR", self.directory.name)
        self._patch("gui.updater.load_settings", return_value={"manifest_url": "https://updates.example/manifest.json"})
        self._patch("gui.updater.can_downgrade", return_value=False)
        self._patch("gui.updater.previous_build_version", return_value="")
        self.check = self._patch("gui.updater.check", return_value=(copy.deepcopy(MANIFEST), True))
        self.download = self._patch("gui.updater.download", side_effect=lambda manifest, report: self._stage())
        self.install = self._patch("gui.updater.install")
        self.downgrade = self._patch("gui.updater.downgrade")
        self.quit = self._patch("gui.QApplication.quit")
        self.threads, self.gates, self.dialogs = [], [], []
        real_thread = threading.Thread
        def thread_factory(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            self.threads.append(thread)
            return thread
        self._patch("gui.threading.Thread", side_effect=thread_factory)
        self.addCleanup(self._cleanup_dialogs)
        self.dialog = self._new_dialog()

    def _patch(self, target, *args, **kwargs):
        patcher = patch(target, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def _new_dialog(self):
        dialog = gui.UpdateDialog()
        dialog._poll.stop()  # Drain explicitly to make worker/UI ordering observable.
        self.dialogs.append(dialog)
        return dialog

    def _cleanup_dialogs(self):
        for dialog in self.dialogs:
            dialog.close()
            # Accepted handoffs intentionally survive close in production.
            # A mocked quit must never leave that timer for the next fixture.
            dialog._restart_timer.stop()
        for gate in self.gates:
            gate.set()
        for thread in self.threads:
            thread.join(3)
        for dialog in self.dialogs:
            dialog.deleteLater()
        self.app.processEvents()

    def _stage(self, name=None):
        fd, name = tempfile.mkstemp(prefix=".fbvenom-update-", dir=self.directory.name) if name is None else (None, name)
        if fd is not None:
            os.close(fd)
        path = Path(name)
        path.write_bytes(b"verified build")
        return str(path)

    def _gate(self):
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        return entered, release

    def _join(self):
        for thread in self.threads:
            thread.join(2)
            self.assertFalse(thread.is_alive(), "Updater worker did not complete")

    def _wait(self, predicate, dialog=None):
        dialog = dialog or self.dialog
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app.processEvents()
            dialog._drain()
            if predicate():
                return
            time.sleep(0.002)
        self.fail("Updater GUI did not settle")

    def test_available_update_is_shown_and_install_is_enabled(self):
        self.dialog.check()
        self._wait(lambda: not self.dialog._busy)
        self.check.assert_called_once_with("https://updates.example/manifest.json")
        self.assertEqual(self.dialog._manifest, MANIFEST)
        self.assertIn(MANIFEST["version"], self.dialog.status.text())
        self.assertIn(MANIFEST["notes"], self.dialog.status.text())
        self.assertTrue(self.dialog.check_btn.isEnabled())
        self.assertTrue(self.dialog.install_btn.isEnabled())
        self.install.assert_not_called()

    def test_up_to_date_result_clears_a_previously_available_update(self):
        self.dialog._manifest = dict(MANIFEST)
        self.check.return_value = (dict(MANIFEST), False)
        self.dialog.check()
        self._wait(lambda: not self.dialog._busy)
        self.assertIsNone(self.dialog._manifest)
        self.assertFalse(self.dialog.install_btn.isEnabled())
        self.assertIn("newest", self.dialog.status.text())

    def test_errors_are_drained_on_gui_thread_and_controls_recover(self):
        self.check.side_effect = gui.updater.UpdateError("The update manifest is invalid.")
        self.dialog.check()
        self._wait(lambda: not self.dialog._busy)
        self.assertIn("manifest is invalid", self.dialog.status.text())
        self.assertTrue(self.dialog.check_btn.isEnabled())
        self.assertTrue(self.dialog.url.isEnabled())
        self.assertFalse(self.dialog.install_btn.isEnabled())
        self.install.assert_not_called()
        self.check.side_effect = RuntimeError("private transport details")
        self.dialog.check()
        self._wait(lambda: not self.dialog._busy)
        self.assertNotIn("private transport", self.dialog.status.text())

    def test_check_captures_address_on_gui_thread_and_discards_a_changed_draft(self):
        entered, release = self._gate()
        original_url = self.dialog.url.text()
        observed = []
        def check(url):
            observed.append((threading.get_ident(), url))
            entered.set()
            release.wait(3)
            return dict(MANIFEST), True
        self.check.side_effect = check
        self.dialog.check()
        self.assertTrue(entered.wait(1))
        self.assertFalse(self.dialog.url.isEnabled())
        self.assertNotEqual(observed[0][0], threading.get_ident())
        self.dialog.url.setText("https://another.example/manifest.json")
        release.set()
        self._wait(lambda: not self.dialog._busy)
        self.assertEqual(observed[0][1], original_url)
        self.assertIsNone(self.dialog._manifest)
        self.assertFalse(self.dialog.install_btn.isEnabled())
        self.assertIn("changed", self.dialog.status.text())

    def test_download_progress_updates_widgets_only_when_gui_drains(self):
        entered, release = self._gate()
        worker_ids, received_manifests, widget_ids = [], [], []
        staged = self._stage()
        def download(manifest, report):
            received_manifests.append(manifest)
            worker_ids.append(threading.get_ident())
            report(5, 10)
            entered.set()
            release.wait(3)
            return staged
        self.download.side_effect = download
        self.dialog._manifest = copy.deepcopy(MANIFEST)
        original_set_text = self.dialog.status.setText
        def set_text(text):
            widget_ids.append(threading.get_ident())
            original_set_text(text)
        with patch.object(self.dialog.status, "setText", side_effect=set_text):
            self.dialog.install()
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.dialog.status.text(), "Downloading…")
            self.assertTrue(all(item == threading.get_ident() for item in widget_ids))
            self.dialog._drain()
            self.assertIn("50%", self.dialog.status.text())
            self.assertNotEqual(worker_ids[0], threading.get_ident())
            self.assertIsNot(received_manifests[0], self.dialog._manifest)
            self.dialog._manifest["url"] = "https://changed.example/untrusted.exe"
            self.assertEqual(received_manifests[0]["url"], MANIFEST["url"])
            release.set()
            self._wait(lambda: not self.dialog._busy)
            self.assertTrue(all(item == threading.get_ident() for item in widget_ids))
        self.install.assert_called_once_with(staged)
        self.assertTrue(self.dialog._restart_timer.isActive())
        self.dialog._quit_after_install()
        self.quit.assert_called_once()

    def test_closing_during_check_ignores_late_results_and_errors(self):
        for action, error in (("reject", False), ("close", True)):
            with self.subTest(action=action):
                dialog = self._new_dialog()
                entered, release = self._gate()
                def check(url):
                    entered.set()
                    release.wait(3)
                    if error:
                        raise gui.updater.UpdateError("late error")
                    return dict(MANIFEST), True
                self.check.side_effect = check
                dialog.check()
                self.assertTrue(entered.wait(1))
                getattr(dialog, action)()
                before = dialog.status.text()
                release.set()
                self._join()
                dialog._drain()
                self.assertEqual(dialog.status.text(), before)
                self.assertTrue(dialog._closed)
                self.assertFalse(dialog._poll.isActive())
        self.install.assert_not_called()
        self.quit.assert_not_called()

    def test_closing_download_raises_from_progress_so_partial_file_is_removed(self):
        entered, release = self._gate()
        staged = Path(self._stage())
        cancelled = threading.Event()
        def download(manifest, report):
            report(1, 10)
            entered.set()
            release.wait(3)
            try:
                report(2, 10)
            except gui.updater.UpdateError:
                staged.unlink()  # Same cleanup contract as updater.download.
                cancelled.set()
                raise
            return str(staged)
        self.download.side_effect = download
        self.dialog._manifest = dict(MANIFEST)
        self.dialog.install()
        self.assertTrue(entered.wait(1))
        self.dialog.reject()
        release.set()
        self._join()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(staged.exists())
        self.dialog._drain()
        self.install.assert_not_called()
        self.quit.assert_not_called()

    def test_late_successful_download_is_discarded_after_close(self):
        entered, release = self._gate()
        staged = Path(self._stage())
        def download(manifest, report):
            entered.set()
            release.wait(3)
            return str(staged)
        self.download.side_effect = download
        self.dialog._manifest = dict(MANIFEST)
        self.dialog.install()
        self.assertTrue(entered.wait(1))
        self.dialog.close()
        release.set()
        self._join()
        self.assertFalse(staged.exists())
        self.dialog._drain()
        self.install.assert_not_called()
        self.quit.assert_not_called()

    def test_queued_verified_download_is_discarded_when_dialog_is_rejected(self):
        staged = Path(self._stage())
        self.download.side_effect = lambda manifest, report: str(staged)
        self.dialog._manifest = dict(MANIFEST)
        self.dialog.install()
        self._join()
        self.assertTrue(staged.exists())
        self.dialog.reject()
        self.assertFalse(staged.exists())
        self.dialog._drain()
        self.install.assert_not_called()
        self.quit.assert_not_called()

    def test_staged_cleanup_never_deletes_an_unrelated_file_or_symlink(self):
        unrelated = self.directory_path / "important.exe"
        unrelated.write_bytes(b"keep this")
        link = self.directory_path / ".fbvenom-update-link"
        link.symlink_to(unrelated)
        for path in (unrelated, link):
            with self.subTest(path=path.name):
                gui.UpdateDialog._discard_staged(str(path), self.directory.name)
                self.assertTrue(path.exists())
        self.assertEqual(unrelated.read_bytes(), b"keep this")
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        outside = Path(other.name) / ".fbvenom-update-outside"
        outside.write_bytes(b"keep this too")
        gui.UpdateDialog._discard_staged(str(outside), self.directory.name)
        self.assertTrue(outside.exists())

    def test_install_failure_cleans_staged_file_and_does_not_quit(self):
        staged = Path(self._stage())
        self.download.side_effect = lambda manifest, report: str(staged)
        self.install.side_effect = gui.updater.UpdateError("The installer could not start.")
        self.dialog._manifest = dict(MANIFEST)
        self.dialog.install()
        self._wait(lambda: not self.dialog._busy)
        self.assertFalse(staged.exists())
        self.assertIn("installer", self.dialog.status.text())
        self.assertTrue(self.dialog.install_btn.isEnabled())
        self.assertFalse(self.dialog._restart_timer.isActive())
        self.quit.assert_not_called()

    def test_close_after_install_handoff_keeps_scheduled_quit_and_blocks_new_actions(self):
        self.dialog._manifest = dict(MANIFEST)
        self.dialog.install()
        self._wait(lambda: not self.dialog._busy)
        self.install.assert_called_once()
        self.dialog.reject()
        self.assertTrue(self.dialog._restart_timer.isActive())
        self.dialog.check()
        self.dialog.install()
        self.dialog.rollback()
        self._wait(lambda: self.quit.called)
        self.quit.assert_called_once()
        self.check.assert_not_called()
        self.downgrade.assert_not_called()
        self.install.assert_called_once()

    def test_restart_callback_is_idempotent_and_stops_its_timer(self):
        self.dialog._manifest = dict(MANIFEST)
        self.dialog.install()
        self._wait(lambda: not self.dialog._busy)
        self.assertTrue(self.dialog._restart_timer.isActive())
        self.dialog._quit_after_install()
        self.dialog._quit_after_install()
        self.quit.assert_called_once()
        self.assertFalse(self.dialog._restart_timer.isActive())
        self.assertTrue(self.dialog._restart_pending)
        self.dialog.check()
        self.dialog.install()
        self.install.assert_called_once()
        self.check.assert_not_called()

    def test_rollback_requires_confirmation_and_uses_guarded_restart(self):
        with patch.object(gui.QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.dialog.rollback()
        self.downgrade.assert_not_called()
        with patch.object(gui.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog.rollback()
        self.downgrade.assert_called_once()
        self.assertTrue(self.dialog._restart_timer.isActive())
        self.dialog.reject()
        self.assertTrue(self.dialog._restart_timer.isActive())
        self._wait(lambda: self.quit.called)
        self.quit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
