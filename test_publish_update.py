"""Manifest publishing uses only verified build bytes and version-pinned assets."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import tools_publish_update as publisher
import version


VERSION = "7.8.9"
COMMIT = "a" * 40


class PublishUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="publish update ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.build = self.root / "FBVenom.exe"
        image = bytearray(256)
        image[:2] = b"MZ"
        struct.pack_into("<I", image, 60, 128)
        image[128:132] = b"PE\0\0"
        struct.pack_into("<H", image, 132, 0x8664)
        struct.pack_into("<H", image, 152, 0x20B)
        struct.pack_into("<H", image, 220, 2)
        self.build.write_bytes(image)
        self.metadata = self.build.with_suffix(".build.json")
        self.out = self.root / "manifest.json"
        self.report = {
            "version": VERSION, "source_commit": COMMIT, "filename": self.build.name,
            "size_bytes": self.build.stat().st_size, "sha256": publisher.sha256_of(self.build),
            "format": "PE32+", "architecture": "x86_64", "subsystem": "Windows GUI",
            "windows_smoke_test": "passed",
            "checks": {name: True for name in publisher._REQUIRED_CHECKS},
            "gui_features": {name: True for name in publisher._REQUIRED_FEATURES},
        }
        self.write_report()
        for item in (patch.object(version, "APP_VERSION", VERSION),
                     patch.object(publisher, "stale_sources", return_value=[])):
            mock = item.start()
            self.addCleanup(item.stop)
            if hasattr(mock, "return_value"):
                self.stale = mock
        self.stdout = ""
        self.stderr = ""

    def write_report(self, report=None):
        self.metadata.write_text(json.dumps(self.report if report is None else report), encoding="utf-8")

    def run_main(self, *arguments, requested_version=VERSION):
        output, error = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(output), redirect_stderr(error):
                return publisher.main([str(self.build), "--version", requested_version, *arguments])
        finally:
            self.stdout, self.stderr = output.getvalue(), error.getvalue()

    def assert_rejected(self, *arguments, requested_version=VERSION):
        with self.assertRaises(SystemExit) as error:
            self.run_main(*arguments, requested_version=requested_version)
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.out.exists())

    def test_default_manifest_pins_the_exact_release_and_preserves_build_and_version(self):
        before = self.build.read_bytes()
        with patch.object(publisher, "bump_version_file", side_effect=AssertionError("Must bump before building")) as bump:
            self.assertEqual(self.run_main("--notes", "LAN and updater fixes"), 0)
        manifest = json.loads(self.out.read_text())
        self.assertEqual(manifest["url"], "https://github.com/kaneki654/FBVenom/releases/download/v7.8.9/FBVenom.exe")
        self.assertNotIn("/latest/", manifest["url"])
        self.assertNotIn("/main/", manifest["url"])
        self.assertEqual(manifest["version"], VERSION)
        self.assertEqual(manifest["sha256"], self.report["sha256"])
        self.assertEqual(manifest["size"], self.report["size_bytes"])
        self.assertEqual(manifest["notes"], "LAN and updater fixes")
        self.assertEqual(self.build.read_bytes(), before)
        self.assertEqual(version.APP_VERSION, VERSION)
        bump.assert_not_called()
        self.assertIn("gh release create v7.8.9", self.stdout)
        self.assertIn("--target " + COMMIT, self.stdout)
        self.assertIn("does not publish that release", self.stdout)
        self.assertNotIn("REBUILD", self.stdout)

    def test_explicit_https_and_local_build_locations_remain_supported(self):
        for url in ("https://downloads.example/specific.exe", "/mnt/share/FBVenom.exe",
                    "file:///tmp/FBVenom.exe", r"C:\Builds\FBVenom.exe", r"\\server\updates\FBVenom.exe"):
            with self.subTest(url=url):
                self.assertEqual(self.run_main("--url", url), 0)
                self.assertEqual(json.loads(self.out.read_text())["url"], url)

    def test_unsafe_or_empty_locations_are_rejected(self):
        for url in ("http://example.com/FBVenom.exe", "ftp://example.com/file", ""):
            with self.subTest(url=url):
                self.assert_rejected("--url", url)

    def test_missing_invalid_or_oversized_metadata_is_rejected_even_with_force(self):
        self.metadata.unlink()
        self.assert_rejected("--force")
        for raw in (b"{", b"[]", b"null", b"\xff", b" " * (64 * 1024 + 1)):
            with self.subTest(raw=raw[:5]):
                self.metadata.write_bytes(raw)
                self.assert_rejected("--force")

    def test_requested_version_must_match_both_source_and_frozen_report(self):
        self.assert_rejected(requested_version="7.8.10")
        changed = {**self.report, "version": "7.8.8"}
        self.write_report(changed)
        self.assert_rejected("--force")
        self.assertIn("metadata version", self.stderr)

    def test_bad_version_cannot_create_a_tag_or_change_version_source(self):
        for value in ("v7.8.9", "7.8.9; touch bad", "../7.8.9", "", "7.8"):
            with self.subTest(value=value):
                self.assert_rejected(requested_version=value)
        self.assertEqual(version.APP_VERSION, VERSION)

    def test_changed_executable_cannot_reuse_old_hash_metadata(self):
        image = bytearray(self.build.read_bytes())
        image[-1] = 1
        self.build.write_bytes(image)
        self.assert_rejected("--force")
        self.assertIn("SHA-256", self.stderr)

    def test_metadata_size_filename_and_source_commit_must_match(self):
        for field, value in (("size_bytes", 1), ("size_bytes", "256"),
                             ("filename", "another.exe"), ("source_commit", "unverified")):
            with self.subTest(field=field, value=value):
                self.write_report({**self.report, field: value})
                self.assert_rejected("--force")

    def test_real_windows_pe_and_report_format_are_required(self):
        for field, value in (("format", "ELF"), ("architecture", "x86"), ("subsystem", "Console")):
            with self.subTest(field=field):
                self.write_report({**self.report, field: value})
                self.assert_rejected("--force")
        self.build.write_bytes(b"\x7fELF" + b"\0" * 252)
        changed = {**self.report, "sha256": publisher.sha256_of(self.build)}
        self.write_report(changed)
        self.assert_rejected("--force")
        self.assertIn("Windows executable", self.stderr)

    def test_failed_frozen_checks_or_missing_backup_feature_are_rejected(self):
        bad_reports = []
        for smoke in (None, "failed"):
            bad_reports.append({**self.report, "windows_smoke_test": smoke})
        for name in publisher._REQUIRED_CHECKS:
            changed = deepcopy(self.report)
            changed["checks"][name] = False
            bad_reports.append(changed)
        for name in publisher._REQUIRED_FEATURES:
            changed = deepcopy(self.report)
            changed["gui_features"].pop(name)
            bad_reports.append(changed)
        for index, report in enumerate(bad_reports):
            with self.subTest(index=index):
                self.write_report(report)
                self.assert_rejected("--force")

    def test_stale_source_guard_has_an_explicit_mtime_only_override(self):
        self.stale.return_value = ["updater.py"]
        self.assert_rejected()
        self.assertIn("OLDER than the source", self.stderr)
        self.assertEqual(self.run_main("--force"), 0)
        self.out.unlink()
        self.write_report({**self.report, "sha256": "0" * 64})
        self.assert_rejected("--force")

    def test_atomic_write_failure_preserves_previous_manifest_and_removes_temporary_file(self):
        self.out.write_text('{"version":"previous"}\n')
        before = self.out.read_bytes()
        with patch.object(publisher.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(SystemExit):
                self.run_main()
        self.assertEqual(self.out.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".manifest-*.tmp")), [])
        self.assertIn("atomically", self.stderr)

    def test_output_must_not_overwrite_build_metadata_checksum_or_version_source(self):
        version_path = self.root / "version.py"
        version_path.write_text('APP_VERSION = "7.8.9"\n')
        before = self.build.read_bytes()
        for path in (self.build, self.metadata, self.build.with_suffix(".sha256"), version_path):
            with self.subTest(path=path):
                self.assert_rejected("--out", str(path))
        self.assertEqual(self.build.read_bytes(), before)
        self.assertEqual(version_path.read_text(), 'APP_VERSION = "7.8.9"\n')

    def test_custom_output_directory_is_supported_without_temporary_file_residue(self):
        directory = self.root / "release output"
        directory.mkdir()
        out = directory / "manifest.json"
        self.assertEqual(self.run_main("--out", str(out)), 0)
        self.assertTrue(out.is_file())
        self.assertEqual(list(directory.glob(".manifest-*.tmp")), [])


class StaleSourceTests(unittest.TestCase):
    def test_only_newer_non_test_python_sources_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = root / "FBVenom.exe"
            build.write_bytes(b"build")
            os.utime(build, (100, 100))
            for name, modified in (("core.py", 101), ("updater.py", 102),
                                   ("old.py", 99), ("test_runtime.py", 103), ("README.md", 104)):
                path = root / name
                path.write_text("fixture")
                os.utime(path, (modified, modified))
            self.assertEqual(publisher.stale_sources(build, root), ["updater.py", "core.py"])


if __name__ == "__main__":
    unittest.main()
