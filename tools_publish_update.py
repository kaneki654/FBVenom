"""Prepare a verified Windows build's manifest for a GitHub release.

Bump version.py BEFORE building on Windows, then run:

    python3 tools_publish_update.py dist/FBVenom.exe \
        --version 1.1.3 --notes "Update and LAN connection fixes"

The executable and its adjacent .build.json report must already match the
requested version. This tool validates them and atomically writes manifest.json;
it does not rebuild, change version.py, upload files, or publish a release.

Upload the executable and manifest together using the printed release command.
PCs discover releases through releases/latest/download/manifest.json, while each
manifest pins its executable to that version's immutable release asset URL.
"""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile

from tools_validate_windows_build import windows_pe_info
from updater import DEFAULT_MANIFEST_URL, GITHUB_REPO, UpdateError, _check_location


_REQUIRED_CHECKS = ("frozen", "windows", "adb_bundled", "adb_version_ok", "icon_loaded",
                    "config_beside_exe", "app_beside_exe", "auth_imported",
                    "usb_internet_bundled", "usb_relay_cli_ok")
_REQUIRED_FEATURES = ("live_link_normalization", "structured_errors", "venom_theme",
                      "diagnostics_panel", "backup_server", "usb_internet")


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bump_version_file(version, path="version.py"):
    """Compatibility helper for scripts that bump the version BEFORE building."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"  [warn] could not read {path}: {exc}")
        return False
    new, count = re.subn(r'(?m)^APP_VERSION\s*=\s*".*"$',
                         f'APP_VERSION = "{version}"', text)
    if not count:
        print(f"  [warn] no APP_VERSION line in {path}; left it alone")
        return False
    if new != text:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new)
    return True


def stale_sources(build, root="."):
    """Return source files newer than the executable, newest first."""
    try:
        built = os.path.getmtime(build)
    except OSError:
        return []
    build_abs = os.path.abspath(build)
    newer = []
    for name in os.listdir(root):
        if not name.endswith(".py") or name.startswith("test_"):
            continue
        path = os.path.join(root, name)
        if os.path.abspath(path) == build_abs:
            continue
        try:
            if os.path.getmtime(path) > built:
                newer.append((os.path.getmtime(path), name))
        except OSError:
            continue
    return [name for _, name in sorted(newer, reverse=True)]


def release_build_url(version):
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("--version must be a numeric version such as 1.1.3")
    return f"https://github.com/{GITHUB_REPO}/releases/download/v{version}/FBVenom.exe"


def validate_build(build, version):
    """Verify the Windows report against the exact bytes being published."""
    from version import APP_VERSION
    release_build_url(version)
    if version != APP_VERSION:
        raise ValueError("Requested version does not match APP_VERSION. Bump version.py before building.")
    build = Path(build)
    if not build.is_file():
        raise ValueError(f"No such build: {build}")
    metadata = build.with_suffix(".build.json")
    try:
        with metadata.open("rb") as source:
            raw = source.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError
        report = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError):
        raise ValueError("A valid adjacent .build.json Windows verification report is required") from None
    if not isinstance(report, dict):
        raise ValueError("The .build.json verification report must be an object")
    if report.get("version") != version:
        raise ValueError("Build metadata version does not match --version; rebuild with the requested version")
    if (report.get("filename") != build.name or type(report.get("size_bytes")) is not int
            or report["size_bytes"] != build.stat().st_size):
        raise ValueError("Build metadata filename or size does not match the executable")
    digest = sha256_of(build)
    if report.get("sha256") != digest:
        raise ValueError("Build metadata SHA-256 does not match the executable")
    info = windows_pe_info(build)
    if any(report.get(name) != value for name, value in info.items()):
        raise ValueError("Build metadata does not match the Windows x64 GUI executable format")
    checks = report.get("checks")
    features = report.get("gui_features")
    if (report.get("windows_smoke_test") != "passed" or not isinstance(checks, dict)
            or not all(checks.get(name) is True for name in _REQUIRED_CHECKS)):
        raise ValueError("Build metadata must confirm successful frozen Windows startup checks")
    if not isinstance(features, dict) or not all(features.get(name) is True for name in _REQUIRED_FEATURES):
        raise ValueError("Build metadata is missing required application features, including Backup server")
    if not isinstance(report.get("source_commit"), str) or not re.fullmatch(r"[0-9a-f]{40}", report["source_commit"]):
        raise ValueError("Build metadata must identify the verified source commit")
    return report


def write_manifest(path, manifest):
    """Replace an existing manifest only after the new JSON is fully written."""
    path = Path(path)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(manifest, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("build", help="the verified Windows file, e.g. dist/FBVenom.exe")
    ap.add_argument("--version", required=True, help="version already built into the executable, e.g. 1.1.3")
    ap.add_argument("--url", default=None,
                    help="override the version-pinned HTTPS release URL with an HTTPS or local/network location")
    ap.add_argument("--notes", default="", help="what changed, shown in the app")
    ap.add_argument("--out", default="", help="where to write manifest.json")
    ap.add_argument("--force", action="store_true",
                    help="ignore source file modification times; never bypass build metadata verification")
    args = ap.parse_args(argv)
    build = Path(args.build).resolve()
    try:
        report = validate_build(build, args.version)
        url = release_build_url(args.version) if args.url is None else args.url
        if not url:
            raise ValueError("--url must be an HTTPS address or a local/network path")
        _check_location(url, "Build URL")
    except (OSError, ValueError, UpdateError) as exc:
        ap.error(str(exc))
    stale = stale_sources(build)
    if stale and not args.force:
        newest = ", ".join(stale[:4]) + ("..." if len(stale) > 4 else "")
        ap.error(f"The build is OLDER than the source ({newest}). Rebuild first; "
                 "--force bypasses modification times only for an intentionally retained verified build.")
    out = Path(args.out).resolve() if args.out else build.with_name("manifest.json")
    if (out in (build, build.with_suffix(".build.json"), build.with_suffix(".sha256"))
            or out.name.lower() == "version.py"):
        ap.error("The manifest output must not overwrite the executable, build metadata, checksum or version.py")
    manifest = {
        "version": args.version, "url": url, "sha256": report["sha256"],
        "notes": args.notes, "size": report["size_bytes"],
        "published": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
    }
    try:
        write_manifest(out, manifest)
    except OSError:
        ap.error("Could not atomically write the manifest; check the output directory and permissions")
    print(f"  build    : {build}  ({report['size_bytes'] / 1048576:.1f} MB)")
    print(f"  version  : {args.version}")
    print(f"  sha256   : {report['sha256']}")
    print(f"  manifest : {out}")
    print(f"  asset URL: {url}")
    print("\nManifest prepared. Publish the executable and manifest together as a GitHub release:")
    command = ["gh", "release", "create", f"v{args.version}", str(build), str(out),
               "--repo", GITHUB_REPO, "--target", report["source_commit"],
               "--title", f"v{args.version}", "--notes", args.notes or args.version]
    print("  " + shlex.join(command))
    print(f"  PCs discover published releases at {DEFAULT_MANIFEST_URL}")
    print("  Creating a manifest or pushing files to main does not publish that release.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
