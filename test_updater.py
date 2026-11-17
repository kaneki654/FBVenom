"""Updating and rolling back, exercised end to end on fake builds.

The swap is the risky part - a running program replacing its own executable -
so these tests do not mock it. They build real files in a temp folder, run the
real swap script, and check which build ends up where.

No network: manifests and downloads are served from local paths, which the
updater supports for LAN installs anyway.
"""

import hashlib
import io
import email.message
import datetime
import json
import os
import subprocess
import ssl
import urllib.error
import urllib.request
import urllib.response
import tempfile
import unittest
from unittest.mock import mock_open, patch

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
        self.app = os.path.join(self.root, "FBVenom.cmd" if os.name == "nt" else "FBVenom")
        self._write(self.app, self._application_body(), executable=True)
        for name, value in (("APP_DIR", self.root), ("APP_PATH", self.app),
                            ("SETTINGS_PATH",
                             os.path.join(self.root, "venom_update.json"))):
            p = patch.object(updater, name, value)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _application_body(label="fixture"):
        if os.name == "nt":
            # `start` launches a batch file in a separate command processor;
            # exit that child entirely so the test never leaves a console up.
            return f"@echo off\r\nrem # {label}\r\nexit 0\r\n"
        return f"#!/bin/sh\n# {label}\nexit 0\n"

    def _write(self, path, text, executable=False):
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        if executable:
            os.chmod(path, 0o755)
        return path

    def _manifest_file(self, **over):
        build = over.pop("build", None) or self._write(
            os.path.join(self.root, "new-build"), self._application_body("NEW"), True)
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


class LocationTests(unittest.TestCase):
    def test_absolute_windows_drive_and_unc_paths_are_local(self):
        for path in (r"C:\Program Files\FBVenom\manifest.json",
                     "c:/Users/Test User/FBVenom.exe",
                     r"\\server\updates\FBVenom.exe", "//server/updates/manifest.json"):
            with self.subTest(path=path):
                self.assertEqual(updater._check_location(path, "Update address"), "local")
                self.assertEqual(updater._local_path(path), path)

    def test_windows_paths_are_read_from_disk_without_urlopen(self):
        for path in (r"C:\updates\manifest.json", r"\\server\updates\manifest.json"):
            with self.subTest(path=path), \
                    patch("builtins.open", mock_open(read_data=b"{}")) as disk, \
                    patch.object(updater.urllib.request, "urlopen") as network:
                self.assertEqual(updater._read(path, 1024), b"{}")
                disk.assert_called_once_with(path, "rb")
                network.assert_not_called()

    def test_drive_path_support_preserves_https_only_network_policy(self):
        self.assertEqual(updater._check_location("https://example.com/manifest.json", "Update"), "https")
        for address in ("http://example.com/manifest.json", "ftp://example.com/build.exe",
                        r"C:relative\manifest.json"):
            with self.subTest(address=address), self.assertRaises(updater.UpdateError):
                updater._check_location(address, "Update")


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


