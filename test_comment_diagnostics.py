"""Offline regressions for comment discovery, verification and uncertain sends."""

from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import aiconfig

with patch.object(aiconfig, "load", return_value={}):
    import core


class CommentActionRowTests(unittest.TestCase):
    def row(self, bounds):
        with patch.object(core, "safe_window_size", return_value=(720, 1600)), \
                patch.object(core, "clickable_bounds", return_value=bounds):
            return core.find_feed_action_row(object())

    def test_current_narrow_pills_find_comment_button(self):
        pills = [(12, 800, 120, 866), (145, 800, 253, 866),
                 (280, 800, 410, 866)]
        self.assertEqual(self.row(pills), pills)
        with patch.object(core, "safe_window_size", return_value=(720, 1600)), \
                patch.object(core, "clickable_bounds", return_value=pills):
            self.assertEqual(core.find_feed_comment_point(object()), (199, 833))

    def test_three_wide_legacy_buttons_still_work(self):
        buttons = [(0, 800, 235, 866), (240, 800, 475, 866),
                   (480, 800, 715, 866)]
        self.assertEqual(self.row(buttons), buttons)

    def test_two_wide_controls_work_when_group_sharing_is_disabled(self):
        buttons = [(0, 800, 350, 866), (360, 800, 715, 866)]
        self.assertEqual(self.row(buttons), buttons)

    def test_tabs_navigation_and_indented_replies_do_not_match(self):
        for row in ([(0, 100, 120, 166), (145, 100, 253, 166), (280, 100, 410, 166)],
                    [(0, 1530, 120, 1596), (145, 1530, 253, 1596), (280, 1530, 410, 1596)],
                    [(180, 800, 288, 866), (310, 800, 418, 866), (440, 800, 548, 866)]):
            with self.subTest(row=row):
                self.assertIsNone(self.row(row))

    def test_two_arbitrary_buttons_do_not_prove_an_action_row(self):
        self.assertIsNone(self.row([(0, 800, 235, 866), (240, 800, 475, 866)]))


class CommentSubmissionTests(unittest.TestCase):
    TEXT = "A useful example comment"

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.device = SimpleNamespace(serial="phone-a")
        self.mocks = {}
        defaults = {"stop_requested": False, "on_post_view": True,
                    "ensure_on_target": True, "is_action_blocked": False,
                    "open_comment_composer": (180, 950), "type_text": True,
                    "abort_sleep": True, "find_send_button": (650, 950),
                    "run": (0, "", ""), "current_post_url": "https://www.facebook.com/live/123",
                    "report_error": None, "safe_window_size": (720, 1600),
                    "hierarchy_root": ET.fromstring("<hierarchy/>"),
                    "edit_nodes": self.edits(self.TEXT), "visible_text": self.TEXT}
        for name, value in defaults.items():
            self.mocks[name] = self.stack.enter_context(patch.object(core, name, return_value=value))

    @staticmethod
    def edits(text):
        return [((20, 900, 600, 1000), {"class": "android.widget.EditText", "text": text})]

    def submit(self):
        return core.post_comment("phone-a", self.device, self.TEXT)

    def test_only_editable_draft_clearing_on_post_counts_as_success(self):
        self.mocks["edit_nodes"].side_effect = [self.edits(self.TEXT), self.edits("")]
        self.assertEqual(self.submit(), "commented:" + self.TEXT)
        self.mocks["run"].assert_called_once_with(
            ["shell", "input", "tap", "650", "950"], serial="phone-a")

    def test_exact_composer_placeholder_after_draft_counts_as_cleared(self):
        for hint in ("Write a comment…", "Write a public comment...", "Magsulat ng komento"):
            with self.subTest(hint=hint):
                self.mocks["edit_nodes"].side_effect = [self.edits(self.TEXT), self.edits(hint)]
                self.assertEqual(self.submit(), "commented:" + self.TEXT)

    def test_comment_placeholder_embedded_in_other_text_is_not_cleared(self):
        self.mocks["edit_nodes"].side_effect = [self.edits(self.TEXT)] + [
            self.edits("Please write a comment about this post")
        ] * 8
        self.assertEqual(self.submit(), "comment-submit-unverified")

    def test_hiding_composer_cannot_claim_success_or_trigger_another_send(self):
        self.mocks["edit_nodes"].side_effect = [self.edits(self.TEXT)] + [[]] * 8
        self.assertEqual(self.submit(), "comment-submit-unverified")
        self.mocks["run"].assert_called_once()
        self.assertEqual(self.mocks["report_error"].call_args.args[0], "COMMENT_SUBMIT_UNVERIFIED")

    def test_a_pending_draft_is_not_submitted_twice(self):
        self.assertEqual(self.submit(), "comment-submit-unverified")
        self.mocks["run"].assert_called_once()

    def test_matching_caption_does_not_prove_text_was_typed(self):
        self.mocks["edit_nodes"].return_value = self.edits("")
        self.assertEqual(self.submit(), "comment-not-typed")
        self.mocks["run"].assert_not_called()

    def test_failed_typing_does_not_send(self):
        self.mocks["type_text"].return_value = False
        self.assertEqual(self.submit(), "comment-not-typed")
        self.mocks["run"].assert_not_called()

    def test_stop_before_typing_or_between_typing_and_sending(self):
        self.mocks["stop_requested"].return_value = True
        self.assertEqual(self.submit(), "stopped")
        self.mocks["type_text"].assert_not_called()
        self.mocks["stop_requested"].return_value = False
        self.mocks["abort_sleep"].return_value = False
        self.assertEqual(self.submit(), "stopped")
        self.mocks["run"].assert_not_called()

    def test_action_block_prevents_typing(self):
        self.mocks["is_action_blocked"].return_value = True
        self.assertEqual(self.submit(), "action-blocked")
        self.mocks["type_text"].assert_not_called()
        self.mocks["run"].assert_not_called()

    def test_action_block_appearing_after_send_does_not_send_again(self):
        self.mocks["is_action_blocked"].side_effect = [False, False, True]
        self.assertEqual(self.submit(), "action-blocked")
        self.mocks["run"].assert_called_once()

    def test_keyboard_done_is_not_a_send_control(self):
        self.mocks["find_send_button"].return_value = None
        self.mocks["hierarchy_root"].return_value = ET.fromstring(
            '<hierarchy><node content-desc="Done" bounds="[600,1300][700,1400]"/></hierarchy>')
        self.assertEqual(self.submit(), "comment-send-failed")
        self.mocks["run"].assert_not_called()

    def test_keyboard_enter_can_submit_when_send_button_is_absent(self):
        self.mocks["find_send_button"].return_value = None
        self.mocks["hierarchy_root"].return_value = ET.fromstring(
            '<hierarchy><node content-desc="Enter" bounds="[600,1300][700,1400]"/></hierarchy>')
        self.mocks["edit_nodes"].side_effect = [self.edits(self.TEXT), self.edits("")]
        self.assertEqual(self.submit(), "commented:" + self.TEXT)
        self.mocks["run"].assert_called_once_with(
            ["shell", "input", "tap", "650", "1350"], serial="phone-a")

    def test_composer_clearing_on_another_screen_does_not_count(self):
        self.mocks["edit_nodes"].side_effect = [self.edits(self.TEXT)] + [self.edits("")] * 8
        self.mocks["on_post_view"].side_effect = [True] + [False] * 8
        self.assertEqual(self.submit(), "comment-submit-unverified")
        self.mocks["run"].assert_called_once()


class SendButtonTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(core, "safe_window_size", return_value=(720, 1600)))
        self.stack.enter_context(patch.object(core, "edit_nodes", return_value=[
            ((20, 900, 600, 1000), {"class": "android.widget.EditText", "text": "draft"})]))
        self.items = self.stack.enter_context(patch.object(core, "clickable_items", return_value=[]))

    def test_labeled_wide_button_is_accepted_beside_composer(self):
        self.items.return_value = [((500, 920, 710, 980), {"text": "Post"})]
        self.assertEqual(core.find_send_button(object()), (605, 950))

    def test_uiobject_info_property_and_sequential_label_fallback(self):
        calls = []

        def device(**selector):
            calls.append(selector)
            return SimpleNamespace(exists=selector["text"] == "Post", info={
                "bounds": {"left": 500, "top": 920, "right": 710, "bottom": 980}})

        self.assertEqual(core.find_send_button(device), (605, 950))
        self.assertEqual([call["text"] for call in calls], ["Send", "Post"])

    def test_keyboard_key_and_named_non_send_controls_are_excluded(self):
        self.items.return_value = [
            ((630, 1040, 700, 1110), {}),
            ((630, 920, 700, 980), {"content-desc": "Emoji"}),
            ((630, 920, 700, 980), {"text": "Post a GIF"})]

        def device(**selector):
            return SimpleNamespace(exists=False)

        self.assertIsNone(core.find_send_button(device))

    def test_unlabeled_send_icon_remains_usable_in_composer_band(self):
        self.items.return_value = [((630, 920, 700, 980), {})]
        self.assertEqual(core.find_send_button(object()), (665, 950))


class TargetProcessingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.mocks = {}
        for name, value in {"stop_requested": False, "ensure_on_target": True,
                            "unique_comment": "Example comment", "humanize_typo": "Example comment",
                            "is_action_blocked": False, "report_progress": None,
                            "report_error": None, "dbg": None, "_acct_log": None,
                            "post_comment": "comment-submit-unverified"}.items():
            self.mocks[name] = self.stack.enter_context(patch.object(core, name, return_value=value))
        self.target = {"url": "https://www.facebook.com/live/123", "comment": "Example comment",
                       "do_react": False, "do_comment": True}

    def process(self, results):
        return core._process_target("phone-a", object(), self.target, 1, 1, 1,
                                    False, results, account_name="Test account")

    def test_failed_target_verification_prevents_generation_and_posting(self):
        self.mocks["ensure_on_target"].return_value = False
        results = core.RunResults()
        self.assertEqual(self.process(results), "ok")
        self.mocks["unique_comment"].assert_not_called()
        self.mocks["post_comment"].assert_not_called()
        self.assertEqual(results.link_tally[1]["failed"], 1)

    def test_unverified_send_is_not_retried_now_or_by_coverage_sweep(self):
        results = core.RunResults()
        self.assertEqual(self.process(results), "ok")
        self.mocks["post_comment"].assert_called_once()
        self.assertEqual(results.account_tally[("Test account", 1)]["commented"],
                         "comment-submit-unverified")
        self.assertEqual(core._accounts_to_retry(["Test account"], results, [self.target]), [])
        self.process(results)
        self.mocks["post_comment"].assert_called_once()

    def test_mixed_mode_coverage_uses_original_target_link_indexes(self):
        results = core.RunResults()
        results.note_account("Test account", 2, "commented", "comment-submit-unverified")
        targets = [dict(self.target, do_comment=False), self.target]
        self.assertEqual(core._accounts_to_retry(["Test account"], results, targets), [])
        results.account_tally.clear()
        results.note_account("Test account", 1, "commented", "ok")
        self.assertEqual(core._accounts_to_retry(["Test account"], results, targets), ["Test account"])


if __name__ == "__main__":
    unittest.main()
