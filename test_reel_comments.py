"""Reels and videos: recognise the player, open its comments, post - offline.

Every geometry below is copied from the fleet's own debug dumps (FBPoison and
FBVenom debug folders, all 720x1612, Aug-Nov 2026). Facebook Lite's reel
player exposes no text at all, so the bounds ARE the screen. Names and text
are replaced. No phone, ADB or network is used.

Where the comment box is, per link (see core._target_kind):
  reel / video  -> the reel player: Comment is the 2nd button of the rail down
                   the right edge; it opens a sheet with the box at the bottom.
  post / photo  -> the post view: the box sits at the bottom of the post.
"""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import core


def node(cls, bounds, text="", desc="", clickable=True, package="com.facebook.lite"):
    x1, y1, x2, y2 = bounds
    return (f'<node class="android.{cls}" package="{package}" '
            f'bounds="[{x1},{y1}][{x2},{y2}]" text="{text}" content-desc="{desc}" '
            f'clickable="{"true" if clickable else "false"}" />')


def screen(nodes, width=720, height=1612):
    return ('<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
            f'<node class="android.widget.FrameLayout" package="com.facebook.lite" '
            f'bounds="[0,0][{width},{height}]" text="" content-desc="" clickable="false">'
            + "".join(nodes) + "</node></hierarchy>")


def group(bounds, clickable=True):
    return node("view.ViewGroup", bounds, clickable=clickable)


def reel_player_oct():
    """English "Reels" layout, Oct 2026 (one real 720x1612 dump)."""
    return screen(
        [group((x, 74, x + 120, 180)) for x in range(0, 720, 120)] + [   # top tabs
            group((356, 268, 364, 276)),
            group((626, 1062, 720, 1174)),      # Like
            group((626, 1188, 720, 1300)),      # Comment
            group((626, 1314, 720, 1426)),      # Share
            group((626, 1440, 720, 1524)),      # More
            group((21, 1386, 77, 1442)),        # author avatar
            group((91, 1392, 278, 1437)),       # author name
            group((367, 1385, 507, 1441)),      # Follow
            node("view.View", (21, 1463, 587, 1508)),                  # caption
            node("widget.SeekBar", (21, 1503, 699, 1547), text="1255.0", clickable=False),
            group((461, 207, 517, 263)), group((552, 207, 608, 263)),
            group((643, 207, 699, 263)),        # search / camera / profile
        ])


def reel_player_aug():
    """Taglish "Mga Reel" layout, Aug 2026 (one real 720x1612 dump).

    Its square 96x96 avatar + name + "I-follow" band reads as a post's action
    row - which is why a reel opened from a /share/v/ link was refused.
    """
    return screen(
        [group((x, 67, x + 120, 173)) for x in range(0, 720, 120)] + [
            group((355, 273, 365, 283)),
            group((612, 981, 720, 1109)),       # Like
            group((612, 1125, 720, 1253)),      # Comment
            group((612, 1269, 720, 1397)),      # Share
            group((612, 1413, 720, 1509)),      # More
            group((24, 1326, 120, 1422)),       # avatar
            group((136, 1352, 319, 1397)),      # name
            group((380, 1345, 524, 1402)),      # I-follow
            node("view.View", (24, 1446, 568, 1491)),
            node("widget.SeekBar", (24, 1485, 696, 1535), text="12809.0", clickable=False),
            group((424, 199, 488, 263)), group((528, 199, 592, 263)),
            group((632, 199, 696, 263)),
        ])


