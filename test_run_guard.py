"""The run guard: glued links, written-off links, no double comments - offline.

Replays what went wrong on this PC's run of 2026-11-22 (7 Redmi phones, 3
links): link 2 was two share links pasted into one box. It was run as ONE link,
".../share/p/1CiUaLY336/https:/www.facebook.com/share/p/1JnE1NBsYP", which
Facebook Lite opens on the news feed. Every phone retried it for every account
- ~13 minutes of closing and reopening Facebook Lite per account, 296
TARGET_OPEN_FAILED - while links 1 and 3 waited: "it's not commenting and
starting again". No phone, ADB or network is used.
"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import core

GLUED_AS_PASTED = ("https://www.facebook.com/share/p/1CiUaLY336/"
                   "https://www.facebook.com/share/p/1JnE1NBsYP/")
GLUED_AS_RUN = ("https://www.facebook.com/share/p/1CiUaLY336/"
                "https:/www.facebook.com/share/p/1JnE1NBsYP")      # from the log
FIRST = "https://www.facebook.com/share/p/1CiUaLY336"
SECOND = "https://www.facebook.com/share/p/1JnE1NBsYP"


class SplitLinksTests(unittest.TestCase):
    def test_the_glued_link_of_the_22nd_is_two_links_again(self):
        for text in (GLUED_AS_PASTED, GLUED_AS_RUN,
                     FIRST + "https://www.facebook.com/share/p/1JnE1NBsYP",
                     FIRST + " " + SECOND, FIRST + ",\n" + SECOND):
            with self.subTest(text=text):
                self.assertEqual(core.split_links(text), [FIRST, SECOND])

    def test_one_link_stays_one_link(self):
        for text, want in (
                ("https://l.facebook.com/l.php?u=https://www.facebook.com/reel/42&h=x",
                 "https://www.facebook.com/reel/42"),
                ("fb://facewebmodal/f?href=https://www.facebook.com/reel/7",
                 "https://www.facebook.com/reel/7"),
                ("https://m.facebook.com/story.php?story_fbid=1&id=2",
                 "https://www.facebook.com/story.php?story_fbid=1&id=2"),
                ("Check this " + FIRST + " please!", FIRST),
                ("https://www.facebook.com/reel/9 https://www.facebook.com/reel/9/",
                 "https://www.facebook.com/reel/9")):
            with self.subTest(text=text):
                self.assertEqual(core.split_links(text), [want])

    def test_the_phones_get_both_links_with_the_same_example(self):
        events = []
        with patch.object(core, "report_error",
                          side_effect=lambda code, *a, **k: events.append(code)), \
                patch.object(core, "_lookup") as lookup:
            lookup.side_effect = lambda url: _done(url)
            targets = core.normalize_targets_for_phones(
                [core.make_target(GLUED_AS_PASTED, comment="ang ganda", do_react=False,
                                  do_comment=True)], budget=1)
        self.assertEqual([t["url"] for t in targets], [FIRST, SECOND])
        self.assertEqual({t["comment"] for t in targets}, {"ang ganda"})
        self.assertIn("LINK_SPLIT", events)


def _done(value):
    import concurrent.futures
    future = concurrent.futures.Future()
    future.set_result(value)
    return future


class LinkHealthTests(unittest.TestCase):
    def setUp(self):
        self.health = core.LinkHealth()

    def test_written_off_after_two_phones_with_healthy_accounts(self):
        self.assertFalse(self.health.failed("L", "phone-a", 1))
        self.assertFalse(self.health.is_dead("L"))
        self.assertTrue(self.health.failed("L", "phone-b", 1))
        self.assertTrue(self.health.is_dead("L"))
        self.assertIn("never opened", self.health.reason("L"))

    def test_written_off_after_three_accounts_on_one_phone(self):
        self.assertFalse(self.health.failed("L", "phone-a", 1))
        self.assertFalse(self.health.failed("L", "phone-a", 2))
        self.assertTrue(self.health.failed("L", "phone-a", 3))

    def test_a_link_that_opened_anywhere_is_never_written_off(self):
        # Some accounts cannot see a post (a group they are not in).
        self.health.opened("L")
        for phone in ("phone-a", "phone-b", "phone-c"):
            self.assertFalse(self.health.failed("L", phone, 1))
        self.assertFalse(self.health.is_dead("L"))

    def test_a_new_run_starts_clean(self):
        self.health.failed("L", "phone-a", 1)
        self.health.failed("L", "phone-b", 1)
        self.health.reset()
        self.assertFalse(self.health.is_dead("L"))


class FakePhone:
    serial = "REDMI-1"
    _frozen = True

    def dump_hierarchy(self, *a, **k):
        return "<hierarchy/>"

    def window_size(self):
        return (1080, 2340)


class ReplayTheTwentySecondTests(unittest.TestCase):
    """One phone, four accounts, links 1 and 3 fine, link 2 never opens."""

    ACCOUNTS = ["Ana", "Ben", "Cy", "Dee"]

    def setUp(self):
        core.begin_run()
        self.addCleanup(core.begin_run)
        self.account = 0
        self.opens = []          # (account, url) per open_target_post() call
        self.comments = []       # (account, link_no)
        self.codes = []
        targets = core.normalize_targets_for_phones(
            [core.make_target(u, comment="ok", do_react=False, do_comment=True)
             for u in ("https://www.facebook.com/page/posts/111",
                       "https://www.facebook.com/page/posts/222",
                       "https://www.facebook.com/page/posts/333")], budget=0)
        # The engine is handed the glued link as one URL, like on the 22nd.
        targets[1] = dict(targets[1], url=GLUED_AS_RUN, phone_ready=True)
        self.targets = targets
        phone = FakePhone()

        def open_target_post(serial, d, url, attempts=3):
            self.opens.append((self.ACCOUNTS[self.account], url))
            return url != GLUED_AS_RUN

        def process_target(serial, d, tgt, account_num, link_no, n_links,
                           take_screenshots, results, account_name=""):
            self.comments.append((self.ACCOUNTS[self.account], link_no))
            core._tally(results, link_no, tgt["url"], "commented")
            core._note_account(results, account_name, link_no, "commented", "ok")
            return "ok"

        def switch(serial, d, used, state, roster_out=None):
            if roster_out is not None:
                for name in self.ACCOUNTS:
                    core._remember_account(roster_out, name)
            state.setdefault("was_current", self.ACCOUNTS[0])
            used.add(self.ACCOUNTS[self.account].lower())
            for index, name in enumerate(self.ACCOUNTS):
                if name.lower() not in used:
                    self.account = index
                    used.add(name.lower())
                    return True, name, "switched"
            return False, None, "no-available-account"

        for name, value in (
                ("connect_resilient", lambda serial: phone),
                ("ensure_device", lambda serial, d: d),
                ("wake_and_unlock", lambda serial: None),
                ("run", lambda *a, **k: (0, "", "")),
                ("stop_stale_usb_vpn", lambda serial: False),
                ("dismiss_system_popup", lambda serial: False),
                ("wait_for_account_block", lambda d, timeout=6: None),
                ("open_target_post", open_target_post),
                ("_process_target", process_target),
                ("switch_to_next_account", switch),
                ("prefetch_comment", lambda tgt: None),
                ("pace", lambda *a, **k: True),
                ("abort_sleep", lambda *a, **k: True),
                ("stop_requested", lambda: False),
                ("dbg", lambda *a, **k: None),
                ("task_log", lambda *a, **k: None),
                ("report_error", lambda code, *a, **k: self.codes.append(code))):
            patcher = patch.object(core, name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_phone(self):
        return core._react_all_saved_accounts("REDMI-1", self.targets)

    def test_the_bad_link_is_written_off_and_the_good_ones_keep_going(self):
        results = self.run_phone()
        bad = [account for account, url in self.opens if url == GLUED_AS_RUN]
        # Tried by 3 healthy accounts (2 rounds each), then never again -
        # instead of 5 rounds for EVERY account.
        self.assertEqual(bad, ["Ana", "Ana", "Ben", "Ben", "Cy", "Cy"])
        self.assertEqual(self.codes.count("LINK_DEAD"), 1)
        # Links 1 and 3 were commented by every account, exactly once each.
        self.assertEqual(sorted(self.comments),
                         sorted((a, n) for a in self.ACCOUNTS for n in (1, 3)))
        self.assertIn("all-accounts-done", results)
        self.assertEqual(results.link_tally[1]["commented"], 4)
        self.assertEqual(results.link_tally[3]["commented"], 4)

    def test_the_coverage_pass_does_not_send_everyone_round_again(self):
        self.run_phone()
        # 4 accounts x 2 good links; a second sweep would have doubled them.
        self.assertEqual(len(self.comments), 8)
        self.assertEqual(len(set(self.comments)), 8)

    def test_an_account_that_returns_never_comments_twice(self):
        results = core.RunResults()
        core._note_account(results, "Ana", 1, "commented", "ok")
        target = {"do_comment": True}
        self.assertTrue(core._account_did(results, "Ana", 1, target))
        self.assertFalse(core._account_did(results, "Ana", 2, target))
        self.assertFalse(core._account_did(results, "Ben", 1, target))
        core._note_account(results, "Ben", 1, "commented", "open-failed")
        self.assertFalse(core._account_did(results, "Ben", 1, target))


class EvidenceBeforeCloseTests(unittest.TestCase):
    def test_the_screen_is_saved_before_facebook_lite_is_closed(self):
        order = []

        def run(args, serial=None, timeout=60):
            if args[:3] == ["shell", "am", "force-stop"]:
                order.append("force-stop")
            return 0, "Starting: Intent", ""

        with patch.object(core, "run", side_effect=run), \
                patch.object(core, "wait_online", return_value=True), \
                patch.object(core, "fb_foreground", return_value=True), \
                patch.object(core, "poll_until", side_effect=lambda check, t, *a: check()), \
                patch.object(core, "abort_sleep", return_value=True), \
                patch.object(core, "stop_requested", return_value=False), \
                patch.object(core, "report_error"), \
                patch.object(core, "_save_open_failure",
                             side_effect=lambda *a: order.append("evidence")):
            opened = core.open_post("REDMI-1", "https://www.facebook.com/page/posts/111",
                                    allow_warm=False, verify=lambda: False,
                                    evidence=FakePhone())
        self.assertFalse(opened)
        self.assertIn("evidence", order)
        self.assertEqual(order[order.index("evidence") + 1], "force-stop")


class SentCommentWithKeyboardUpTests(unittest.TestCase):
    """After Send, 3 of 7 Redmis kept the keyboard up: the emptied box sat at
    y=1381-1475 of 2340 with text "" and no marker words, so on_post_view()
    said "not on the post" and each sent comment was reported unverified."""

    @staticmethod
    def screen(box=None, package="com.facebook.lite"):
        nodes = ""
        if box:
            x1, y1, x2, y2 = box
            nodes = (f'<node class="android.widget.MultiAutoCompleteTextView" '
                     f'package="{package}" bounds="[{x1},{y1}][{x2},{y2}]" text="" '
                     f'content-desc="" clickable="true" />')
        return ('<hierarchy rotation="0"><node class="android.widget.FrameLayout" '
                'package="com.facebook.lite" bounds="[0,0][1080,2340]" text="" '
                f'content-desc="" clickable="false">{nodes}</node></hierarchy>')

    def phone(self, xml):
        phone = FakePhone()
        phone.dump_hierarchy = lambda *a, **k: xml
        phone.serial = None
        return phone

    def test_the_emptied_box_mid_screen_counts_as_still_on_the_post(self):
        self.assertTrue(core._comment_box_on_screen(
            self.phone(self.screen((55, 1381, 945, 1475)))))

    def test_no_box_a_top_search_box_or_another_apps_box_does_not(self):
        for xml in (self.screen(), self.screen((40, 120, 900, 210)),
                    self.screen((55, 1381, 945, 1475), package="com.android.chrome")):
            with self.subTest(xml=xml[-160:]):
                self.assertFalse(core._comment_box_on_screen(self.phone(xml)))

    def test_post_comment_counts_the_comment_that_went(self):
        box = (55, 1381, 945, 1475)
        states = iter(["draft"] + ["empty"] * 20)
        texts = {"draft": "test lang po hindi ipapadala", "empty": ""}
        current = {"text": ""}

        def edit_nodes(d):
            return [(box, {"class": "android.widget.MultiAutoCompleteTextView",
                           "text": current["text"], "package": "com.facebook.lite"})]

        def type_text(serial, d, point, text):
            current["text"] = texts[next(states)]
            return True

        taps = []

        def run(args, serial=None, timeout=60):
            if args[:3] == ["shell", "input", "tap"]:
                taps.append(args[3:])
                current["text"] = ""          # Facebook Lite empties the box
            return 0, "", ""

        phone = self.phone(self.screen(box))
        with patch.object(core, "on_post_view", side_effect=[True] + [False] * 30), \
                patch.object(core, "is_action_blocked", return_value=False), \
                patch.object(core, "open_comment_composer", return_value=(500, 1428)), \
                patch.object(core, "type_text", side_effect=type_text), \
                patch.object(core, "edit_nodes", side_effect=edit_nodes), \
                patch.object(core, "find_send_button", return_value=(1001, 1429)), \
                patch.object(core, "run", side_effect=run), \
                patch.object(core, "abort_sleep", return_value=True), \
                patch.object(core, "stop_requested", return_value=False), \
                patch.object(core, "report_error"), patch.object(core, "dump_debug"):
            status = core.post_comment("REDMI-1", phone, "test lang po hindi ipapadala")
        self.assertEqual(taps, [["1001", "1429"]])          # one Send, never two
        self.assertTrue(status.startswith("commented:"), status)


class GuardAndPopupTests(unittest.TestCase):
    def test_the_pocket_guard_then_the_usb_popup_are_closed_with_their_own_keys(self):
        screens = ["  mCurrentFocus=Window{1 u0 ScreenOnProximitySensorGuide}\n",
                   "  mCurrentFocus=Window{2 u0 com.android.settings/"
                   "com.android.settings.Settings$UsbDetailsActivity}\n",
                   "  mCurrentFocus=Window{3 u0 com.facebook.lite/"
                   "com.facebook.lite.MainActivity}\n"]
        keys = []

        def run(args, serial=None, timeout=60):
            if args == ["shell", "dumpsys", "window"]:
                return 0, screens[min(len(keys), 2)], ""
            if args[:3] == ["shell", "input", "keyevent"]:
                keys.append(args[3])
            return 0, "", ""

        with patch.object(core, "run", side_effect=run), \
                patch.object(core.time, "sleep"), patch.object(core, "dbg"):
            self.assertTrue(core.dismiss_system_popup("REDMI-1"))
        self.assertEqual(keys, ["KEYCODE_VOLUME_UP", "KEYCODE_BACK"])


class WindowSplitsPastedLinksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import aiconfig
        with patch.object(aiconfig, "load", return_value={}):
            import gui
        from PyQt6.QtWidgets import QApplication
        cls.gui = gui
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import tempfile
        from pathlib import Path
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for target, kwargs in (
                ("gui.ensure_online", {"side_effect": lambda: {"phone-a": "device",
                                                               "phone-b": "unauthorized"}}),
                ("gui.aiconfig.is_ready", {"return_value": False}),
                ("gui.aiconfig.describe", {"return_value": "Test AI settings"}),
                ("gui.aiconfig.load", {"return_value": {}}),
                ("gui.fbcore.set_speed", {}),
                ("gui.threading.Thread", {})):      # no worker, no phone, no posting
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = self.gui.ReactionGUI(
            workspace_path=Path(directory.name) / "workspaces.json")
        self.window.timer.stop()
        self.window.anim_timer.stop()
        self.addCleanup(self._close)

    def _close(self):
        self.window.running = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_a_box_holding_two_glued_links_becomes_two_rows(self):
        row = self.window.link_rows[0]
        row.url.setText(GLUED_AS_PASTED)
        row.comment.setText("ang ganda nito")
        self.window.start_reaction()
        rows = self.window.link_rows
        self.assertEqual([r.url.text() for r in rows], [FIRST, SECOND])
        self.assertEqual([r.comment.text() for r in rows], ["ang ganda nito"] * 2)
        self.assertEqual([r.index for r in rows], [1, 2])

    def test_an_unauthorized_phone_is_not_picked_by_default(self):
        self.assertEqual(self.window.get_selected_devices(), ["phone-a"])


if __name__ == "__main__":
    unittest.main()
