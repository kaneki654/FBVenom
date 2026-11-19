"""Any phone box: other screens, hung USB links, MIUI pop-ups - offline.

Swapping in a different phone box broke the run three ways, all measured live
on a box of eight Redmi 9 phones (M2004J19C, MIUI 12, Android 10, 1080x2340,
density 440) on cascaded USB 2.0 hubs, with scrcpy mirroring every phone:

  * The reel rail's buttons are 148x176 px there (~108x128 on the fleet's
    720-px phones), past the 140x175 limit, so no reel or video ever verified
    - every one was force-stopped and reopened until it was given up.
  * Two phones on one sub-hub stopped answering (`adb shell` timed out for 7+
    minutes) while `adb devices` still listed them, so nothing repaired them.
  * MIUI's "Use USB for" pop-up sits over Facebook after every (re)connect.

Geometry below is the live Redmi reel screen, bounds only (names and text
dropped). No phone, ADB or network is used.
"""
import subprocess
import unittest
from unittest.mock import patch

import core
import uiautomator2 as u2

from test_reel_comments import Phone, group, node, screen, REEL_LINKS, in_context


def redmi_reel_player():
    """Facebook Lite 538 reel player on a Redmi 9 (1080x2340, 2.75 px/dp)."""
    return screen(
        [group((x, 76, x + 180, 222)) for x in range(0, 1080, 180)] + [  # top tabs
            group((673, 259, 761, 347)), group((816, 259, 904, 347)),
            group((959, 259, 1047, 347)),       # search / camera / profile
            group((915, 387, 1047, 519)),
            group((386, 1524, 694, 1608)),      # Fullscreen
            group((932, 1408, 1080, 1584)),     # Like
            group((932, 1606, 1080, 1782)),     # Comment
            group((932, 1804, 1080, 1980)),     # Share
            group((932, 2002, 1080, 2134)),     # More
            group((33, 1863, 101, 1951)),       # author avatar
            group((123, 1876, 587, 1939)),      # author name
            group((670, 1866, 868, 1945)),      # Follow
            node("view.View", (33, 1984, 871, 2110)),                  # caption
        ], width=1080, height=2340)


REDMI = (1080, 2340)
REDMI_PX_PER_DP = 1080 / 393            # uiautomator2: displaySizeDpX = 393


class RedmiPhone(Phone):
    serial = "REDMI-BOX-TEST"