SHEET_ROWS = (  # avatar, body, name, meta1, meta2, comment Like  (one comment each)
    ((16, 240, 102, 324), (118, 240, 704, 318), (142, 240, 229, 279),
     (118, 335, 299, 370), (299, 334, 347, 370), (632, 322, 704, 370)),
    ((16, 418, 102, 502), (118, 418, 704, 535), (142, 418, 322, 457),
     (118, 552, 299, 587), (299, 551, 401, 587), (632, 539, 704, 587)),
    ((16, 635, 102, 719), (118, 635, 704, 755), (142, 635, 514, 674),
     (118, 772, 299, 807), (299, 771, 401, 807), (632, 759, 704, 807)),
    ((16, 855, 102, 939), (118, 855, 704, 933), (142, 855, 466, 894),
     (118, 950, 299, 985), (299, 949, 429, 985), (632, 937, 704, 985)),
    ((16, 1033, 102, 1117), (118, 1033, 704, 1111), (142, 1033, 343, 1072),
     (118, 1128, 299, 1163), (299, 1127, 347, 1163), (632, 1115, 704, 1163)),
    ((16, 1211, 102, 1295), (118, 1211, 704, 1289), (142, 1211, 285, 1250),
     (118, 1306, 299, 1341), (299, 1305, 347, 1341), (632, 1293, 704, 1341)),
)
PLACEHOLDER = "Sumulat ng comment..."
DRAFT = "grabe ang ganda ng video"


def reel_sheet(text=PLACEHOLDER, top=1472, rows=6, composer=True, send=False):
    """A reel's comment sheet, Aug 2026 (one real 720x1612 dump).

    Six one-line comments put their Like buttons at an even 178 px - the shape
    of a rail. `top` moves the comment row: 1472 with the keyboard down, 916
    (centre y=950, as measured on a reel) with it up. With a draft typed the
    row shows ONE Send icon at its right end, like every Facebook Lite comment
    row in the fleet's dumps (68x64 at x=626).
    """
    nodes = [group((0, 67, 96, 160)), group((608, 67, 712, 160)),
             group((118, 162, 299, 192)), group((299, 162, 347, 192)),
             group((632, 162, 704, 192))]
    for row in SHEET_ROWS[:rows]:
        nodes += [group(row[0]), group(row[1]), node("view.View", row[2]),
                  group(row[3]), group(row[4]), group(row[5])]
    if composer:
        nodes.append(node("widget.MultiAutoCompleteTextView", (40, top, 472, top + 68),
                          text=text))
        if send:
            nodes.append(group((626, top, 694, top + 64)))
        else:
            nodes += [group((474, top, 550, top + 72)), group((550, top, 626, top + 72)),
                      group((626, top, 702, top + 72))]
    return screen(nodes)


class Phone:
    """A frozen screen the core helpers can read; taps can change it."""
    _frozen = True
    serial = None

    def __init__(self, xml, size=(720, 1612), on_click=None):
        self.xml, self.size = xml, size
        self.clicks, self.swipes = [], []
        self.on_click = on_click

    def dump_hierarchy(self, *args, **kwargs):
        return self.xml

    def window_size(self):
        return self.size

    def __call__(self, **selector):
        return SimpleNamespace(exists=False)

    def click(self, x, y):
        self.clicks.append((x, y))
        if self.on_click:
            self.on_click(self, (x, y))

    def swipe(self, *args, **kwargs):
        self.swipes.append(args)

    def press(self, key):
        self.clicks.append(key)
        if self.on_click:
            self.on_click(self, key)


REEL_LINKS = (
    "https://www.facebook.com/share/v/19zVwRPKRW",          # unresolved share/v
    "https://www.facebook.com/share/r/1DTPaZadbE",          # unresolved share/r
    "https://www.facebook.com/reel/1389919633103002",
    "https://www.facebook.com/watch?v=1945917729708319",
    "https://www.facebook.com/somepage/videos/1945917729708319",
    "https://www.facebook.com/somepage/videos/141169-live/1945917729708319",
    "https://fb.watch/AbCd12",
    "fb://reel/1389919633103002",
)
POST_LINK = "https://www.facebook.com/somepage/posts/1234567890"


def in_context(url):
    core.set_current_post_url(url)


