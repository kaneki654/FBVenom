"""Verify a genuine Windows build and its offline frozen startup check."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile


def windows_pe_info(path):
    """Reject renamed ELF files, corrupt files and non-GUI/non-x64 binaries."""
    with Path(path).open("rb") as source:
        dos = source.read(64)
        if len(dos) != 64 or dos[:2] != b"MZ":
            raise ValueError("The build is not a Windows executable (missing MZ header).")
        offset = struct.unpack_from("<I", dos, 60)[0]
        if not 64 <= offset <= 1024 * 1024:
            raise ValueError("Invalid Windows PE header offset.")
        source.seek(offset)
        pe = source.read(94)
        if len(pe) != 94 or pe[:4] != b"PE\0\0":
            raise ValueError("Missing or truncated Windows PE header.")
        machine = struct.unpack_from("<H", pe, 4)[0]
        magic = struct.unpack_from("<H", pe, 24)[0]
        subsystem = struct.unpack_from("<H", pe, 92)[0]
        if machine != 0x8664 or magic != 0x20B or subsystem != 2:
            raise ValueError("The build must be a 64-bit Windows GUI executable.")
    return {"format": "PE32+", "architecture": "x86_64", "subsystem": "Windows GUI"}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate(path, source_commit):
    path = Path(path).resolve()
    info = windows_pe_info(path)
    if sys.platform != "win32":
        raise RuntimeError("The frozen startup check must run on Windows.")
    if (path.parent / "venom_ai.json").exists():
        raise RuntimeError("Validate in a clean dist folder without private AI settings.")
    with tempfile.TemporaryDirectory(prefix="fbvenom-build-check-") as directory:
        report_path = Path(directory) / "report.json"
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                           FBVENOM_BUILD_CHECK_PATH=str(report_path))
        process = subprocess.run([str(path), "--build-check"], env=environment,
                                 cwd=directory, timeout=120, capture_output=True)
        if not report_path.is_file():
            raise RuntimeError(f"The executable did not produce a build report (exit {process.returncode}).")
        smoke = json.loads(report_path.read_text(encoding="utf-8"))
        if process.returncode or not smoke.get("ok"):
            raise RuntimeError("Frozen startup failed: " + json.dumps(smoke))
    from version import APP_VERSION
    required = ("frozen", "windows", "adb_bundled", "adb_version_ok", "icon_loaded",
                "config_beside_exe", "app_beside_exe", "auth_imported",
                "usb_internet_bundled", "usb_relay_cli_ok")
    if not all(smoke.get(key) is True for key in required):
        raise RuntimeError("Frozen startup omitted required packaging checks.")
    features = smoke.get("gui_features", {})
    if not isinstance(features, dict) or not all(features.get(name) is True for name in (
            "live_link_normalization", "structured_errors", "venom_theme", "diagnostics_panel",
            "backup_server", "usb_internet")):
        raise RuntimeError("Frozen startup omitted required application features.")
    if smoke.get("app_version") != APP_VERSION:
        raise RuntimeError("The executable version does not match version.py.")
    digest = sha256_file(path)
    report = {
        "version": APP_VERSION, "source_commit": source_commit,
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "filename": path.name, "size_bytes": path.stat().st_size,
        "sha256": digest, **info,
        "windows_smoke_test": "passed",
        "checks": {key: smoke[key] for key in required},
        "gui_features": features,
        "usb_internet": smoke.get("usb_internet", {}),
        "adb_version": smoke.get("adb_version", "").splitlines()[0],
        "workflow_run": (f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
                         f"{os.environ['GITHUB_RUN_ID']}" if os.environ.get("GITHUB_RUN_ID") else ""),
    }
    path.with_suffix(".build.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    path.with_suffix(".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
    print(json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable")
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    validate(args.executable, args.source_commit)


if __name__ == "__main__":
    main()
