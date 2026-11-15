"""Portable regressions for detecting corrupt or incorrectly targeted builds."""
from pathlib import Path
import struct
import tempfile
import unittest

from tools_validate_windows_build import windows_pe_info


class WindowsBuildValidationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "FBVenom.exe"

    def test_linux_and_corrupt_executables_are_rejected(self):
        for header in (b"\x7fELF", b"\xff" * 64, b"MZ"):
            with self.subTest(header=header[:4]):
                self.path.write_bytes(header)
                with self.assertRaises(ValueError):
                    windows_pe_info(self.path)

    def image(self, *, machine=0x8664, magic=0x20B, subsystem=2):
        image = bytearray(256)
        image[:2] = b"MZ"
        struct.pack_into("<I", image, 60, 128)
        image[128:132] = b"PE\0\0"
        struct.pack_into("<H", image, 132, machine)
        struct.pack_into("<H", image, 152, magic)
        struct.pack_into("<H", image, 220, subsystem)
        self.path.write_bytes(image)

    def test_valid_x64_gui_header_is_accepted(self):
        self.image()
        self.assertEqual(windows_pe_info(self.path)["architecture"], "x86_64")

    def test_console_and_wrong_architecture_builds_are_rejected(self):
        for fields in ({"machine": 0x14C}, {"magic": 0x10B}, {"subsystem": 3}):
            with self.subTest(fields=fields):
                self.image(**fields)
                with self.assertRaises(ValueError):
                    windows_pe_info(self.path)

    def test_invalid_and_truncated_pe_offsets_are_rejected(self):
        for offset in (0, 4 * 1024 * 1024, 250):
            self.image()
            image = bytearray(self.path.read_bytes())
            struct.pack_into("<I", image, 60, offset)
            self.path.write_bytes(image)
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                windows_pe_info(self.path)


if __name__ == "__main__":
    unittest.main()
