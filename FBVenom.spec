# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPEC).resolve().parent

datas = [
    (str(ROOT / "icon.png"), "."),
    (str(ROOT / "icon.ico"), "."),
    (str(ROOT / "icon_nobg.png"), "."),
]
binaries = []
for name in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"):
    path = Path("/usr/bin/adb")
    if not path.exists():
        raise FileNotFoundError(f"Required Android platform-tool is missing: {path}")
    binaries.append((str(path), "platform-tools"))

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
    upx=True,
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
