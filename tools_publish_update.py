"""Publish a build so the other PCs can install it.

Run this on THIS pc after `pyinstaller FBVenom.spec`:

    python3 tools_publish_update.py dist/FBVenom.exe \
        --version 1.1.0 \
        --notes "fixed the devices counter"

It bumps version.py, checksums the build, and writes manifest.json beside it.
Then publish BOTH files as a GitHub release:

    gh release create v1.1.0 dist/FBVenom.exe dist/manifest.json \
        --repo kaneki654/FBVenom --title v1.1.0 --notes "..."

The PCs are pointed at .../releases/latest/download/manifest.json, which
always resolves to the newest release - so nothing has to be retyped anywhere
when you ship.

The checksum is the whole safety story: a PC refuses any download that does
not match it, so the manifest and the build must go out together.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

from updater import DEFAULT_BUILD_URL, DEFAULT_MANIFEST_URL


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bump_version_file(version, path="version.py"):
    """Keep version.py and the manifest in step - a build that reports an old
    number would offer itself an update forever."""
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
    """Source files newer than the build, newest first.

    Guards the mistake that is easy to make and expensive to undo: publishing
    yesterday's .exe. If the binary predates updater.py it cannot update
    itself, so every PC that installs it needs a visit.
    """
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


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("build", help="the built file, e.g. dist/FBVenom.exe")
    ap.add_argument("--version", required=True, help="new version, e.g. 1.1.0")
    ap.add_argument("--url", default=DEFAULT_BUILD_URL,
                    help="where the PCs download the build from "
                         f"(default: {DEFAULT_BUILD_URL})")
    ap.add_argument("--notes", default="", help="what changed, shown in the app")
    ap.add_argument("--out", default="", help="where to write manifest.json")
    ap.add_argument("--force", action="store_true", help="Publish even if build is older than source")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.build):
        ap.error(f"no such build: {args.build}")
    stale = stale_sources(args.build)
    if stale and not args.force:
        newest = ", ".join(stale[:4]) + ("..." if len(stale) > 4 else "")
        ap.error(
            f"{args.build} is OLDER than the source ({newest}).\n"
            "Publishing it would ship a build without those changes - and if "
            "it predates updater.py, the PCs that install it lose the Update "
            "button and have to be visited in person.\n"
            "Rebuild first, then re-run this. Use --force only if you really "
            "mean to publish the older binary.")
    if not (args.url.startswith(("https://", "file://", "\\\\"))
            or "://" not in args.url):
        ap.error("--url must be https:// (or a local/network path). "
                 "Plain http is refused by the updater.")

    digest = sha256_of(args.build)
    size = os.path.getsize(args.build)
    manifest = {
        "version": args.version,
        "url": args.url,
        "sha256": digest,
        "notes": args.notes,
        "size": size,
        "published": datetime.datetime.now(datetime.timezone.utc)
                     .replace(microsecond=0).isoformat(),
    }
    out = args.out or os.path.join(
        os.path.dirname(os.path.abspath(args.build)), "manifest.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    bumped = bump_version_file(args.version)
    print(f"  build    : {args.build}  ({size / 1048576:.1f} MB)")
    print(f"  version  : {args.version}"
          + ("" if bumped else "  (version.py NOT bumped)"))
    print(f"  sha256   : {digest}")
    print(f"  manifest : {out}")
    print()
    print("  Publish both as one release:")
    print(f"    gh release create v{args.version} {args.build} {out} \\")
    print(f"        --repo {DEFAULT_MANIFEST_URL.split('/releases/')[0].split('github.com/')[-1]} "
          f"--title v{args.version} --notes {json.dumps(args.notes or args.version)}")
    print(f"    the PCs read {DEFAULT_MANIFEST_URL}")
    if bumped:
        print()
        print("  version.py was bumped - REBUILD so the .exe reports the new "
              "number, then re-run this with the same --version.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
