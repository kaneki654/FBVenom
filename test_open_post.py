"""Opening a post on the phone: links, slow loading, no internet, USB leftovers.

Nothing here touches a phone, ADB or the network. Screens are fakes, time is a
fake clock, and the one test that runs a real shell (POSIX only) proves what a
phone's /system/bin/sh receives from `adb shell`.
"""
from contextlib import ExitStack
import os
import shutil
import socket
import subprocess
import unittest
from unittest.mock import patch

import aiconfig

with patch.object(aiconfig, "load", return_value={}):
    import core

AMP_LINKS = (
    "https://www.facebook.com/permalink.php?story_fbid=123&id=456",
    "https://www.facebook.com/story.php?story_fbid=pfbid0abc&id=100064",
    "https://www.facebook.com/photo?fbid=55&set=a.1",
)
NO_INTERNET = ('<hierarchy><node package="com.android.systemui" '
               'content-desc="Wifi signal full.,No internet" /></hierarchy>')
ONLINE = ('<hierarchy><node package="com.android.systemui" '
          'content-desc="Wifi signal full." /></hierarchy>')
SERVICE = "ServiceRecord{1a2b u0 com.genymobile.gnirehtet/.GnirehtetService}\n"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        return True


class ShellQuotingTests(unittest.TestCase):
    def test_plain_links_are_left_alone_and_others_are_quoted(self):
        for value in ("https://www.facebook.com/reel/123", "fb://post/123",
                      "https://www.facebook.com/watch"):
            self.assertEqual(core._device_shell_quote(value), value)
        for value in AMP_LINKS + ("it's", "a b"):
            quoted = core._device_shell_quote(value)
            self.assertTrue(quoted.startswith("'") and quoted.endswith("'"), quoted)

    @unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "needs a POSIX shell")
    def test_the_phone_shell_receives_the_whole_link(self):
        for url in AMP_LINKS + ("https://www.facebook.com/x/posts/1?a='b'&c=d",):
            for args in core._post_view_attempts(url):
                with self.subTest(url=url, opener=args[3:5]):
                    # What adb does: join everything after "shell" with spaces
                    # and hand the line to the phone's sh. printf stands in for
                    # `am start` so the arguments it would receive are visible.
                    line = " ".join(args[1:]).replace("am start", "printf '%s\\n'", 1)
                    out = subprocess.run(["sh", "-c", line], capture_output=True,
                                         text=True, timeout=10).stdout.splitlines()
                    self.assertEqual(out[out.index("-d") + 1], url)
                    self.assertEqual(out[-1], args[-1])        # nothing after -d was lost

    @unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "needs a POSIX shell")
    def test_the_unquoted_line_used_to_cut_the_link_at_the_ampersand(self):
        url = AMP_LINKS[0]
        line = f"printf '%s\\n' -a android.intent.action.VIEW -d {url} -f 0x14000000"
        result = subprocess.run(["sh", "-c", line], capture_output=True, text=True, timeout=10)
        self.assertNotIn(url, result.stdout)
        self.assertNotEqual(result.returncode, 0)

    def test_package_scoped_opener_comes_before_main_activity(self):
        attempts = core._post_view_attempts("https://www.facebook.com/reel/1")
        targets = [a[a.index("-n") + 1] if "-n" in a else "-p" for a in attempts]
        self.assertEqual(targets, [f"{core.FB_PACKAGE}/{alias}" for alias in core.FB_LINK_ALIASES]
                         + ["-p", core.FB_ACTIVITY])


class PatientPostViewTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.start = self.clock.now
        self.text = lambda: ""
        self.composer_at = None
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(core.time, "time", side_effect=self.clock.time))
        stack.enter_context(patch.object(core, "abort_sleep", side_effect=self.clock.sleep))
        stack.enter_context(patch.object(core, "stop_requested", return_value=False))
        stack.enter_context(patch.object(core, "find_reel_like_point", return_value=None))
        stack.enter_context(patch.object(core, "visible_text", side_effect=lambda d: self.text()))
        stack.enter_context(patch.object(core, "has_comment_composer", side_effect=lambda d: (
            self.composer_at is not None and self.clock.now - self.start >= self.composer_at)))

    def test_a_post_that_needs_nine_seconds_is_waited_for(self):
        self.composer_at = 9.0
        self.assertTrue(core.on_post_view(object(), timeout=core.OPEN_VERIFY_SECONDS,
                                          feed_grace=core.OPEN_FEED_GRACE))

    def test_the_old_six_second_check_gave_up_on_that_post(self):
        self.composer_at = 9.0
        self.assertFalse(core.on_post_view(object(), timeout=6.0))

    def test_a_link_that_stays_on_the_feed_fails_after_the_grace_not_the_timeout(self):
        self.text = lambda: "What's on your mind?"
        self.assertFalse(core.on_post_view(object(), timeout=core.OPEN_VERIFY_SECONDS,
                                           feed_grace=core.OPEN_FEED_GRACE))
        self.assertLess(self.clock.now - self.start, core.OPEN_FEED_GRACE + 1.0)

    def test_a_feed_flash_before_the_post_is_not_a_failure(self):
        self.text = lambda: "What's on your mind?" if self.clock.now - self.start < 2 else ""
        self.composer_at = 3.0
        self.assertTrue(core.on_post_view(object(), timeout=core.OPEN_VERIFY_SECONDS,
                                          feed_grace=core.OPEN_FEED_GRACE))