class ScreenDensityTests(unittest.TestCase):
    def setUp(self):
        core.forget_window_size()
        self.addCleanup(core.forget_window_size)
        self.addCleanup(core.set_current_post_url, None)

    def test_the_redmi_reel_rail_is_found_with_the_density_the_phone_reports(self):
        with core._density_lock:
            core._density_cache[RedmiPhone.serial] = REDMI_PX_PER_DP
        phone = RedmiPhone(redmi_reel_player(), size=REDMI)
        self.assertAlmostEqual(core.dp_scale(phone), 1.374, places=3)
        for url in REEL_LINKS:
            with self.subTest(url=url):
                in_context(url)
                self.assertEqual(core.find_reel_comment_point(phone), (1006, 1694))
                self.assertTrue(core.on_post_view(phone))

    def test_a_saved_redmi_dump_is_judged_by_its_short_side(self):
        in_context(REEL_LINKS[0])
        phone = Phone(redmi_reel_player(), size=REDMI)         # no serial, no info
        self.assertEqual(core.dp_scale(phone), 1.5)
        self.assertEqual(len(core._reel_action_stack(phone)), 4)
        self.assertTrue(core.on_post_view(phone))

    def test_the_old_fixed_pixel_limits_refused_this_rail(self):
        # The bug: 148x176 px buttons against limits of 140x175 px.
        in_context(REEL_LINKS[0])
        phone = Phone(redmi_reel_player(), size=REDMI)
        with patch.object(core, "dp_scale", return_value=1.0):
            self.assertIsNone(core._reel_action_stack(phone))
            self.assertFalse(core.on_post_view(phone))

    def test_the_fleets_720px_phones_keep_their_exact_limits(self):
        for size in ((720, 1612), (720, 1600), (1600, 720), (1612, 720)):
            with self.subTest(size=size):
                phone = Phone("<hierarchy/>", size=size)
                self.assertEqual(core.dp_scale(phone), 1.0)
                px = core._scaler(phone)
                self.assertEqual([px(n) for n in (8, 35, 140, 175)], [8, 35, 140, 175])

    def test_a_phone_drawing_fewer_px_per_dp_than_the_fleet_is_never_shrunk(self):
        # The TECNO KM4 draws ~1.75 px per dp (rail gap 126 px = 72 dp).
        class Tecno(Phone):
            serial = "TECNO-TEST"
        with core._density_lock:
            core._density_cache["TECNO-TEST"] = 1.75
        self.assertEqual(core.dp_scale(Tecno("<hierarchy/>", size=(720, 1600))), 1.0)

    def test_a_live_phones_density_comes_from_uiautomator(self):
        class LiveRedmi(u2.Device):            # never connects: no __init__
            serial = "REDMI-LIVE-TEST"
            info = {"displayWidth": 1080, "displaySizeDpX": 393}

            def window_size(self):
                return REDMI

        phone = object.__new__(LiveRedmi)
        self.assertAlmostEqual(core.px_per_dp(phone), REDMI_PX_PER_DP)
        # Cached per phone, so a frozen Snapshot of it is sized the same way.
        frozen = Phone("<hierarchy/>", size=REDMI)
        frozen.serial = "REDMI-LIVE-TEST"
        self.assertAlmostEqual(core.dp_scale(frozen), REDMI_PX_PER_DP / 2.0)

    def test_a_phone_reporting_nonsense_falls_back_to_its_screen(self):
        class Broken(u2.Device):
            serial = "BROKEN-TEST"
            info = {"displayWidth": 1080, "displaySizeDpX": 0}

            def window_size(self):
                return REDMI

        self.assertEqual(core.px_per_dp(object.__new__(Broken)), 3.0)

    def test_a_phone_that_cannot_answer_is_not_asked_on_every_check(self):
        class Recovering(u2.Device):
            serial = "RECOVERING-TEST"
            asked = 0

            @property
            def info(self):
                type(self).asked += 1
                raise OSError("uiautomator is restarting")

            def window_size(self):
                return REDMI

        phone = object.__new__(Recovering)
        for _ in range(5):
            self.assertEqual(core.px_per_dp(phone), 3.0)
        self.assertEqual(Recovering.asked, 1)
        with patch.object(core.time, "time", return_value=core.time.time() + 31):
            core.px_per_dp(phone)
        self.assertEqual(Recovering.asked, 2)


class FakeAdb:
    """`adb` as subprocess.run sees it: one listed phone whose link hangs."""

    def __init__(self, state="device", echo_ok=False, heals=True):
        self.state, self.echo_ok, self.heals = state, echo_ok, heals
        self.calls = []

    def __call__(self, cmd, **kwargs):
        args = cmd[1:]
        if args[:1] == ["-s"]:
            args = args[2:]
        self.calls.append(args)
        if args == ["devices"]:
            return subprocess.CompletedProcess(
                cmd, 0, f"List of devices attached\nBOX1\t{self.state}\n", "")
        if args == ["reconnect"]:
            if self.heals:
                self.echo_ok = True
            return subprocess.CompletedProcess(cmd, 0, "reconnecting BOX1 [device]\n", "")
        if args == ["shell", "echo", "ok"] and self.echo_ok:
            return subprocess.CompletedProcess(cmd, 0, "ok\n", "")
        if args == ["shell", "dumpsys", "window"] and self.echo_ok:
            return subprocess.CompletedProcess(cmd, 0, "  mCurrentFocus=null\n", "")
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))


