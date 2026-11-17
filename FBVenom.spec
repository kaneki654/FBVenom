# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path

if sys.platform != "win32":
    raise RuntimeError(
        "FBVenom.exe must be built with Windows Python and PyInstaller. "
        "Run this spec on Windows; a Linux build is not a Windows executable.")

from PyInstaller.utils.hooks import collect_all
from tools_prepare_usb_internet import verify_assets

ROOT = Path(SPEC).resolve().parent

datas = [
    (str(ROOT / "icon.png"), "."),
    (str(ROOT / "icon.ico"), "."),
    (str(ROOT / "icon_nobg.png"), "."),
]
binaries = []
tools_dir = Path(os.environ.get("FBVENOM_PLATFORM_TOOLS") or
                 str(ROOT / "platform-tools")).expanduser().resolve()


def require_windows_binary(path):
    """Reject missing tools, Linux adb, and truncated/non-PE downloads."""
    if not path.is_file():
        raise FileNotFoundError(
            f"Required Windows Android platform-tool is missing: {path}. "
            "Extract Google's Windows platform-tools into platform-tools/ "
            "or set FBVENOM_PLATFORM_TOOLS to the extracted directory.")
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError(f"Expected a Windows PE binary: {path}")
        stream.seek(0x3C)
        offset_bytes = stream.read(4)
        if len(offset_bytes) != 4:
            raise ValueError(f"Truncated Windows PE binary: {path}")
        offset = int.from_bytes(offset_bytes, "little")
        if offset < 0x40 or offset > path.stat().st_size - 4:
            raise ValueError(f"Invalid Windows PE header offset: {path}")
        stream.seek(offset)
        if stream.read(4) != b"PE\0\0":
            raise ValueError(f"Invalid Windows PE signature: {path}")


for name in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"):
    path = tools_dir / name
    require_windows_binary(path)
    binaries.append((str(path), "platform-tools"))

notice = tools_dir / "NOTICE.txt"
if notice.is_file():
    datas.append((str(notice), "platform-tools"))

gnirehtet_dir = Path(os.environ.get("FBVENOM_GNIREHTET_DIR") or
                     str(ROOT / "gnirehtet")).expanduser().resolve()
usb_assets = verify_assets(gnirehtet_dir, platform="windows")
require_windows_binary(Path(usb_assets["relay"]))
binaries.append((usb_assets["relay"], "gnirehtet"))
for name in ("gnirehtet.apk", "LICENSE", "provenance.json"):
    datas.append((str(gnirehtet_dir / name), "gnirehtet"))

hiddenimports = []
for package in ("uiautomator2", "adbutils", "PIL"):
    package_datas, package_bins, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_bins
    hiddenimports += package_hidden

a = Analysis(
    [str(ROOT / "fbvenom.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="FBVenom",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "icon.ico"),
)