class FakeScreen:
    serial = "phone-1"

    def __init__(self, screens):
        self.screens = list(screens)

    def dump_hierarchy(self, *args, **kwargs):
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]


class OpenTargetPostTests(unittest.TestCase):
    URL = "https://www.facebook.com/reel/777"

    def setUp(self):
        self.clock = Clock()
        self.errors, self.dumps = [], []
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.open_post = stack.enter_context(patch.object(core, "open_post", return_value=False))
        for name, value in (("stop_requested", False), ("dismiss_chooser", False),
                            ("dump_is_blind", False), ("on_post_view", True),
                            ("run", (0, "", "")), ("stop_stale_usb_vpn", False)):
            stack.enter_context(patch.object(core, name, return_value=value))
        stack.enter_context(patch.object(core, "abort_sleep", side_effect=self.clock.sleep))
        stack.enter_context(patch.object(core.time, "monotonic", side_effect=self.clock.time))
        stack.enter_context(patch.object(core, "report_error", side_effect=lambda code, *a, **k:
                                         self.errors.append((code, k.get("detail", "")))))
        stack.enter_context(patch.object(core, "dump_debug", side_effect=lambda s, d, tag:
                                         self.dumps.append(tag)))
        core._open_failure_dumps.clear()

    def test_no_internet_is_waited_out_instead_of_burning_the_attempts(self):
        self.open_post.side_effect = [False, True]
        phone = FakeScreen([NO_INTERNET, NO_INTERNET, NO_INTERNET, ONLINE])
        self.assertTrue(core.open_target_post("phone-1", phone, self.URL))
        self.assertEqual([code for code, _ in self.errors], ["PHONE_NO_INTERNET"])
        self.assertEqual(self.open_post.call_count, 2)
        self.assertEqual(self.dumps, [])

    def test_a_phone_that_stays_offline_is_reported_as_such_and_its_screen_saved(self):
        phone = FakeScreen([NO_INTERNET])
        self.assertFalse(core.open_target_post("phone-1", phone, self.URL))
        codes = [code for code, _ in self.errors]
        self.assertEqual(codes, ["PHONE_NO_INTERNET", "TARGET_NOT_VERIFIED"])
        self.assertIn("no internet", self.errors[-1][1])
        self.assertEqual(self.open_post.call_count, 1)        # no pointless reopen loop
        self.assertEqual(self.dumps, ["target_not_verified"])

    def test_an_online_phone_still_gets_every_attempt_and_one_saved_screen_per_link(self):
        phone = FakeScreen([ONLINE])
        self.assertFalse(core.open_target_post("phone-1", phone, self.URL))
        self.assertFalse(core.open_target_post("phone-1", phone, self.URL))
        self.assertEqual(self.open_post.call_count, 6)
        self.assertEqual([code for code, _ in self.errors], ["TARGET_NOT_VERIFIED"] * 2)
        self.assertEqual(self.dumps, ["target_not_verified"])
        core.open_target_post("phone-1", phone, "https://www.facebook.com/reel/888")
        self.assertEqual(self.dumps, ["target_not_verified"] * 2)

    def test_a_screen_that_already_looks_like_a_target_gets_a_cold_start(self):
        # The previous link's reel or post is still showing: a warm open would
        # verify against IT (measured on a real phone), so start cold.
        self.open_post.side_effect = [True]
        core.open_target_post("phone-1", FakeScreen([ONLINE]), self.URL)
        self.assertFalse(self.open_post.call_args.kwargs["allow_warm"])

    def test_the_feed_or_a_menu_still_gets_the_quick_warm_open(self):
        self.open_post.side_effect = [True]
        with patch.object(core, "on_post_view", side_effect=[False, True]):
            self.assertTrue(core.open_target_post("phone-1", FakeScreen([ONLINE]), self.URL))
        self.assertTrue(self.open_post.call_args.kwargs["allow_warm"])

    def test_the_verifier_handed_to_open_post_is_patient(self):
        core.open_target_post("phone-1", FakeScreen([ONLINE]), self.URL, attempts=1)
        verify = self.open_post.call_args.kwargs["verify"]
        with patch.object(core, "on_post_view", return_value=True) as view:
            self.assertTrue(verify())
        view.assert_called_once()
        self.assertEqual(view.call_args.kwargs, {"timeout": core.OPEN_VERIFY_SECONDS,
                                                 "feed_grace": core.OPEN_FEED_GRACE})


