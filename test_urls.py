import contextlib
import unittest
from unittest import mock

import core


class _FakeResponse:
    """Just enough of an http.client.HTTPResponse for the resolver."""

    def __init__(self, final_url, body=""):
        self._final = final_url
        self._body = body.encode("utf-8")
        self._pos = 0

    def geturl(self):
        return self._final

    def read(self, limit=None):
        # CONSUME, like a real socket: the resolver reads in chunks until the
        # canonical tag shows up or the stream ends, so a fake that returns the
        # same bytes on every call never reaches EOF and spins to the byte cap.
        if limit is None:
            chunk = self._body[self._pos:]
        else:
            chunk = self._body[self._pos:self._pos + limit]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextlib.contextmanager
def _fake_network(final_url, body=""):
    """Answer every resolver fetch with one canned response."""
    class _Opener:
        def open(self, req, timeout=None):
            return _FakeResponse(final_url, body)

    original = core.NOPROXY
    core.NOPROXY = _Opener()
    core._url_resolve_cache.clear()
    try:
        with mock.patch.object(core, "report_error"):
            yield
    finally:
        core.NOPROXY = original
        core._url_resolve_cache.clear()


class FacebookUrlNormalizationTests(unittest.TestCase):
    CASES = (
        ("facebook.com/user/posts/123/?mibextid=abc&utm_source=x",
         "https://www.facebook.com/user/posts/123"),
        ("https://m.facebook.com/story.php?story_fbid=123&id=456&ref=x",
         "https://www.facebook.com/story.php?story_fbid=123&id=456"),
        ("https://mbasic.facebook.com/groups/111/posts/222/?__tn__=R",
         "https://www.facebook.com/groups/111/posts/222"),
        ("https://web.facebook.com/reel/12345/?mibextid=x",
         "https://www.facebook.com/reel/12345"),
        ("https://touch.facebook.com/photo.php?fbid=999&id=888&__cft__=x",
         "https://www.facebook.com/photo.php?fbid=999&id=888"),
        ("Copied: https://www.facebook.com/share/p/ABC123/?mibextid=x thanks",
         "https://www.facebook.com/share/p/ABC123"),
        ("https://l.facebook.com/l.php?u=https%3A%2F%2Fm.facebook.com%2F"
         "reel%2F777%2F%3Fmibextid%3Dx&h=unused",
         "https://www.facebook.com/reel/777"),
        ("https://www.facebook.com/watch/?v=321&ref=sharing",
         "https://www.facebook.com/watch?v=321"),
        ("https://fb.watch/AbCd12/", "https://fb.watch/AbCd12"),
        ("Facebook://post?id=123", "facebook://post?id=123"),
        ("Open this FB://post/456 please", "fb://post/456"),
        ('Look: (https://web.facebook.com/live/123/?ref=sharing).',
         "https://www.facebook.com/live/123"),
        ('https://www.facebook.com:443/photo/?fbid=123&set=a.456&type=3',
         "https://www.facebook.com/photo?fbid=123&set=a.456&type=3"),
        ('https%3A%2F%2Fm.facebook.com%2Freel%2F777%3Ffbclid%3Dtracking',
         "https://www.facebook.com/reel/777"),
        ('https://www.facebook.com/sharer/sharer.php?u=https%3A%2F%2F'
         'm.facebook.com%2Fstory.php%3Fstory_fbid%3D123%26id%3D456',
         "https://www.facebook.com/story.php?story_fbid=123&id=456"),
        ('fb://facewebmodal/f?href=https%3A%2F%2Fm.facebook.com%2Flive%2F123',
         "https://www.facebook.com/live/123"),
        ("https://notfacebook.com/reel/123", ""),
        ("https://facebook.com.evil.example/reel/123", ""),
        ("https://www.facebook.com@evil.com/reel/123", ""),
        ("https://user:pass@facebook.com/reel/123", ""),
        ("https://facebook.com:bad/reel/123", ""),
        ("https://evil.com/https://facebook.com/reel/123", ""),
        ("https://example.com/?next=https://facebook.com/reel/123", ""),
        ("https://l.facebook.com/l.php?u=https%3A%2F%2Fexample.com%2Fpost", ""),
        ("fb://facewebmodal/f?href=https%3A%2F%2Fexample.com%2Fpost", ""),
        ("fb://", ""),
        ("https://example.com/post/123", ""),
    )

    def test_normalization_corpus(self):
        for raw, expected in self.CASES:
            with self.subTest(raw=raw):
                self.assertEqual(core.normalize_url(raw), expected)
                self.assertEqual(core.normalize_url(expected), expected)

    def test_routing_and_comment_ids_are_preserved(self):
        self.assertEqual(core.normalize_url(
            "https://m.facebook.com/story.php?story_fbid=123&id=456"
            "&comment_id=777&reply_comment_id=888&fbclid=x&ref=sharing"),
            "https://www.facebook.com/story.php?story_fbid=123&id=456"
            "&comment_id=777&reply_comment_id=888")

    def test_native_links_have_exact_https_fallbacks(self):
        cases = (
            ("fb://post/123", "https://www.facebook.com/permalink/123"),
            ("facebook://post?id=123", "https://www.facebook.com/permalink/123"),
            ("fb://post/111_222",
             "https://www.facebook.com/story.php?story_fbid=222&id=111"),
            ("fb://post?id=111_222",
             "https://www.facebook.com/story.php?story_fbid=222&id=111"),
            ("fb://reel/123", "https://www.facebook.com/reel/123"),
            ("fb://live/123", "https://www.facebook.com/watch?v=123"),
            ("fb://video?v=123&id=456", "https://www.facebook.com/watch?v=123"),
            ("fb://photo?fbid=123&id=456",
             "https://www.facebook.com/photo.php?fbid=123"),
            ("fb://story?story_fbid=123&id=456",
             "https://www.facebook.com/story.php?story_fbid=123&id=456"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                variants = core._fb_uri_variants(raw)
                self.assertIn(expected, variants)
                self.assertEqual(variants[0], core.normalize_url(raw))
                self.assertEqual(len(variants), len(set(variants)))

    def test_target_reference_does_not_truncate_ids_or_mine_query_text(self):
        for raw in (
            "https://www.facebook.com/reel/123abc",
            "https://www.facebook.com/user/videos/123abc",
            "https://www.facebook.com/watch",
            "https://www.facebook.com/watch/live",
            "https://www.facebook.com/photos/a.123",
            "https://www.facebook.com/login.php?next=%2Freel%2F123",
            "https://www.facebook.com/profile.php?id=123&next=/reel/456",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(core._fb_target_reference(raw))
                self.assertFalse(any(uri.startswith("fb://post/")
                                     for uri in core._fb_uri_variants(raw)))

    def test_photo_slug_post_and_group_query_targets_have_exact_ids(self):
        cases = (
            ("https://www.facebook.com/user/photos/a.111/222", ("photo", "222")),
            ("https://www.facebook.com/user/posts/a-headline/333", ("post", "333")),
            ("https://www.facebook.com/groups/111/permalink/pfbidABC123",
             ("post", "pfbidABC123")),
            ("https://www.facebook.com/groups/111/?multi_permalinks=444",
             ("post", "444")),
            ("https://www.facebook.com/SomePage/live/a-title/555", ("video", "555")),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(core._fb_target_reference(raw), expected)
                self.assertIn("fb://post/" + expected[1], core._fb_uri_variants(raw))

    def test_live_has_a_same_video_watch_fallback(self):
        variants = core._fb_uri_variants("https://m.facebook.com/SomePage/live/123")
        self.assertEqual(variants[0], "https://www.facebook.com/SomePage/live/123")
        self.assertIn("https://www.facebook.com/watch?v=123", variants)

    def test_group_post_gets_direct_and_web_openers(self):
        variants = core._fb_uri_variants(
            "https://www.facebook.com/groups/111/posts/222")
        # HTTPS leads deliberately - fb://post/<id> opens FB Lite on the feed,
        # so the verifier rejects it and the retry costs seconds per account.
        # The deep link stays as a verified fallback.
        self.assertEqual(variants[0],
                         "https://www.facebook.com/groups/111/posts/222")
        self.assertIn("fb://post/222", variants)
        self.assertEqual(len(variants), len(set(variants)))

    def test_reel_prefers_https_then_native_reel(self):
        variants = core._fb_uri_variants("https://www.facebook.com/reel/777")
        self.assertEqual(variants[:2], [
            "https://www.facebook.com/reel/777", "fb://reel/777"])

    def test_pfbid_under_newer_path_gets_post_opener(self):
        variants = core._fb_uri_variants(
            "https://www.facebook.com/user/videos/pfbidABC123")
        self.assertEqual(variants[0],
                         "https://www.facebook.com/user/videos/pfbidABC123")
        self.assertIn("fb://post/pfbidABC123", variants)

    # ── live broadcasts ────────────────────────────────────────────────────
    def test_live_links_all_yield_a_deep_link(self):
        """A live broadcast is a video, but it is shared under /live/. Without
        the id the phone got only the raw HTTPS link, which on FB Lite can land
        on the feed instead of the broadcast."""
        for raw in ("https://www.facebook.com/live/1234567890",
                    "https://www.facebook.com/SomePage/live/1234567890",
                    "https://www.facebook.com/SomePage/live_videos/1234567890",
                    "https://www.facebook.com/watch/live/?v=1234567890",
                    "https://www.facebook.com/SomePage/videos/1234567890",
                    "https://www.facebook.com/video.php?v=1234567890"):
            with self.subTest(raw=raw):
                variants = core._fb_uri_variants(core.normalize_url(raw))
                self.assertIn("fb://post/1234567890", variants)
                self.assertTrue(variants[0].startswith("https://"))

    def test_live_tracking_and_hosts_normalize_to_one_target(self):
        """Every host form and tracking tail of one broadcast is one target."""
        canonical = "https://www.facebook.com/watch/live?v=1234567890"
        for raw in ("https://m.facebook.com/watch/live/?v=1234567890",
                    "https://web.facebook.com/watch/live/?v=1234567890",
                    "https://mbasic.facebook.com/watch/live/?v=1234567890",
                    "https://facebook.com/watch/live/?v=1234567890",
                    "https://www.facebook.com/watch/live/?v=1234567890&ref=watch_permalink",
                    "https://www.facebook.com/watch/live/?v=1234567890&fbclid=AbC"):
            with self.subTest(raw=raw):
                self.assertEqual(core.normalize_url(raw), canonical)

    def test_facebook_gaming_short_host_is_accepted_and_kept(self):
        """fb.gg is how game LIVE links are shared; it was rejected outright.
        It must survive normalization intact so the run can resolve it."""
        self.assertEqual(core.normalize_url("https://fb.gg/v/1234567890"),
                         "https://fb.gg/v/1234567890")
        self.assertEqual(core.normalize_url("https://www.fb.gg/somegamer"),
                         "https://fb.gg/somegamer")
        self.assertEqual(core.normalize_url("watch this fb.gg/somegamer now"),
                         "https://fb.gg/somegamer")


class ShareLinkResolutionTests(unittest.TestCase):
    """/share/* links answer 200 with no redirect.

    Facebook stopped redirecting share tokens, so the canonical permalink is
    only in the page's <head>. Without reading it the phone got a /share/r/
    token with no post id, opened Facebook on the feed, and the run refused to
    comment because it was not on the target post.
    """

    SHARE = "https://www.facebook.com/share/r/1DTPaZadbE"
    REEL_HTML = (
        '<head><link rel="canonical" '
        'href="https://www.facebook.com/reel/1389919633103002/" />'
        '<meta property="og:url" '
        'content="https://www.facebook.com/reel/1389919633103002/" /></head>')

    def test_share_reel_resolves_from_page_head(self):
        with _fake_network(self.SHARE + "/", self.REEL_HTML):
            self.assertEqual(
                core.normalize_phone_url(self.SHARE + "/?mibextid=87OH41"),
                "https://www.facebook.com/reel/1389919633103002")

    def test_resolved_reel_gets_native_reel_opener(self):
        with _fake_network(self.SHARE + "/", self.REEL_HTML):
            variants = core._fb_uri_variants(self.SHARE)
        self.assertEqual(variants[:2], [
            "https://www.facebook.com/reel/1389919633103002",
            "fb://reel/1389919633103002"])

    def test_og_url_is_used_when_canonical_is_absent(self):
        html = ('<meta content="https://www.facebook.com/reel/42/" '
                'property="og:url">')
        with _fake_network(self.SHARE + "/", html):
            self.assertEqual(core.normalize_phone_url(self.SHARE),
                             "https://www.facebook.com/reel/42")

    def test_html_entities_in_canonical_are_decoded(self):
        html = ('<link rel="canonical" href="https://www.facebook.com/'
                'story.php?story_fbid=123&amp;id=456">')
        with _fake_network(self.SHARE + "/", html):
            self.assertEqual(
                core.normalize_phone_url(self.SHARE),
                "https://www.facebook.com/story.php?story_fbid=123&id=456")

    def test_login_wall_never_replaces_the_original_link(self):
        html = ('<link rel="canonical" '
                'href="https://www.facebook.com/login.php?next=%2Freel%2F1">')
        with _fake_network("https://www.facebook.com/login.php", html):
            self.assertEqual(core.normalize_phone_url(self.SHARE), self.SHARE)

    def test_share_token_echoed_back_is_not_accepted_as_a_post(self):
        with _fake_network(self.SHARE + "/", "<html>no canonical here</html>"):
            self.assertEqual(core.normalize_phone_url(self.SHARE), self.SHARE)

    def test_real_redirect_still_wins_without_reading_the_body(self):
        with _fake_network("https://m.facebook.com/reel/777/?mibextid=x"):
            self.assertEqual(core.normalize_phone_url("https://fb.watch/AbCd12"),
                             "https://www.facebook.com/reel/777")

    def test_gaming_short_link_resolves_to_a_live_video(self):
        with _fake_network("https://m.facebook.com/gamer/live/123/?ref=sharing"):
            self.assertEqual(core.normalize_phone_url("https://fb.gg/gamer"),
                             "https://www.facebook.com/gamer/live/123")

    def test_legacy_share_token_resolves(self):
        with _fake_network("https://www.facebook.com/share/ABCD123", self.REEL_HTML):
            self.assertEqual(core.normalize_phone_url("https://facebook.com/share/ABCD123"),
                             "https://www.facebook.com/reel/1389919633103002")

    def test_live_canonical_relative_to_facebook_is_accepted(self):
        body = '<link rel="canonical" href="/watch/live/?v=123&amp;ref=sharing">'
        with _fake_network(self.SHARE, body):
            self.assertEqual(core.normalize_phone_url(self.SHARE),
                             "https://www.facebook.com/watch/live?v=123")

    def test_unusable_canonical_does_not_hide_usable_og_url(self):
        body = ('<link rel="canonical" href="https://www.facebook.com/watch/">'
                '<meta property="og:url" content="https://www.facebook.com/live/123">')
        with _fake_network(self.SHARE, body):
            self.assertEqual(core.normalize_phone_url(self.SHARE),
                             "https://www.facebook.com/live/123")

    def test_feed_external_and_embedded_query_destinations_are_rejected(self):
        for destination in (
            "https://www.facebook.com/watch/",
            "https://www.facebook.com/watch/live/",
            "https://www.facebook.com/profile.php?id=123",
            "https://www.facebook.com/login.php?next=/reel/123",
            "https://www.facebook.com/user/videos/123abc",
            "https://example.com/reel/123",
        ):
            with self.subTest(destination=destination), _fake_network(
                    destination, '<link rel="canonical" href="' + destination + '">'):
                self.assertEqual(core.normalize_phone_url(self.SHARE), self.SHARE)

    def test_failed_resolution_is_cached_and_reports_original_link_fallback(self):
        core._url_resolve_cache.clear()
        try:
            with mock.patch.object(core.NOPROXY, "open", side_effect=OSError("offline")) as fetch:
                with mock.patch.object(core, "report_error") as error:
                    self.assertEqual(core.normalize_phone_url(self.SHARE), self.SHARE)
                    self.assertEqual(core.normalize_phone_url(self.SHARE), self.SHARE)
            self.assertEqual(fetch.call_count, 1)
            error.assert_called_once()
            self.assertEqual(error.call_args.args[0], "SHARE_UNRESOLVED")
            self.assertEqual(error.call_args.kwargs["severity"], "warning")
            self.assertEqual(error.call_args.kwargs["url"], self.SHARE)
            self.assertIn("offline", error.call_args.kwargs["detail"])
        finally:
            core._url_resolve_cache.clear()

    def test_metadata_fetch_obeys_its_byte_limit(self):
        response = _FakeResponse(self.SHARE, "x" * 100)
        self.assertEqual(core._read_until_canonical(response, limit=10, chunk=32), "x" * 10)
        self.assertEqual(response._pos, 10)

    def test_non_share_links_never_touch_the_network(self):
        def _boom(*a, **k):
            raise AssertionError("plain post URLs must not be fetched")

        original = core.NOPROXY
        core.NOPROXY = type("X", (), {"open": staticmethod(_boom)})()
        try:
            self.assertEqual(
                core.normalize_phone_url("https://m.facebook.com/reel/777/"),
                "https://www.facebook.com/reel/777")
        finally:
            core.NOPROXY = original


class TargetOpeningDiagnosticsTests(unittest.TestCase):
    def test_invalid_target_prints_an_actionable_error_without_adb(self):
        with mock.patch.object(core, "stop_requested", return_value=False), \
                mock.patch.object(core, "run") as adb, \
                mock.patch.object(core, "report_error") as error:
            self.assertFalse(core.open_post("phone", "https://example.com/reel/123"))
        adb.assert_not_called()
        self.assertEqual(error.call_args.args[0], "INVALID_URL")

    def test_all_failed_intents_emit_one_final_error_with_the_adb_reason(self):
        target = "https://www.facebook.com/live/123"
        with contextlib.ExitStack() as stack:
            for name, value in (("stop_requested", False), ("wait_online", True),
                                ("fb_foreground", False), ("poll_until", False),
                                ("abort_sleep", True)):
                stack.enter_context(mock.patch.object(core, name, return_value=value))
            stack.enter_context(mock.patch.object(core, "_fb_uri_variants", return_value=[target]))
            stack.enter_context(mock.patch.object(core, "_post_view_attempts", return_value=[["intent"]]))
            stack.enter_context(mock.patch.object(core, "run", return_value=(1, "", "Unable to resolve Intent")))
            error = stack.enter_context(mock.patch.object(core, "report_error"))
            self.assertFalse(core.open_post("phone", target))
        error.assert_called_once()
        self.assertEqual(error.call_args.args[0], "TARGET_OPEN_FAILED")
        self.assertIn("Unable to resolve Intent", error.call_args.kwargs["detail"])

    def test_abort_prevents_any_new_intent(self):
        with mock.patch.object(core, "stop_requested", return_value=True), \
                mock.patch.object(core, "run") as adb:
            self.assertFalse(core.open_post("phone", "https://www.facebook.com/live/123"))
        adb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