class HungLinkTests(unittest.TestCase):
    def setUp(self):
        core._link_healed_at.clear()
        self.addCleanup(core._link_healed_at.clear)
        for item in (patch.object(core.time, "sleep"),
                     patch.object(core, "report_error"),
                     patch.object(core, "dbg"),
                     patch.object(core, "stop_requested", return_value=False)):
            item.start()
            self.addCleanup(item.stop)

    def adb(self, fake):
        return patch.object(core.subprocess, "run", side_effect=fake)

    def test_a_listed_phone_that_stops_answering_is_reconnected_once(self):
        fake = FakeAdb()
        with self.adb(fake):
            rc, _, err = core.run(["shell", "input", "tap", "997", "1427"], serial="BOX1")
        self.assertEqual((rc, err), (1, "timeout"))
        self.assertEqual(fake.calls.count(["reconnect"]), 1)
        self.assertTrue(fake.echo_ok)
        # The timed-out command itself is never sent again: it may have landed.
        self.assertEqual(fake.calls.count(["shell", "input", "tap", "997", "1427"]), 1)

    def test_a_healthy_phone_with_one_slow_command_is_left_alone(self):
        fake = FakeAdb(echo_ok=True)
        with self.adb(fake):
            core.run(["shell", "uiautomator", "dump"], serial="BOX1")
        self.assertNotIn(["reconnect"], fake.calls)

    def test_missing_offline_and_unauthorized_phones_are_not_this_fault(self):
        for state in ("offline", "unauthorized", "missing"):
            with self.subTest(state=state):
                fake = FakeAdb(state=state)
                with self.adb(fake):
                    self.assertFalse(core.heal_hung_link("BOX1"))
                self.assertNotIn(["reconnect"], fake.calls)
                self.assertNotIn(["shell", "echo", "ok"], fake.calls)

    def test_a_link_that_will_not_heal_is_reconnected_at_most_every_30s(self):
        fake = FakeAdb(heals=False)
        with self.adb(fake), \
                patch.object(core.time, "time", side_effect=[0.0, 0.0, 99.0, 99.0, 99.0]):
            self.assertFalse(core.heal_hung_link("BOX1"))
        with self.adb(fake), patch.object(core.time, "time", return_value=10.0):
            self.assertFalse(core.heal_hung_link("BOX1"))
        self.assertEqual(fake.calls.count(["reconnect"]), 1)

    def test_a_failing_uiautomator_read_checks_the_link_too(self):
        class Live:
            serial = "BOX1"

            def __init__(self):
                self.reads = 0

            def dump_hierarchy(self, *a, **k):
                self.reads += 1
                if self.reads == 1:
                    raise OSError("uiautomator: connection timed out")
                return '<hierarchy><node package="com.facebook.lite"/></hierarchy>'

        fake = FakeAdb()
        with self.adb(fake):
            xml = core.safe_dump(Live())
        self.assertIn("com.facebook.lite", xml)
        self.assertEqual(fake.calls.count(["reconnect"]), 1)


USB_POPUP = ("  mCurrentFocus=Window{2c1 u0 com.android.settings/"
             "com.android.settings.Settings$UsbDetailsActivity}\n")


class SystemPopupTests(unittest.TestCase):
    def setUp(self):
        for item in (patch.object(core.time, "sleep"), patch.object(core, "dbg")):
            item.start()
            self.addCleanup(item.stop)

    def dumpsys(self, focus):
        calls = []

        def run(args, serial=None, timeout=60):
            calls.append(args)
            if args == ["shell", "dumpsys", "window"]:
                return 0, focus, ""
            return 0, "", ""
        return calls, run

    def test_miuis_use_usb_for_popup_is_closed_with_back(self):
        calls, run = self.dumpsys(USB_POPUP)
        with patch.object(core, "run", side_effect=run):
            self.assertTrue(core.dismiss_system_popup("BOX1"))
        self.assertIn(["shell", "input", "keyevent", "KEYCODE_BACK"], calls)

    def test_facebook_and_usb_debugging_prompts_are_never_touched(self):
        for focus in (
                "  mCurrentFocus=Window{1 u0 com.facebook.lite/com.facebook.lite.MainActivity}\n",
                "  mCurrentFocus=Window{2 u0 com.android.systemui/"
                "com.android.systemui.usb.UsbDebuggingActivity}\n",
                "  mCurrentFocus=null\n"):
            with self.subTest(focus=focus.strip()):
                calls, run = self.dumpsys(focus)
                with patch.object(core, "run", side_effect=run):
                    self.assertFalse(core.dismiss_system_popup("BOX1"))
                self.assertNotIn(["shell", "input", "keyevent", "KEYCODE_BACK"], calls)

    def test_a_read_blocked_by_the_popup_closes_it_instead_of_waiting(self):
        class Live:
            serial = "BOX1"

            def __init__(self):
                self.reads = 0

            def dump_hierarchy(self, *a, **k):
                self.reads += 1
                if self.reads == 1:          # the pop-up's window, not Facebook's
                    return '<hierarchy><node package="com.android.settings"/></hierarchy>'
                return '<hierarchy><node package="com.facebook.lite"/></hierarchy>'

        calls, run = self.dumpsys(USB_POPUP)
        phone = Live()
        with patch.object(core, "run", side_effect=run), \
                patch.object(core, "fb_foreground", return_value=True), \
                patch.object(core, "poll_until") as wait:
            xml = core.safe_dump(phone)
        self.assertIn("com.facebook.lite", xml)
        self.assertEqual(phone.reads, 2)
        wait.assert_not_called()             # no 8 s wait for focus


if __name__ == "__main__":
    unittest.main()