class QuickOpenTests(unittest.TestCase):
    """What ENVENOM does in its first seconds on the phone."""

    def setUp(self):
        self.calls = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(core, "stop_requested", return_value=False))
        stack.enter_context(patch.object(core, "wait_online", return_value=True))
        stack.enter_context(patch.object(core, "fb_foreground", return_value=True))
        stack.enter_context(patch.object(core, "poll_until", return_value=True))
        stack.enter_context(patch.object(core, "run", side_effect=lambda args, serial=None, timeout=60:
                                         self.calls.append(("adb", " ".join(args))) or (0, "Starting", "")))
        stack.enter_context(patch.object(core, "abort_sleep", side_effect=lambda s:
                                         self.calls.append(("sleep", s)) or True))

    def test_a_warm_open_lets_the_new_screen_replace_the_old_before_checking(self):
        def verify():
            self.calls.append(("verify", None))
            return True
        self.assertTrue(core.open_post("phone-1", "https://www.facebook.com/page/posts/123",
                                       allow_warm=True, verify=verify))
        kinds = [kind for kind, _ in self.calls]
        start = kinds.index("adb")
        self.assertEqual(self.calls[start + 1], ("sleep", core.WARM_SETTLE))
        self.assertEqual(kinds[start + 2], "verify")
        self.assertNotIn("force-stop", " ".join(c for k, c in self.calls if k == "adb"))

    def test_post_links_keep_the_quick_open_even_with_a_post_showing(self):
        with patch.object(core, "on_post_view", return_value=True), \
                patch.object(core, "open_post", return_value=True) as opened, \
                patch.object(core, "dismiss_chooser", return_value=False):
            core.open_target_post("phone-1", FakeScreen([ONLINE]),
                                  "https://www.facebook.com/page/posts/123")
        self.assertTrue(opened.call_args.kwargs["allow_warm"])

    def test_a_verified_first_link_skips_the_six_second_login_watch(self):
        waits = []

        def account_block(d, timeout=6):
            waits.append(timeout)
            return None
        stop = []
        target = core.make_target("https://www.facebook.com/reel/42", comment="x",
                                  do_react=False, do_comment=True)
        target["phone_ready"] = True
        with patch.object(core, "connect_resilient", return_value=object()), \
                patch.object(core, "ensure_device", side_effect=lambda s, d: d), \
                patch.object(core, "wake_and_unlock"), \
                patch.object(core, "stop_stale_usb_vpn", return_value=False), \
                patch.object(core, "prefetch_comment"), \
                patch.object(core, "open_target_post", return_value=True), \
                patch.object(core, "wait_for_account_block", side_effect=account_block), \
                patch.object(core, "close_fb"), \
                patch.object(core, "stop_requested", side_effect=lambda: bool(stop)), \
                patch.object(core, "_process_target",
                             side_effect=lambda *a, **k: stop.append(1) or "abort"):
            core.react_all_saved_accounts("phone-1", [target], "like", "x", False, True)
        self.assertEqual(waits[0], 0)


class StaleUSBVPNTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.errors = [], []
        self.service, self.reverse = SERVICE, ""
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(core, "run", side_effect=self.adb))
        stack.enter_context(patch.object(core, "report_error", side_effect=lambda code, *a, **k:
                                         self.errors.append(code)))
        stack.enter_context(patch.object(core, "USB_INTERNET_ACTIVE", None))

    def adb(self, args, serial=None, timeout=60):
        self.calls.append(list(args))
        if args == ["shell", "dumpsys", "activity", "services", core.GNIREHTET_PACKAGE]:
            return 0, self.service, ""
        if args == ["reverse", "--list"]:
            return 0, self.reverse, ""
        return 0, "", ""

    def stopped(self):
        return any("com.genymobile.gnirehtet.STOP" in call for call in self.calls)

    def test_vpn_with_no_tunnel_is_stopped(self):
        self.assertTrue(core.stop_stale_usb_vpn("phone-1"))
        self.assertTrue(self.stopped())
        self.assertEqual(self.errors, ["USB_VPN_STALE_STOPPED"])

    def test_vpn_whose_tunnel_points_at_a_dead_port_is_stopped_and_unmapped(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.reverse = f"UsbFfs localabstract:gnirehtet tcp:{port}\n"
        self.assertTrue(core.stop_stale_usb_vpn("phone-1"))
        self.assertTrue(self.stopped())
        self.assertIn(["reverse", "--remove", "localabstract:gnirehtet"], self.calls)

    def test_vpn_served_by_a_live_relay_is_left_alone(self):
        with socket.socket() as relay:
            relay.bind(("127.0.0.1", 0))
            relay.listen(1)
            self.reverse = f"UsbFfs localabstract:gnirehtet tcp:{relay.getsockname()[1]}\n"
            self.assertFalse(core.stop_stale_usb_vpn("phone-1"))
        self.assertFalse(self.stopped())
        self.assertEqual(self.errors, [])

    def test_no_gnirehtet_running_means_nothing_to_do(self):
        self.service = "(nothing)"
        self.assertFalse(core.stop_stale_usb_vpn("phone-1"))
        self.assertEqual(self.calls, [["shell", "dumpsys", "activity", "services",
                                       core.GNIREHTET_PACKAGE]])

    def test_this_apps_running_usb_internet_is_never_touched(self):
        with patch.object(core, "USB_INTERNET_ACTIVE", lambda: True):
            self.assertFalse(core.stop_stale_usb_vpn("phone-1"))
        self.assertEqual(self.calls, [])


class EnsureOnlineTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(core, "run", side_effect=lambda args, serial=None, timeout=60:
                                         self.calls.append(args[0]) or (0, "", "")))
        stack.enter_context(patch.object(core.time, "sleep"))
        self.devices = stack.enter_context(patch.object(core, "list_devices"))
        stack.enter_context(patch.object(core, "USB_INTERNET_ACTIVE", None))

    def test_an_unauthorized_phone_never_restarts_adb_for_everyone(self):
        self.devices.return_value = {"A": "device", "B": "unauthorized"}
        self.assertEqual(core.ensure_online(), {"A": "device", "B": "unauthorized"})
        self.assertEqual(self.calls, [])

    def test_an_offline_phone_still_gets_the_full_recovery(self):
        self.devices.return_value = {"A": "device", "B": "offline"}
        core.ensure_online()
        self.assertIn("kill-server", self.calls)

    def test_no_adb_restart_while_usb_internet_is_sharing_through_it(self):
        self.devices.return_value = {"A": "device", "B": "offline"}
        with patch.object(core, "USB_INTERNET_ACTIVE", lambda: True):
            core.ensure_online()
        self.assertIn("reconnect", self.calls)
        self.assertNotIn("kill-server", self.calls)


class ShareResolutionRetryTests(unittest.TestCase):
    SHARE = "https://www.facebook.com/share/r/AbCdEf123/"

    def setUp(self):
        core._url_resolve_cache.clear()
        core._url_resolve_failed_at.clear()
        self.addCleanup(core._url_resolve_cache.clear)
        self.addCleanup(core._url_resolve_failed_at.clear)

    def test_a_failed_lookup_is_retried_after_a_few_minutes_not_never(self):
        clock = Clock()
        with patch.object(core.NOPROXY, "open", side_effect=OSError("offline")) as fetch, \
                patch.object(core, "report_error"), \
                patch.object(core.time, "monotonic", side_effect=clock.time):
            core.normalize_phone_url(self.SHARE)
            # Each lookup asks twice: as a phone browser, then as Facebook's
            # link-preview crawler.
            self.assertEqual(fetch.call_count, 2)
            clock.now += 60
            core.normalize_phone_url(self.SHARE)
            self.assertEqual(fetch.call_count, 2)
            clock.now += core.SHARE_RETRY_AFTER
            core.normalize_phone_url(self.SHARE)
            self.assertEqual(fetch.call_count, 4)


class PhoneInternetTests(unittest.TestCase):
    def test_status_bar_no_internet_is_recognised(self):
        self.assertTrue(core.phone_reports_no_internet(FakeScreen([NO_INTERNET])))
        self.assertFalse(core.phone_reports_no_internet(FakeScreen([ONLINE])))

    def test_an_unreadable_screen_is_not_called_offline(self):
        class Broken:
            def dump_hierarchy(self):
                raise RuntimeError("uiautomator gone")
        self.assertFalse(core.phone_reports_no_internet(Broken()))


if __name__ == "__main__":
    unittest.main()