class ReelPlayerRecognitionTests(unittest.TestCase):
    def tearDown(self):
        core.set_current_post_url(None)

    def test_every_reel_and_video_link_accepts_the_real_reel_player(self):
        for url in REEL_LINKS:
            for build in (reel_player_oct, reel_player_aug):
                with self.subTest(url=url, layout=build.__name__):
                    in_context(url)
                    self.assertTrue(core.on_post_view(Phone(build())))

    def test_the_august_reel_reads_as_a_post_row_which_is_why_context_matters(self):
        phone = Phone(reel_player_aug())
        self.assertTrue(core._has_post_action_row(phone))
        # Before: only /reel/ and /share/r/ counted, so /share/v/ was refused.
        in_context("https://www.facebook.com/share/v/19zVwRPKRW")
        self.assertTrue(core._is_reel_context())
        self.assertIsNotNone(core.find_reel_like_point(phone))

    def test_a_post_link_still_refuses_a_reel_that_looks_like_a_post(self):
        in_context(POST_LINK)
        self.assertFalse(core._is_reel_context())
        self.assertFalse(core.on_post_view(Phone(reel_player_aug())))

    def test_comment_sheet_like_buttons_are_never_taken_for_the_rail(self):
        in_context(REEL_LINKS[0])
        for composer in (True, False):              # False: comments switched off
            with self.subTest(composer=composer):
                phone = Phone(reel_sheet(composer=composer))
                self.assertIsNone(core._reel_action_stack(phone))
                self.assertIsNone(core.find_reel_comment_point(phone))

    def test_reel_sheet_with_the_keyboard_up_is_still_the_target(self):
        in_context(REEL_LINKS[1])
        phone = Phone(reel_sheet(top=916, rows=3))
        self.assertFalse(core.has_comment_composer(phone))   # box is mid-screen
        self.assertTrue(core.on_post_view(phone))

    def test_one_check_reads_a_live_phone_once_not_once_per_detector(self):
        class LivePhone:                 # not frozen: every read is a phone round trip
            serial = None

            def __init__(self, xml):
                self.xml, self.reads = xml, 0

            def dump_hierarchy(self, *args, **kwargs):
                self.reads += 1
                return self.xml

            def window_size(self):
                return (720, 1612)

        for url, xml, expected in ((POST_LINK, reel_player_aug(), False),
                                   (REEL_LINKS[0], reel_sheet(top=916, rows=3), True)):
            with self.subTest(url=url):
                in_context(url)
                phone = LivePhone(xml)
                self.assertEqual(core.on_post_view(phone), expected)
                self.assertEqual(phone.reads, 1)

    def test_another_apps_search_box_is_not_a_reel_sheet(self):
        in_context(REEL_LINKS[1])
        xml = screen([node("widget.EditText", (112, 900, 445, 984), text="Search…",
                           package="com.google.android.apps.searchlite")])
        self.assertFalse(core._reel_sheet_open(Phone(xml)))


