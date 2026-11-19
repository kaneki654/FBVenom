"""Finding and pressing the comment's Send control on the fleet's real layouts.

Every geometry below is copied from the phones' own debug dumps (FBPoison and
FBVenom debug folders, all 720 px wide), with names and post text replaced.
No phone, ADB or network is used.
"""
from html import escape
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import core


def node(cls, bounds, text="", desc="", clickable=True):
    x1, y1, x2, y2 = bounds
    return (f'<node class="android.{cls}" bounds="[{x1},{y1}][{x2},{y2}]" text="{text}" '
            f'content-desc="{desc}" clickable="{"true" if clickable else "false"}" />')


def screen(width, height, nodes):
    return ('<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
            f'<node class="android.widget.FrameLayout" bounds="[0,0][{width},{height}]" text="" '
            'content-desc="" clickable="false">' + "".join(nodes) + "</node></hierarchy>")


def emoji_strip(top, bottom):
    """The quick-emoji cells Facebook Lite shows above a draft (90x112 each)."""
    return [node("view.ViewGroup", (x, top, x + 90, bottom)) for x in range(90, 720, 90)]


DRAFT = "grabe ang ganda ng view dito"

# Nov-2026 "Comment as <name>" layout, keyboard down, gesture navigation: the
# comment row is the bottom of the screen. Send = the 68x64 icon at x=626.
def new_gesture(text=DRAFT, send=True):
    return screen(720, 1612, emoji_strip(1386, 1498) + [
        node("view.ViewGroup", (16, 1523, 80, 1587)),                       # avatar
        node("widget.MultiAutoCompleteTextView", (116, 1520, 472, 1588), text=text),
    ] + ([node("view.ViewGroup", (626, 1520, 694, 1584))] if send else []))


# Same layout on a phone with the 3-button bar: the row sits above the bar.
def new_navbar(text=DRAFT):
    return screen(720, 1612, emoji_strip(1290, 1402) + [
        node("view.ViewGroup", (16, 1427, 80, 1491)),
        node("widget.MultiAutoCompleteTextView", (116, 1424, 472, 1492), text=text),
        node("view.ViewGroup", (626, 1424, 694, 1488)),
        node("widget.ImageView", (100, 1516, 260, 1612), desc="Overview"),
        node("widget.ImageView", (280, 1516, 440, 1612), desc="Home"),
        node("widget.ImageView", (460, 1516, 620, 1612), desc="Back"),
    ])


# Older layout on the 720x1600 phones: two greyed icons, Send = 59x56 at x=638.
def old_1600(text=DRAFT):
    return screen(720, 1600, emoji_strip(1374, 1472) + [
        node("view.ViewGroup", (14, 1494, 70, 1550)),
        node("widget.MultiAutoCompleteTextView", (103, 1486, 503, 1554), text=text),
        node("view.ViewGroup", (505, 1492, 571, 1555), clickable=False),
        node("view.ViewGroup", (572, 1492, 639, 1555), clickable=False),
        node("view.ViewGroup", (638, 1492, 697, 1548)),
    ])


class Phone:
    """A frozen screen the core helpers can read, swappable mid-test."""
    _frozen = True
    serial = None

    def __init__(self, xml, size=(720, 1612)):
        self.xml, self.size = xml, size

    def dump_hierarchy(self, *args, **kwargs):
        return self.xml

    def window_size(self):
        return self.size

    def __call__(self, **selector):
        return SimpleNamespace(exists=False)

    def click(self, x, y):
        pass


