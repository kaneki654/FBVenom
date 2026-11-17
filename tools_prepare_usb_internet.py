"""Fetch and verify the pinned official Gnirehtet Rust USB Internet helpers.

    python tools_prepare_usb_internet.py --platform windows --out gnirehtet

Only the relay, Android client, Apache license and provenance are installed.
Importing this module or calling verify_assets() never downloads or runs them.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

VERSION = "2.5.1"
PROJECT = "Genymobile/gnirehtet"
ARCHIVES = {
    "windows": {"name": "gnirehtet-rust-win64-v2.5.1.zip", "relay": "gnirehtet.exe",
                "sha256": "7f5b1063e7895182aa60def1437e50363c3758144088dcd079037bb7c3c46a1c"},
    "linux": {"name": "gnirehtet-rust-linux64-v2.5.1.zip", "relay": "gnirehtet",
              "sha256": "dee55499ca4fef00ce2559c767d2d8130163736d43fdbce753e923e75309c275"},
}
LICENSE_URL = "https://raw.githubusercontent.com/Genymobile/gnirehtet/v2.5.1/LICENSE"
LICENSE_SHA256 = "59f023291b5fa3b10141468200d3f477d91007f7d576c7af1bb22b0c5c6b48d1"
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024


def _platform(value=None):
    value = value or ("windows" if sys.platform == "win32" else "linux" if sys.platform.startswith("linux") else "")
    if value not in ARCHIVES:
        raise ValueError("USB Internet helpers support Windows x64 and Linux x64")
    return value


def _archive_url(platform):
    return f"https://github.com/{PROJECT}/releases/download/v{VERSION}/{ARCHIVES[platform]['name']}"


def _https(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Helper downloads and redirects must use HTTPS")
    return url


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_https(url, limit):
    _https(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPSRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "FBVenom-helper-preparation", "Accept-Encoding": "identity"})
    deadline = time.monotonic() + 90
    with opener.open(request, timeout=20) as response:
        _https(response.geturl())
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isascii() or not length.isdigit() or int(length) > limit):
            raise ValueError("Helper download has an invalid or oversized Content-Length")
        content = bytearray()
        while True:
            if time.monotonic() > deadline:
                raise ValueError("Helper download timed out")
            chunk = response.read(min(65536, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > limit:
                raise ValueError("Helper download exceeds its size limit")
        return bytes(content)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _safe_archive_members(archive):
    entries = archive.infolist()
    if len(entries) > 64 or sum(info.file_size for info in entries) > 32 * 1024 * 1024:
        raise ValueError("Helper archive exceeds its extraction limits")
    for info in entries:
        name = info.filename
        parts = name.rstrip("/").split("/")
        mode = stat.S_IFMT(info.external_attr >> 16)
        if (not name or "\\" in name or "\0" in name or ":" in name or name.startswith("/")
                or any(part in ("", ".", "..") for part in parts)
                or mode not in (0, stat.S_IFREG, stat.S_IFDIR)
                or info.flag_bits & 1 or not 0 <= info.file_size <= MAX_FILE):
            raise ValueError("Helper archive contains an unsafe path or file")
        yield info


def _extract(archive_bytes, platform):
    wanted = {ARCHIVES[platform]["relay"], "gnirehtet.apk"}
    files = {}
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            for info in _safe_archive_members(archive):
                name = PurePosixPath(info.filename).name
                if info.is_dir() or name not in wanted:
                    continue
                if name in files:
                    raise ValueError("Helper archive contains duplicate relay or APK files")
                with archive.open(info) as source:
                    content = source.read(MAX_FILE + 1)
                if len(content) != info.file_size or len(content) > MAX_FILE:
                    raise ValueError("Helper archive file exceeds its size limit")
                files[name] = content
    except zipfile.BadZipFile:
        raise ValueError("The verified helper archive is not a valid ZIP") from None
    if set(files) != wanted:
        raise ValueError("Helper archive is missing the relay or Android client")
    return files


def _validate_relay(content, platform):
    if platform == "linux":
        if len(content) < 20 or content[:5] != b"\x7fELF\x02":
            raise ValueError("Gnirehtet must be a Linux x64 ELF executable")
        return
    if len(content) < 64 or content[:2] != b"MZ":
        raise ValueError("Gnirehtet must be a Windows PE executable")
    offset = struct.unpack_from("<I", content, 60)[0]
    if offset < 64 or offset > len(content) - 4 or content[offset:offset + 4] != b"PE\0\0":
        raise ValueError("Gnirehtet has an invalid Windows PE signature")


def _validate_apk(content):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as apk:
            entries = apk.infolist()
            manifests = [item for item in entries if item.filename == "AndroidManifest.xml"]
            if len(entries) > 512 or len(manifests) != 1 or not 0 < manifests[0].file_size <= 512 * 1024:
                raise ValueError("Gnirehtet APK must contain a bounded AndroidManifest.xml")
            with apk.open(manifests[0]) as source:
                if len(source.read(512 * 1024 + 1)) != manifests[0].file_size:
                    raise ValueError("Gnirehtet APK has an invalid Android manifest")
    except zipfile.BadZipFile:
        raise ValueError("Gnirehtet Android client must be an APK ZIP") from None


def _read_asset(path, limit=MAX_FILE):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"Missing, unsafe or oversized USB Internet asset: {path.name}")
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("USB Internet asset exceeds its size limit")
    return data


def verify_assets(directory, platform=None):
    """Validate all helper hashes and formats without executing or downloading."""
    platform = _platform(platform)
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("USB Internet helper directory is missing or unsafe")
    try:
        provenance = json.loads(_read_asset(directory / "provenance.json", 65536))
    except (OSError, ValueError):
        raise ValueError("USB Internet helper provenance is missing or invalid") from None
    expected = {"schema_version": 1, "project": PROJECT, "version": VERSION, "platform": platform,
                "archive_url": _archive_url(platform), "archive_sha256": ARCHIVES[platform]["sha256"],
                "license_url": LICENSE_URL, "license_sha256": LICENSE_SHA256}
    if not isinstance(provenance, dict) or any(provenance.get(k) != v for k, v in expected.items()):
        raise ValueError("USB Internet helper provenance does not match the pinned official release")
    names = {ARCHIVES[platform]["relay"], "gnirehtet.apk", "LICENSE"}
    files = provenance.get("files")
    if not isinstance(files, dict) or set(files) != names:
        raise ValueError("USB Internet helper provenance has an invalid file inventory")
    content = {}
    for name in names:
        data = _read_asset(directory / name)
        recorded = files[name]
        if (not isinstance(recorded, dict) or type(recorded.get("size_bytes")) is not int
                or recorded["size_bytes"] != len(data) or recorded.get("sha256") != _digest(data)):
            raise ValueError(f"USB Internet helper hash or size mismatch: {name}")
        content[name] = data
    if _digest(content["LICENSE"]) != LICENSE_SHA256:
        raise ValueError("USB Internet helper license does not match upstream")
    _validate_relay(content[ARCHIVES[platform]["relay"]], platform)
    _validate_apk(content["gnirehtet.apk"])
    return {"relay": str(directory / ARCHIVES[platform]["relay"]), "apk": str(directory / "gnirehtet.apk"),
            "license": str(directory / "LICENSE"), "provenance": provenance}


def prepare(platform, out, downloader=None):
    platform = _platform(platform)
    out = Path(out).expanduser().absolute()
    if out.is_symlink():
        raise ValueError("Helper output directory must not be a symbolic link")
    if out.exists() and any(out.iterdir()):
        return verify_assets(out, platform)
    downloader = downloader or download_https
    raw = downloader(_archive_url(platform), MAX_ARCHIVE)
    if len(raw) > MAX_ARCHIVE or _digest(raw) != ARCHIVES[platform]["sha256"]:
        raise ValueError("Official Gnirehtet archive SHA-256 verification failed")
    files = _extract(raw, platform)
    license_bytes = downloader(LICENSE_URL, 32768)
    if len(license_bytes) > 32768 or _digest(license_bytes) != LICENSE_SHA256:
        raise ValueError("Official Gnirehtet license SHA-256 verification failed")
    files["LICENSE"] = license_bytes
    provenance = {"schema_version": 1, "project": PROJECT, "version": VERSION, "platform": platform,
                  "archive_url": _archive_url(platform), "archive_sha256": ARCHIVES[platform]["sha256"],
                  "license_url": LICENSE_URL, "license_sha256": LICENSE_SHA256,
                  "files": {name: {"sha256": _digest(data), "size_bytes": len(data)} for name, data in files.items()}}
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".gnirehtet-", dir=out.parent))
    try:
        for name, data in files.items():
            (staging / name).write_bytes(data)
        (staging / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
        if platform == "linux":
            (staging / "gnirehtet").chmod(0o755)
        verify_assets(staging, platform)
        if out.exists():
            out.rmdir()  # Only an empty destination reaches this point.
        os.replace(staging, out)
        return verify_assets(out, platform)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=tuple(ARCHIVES), default=None)
    parser.add_argument("--out", default=str(Path(__file__).with_name("gnirehtet")))
    args = parser.parse_args(argv)
    try:
        result = prepare(args.platform, args.out)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        parser.error(str(exc))
    print(f"Verified official Gnirehtet {VERSION}: {Path(result['relay']).parent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
