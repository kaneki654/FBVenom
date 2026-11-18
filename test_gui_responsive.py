"""The window stays usable while a session runs - offscreen, no phones.

"When a session is ongoing I can't scroll nor touch the software": the link
and device panels were disabled wholesale (a disabled panel does not even
scroll), and the window's own thread was kept busy - every diagnostic past the
500th rebuilt the whole log, the worker queue was drained to empty in one go,
and every phone card redrew 30 times a second.
"""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import aiconfig

with patch.object(aiconfig, "load", return_value={}):
    import gui

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication


class ResponsiveWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.devices = {f"PHONE{n:02d}": "device" for n in range(12)}
        for item in (
                patch.object(gui, "ensure_online", side_effect=lambda: dict(self.devices)),
                patch.object(gui.aiconfig, "load", return_value={}),
                patch.object(gui.aiconfig, "is_ready", return_value=False),
                patch.object(gui.aiconfig, "describe", return_value="Test AI settings"),
                patch.object(gui.fbcore, "set_speed"),
                patch.object(gui, "stop_llama")):
            item.start()
            self.addCleanup(item.stop)
        self.window = gui.ReactionGUI(workspace_path=Path(self.directory.name) / "w.json")
        self.window.timer.stop()
        self.window.anim_timer.stop()
        self.addCleanup(self.window.close)

    def _pretend_running(self, running=True):
        self.window.running = running
        self.window.stop_flag = False
        self.window._set_session_controls()

    # ── panels ───────────────────────────────────────────────────────────────
    def test_links_and_devices_stay_scrollable_but_locked_during_a_session(self):
        for _ in range(5):
            self.window.add_link_row()
        self.window.link_rows[0].url.setText("https://www.facebook.com/share/v/19zVwRPKRW")
        self.window.resize(1280, 760)
        self.window.show()
        self.app.processEvents()
        self._pretend_running()
        win = self.window
        self.assertTrue(win.cfg_panel.isEnabled())
        self.assertTrue(win.devices_panel.isEnabled())
        self.assertTrue(win.links_scroll.isEnabled())
        self.assertTrue(win.dev_list.isEnabled())
        self.assertTrue(win.stop_btn.isEnabled())
        row = win.link_rows[0]
        self.assertTrue(row.url.isReadOnly())
        self.assertTrue(row.comment.isReadOnly())
        self.assertFalse(row.reaction.isEnabled())
        self.assertFalse(row.kill.isEnabled())
        self.assertFalse(win.add_link_btn.isEnabled())
        self.assertFalse(win.all_btn.isEnabled())
        self.assertFalse(win.refresh_btn.isEnabled())
        self.assertFalse(win.feature_tabs.isEnabled())       # no workspace swap mid-run
        # The link list really scrolls with the wheel while the session runs.
        bar = win.links_scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        viewport = win.links_scroll.viewport()
        centre = QPointF(viewport.width() / 2, viewport.height() / 2)
        wheel = QWheelEvent(centre, QPointF(viewport.mapToGlobal(centre.toPoint())),
                            QPoint(0, 0), QPoint(0, -240), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase,
                            False)
        QApplication.sendEvent(viewport, wheel)
        self.assertGreater(bar.value(), 0)
        # Removing a link mid-run is refused even if called directly.
        win.remove_link_row(win.link_rows[-1])
        self.assertEqual(len(win.link_rows), 6)

    def test_device_selection_cannot_change_mid_session_but_returns_after(self):
        win = self.window
        win.resize(1280, 760)
        win.show()
        self.app.processEvents()
        first = win.dev_list.item(0)
        rect = win.dev_list.visualItemRect(first)
        before = first.isSelected()
        self._pretend_running()
        QTest.mouseClick(win.dev_list.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, rect.center())
        self.assertEqual(first.isSelected(), before)        # click ignored mid-run
        for pill in win.device_pills.values():
            self.assertFalse(pill.isEnabled())
        self._pretend_running(False)
        QTest.mouseClick(win.dev_list.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, rect.center())
        self.assertEqual(first.isSelected(), not before)    # and works again after
        self.assertFalse(win.link_rows[0].url.isReadOnly())
        self.assertTrue(win.add_link_btn.isEnabled())
        for pill in win.device_pills.values():
            self.assertTrue(pill.isEnabled())

    # ── diagnostics ──────────────────────────────────────────────────────────
    def test_diagnostics_past_the_limit_never_rebuild_the_whole_log(self):
        win = self.window
        rebuilt = []
        win.diagnostics_log.setPlainText = lambda *a: rebuilt.append(a)
        for n in range(1200):
            event = {"code": f"E{n}", "message": f"failure {n}", "time": "10:00:00",
                     "severity": ("error", "warning", "info")[n % 3]}
            if n % 4 == 0:
                event["detail"] = "first line\nsecond line"
            win._append_diagnostic(event)
        self.assertEqual(rebuilt, [])
        kept = win._diagnostic_events
        self.assertEqual(len(kept), gui.ReactionGUI.DIAGNOSTIC_LIMIT)
        self.assertEqual(kept[0]["code"], "E700")
        # What is on screen is exactly what Copy log copies.
        self.assertEqual(win.diagnostics_log.toPlainText().strip(),
                         "\n\n".join(win._diagnostic_text(e) for e in kept).strip())
        self.assertEqual(win._diagnostic_errors,
                         sum(e["severity"] == "error" for e in kept))
        self.assertEqual(win._diagnostic_warnings,
                         sum(e["severity"] == "warning" for e in kept))

    # ── worker queue ─────────────────────────────────────────────────────────
    def test_bursts_of_latest_value_updates_collapse_and_order_is_kept(self):
        win = self.window
        handled = []
        win.handle_cmd = lambda cmd: handled.append((cmd["action"], cmd.get("n")))
        for n in range(50):
            win.send_cmd({"action": "msg", "n": n})
            win.send_cmd({"action": "stats", "n": n})
        win.send_cmd({"action": "device_status", "n": "done"})
        win.send_cmd({"action": "msg", "n": "after"})
        win.process_queue()
        self.assertEqual(handled, [("msg", 49), ("stats", 49),
                                   ("device_status", "done"), ("msg", "after")])

    def test_one_tick_never_holds_the_window_past_its_budget(self):
        win = self.window
        win.handle_cmd = lambda cmd: time.sleep(0.01)
        for n in range(100):
            win.send_cmd({"action": "diagnostic", "n": n})
        started = time.perf_counter()
        win.process_queue()
        self.assertLess(time.perf_counter() - started, 0.2)
        self.assertGreater(win.output_queue.qsize(), 0)      # the rest waits a tick

    def test_a_broken_message_does_not_strand_the_rest(self):
        win = self.window
        seen = []

        def handle(cmd):
            if cmd["action"] == "boom":
                raise RuntimeError("bad payload")
            seen.append(cmd["action"])
        win.handle_cmd = handle
        win.send_cmd({"action": "boom"})
        win.send_cmd({"action": "device_status"})
        with patch("sys.stderr"):
            win.process_queue()
        self.assertEqual(seen, ["device_status"])

    # ── device cards ─────────────────────────────────────────────────────────
    def test_processing_cards_redraw_a_few_times_a_second_not_thirty(self):
        card = gui.DeviceCard("PHONE00")
        self.addCleanup(card.deleteLater)
        card.set_status("processing", "1 / 3 commented")
        repaints = []
        card.update = lambda *a: repaints.append(1)
        for _ in range(30):                      # one second of 33 ms ticks
            card.advance(0.033)
        self.assertLessEqual(len(repaints), 9)
        self.assertGreaterEqual(len(repaints), 6)


if __name__ == "__main__":
    unittest.main()