class ReelCommentEntryTests(unittest.TestCase):
    def setUp(self):
        for item in (patch.object(core.time, "sleep"),
                     patch.object(core, "run", return_value=(0, "", "")),
                     patch.object(core, "abort_sleep", return_value=True),
                     patch.object(core, "stop_requested", return_value=False)):
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(core.set_current_post_url, None)

    def test_comment_is_the_second_rail_button_on_both_layouts(self):
        in_context(REEL_LINKS[0])
        self.assertEqual(core.find_reel_comment_point(Phone(reel_player_oct())), (673, 1244))
        self.assertEqual(core.find_reel_comment_point(Phone(reel_player_aug())), (666, 1189))

    def test_tapping_comment_opens_the_sheet_and_returns_its_box(self):
        in_context(REEL_LINKS[0])

        def open_sheet(phone, point):
            if point == (673, 1244):
                phone.xml = reel_sheet()
        phone = Phone(reel_player_oct(), on_click=open_sheet)
        box = core.try_open_comment_from_screen("phone-1", phone)
        self.assertEqual(phone.clicks[0], (673, 1244))      # the rail, nothing else
        self.assertEqual(box, (256, 1506))                  # the sheet's comment box

    def test_the_august_author_row_is_never_tapped_as_a_comment_button(self):
        in_context(REEL_LINKS[1])

        def open_sheet(phone, point):
            if point == (666, 1189):
                phone.xml = reel_sheet()
        phone = Phone(reel_player_aug(), on_click=open_sheet)
        core.try_open_comment_from_screen("phone-1", phone)
        self.assertEqual(phone.clicks[0], (666, 1189))

    def test_a_reel_is_never_swiped_when_its_box_will_not_open(self):
        in_context(REEL_LINKS[0])
        phone = Phone(reel_player_oct())                    # taps change nothing
        dumps = []
        with patch.object(core, "wait_for_edit_box", return_value=None), \
                patch.object(core, "dump_debug", side_effect=lambda s, d, tag: dumps.append(tag)):
            self.assertIsNone(core.open_comment_composer("phone-1", phone))
        self.assertEqual(phone.swipes, [])                  # a swipe = NEXT reel
        self.assertEqual(phone.clicks, [(673, 1244)] * 3)   # Comment, then 2 retries
        self.assertEqual(dumps, ["reel_comment_box_missing"])

    def test_a_post_still_scrolls_down_to_a_box_under_a_long_caption(self):
        in_context(POST_LINK)
        phone = Phone(screen([]))
        with patch.object(core, "try_open_comment_from_screen", return_value=None), \
                patch.object(core, "on_post_view", return_value=True):
            self.assertIsNone(core.open_comment_composer("phone-1", phone, max_scrolls=3))
        self.assertEqual(len(phone.swipes), 3)

    def test_reacting_with_a_sheet_open_closes_it_instead_of_tapping_a_comment(self):
        in_context(REEL_LINKS[0])

        def back_closes(phone, key):
            if key == "back":
                phone.xml = reel_player_oct()
        phone = Phone(reel_sheet(), on_click=back_closes)
        self.assertEqual(core._wait_reel_like(phone, timeout=1.0), (673, 1118))
        self.assertEqual(phone.clicks, ["back"])


class ReelPostCommentTests(unittest.TestCase):
    """post_comment() end to end on a reel, from the player to a verified post."""

    def setUp(self):
        in_context("https://www.facebook.com/share/v/19zVwRPKRW")
        self.addCleanup(core.set_current_post_url, None)
        self.adb_taps, self.errors, self.dumps = [], [], []

        def open_sheet(phone, point):
            if point == (673, 1244):
                phone.xml = reel_sheet()

        self.phone = Phone(reel_player_oct(), on_click=open_sheet)

        def type_text(serial, d, point, text):
            d.xml = reel_sheet(text=text, send=True)
            return True

        def run(args, serial=None, timeout=60):
            if args[:3] == ["shell", "input", "tap"]:
                self.adb_taps.append((int(args[3]), int(args[4])))
                # Sent: the box clears; the keyboard is still up.
                self.phone.xml = reel_sheet(top=916, rows=3)
            return 0, "", ""

        for item in (
                patch.object(core.time, "sleep"),
                patch.object(core, "run", side_effect=run),
                patch.object(core, "type_text", side_effect=type_text),
                patch.object(core, "abort_sleep", return_value=True),
                patch.object(core, "stop_requested", return_value=False),
                patch.object(core, "dump_debug",
                             side_effect=lambda s, d, tag: self.dumps.append(tag)),
                patch.object(core, "report_error",
                             side_effect=lambda code, *a, **k: self.errors.append(code))):
            item.start()
            self.addCleanup(item.stop)

    def test_share_v_reel_is_commented_and_verified(self):
        status = core.post_comment("phone-1", self.phone, DRAFT)
        self.assertEqual(status, f"commented:{DRAFT}")
        self.assertEqual(self.phone.clicks[0], (673, 1244))   # rail Comment
        self.assertEqual(self.adb_taps, [(660, 1504)])        # one Send, once
        self.assertEqual(self.phone.swipes, [])
        self.assertEqual(self.errors, [])
        self.assertEqual(self.dumps, [])


if __name__ == "__main__":
    unittest.main()