class FindSendButtonTests(unittest.TestCase):
    def find(self, xml, size=(720, 1612)):
        return core.find_send_button(Phone(xml, size))

    def test_bottom_of_screen_row_on_gesture_phones_is_found(self):
        # The old rule ignored the bottom 100 px, so this returned None and the
        # comment stayed typed forever.
        self.assertEqual(self.find(new_gesture()), (660, 1552))

    def test_row_above_the_button_bar(self):
        self.assertEqual(self.find(new_navbar()), (660, 1456))

    def test_older_720x1600_layout(self):
        self.assertEqual(self.find(old_1600(), (720, 1600)), (667, 1520))

    def test_keyboard_up_moves_the_row_and_send_follows(self):
        xml = screen(720, 1612, emoji_strip(760, 872) + [
            node("view.ViewGroup", (16, 893, 80, 957)),
            node("widget.MultiAutoCompleteTextView", (116, 890, 472, 958), text=DRAFT),
            node("view.ViewGroup", (626, 890, 694, 954))])
        self.assertEqual(self.find(xml), (660, 922))

    def test_wrapped_multi_line_draft_still_finds_send(self):
        # Five lines tall: the field's centre is ~110 px above the Send icon.
        xml = screen(720, 1612, emoji_strip(1170, 1282) + [
            node("view.ViewGroup", (16, 1523, 80, 1587)),
            node("widget.MultiAutoCompleteTextView", (116, 1300, 472, 1588), text=DRAFT),
            node("view.ViewGroup", (626, 1520, 694, 1584))])
        self.assertEqual(self.find(xml), (660, 1552))

    def test_emoji_strip_and_avatar_are_never_chosen(self):
        self.assertIsNone(self.find(new_gesture(send=False)))

    def test_labelled_send_in_another_language_counts(self):
        xml = screen(720, 1612, emoji_strip(1386, 1498) + [
            node("widget.MultiAutoCompleteTextView", (116, 1520, 472, 1588), text=DRAFT),
            node("view.ViewGroup", (626, 1520, 694, 1584), desc="Ipadala")])
        self.assertEqual(self.find(xml), (660, 1552))

    def test_camera_or_sticker_icons_are_skipped_for_the_real_send(self):
        xml = screen(720, 1612, [
            node("widget.MultiAutoCompleteTextView", (116, 1520, 472, 1588), text=DRAFT),
            node("view.ViewGroup", (572, 1520, 636, 1584)),                   # unlabelled Send
            node("view.ViewGroup", (640, 1520, 704, 1584), desc="Send a sticker")])
        self.assertEqual(self.find(xml), (604, 1552))

    def test_explicit_send_label_wins(self):
        xml = screen(720, 1612, [
            node("widget.MultiAutoCompleteTextView", (116, 1520, 472, 1588), text=DRAFT),
            node("view.ViewGroup", (520, 1520, 584, 1584), desc="Post comment"),
            node("view.ViewGroup", (640, 1520, 704, 1584))])
        self.assertEqual(self.find(xml), (552, 1552))


class PlaceholderTests(unittest.TestCase):
    def test_every_empty_composer_seen_on_the_fleet(self):
        for value in ("Comment as Test User", "Sumulat ng comment...", "Write a comment…",
                      "Write a public comment", "Magsulat ng komento", ""):
            with self.subTest(value=value):
                self.assertTrue(core._composer_is_placeholder(value) or not value)

    def test_a_real_draft_is_not_a_placeholder(self):
        for value in ("grabe ang ganda", "comment ko lang", "nice post"):
            with self.subTest(value=value):
                self.assertFalse(core._composer_is_placeholder(value))


