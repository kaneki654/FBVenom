"""Offscreen checks for live session counting; no phones or AI calls are used.

Drives the real ``run_reactions`` with a stand-in commenting engine so the
reporting path - the per-link Session activity tally, the Devices tile, the
per-phone account coverage and the remembered account counts - is exercised
exactly as it runs against real devices, minus any ADB or provider work.

Run explicitly with ``python3 -m unittest test_live_counting -v``.
"""

import json
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

import core as fbcore
from PyQt6.QtWidgets import QApplication


# account outcomes per phone: True = this account comments, False = the account
# is reached but never posts a comment (duplicate wording, block, etc.).
PLAN = {
    "phone-a": [True, True],    # every account commented -> counts on the tile
    "phone-b": [True, False],   # one account missed     -> partial, not counted
}


def _fake_react_all_saved_accounts(serial, targets, reaction, example,
                                   do_react, do_comment, shots):
    """Replay a device's run through the real progress hook and result tally."""
    res = fbcore.RunResults()
    for account_num, commented in enumerate(PLAN[serial], start=1):
        for link_no, target in enumerate(targets, start=1):
            url = target["url"]
            # Every reached account is announced before it is worked, so a
            # reached-but-silent account still counts toward "all accounts".
            fbcore.report_progress(serial, "account", account_num, link_no, url)
            if commented:
                fbcore.report_progress(serial, "commented",
                                       account_num, link_no, url)
                res.bump(link_no, url, "commented")
                res.note_account(f"{serial}-a{account_num}", link_no,
                                 "commented", "ok")
                res.append(f"commented:ok account {account_num} link {link_no}")
            else:
                res.bump(link_no, url, "failed")
                res.append(f"comment-skipped-duplicate account {account_num}")
    return res


class LiveCountingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "venom_workspaces.json"
        self.devices = {"phone-a": "device", "phone-b": "device"}

        self._patch("gui.ensure_online", side_effect=lambda: dict(self.devices))
        self._patch("gui.aiconfig.is_ready", return_value=False)
        self._patch("gui.aiconfig.describe", return_value="Test AI settings")
        self._patch("gui.aiconfig.load", return_value={})
        self._patch("gui.fbcore.set_speed")
        # Make the run deterministic and phone-free.
        self._patch("gui.normalize_url", side_effect=lambda u: u)
        self._patch("gui.fbcore.normalize_targets_for_phones",
                    side_effect=lambda t: t)
        self._patch("gui.has_fb", return_value=True)
        self._patch("gui.install_fb", return_value=True)
        self._patch("gui.find_apk", return_value=None)
        self._patch("gui.wait_online")
        self._patch("gui.wake_and_unlock")
        self._patch("gui.reset_comment_pool")
        self._patch("gui.start_llama", return_value=True)
        self._patch("gui.stop_llama")
        self._patch("gui.react_all_saved_accounts",
                    side_effect=_fake_react_all_saved_accounts)

        self.window = gui.ReactionGUI(workspace_path=str(self.path))
        self.addCleanup(self.window.close)
        # Record every UI command so the live path can be asserted, not just the
        # final totals.
        self.sent = []
        original_send = self.window.send_cmd

        def recording_send(cmd):
            self.sent.append(cmd)
            original_send(cmd)

        self.window.send_cmd = recording_send

    def _patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def _run_comment_session(self, url="https://www.facebook.com/reel/111",
                             comment="Great post"):
        row = self.window.link_rows[0]
        row.url.setText(url)
        row.comment.setText(comment)
        for item in self.window.dev_items.values():
            item.setSelected(True)
        self.window.start_reaction()
        self._pump()

    def _pump(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            self.window.process_queue()
            if not self.window.running and self.window.output_queue.empty():
                break
            time.sleep(0.02)
        self.window.process_queue()
        self.assertFalse(self.window.running, "session never finished")

    # ── the four behaviours the operator asked for ──────────────────────────
    def test_session_activity_counts_each_dose_live(self):
        self._run_comment_session()
        # Three comments landed (a1, a2 on phone-a; a1 on phone-b) and one was
        # missed on phone-b. Reacted/commented are counted the instant they
        # land, so a non-zero commented total here can only have come from the
        # live per-dose path - the end-of-phone roll-up now adds failures only.
        self.assertEqual(self.window.link_tally.rows,
                         [(1, "https://www.facebook.com/reel/111", 0, 3, 1)])
        self.assertEqual(self.window.stat_reactions._text, "3")
        progress = [c for c in self.sent if c.get("action") == "stats"]
        self.assertTrue(progress, "no live stats were emitted")
        self.assertEqual(progress[-1]["reactions"], 3)

    def test_devices_tile_reaches_every_finished_phone(self):
        self._run_comment_session()
        # Both phones finished, so the tile completes at 2 / 2 even though
        # phone-b missed an account. Parking it below the total was the bug:
        # one skipped account used to strand it there forever.
        self.assertEqual(self.window.stat_devices._text, "2 / 2")
        device_stats = [c["devices"] for c in self.sent
                        if c.get("action") == "stats"]
        self.assertEqual(max(device_stats), 2)
        # Coverage is still tracked, and reported in the summary line.
        self.assertEqual(self.window._devices_full, {"phone-a"})
        self.assertIn("1/2 phones commented on every account",
                      self.window.msg_label.text())

    def test_per_phone_account_coverage_is_reported_live(self):
        self._run_comment_session()
        progress = [c for c in self.sent if c.get("action") == "device_progress"]
        by_serial = {}
        for cmd in progress:
            by_serial.setdefault(cmd["serial"], []).append(cmd)
        # phone-a reaches "2 / 2" commented; phone-b only ever reaches "1 / 2".
        self.assertEqual(max(c["acted"] for c in by_serial["phone-a"]), 2)
        self.assertEqual(max(c["expected"] for c in by_serial["phone-a"]), 2)
        self.assertEqual(max(c["acted"] for c in by_serial["phone-b"]), 1)
        self.assertEqual(max(c["expected"] for c in by_serial["phone-b"]), 2)

    def test_connected_account_counts_are_remembered_and_persist(self):
        self._run_comment_session()
        # Both phones were walked across two accounts, so both counts are learned
        # from the run itself - no hover needed - and written beside the file.
        self.assertEqual(self.window.account_counts["phone-a"], 2)
        self.assertEqual(self.window.account_counts["phone-b"], 2)
        accounts_file = Path(self.directory.name) / "venom_accounts.json"
        self.assertTrue(accounts_file.exists())
        self.assertEqual(json.loads(accounts_file.read_text()),
                         {"phone-a": 2, "phone-b": 2})
        # A fresh window reads them straight back.
        restored = gui.ReactionGUI(workspace_path=str(self.path))
        self.addCleanup(restored.close)
        self.assertEqual(restored.account_counts, {"phone-a": 2, "phone-b": 2})


    def test_session_overview_is_separate_per_workspace(self):
        window = self.window
        alpha = window.create_workspace("Alpha", 2)
        beta = window.create_workspace("Beta", 2)

        # Run a session under Alpha only.
        self.assertTrue(window.select_workspace(alpha["id"]))
        row = window.link_rows[0]
        row.url.setText("https://www.facebook.com/reel/111")
        row.comment.setText("Great post")
        window.start_reaction()
        self._pump()
        devices, comments = window.stat_devices._text, window.stat_reactions._text
        self.assertEqual(devices, "2 / 2")
        self.assertEqual(comments, "3")
        self.assertTrue(window.link_tally.rows)

        # Beta has never run, so it shows its own empty overview - not Alpha's.
        self.assertTrue(window.select_workspace(beta["id"]))
        self.assertEqual(window.stat_devices._text, "0 / 0")
        self.assertEqual(window.stat_reactions._text, "0")
        self.assertEqual(window.link_tally.rows, [])

        # Alpha still remembers what it did.
        self.assertTrue(window.select_workspace(alpha["id"]))
        self.assertEqual(window.stat_devices._text, devices)
        self.assertEqual(window.stat_reactions._text, comments)
        self.assertTrue(window.link_tally.rows)

        # The original Comments tab keeps a set of its own, untouched by either.
        window.feature_tabs.setCurrentIndex(0)
        self.assertNotEqual(window.stat_devices._text, devices)
        self.assertEqual(window.stat_reactions._text, "0")
        self.assertEqual(window.link_tally.rows, [])


    def test_partial_phones_do_not_stall_the_devices_tile(self):
        """The reported bug: on a bigger fleet several phones finish with an
        account skipped, and the tile used to park below the total (15/18) and
        never complete. It must reach N/N while still reporting coverage."""
        fleet = {f"phone-{i}": "device" for i in range(6)}
        self.devices.clear()
        self.devices.update(fleet)
        # First three phones comment on every account; the rest miss one each.
        plan = {name: ([True, True] if i < 3 else [True, False])
                for i, name in enumerate(sorted(fleet))}
        with patch.dict(PLAN, plan, clear=True):
            self.window.refresh_devices()
            self._run_comment_session()
        self.assertEqual(self.window.stat_devices._text, "6 / 6")
        self.assertEqual(len(self.window._devices_full), 3)
        self.assertIn("3/6 phones commented on every account",
                      self.window.msg_label.text())

    def test_device_switches_are_separate_per_workspace(self):
        window = self.window
        alpha = window.create_workspace("Alpha", 2)
        beta = window.create_workspace("Beta", 2)

        # Mute a phone under Alpha only.
        self.assertTrue(window.select_workspace(alpha["id"]))
        window._set_device_comment("phone-b", False)
        self.assertFalse(window.device_comments("phone-b"))
        self.assertNotIn("phone-b", window.get_selected_devices())

        # Beta never muted anything, so every phone still speaks there.
        self.assertTrue(window.select_workspace(beta["id"]))
        self.assertTrue(window.device_comments("phone-b"))

        # Alpha still remembers the mute when you come back to it.
        self.assertTrue(window.select_workspace(alpha["id"]))
        self.assertFalse(window.device_comments("phone-b"))

        # ...and the original Comments tab keeps switches of its own.
        window.feature_tabs.setCurrentIndex(0)
        self.assertTrue(window.device_comments("phone-b"))


if __name__ == "__main__":
    unittest.main()
