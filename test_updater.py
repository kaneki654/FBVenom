"""Updating and rolling back, exercised end to end on fake builds.

The swap is the risky part - a running program replacing its own executable -
so these tests do not mock it. They build real files in a temp folder, run the
real swap script, and check which build ends up where.

No network: manifests and downloads are served from local paths, which the
updater supports for LAN installs anyway.
"""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import updater


class VersionTests(unittest.TestCase):
    def test_numeric_order_not_text_order(self):
        self.assertTrue(updater.is_newer("1.10.0", "1.9.0"))   # the classic trap
        self.assertTrue(updater.is_newer("2.0.0", "1.99.99"))
        self.assertFalse(updater.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(updater.is_newer("0.9", "1.0"))

    def test_odd_versions_do_not_crash(self):
        self.assertEqual(updater.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(updater.parse_version("1.2.3-beta"), (1, 2, 3))
        self.assertEqual(updater.parse_version(""), (0,))
        self.assertEqual(updater.parse_version(None), (0,))

    def test_shorter_version_compares_against_longer(self):
        self.assertTrue(updater.is_newer("1.1", "1.0.9"))
        self.assertFalse(updater.is_newer("1.0", "1.0.1"))


class _Sandbox(unittest.TestCase):
    """Point the updater at a temp folder holding fake builds."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = self.dir.name
        self.app = os.path.join(self.root, "FBVenom")
        self._write(self.app, "#!/bin/sh\nexit 0\n", executable=True)
        for name, value in (("APP_DIR", self.root), ("APP_PATH", self.app),
                            ("SETTINGS_PATH",
                             os.path.join(self.root, "venom_update.json"))):
            p = patch.object(updater, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _write(self, path, text, executable=False):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        if executable:
            os.chmod(path, 0o755)
        return path

    def _manifest_file(self, **over):
        build = over.pop("build", None) or self._write(
            os.path.join(self.root, "new-build"), "#!/bin/sh\nexit 0\n", True)
        with open(build, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        data = {"version": "9.9.9", "url": build, "sha256": digest,
                "notes": "newer"}
        data.update(over)
        path = os.path.join(self.root, "manifest.json")
        self._write(path, json.dumps(data))
        return path, data


class DefaultChannelTests(_Sandbox):
    def test_a_fresh_pc_already_has_the_permanent_address(self):
        """No settings file yet: the PC must still know where to look, or
        every machine needs the address typed in by hand."""
        self.assertFalse(os.path.exists(updater.SETTINGS_PATH))
        self.assertEqual(updater.load_settings()["manifest_url"],
                         updater.DEFAULT_MANIFEST_URL)

    def test_the_default_is_a_permanent_https_release_url(self):
        # "releases/latest/download" resolves to whatever the newest release
        # is, so the address never changes when a build ships.
        for url in (updater.DEFAULT_MANIFEST_URL, updater.DEFAULT_BUILD_URL):
            with self.subTest(url=url):
                self.assertTrue(url.startswith("https://github.com/"))
                self.assertIn("/releases/latest/download/", url)
                self.assertEqual(updater._check_location(url, "x"), "https")

    def test_a_saved_address_still_wins(self):
        updater.save_settings({"manifest_url": "https://elsewhere/m.json"})
        self.assertEqual(updater.load_settings()["manifest_url"],
                         "https://elsewhere/m.json")


class RedirectTests(unittest.TestCase):
    class _Response:
        def __init__(self, url):
            self.url = url

    def test_a_redirect_down_to_http_is_refused(self):
        """A release URL legitimately hops to a CDN; that hop must not be
        allowed to drop the channel to plain http."""
        with self.assertRaises(updater.UpdateError):
            updater._require_https_final(self._Response("http://cdn/evil.exe"))

    def test_an_https_hop_is_fine(self):
        updater._require_https_final(
            self._Response("https://objects.githubusercontent.com/x"))


class ManifestTests(_Sandbox):
    def test_a_good_manifest_reports_an_update(self):
        path, _ = self._manifest_file()
        manifest, available = updater.check(path)
        self.assertEqual(manifest["version"], "9.9.9")
        self.assertTrue(available)

    def test_an_older_published_build_is_not_offered(self):
        path, _ = self._manifest_file(version="0.0.1")
        _, available = updater.check(path)
        self.assertFalse(available)

    def test_plain_http_is_refused(self):
        """An update channel is remote code execution; http is editable in
        transit, so it must never be accepted."""
        with self.assertRaises(updater.UpdateError) as caught:
            updater.fetch_manifest("http://example.com/manifest.json")
        self.assertIn("http", str(caught.exception).lower())

    def test_http_download_url_is_refused_even_from_a_good_manifest(self):
        path, _ = self._manifest_file(url="http://example.com/FBVenom.exe")
        with self.assertRaises(updater.UpdateError):
            updater.fetch_manifest(path)

    def test_a_manifest_without_a_checksum_is_refused(self):
        for bad in ("", "nothex", "ab" * 10):
            with self.subTest(sha=bad):
                path, _ = self._manifest_file(sha256=bad)
                with self.assertRaises(updater.UpdateError):
                    updater.fetch_manifest(path)

    def test_junk_is_reported_not_raised_raw(self):
        path = self._write(os.path.join(self.root, "m.json"), "not json")
        with self.assertRaises(updater.UpdateError):
            updater.fetch_manifest(path)


class DownloadTests(_Sandbox):
    def test_a_matching_build_is_kept(self):
        path, _ = self._manifest_file()
        manifest = updater.fetch_manifest(path)
        staged = updater.download(manifest)
        self.addCleanup(lambda: os.path.exists(staged) and os.unlink(staged))
        self.assertTrue(os.path.isfile(staged))

    def test_a_tampered_build_is_refused_and_deleted(self):
        """The checksum is the only thing standing between a PC and whatever
        the download address actually served."""
        path, _ = self._manifest_file(sha256="a" * 64)
        manifest = updater.fetch_manifest(path)
        before = set(os.listdir(self.root))
        with self.assertRaises(updater.UpdateError) as caught:
            updater.download(manifest)
        self.assertIn("checksum", str(caught.exception).lower())
        # nothing half-downloaded is left lying around
        self.assertEqual(set(os.listdir(self.root)), before)


class SwapTests(_Sandbox):
    """Run the real swap script and check which build ends up where."""

    def _run_swap(self, script):
        subprocess.run(["/bin/sh", script], check=True, timeout=30,
                       cwd=self.root, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

    def _body(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def test_install_then_downgrade_round_trip(self):
        self._write(self.app, "#!/bin/sh\n# OLD\nexit 0\n", True)
        new = self._write(os.path.join(self.root, "staged"),
                          "#!/bin/sh\n# NEW\nexit 0\n", True)

        script = updater.install(new, restart=False)
        self._run_swap(script)
        self.assertIn("# NEW", self._body(self.app))
        self.assertTrue(updater.can_downgrade())
        self.assertIn("# OLD", self._body(updater.previous_build_path()))

        script = updater.downgrade(restart=False)
        self._run_swap(script)
        self.assertIn("# OLD", self._body(self.app))
        # The build we rolled back FROM is kept, so you can move forward again.
        self.assertIn("# NEW", self._body(updater.previous_build_path()))

    def test_downgrade_refuses_when_there_is_nothing_to_go_back_to(self):
        self.assertFalse(updater.can_downgrade())
        with self.assertRaises(updater.UpdateError):
            updater.downgrade(restart=False)

    def test_install_records_the_version_being_replaced(self):
        new = self._write(os.path.join(self.root, "staged"), "#!/bin/sh\n", True)
        updater.install(new, restart=False)
        self.assertEqual(updater.previous_build_version(), updater.APP_VERSION)


if __name__ == "__main__":
    unittest.main()
