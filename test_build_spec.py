"""Exercise packaging guards without running PyInstaller or Windows binaries."""

import builtins
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SPEC_SOURCE = Path(__file__).with_name("FBVenom.spec").read_text(encoding="utf-8")
TOOL_NAMES = ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")


def _pe_stub():
    # Sufficient DOS/PE headers for the format guard, never executable code.
    data = bytearray(132)
    data[:2] = b"MZ"
    data[0x3C:0x40] = (128).to_bytes(4, "little")
    data[128:] = b"PE\0\0"
    return bytes(data)


class WindowsBuildSpecTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tools = self.root / "platform-tools"
        self.tools.mkdir()
        for name in TOOL_NAMES:
            (self.tools / name).write_bytes(_pe_stub())
        self.helpers = self.root / "gnirehtet"
        self.helpers.mkdir()
        (self.helpers / "gnirehtet.exe").write_bytes(_pe_stub())
        for name in ("gnirehtet.apk", "LICENSE", "provenance.json"):
            (self.helpers / name).write_bytes(b"stub")
        self.verified = []

    def _verify_assets(self, directory, platform=None):
        # Stands in for the hash/provenance verifier; the real one is covered
        # in test_usb_internet. Mirrors its contract: raise on a bad folder.
        directory = Path(directory)
        self.verified.append((directory, platform))
        if not (directory / "gnirehtet.exe").is_file():
            raise ValueError("USB Internet helper directory is missing or unsafe")
        return {"relay": str(directory / "gnirehtet.exe"), "apk": str(directory / "gnirehtet.apk"),
                "license": str(directory / "LICENSE"), "provenance": {}}

    def _evaluate(self, platform="win32", override=None, helpers=None):
        collect = Mock(return_value=([], [], []))
        analysis = Mock(return_value=SimpleNamespace(
            pure=[], scripts=[], binaries=[], datas=[]))

        def import_module(name, *args, **kwargs):
            if name == "sys":
                return SimpleNamespace(platform=platform)
            if name == "PyInstaller.utils.hooks":
                return SimpleNamespace(collect_all=collect)
            if name == "tools_prepare_usb_internet":
                return SimpleNamespace(verify_assets=self._verify_assets)
            return builtins.__import__(name, *args, **kwargs)

        namespace = {
            "SPEC": str(self.root / "FBVenom.spec"),
            "Analysis": analysis, "PYZ": Mock(), "EXE": Mock(),
            "__builtins__": dict(vars(builtins), __import__=import_module),
        }
        with patch.dict(os.environ):
            os.environ.pop("FBVENOM_PLATFORM_TOOLS", None)
            os.environ.pop("FBVENOM_GNIREHTET_DIR", None)
            if override is not None:
                os.environ["FBVENOM_PLATFORM_TOOLS"] = str(override)
            if helpers is not None:
                os.environ["FBVENOM_GNIREHTET_DIR"] = str(helpers)
            exec(compile(SPEC_SOURCE, "FBVenom.spec", "exec"), namespace)
        return analysis.call_args.kwargs, namespace

    def test_rejects_non_windows_before_starting_analysis(self):
        with self.assertRaisesRegex(RuntimeError, "Windows Python"):
            self._evaluate(platform="linux")

    def test_bundles_three_distinct_windows_tools_from_default_directory(self):
        arguments, namespace = self._evaluate()
        self.assertEqual(arguments["binaries"], [
            (str((self.tools / name).resolve()), "platform-tools")
            for name in TOOL_NAMES] + [
            (str((self.helpers / "gnirehtet.exe").resolve()), "gnirehtet")])
        self.assertEqual(self.verified, [(self.helpers.resolve(), "windows")])
        self.assertFalse(namespace["EXE"].call_args.kwargs["upx"])
        self.assertEqual(namespace["collect_all"].call_count, 3)

    def test_platform_tools_directory_can_be_overridden(self):
        custom = self.root / "downloaded tools"
        self.tools.rename(custom)
        arguments, _ = self._evaluate(override=custom)
        self.assertEqual({Path(source).parent.resolve()
                          for source, target in arguments["binaries"]
                          if target == "platform-tools"},
                         {custom.resolve()})

    def test_rejects_a_missing_dll(self):
        (self.tools / "AdbWinUsbApi.dll").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "AdbWinUsbApi.dll"):
            self._evaluate()

    def test_rejects_linux_adb_even_if_it_was_renamed_exe(self):
        (self.tools / "adb.exe").write_bytes(b"\x7fELF" + b"\0" * 128)
        with self.assertRaisesRegex(ValueError, "Windows PE binary"):
            self._evaluate()

    def test_rejects_truncated_or_invalid_pe_headers(self):
        bad_headers = (b"MZ", b"MZ" + b"\0" * 130,
                       _pe_stub()[:128] + b"BAD!")
        for raw in bad_headers:
            with self.subTest(length=len(raw)):
                (self.tools / "adb.exe").write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "Windows PE"):
                    self._evaluate()

    def test_does_not_bundle_private_settings_runtime_logs_or_build_folders(self):
        # The spec lists assets explicitly, so local developer data cannot be
        # swept into the executable by a recursive source-directory copy.
        for name in ("venom_ai.json", "venom_workspaces.json", "venom_debug.log"):
            (self.root / name).write_text("private runtime data")
        arguments, _ = self._evaluate()
        bundled = [Path(source).name for source, _ in arguments["datas"]]
        self.assertEqual(set(bundled), {"icon.png", "icon.ico", "icon_nobg.png",
                                        "gnirehtet.apk", "LICENSE", "provenance.json"})

    def test_usb_internet_helpers_are_bundled_from_an_override_directory(self):
        custom = self.root / "pinned helpers"
        self.helpers.rename(custom)
        arguments, _ = self._evaluate(helpers=custom)
        self.assertIn((str((custom / "gnirehtet.exe").resolve()), "gnirehtet"), arguments["binaries"])
        self.assertEqual({target for source, target in arguments["datas"]
                          if Path(source).parent == custom.resolve()}, {"gnirehtet"})

    def test_build_stops_when_usb_internet_helpers_are_missing_or_not_windows(self):
        (self.helpers / "gnirehtet.exe").unlink()
        with self.assertRaisesRegex(ValueError, "USB Internet helper"):
            self._evaluate()
        (self.helpers / "gnirehtet.exe").write_bytes(b"\x7fELF" + b"\0" * 128)
        with self.assertRaisesRegex(ValueError, "Windows PE binary"):
            self._evaluate()


if __name__ == "__main__":
    unittest.main()