class HTTPTransportTests(_Sandbox):
    class Response(io.BytesIO):
        def __init__(self, body=b"", url="https://updates.example/manifest.json", headers=None):
            super().__init__(body)
            self.url = url
            self.headers = headers or {}

    def manifest_bytes(self):
        return json.dumps({"version": "9.9.9", "url": "https://updates.example/FBVenom.exe",
                           "sha256": "a" * 64, "notes": "verified release"}).encode()

    @staticmethod
    def http_error(url, code, headers=None):
        return urllib.error.HTTPError(url, code, "server failure", headers or {}, io.BytesIO(b"private error body"))

    def test_transient_http_500_retries_same_address_and_closes_every_response(self):
        url = "https://updates.example/manifest.json"
        error = self.http_error(url, 500)
        response = self.Response(self.manifest_bytes(), url)
        with patch.object(updater._HTTPS_OPENER, "open", side_effect=[error, response]) as opened, \
                patch.object(updater.time, "sleep") as sleep:
            manifest = updater.fetch_manifest(url)
        self.assertEqual(manifest["version"], "9.9.9")
        self.assertEqual([call.args[0].full_url for call in opened.call_args_list], [url, url])
        self.assertTrue(all(call.kwargs["timeout"] == 20 for call in opened.call_args_list))
        sleep.assert_called_once_with(0.25)
        self.assertTrue(error.fp.closed)
        self.assertTrue(response.closed)

    def test_exhausted_server_errors_report_status_and_safe_address_without_channel_fallback(self):
        url = "https://user:secret@updates.example/channel/manifest.json?token=secret#secret"
        errors = [self.http_error(url, 503) for _ in range(3)]
        with patch.object(updater._HTTPS_OPENER, "open", side_effect=errors) as opened, \
                patch.object(updater.time, "sleep") as sleep:
            with self.assertRaises(updater.UpdateError) as caught:
                updater.fetch_manifest(url)
        message = str(caught.exception)
        self.assertIn("HTTP 503", message)
        self.assertIn("https://updates.example/channel/manifest.json", message)
        self.assertIn("3 attempts", message)
        self.assertNotIn("secret", message)
        self.assertNotIn("user:", message)
        self.assertNotIn("private error body", message)
        self.assertEqual([call.args[0].full_url for call in opened.call_args_list], [url] * 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(error.fp.closed for error in errors))

    def test_default_channel_stays_on_official_release_address_after_failure(self):
        url = updater.DEFAULT_MANIFEST_URL
        with patch.object(updater._HTTPS_OPENER, "open", side_effect=[self.http_error(url, 500) for _ in range(3)]) as opened, \
                patch.object(updater.time, "sleep"):
            with self.assertRaises(updater.UpdateError):
                updater.fetch_manifest(url)
        self.assertEqual([call.args[0].full_url for call in opened.call_args_list], [url] * 3)

    def test_missing_manifest_404_has_publication_diagnostic_without_retry(self):
        url = updater.DEFAULT_MANIFEST_URL
        error = self.http_error(url, 404)
        with patch.object(updater._HTTPS_OPENER, "open", side_effect=error) as opened, \
                patch.object(updater.time, "sleep") as sleep:
            with self.assertRaises(updater.UpdateError) as caught:
                updater.fetch_manifest(url)
        self.assertIn("HTTP 404", str(caught.exception))
        self.assertIn("not published", str(caught.exception))
        opened.assert_called_once()
        sleep.assert_not_called()
        self.assertTrue(error.fp.closed)

    def test_retry_after_is_honored_but_capped_at_two_seconds(self):
        url = "https://updates.example/manifest.json"
        for code, header, expected in ((429, "90", 2.0), (500, "1.5", 1.5), (502, "invalid", 0.25),
                                       (503, "-1", 0.0), (504, "NaN", 0.25)):
            with self.subTest(code=code, header=header):
                response = self.Response(self.manifest_bytes(), url)
                with patch.object(updater._HTTPS_OPENER, "open", side_effect=[
                        self.http_error(url, code, {"Retry-After": header}), response]), \
                        patch.object(updater.time, "sleep") as sleep:
                    updater.fetch_manifest(url)
                sleep.assert_called_once_with(expected)
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5)
        self.assertEqual(updater._retry_delay({"Retry-After": email.utils.format_datetime(future)}, 1), 2.0)

    def test_tls_verification_failure_is_not_retried_or_bypassed(self):
        url = "https://updates.example/manifest.json?token=secret"
        error = urllib.error.URLError(ssl.SSLCertVerificationError("private certificate detail"))
        with patch.object(updater._HTTPS_OPENER, "open", side_effect=error) as opened, \
                patch.object(updater.time, "sleep") as sleep:
            with self.assertRaises(updater.UpdateError) as caught:
                updater.fetch_manifest(url)
        self.assertIn("TLS certificate verification failed", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        opened.assert_called_once()
        sleep.assert_not_called()

    def test_timeout_and_dns_failures_are_actionable_without_private_exception_text(self):
        url = "https://updates.example/manifest.json?token=secret"
        for error in (TimeoutError("private timeout"), urllib.error.URLError("private DNS details")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(updater._HTTPS_OPENER, "open", side_effect=error):
                with self.assertRaises(updater.UpdateError) as caught:
                    updater.fetch_manifest(url)
                self.assertIn("updates.example/manifest.json", str(caught.exception))
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))

    def test_final_url_validation_failure_closes_response(self):
        response = self.Response(self.manifest_bytes(), "http://unsafe.example/manifest.json")
        with patch.object(updater._HTTPS_OPENER, "open", return_value=response):
            with self.assertRaises(updater.UpdateError):
                updater.fetch_manifest("https://updates.example/manifest.json")
        self.assertTrue(response.closed)

    def test_non_json_and_oversized_manifests_are_closed_and_rejected(self):
        url = "https://updates.example/manifest.json?token=secret"
        for raw in (b"<html>private server error</html>", b"x" * (updater.MAX_MANIFEST + 1)):
            response = self.Response(raw, url)
            with self.subTest(length=len(raw)), patch.object(updater._HTTPS_OPENER, "open", return_value=response):
                with self.assertRaises(updater.UpdateError) as caught:
                    updater.fetch_manifest(url)
            self.assertTrue(response.closed)
            self.assertNotIn("private", str(caught.exception))
            self.assertNotIn("secret", str(caught.exception))

    def redirect_opener(self, downgrade):
        visits, responses = [], []
        manifest = self.manifest_bytes()
        class Transport(urllib.request.BaseHandler):
            handler_order = 100
            def serve(self, request):
                visits.append(request.full_url)
                headers = email.message.Message()
                if request.full_url.endswith("/start"):
                    target = "http://cdn.example/intermediate" if downgrade else "https://cdn.example/final"
                    headers["Location"] = target
                    response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                    response.msg = "Found"
                elif request.full_url.endswith("/intermediate"):
                    headers["Location"] = "https://cdn.example/final"
                    response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                    response.msg = "Found"
                else:
                    response = urllib.response.addinfourl(io.BytesIO(manifest), headers, request.full_url, 200)
                    response.msg = "OK"
                responses.append(response)
                return response
            https_open = serve
            http_open = serve
        return urllib.request.build_opener(updater._HTTPSOnlyRedirect(), Transport()), visits, responses

    def test_https_to_http_to_https_redirect_is_refused_before_insecure_hop(self):
        opener, visits, responses = self.redirect_opener(downgrade=True)
        with patch.object(updater, "_HTTPS_OPENER", opener):
            with self.assertRaises(updater.UpdateError):
                updater.fetch_manifest("https://updates.example/start")
        self.assertEqual(visits, ["https://updates.example/start"])
        self.assertTrue(all(response.closed for response in responses))

    def test_all_https_cdn_redirect_remains_supported(self):
        opener, visits, responses = self.redirect_opener(downgrade=False)
        with patch.object(updater, "_HTTPS_OPENER", opener):
            manifest = updater.fetch_manifest("https://updates.example/start")
        self.assertEqual(manifest["version"], "9.9.9")
        self.assertEqual(visits, ["https://updates.example/start", "https://cdn.example/final"])
        self.assertTrue(all(response.closed for response in responses))

    def test_download_retries_before_body_and_keeps_exact_verified_size(self):
        content = b"verified build bytes"
        url = "https://updates.example/FBVenom.exe"
        response = self.Response(content, "https://cdn.example/FBVenom.exe", {"Content-Length": str(len(content))})
        manifest = {"url": url, "sha256": hashlib.sha256(content).hexdigest()}
        progress = []
        with patch.object(updater._HTTPS_OPENER, "open", side_effect=[self.http_error(url, 500), response]) as opened, \
                patch.object(updater.time, "sleep"):
            staged = updater.download(manifest, lambda done, total: progress.append((done, total)))
        self.addCleanup(lambda: os.path.exists(staged) and os.unlink(staged))
        with open(staged, "rb") as source:
            self.assertEqual(source.read(), content)
        self.assertEqual(progress, [(len(content), len(content))])
        self.assertTrue(response.closed)
        self.assertTrue(all(call.kwargs["timeout"] == 60 for call in opened.call_args_list))

    def test_failed_download_http_and_redirect_validation_remove_staged_file(self):
        url = "https://updates.example/FBVenom.exe"
        manifest = {"url": url, "sha256": "a" * 64}
        for failure in (self.http_error(url, 404), self.Response(b"bad", "http://unsafe.example/build.exe")):
            before = set(os.listdir(self.root))
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(updater._HTTPS_OPENER, "open", side_effect=failure if isinstance(failure, Exception) else None,
                                 return_value=failure):
                with self.assertRaises(updater.UpdateError):
                    updater.download(manifest)
            self.assertEqual(set(os.listdir(self.root)), before)
            if not isinstance(failure, Exception):
                self.assertTrue(failure.closed)

    def test_download_cancellation_from_progress_closes_stream_and_removes_staging(self):
        content = b"verified build bytes"
        response = self.Response(content, "https://updates.example/FBVenom.exe")
        manifest = {"url": response.url, "sha256": hashlib.sha256(content).hexdigest()}
        def cancel(*args):
            raise updater.UpdateError("Update cancelled")
        before = set(os.listdir(self.root))
        with patch.object(updater._HTTPS_OPENER, "open", return_value=response):
            with self.assertRaisesRegex(updater.UpdateError, "Update cancelled"):
                updater.download(manifest, cancel)
        self.assertTrue(response.closed)
        self.assertEqual(set(os.listdir(self.root)), before)

    def test_read_only_application_folder_is_reported_without_raw_exception(self):
        with patch.object(updater.tempfile, "mkstemp", side_effect=OSError("private filesystem details")):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.download({"url": "https://updates.example/FBVenom.exe", "sha256": "a" * 64})
        self.assertIn("application folder", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))


class SwapTests(_Sandbox):
    """Run the real swap script and check which build ends up where."""

    def _run_swap(self, script):
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", script] if os.name == "nt" else ["/bin/sh", script]
        result = subprocess.run(command, check=False, timeout=30,
                                cwd=self.root, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0,
                         f"Swap failed ({result.returncode}).\n"
                         f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    def _body(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def test_install_then_downgrade_round_trip(self):
        self._write(self.app, self._application_body("OLD"), True)
        new = self._write(os.path.join(self.root, "staged"),
                          self._application_body("NEW"), True)

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
        new = self._write(os.path.join(self.root, "staged"), self._application_body("NEW"), True)
        updater.install(new, restart=False)
        self.assertEqual(updater.previous_build_version(), updater.APP_VERSION)


if __name__ == "__main__":
    unittest.main()