class PostCommentTests(unittest.TestCase):
    """The whole send step on a fake phone whose screen changes when tapped."""

    def setUp(self):
        self.phone = Phone(new_gesture(text="Comment as Test User"))
        self.taps, self.dumps, self.errors = [], [], []
        self.after_tap = new_gesture(text="Comment as Test User")

        def type_text(serial, d, point, text):
            self.phone.xml = new_gesture(text=text)
            return True

        def run(args, serial=None, timeout=60):
            if args[:3] == ["shell", "input", "tap"]:
                self.taps.append((int(args[3]), int(args[4])))
                self.phone.xml = self.after_tap
            return 0, "", ""

        patches = [
            patch.object(core, "on_post_view", return_value=True),
            patch.object(core, "is_action_blocked", return_value=False),
            patch.object(core, "open_comment_composer", return_value=(294, 1554)),
            patch.object(core, "type_text", side_effect=type_text),
            patch.object(core, "run", side_effect=run),
            patch.object(core, "abort_sleep", return_value=True),
            patch.object(core, "stop_requested", return_value=False),
            patch.object(core, "dump_debug", side_effect=lambda s, d, tag: self.dumps.append(tag)),
            patch.object(core, "report_error",
                         side_effect=lambda code, *a, **k: self.errors.append(code)),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_new_layout_on_a_gesture_phone_is_sent_and_verified(self):
        status = core.post_comment("phone-1", self.phone, DRAFT)
        self.assertEqual(status, f"commented:{DRAFT}")
        self.assertEqual(self.taps, [(660, 1552)])
        self.assertEqual(self.errors, [])
        self.assertEqual(self.dumps, [])

    def test_taglish_placeholder_after_sending_counts_as_posted(self):
        self.after_tap = old_1600(text="Sumulat ng comment...")
        self.phone.size = (720, 1600)
        with patch.object(core, "type_text",
                          side_effect=lambda s, d, p, t: setattr(self.phone, "xml", old_1600(text=t)) or True):
            status = core.post_comment("phone-2", self.phone, DRAFT)
        self.assertEqual(status, f"commented:{DRAFT}")
        self.assertEqual(self.taps, [(667, 1520)])

    def test_missing_send_saves_the_screen_and_reports_it(self):
        with patch.object(core, "type_text",
                          side_effect=lambda s, d, p, t: setattr(self.phone, "xml", new_gesture(t, send=False)) or True):
            status = core.post_comment("phone-3", self.phone, DRAFT)
        self.assertEqual(status, "comment-send-failed")
        self.assertEqual(self.taps, [])
        self.assertEqual(self.dumps, ["comment_send_failed"])
        self.assertEqual(self.errors, ["COMMENT_SEND_FAILED"])

    def test_draft_that_stays_after_the_tap_is_unverified_once_and_saved(self):
        self.after_tap = new_gesture(text=DRAFT)          # nothing happened
        status = core.post_comment("phone-4", self.phone, DRAFT)
        self.assertEqual(status, "comment-submit-unverified")
        self.assertEqual(self.taps, [(660, 1552)])         # one tap, never a second send
        self.assertEqual(self.dumps, ["comment_unverified"])
        self.assertEqual(self.errors, ["COMMENT_SUBMIT_UNVERIFIED"])


class TypeTextTests(unittest.TestCase):
    """Typing into the comment box without a single keystroke.

    Measured live on a TECNO KM4 (Facebook Lite 538): the post's "Comment as"
    box drops focus after 2-3 injected keystrokes, the rest move focus to the
    header's Back arrow, and a typed space presses it. set_text wrote the full
    comment and the Send arrow appeared; the input method's send_keys did too.
    """

    def type(self, text, set_text_works=True, ime_works=True):
        adb, phone = [], Phone(new_gesture(text="Comment as Test User"))
        used = []

        class Field:
            exists = True

            def set_text(self, value):
                used.append("set_text")
                if set_text_works:
                    phone.xml = new_gesture(text=escape(value))

        def send_keys(value):
            used.append("send_keys")
            if ime_works:
                phone.xml = new_gesture(text=escape(value))
        phone.__class__ = type("BoxPhone", (Phone,), {"__call__": lambda self, **k: Field()})
        phone.send_keys = send_keys
        with patch.object(core, "run", side_effect=lambda args, serial=None, timeout=60:
                          adb.append(args) or (0, "", "")), \
                patch.object(core, "abort_sleep", return_value=True), \
                patch.object(core, "stop_requested", return_value=False), \
                patch.object(core.time, "sleep"):
            ok = core.type_text("phone-5", phone, (294, 1554), text)
        return ok, used, adb

    def test_every_comment_is_written_into_the_box_with_no_keystrokes(self):
        for text in ("grabe ang ganda ng video", "grabe 🤣🤣🤣", "ok (yes) & 100% sure?"):
            with self.subTest(text=text):
                ok, used, adb = self.type(text)
                self.assertTrue(ok)
                self.assertEqual(used, ["set_text"])
                # No `input text`, no Backspace clearing, no space-then-delete:
                # each of those can land on the Back arrow once focus is gone.
                self.assertEqual(adb, [])

    def test_the_input_method_takes_over_when_set_text_does_not_land(self):
        ok, used, adb = self.type("salamat sa balita", set_text_works=False)
        self.assertTrue(ok)
        self.assertEqual(used, ["set_text", "send_keys"])
        self.assertEqual(adb, [])

    def test_a_draft_that_never_reaches_the_box_fails_cleanly(self):
        ok, used, adb = self.type("salamat sa balita", set_text_works=False, ime_works=False)
        self.assertFalse(ok)
        self.assertEqual(adb, [])


if __name__ == "__main__":
    unittest.main()
