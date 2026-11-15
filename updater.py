"""Self-update for the fleet: publish one build, every PC installs it.

A running .exe cannot overwrite itself, so installing is staged rather than
done in place:

    download -> verify -> write a swap script -> quit -> script swaps -> relaunch

The build being replaced is kept beside the new one as `<name>.prev<ext>`.
That file IS the downgrade: rolling back is the same swap run the other way,
so a bad release never needs a visit to the machine.

Nothing here trusts the network. The manifest must be HTTPS (or a path on the
LAN), the download must match the SHA-256 the manifest promises, and a
mismatch deletes the file instead of installing it.
"""

import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

from version import APP_VERSION

# Where the app actually lives. Frozen, that is the folder holding the .exe -
# never the PyInstaller bundle, which is a temp dir that disappears.
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    APP_PATH = os.path.abspath(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_PATH = os.path.abspath(sys.argv[0] or __file__)

SETTINGS_PATH = os.path.join(APP_DIR, "venom_update.json")
MAX_DOWNLOAD = 400 * 1024 * 1024       # a build is ~45MB; this is a sanity cap
MAX_MANIFEST = 64 * 1024

# The permanent home of the published build.
#
# GitHub's "releases/latest/download/<asset>" is the one address that never
# changes: cut a new release and this URL resolves to it, so no PC ever needs
# the address retyped. That is why the channel lives here and not on the
# Cloudflare tunnel, whose domain changes every time the host reboots.
GITHUB_REPO = "kaneki654/FBVenom"
RELEASE_BASE = f"https://github.com/{GITHUB_REPO}/releases/latest/download"
DEFAULT_MANIFEST_URL = f"{RELEASE_BASE}/manifest.json"
DEFAULT_BUILD_URL = f"{RELEASE_BASE}/FBVenom.exe"


class UpdateError(Exception):
    """Anything that should be shown to the operator rather than raised."""


# ── version numbers ────────────────────────────────────────────────────────
def parse_version(text):
    """"1.2.10" -> (1, 2, 10). Unparseable parts sort lowest, never crash."""
    parts = []
    for chunk in str(text or "").strip().lstrip("vV").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts or [0])


def is_newer(candidate, current=APP_VERSION):
    """True when `candidate` is a higher version than `current`."""
    a, b = parse_version(candidate), parse_version(current)
    size = max(len(a), len(b))
    return a + (0,) * (size - len(a)) > b + (0,) * (size - len(b))


# ── settings ───────────────────────────────────────────────────────────────
_EMPTY = {"manifest_url": "", "last_check": "", "last_installed": ""}


def load_settings():
    """Saved settings, falling back to the permanent release channel.

    A fresh PC therefore has a working update address with nothing typed in.
    """
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = {k: str(data.get(k) or "") for k in _EMPTY}
    if not out["manifest_url"]:
        out["manifest_url"] = DEFAULT_MANIFEST_URL
    return out


def save_settings(settings):
    """Write atomically; a half-written settings file must never strand a PC."""
    data = {k: str(settings.get(k) or "") for k in _EMPTY}
    tmp = SETTINGS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, SETTINGS_PATH)
    except OSError as exc:
        raise UpdateError(f"Could not save update settings: {exc}") from exc


# ── the manifest ───────────────────────────────────────────────────────────
def _check_location(url, what):
    """Only HTTPS, or a path on this machine / the LAN.

    Plain HTTP is refused on purpose: an update channel is remote code
    execution, and http is editable in transit by anyone on the way.
    """
    text = str(url or "")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme in ("", "file") or text.startswith("\\\\"):
        return "local"
    if parsed.scheme == "https":
        return "https"
    raise UpdateError(
        f"{what} must be an https:// address or a local/network path "
        f"(got '{parsed.scheme}://'). Plain http is refused.")


def _local_path(url):
    return url[7:] if str(url).startswith("file://") else str(url)


def _read(url, limit):
    if _check_location(url, "The update address") == "local":
        with open(_local_path(url), "rb") as fh:
            return fh.read(limit + 1)
    with urllib.request.urlopen(url, timeout=20) as response:  # nosec B310 - scheme checked above
        _require_https_final(response)
        return response.read(limit + 1)


def _require_https_final(response):
    """Redirects are normal (a release URL hops to a CDN), but one must never
    quietly downgrade the channel to plain http."""
    final = getattr(response, "url", "") or ""
    if not final:
        try:
            final = response.geturl()
        except Exception:
            final = ""
    if final and not final.lower().startswith("https://"):
        raise UpdateError("The update was redirected to a non-HTTPS address "
                          "and was refused.")


def fetch_manifest(url):
    """Read and validate the published manifest.

    Shape:
      {"version": "1.1.0",
       "url":     "https://.../FBVenom.exe",
       "sha256":  "<64 hex>",
       "notes":   "what changed"}
    """
    if not str(url or "").strip():
        raise UpdateError("No update address is set yet.")
    try:
        raw = _read(url, MAX_MANIFEST)
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"Could not reach the update address: {exc}") from exc
    if len(raw) > MAX_MANIFEST:
        raise UpdateError("That address did not answer with an update manifest.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise UpdateError(f"The update manifest is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise UpdateError("The update manifest must be a JSON object.")

    version = str(data.get("version") or "").strip()
    build_url = str(data.get("url") or "").strip()
    digest = str(data.get("sha256") or "").strip().lower()
    if not version:
        raise UpdateError("The manifest has no version.")
    if not build_url:
        raise UpdateError("The manifest has no download url.")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise UpdateError("The manifest has no usable sha256 checksum.")
    _check_location(build_url, "The download address")
    return {"version": version, "url": build_url, "sha256": digest,
            "notes": str(data.get("notes") or "").strip()}


def check(url=None):
    """Look for a newer build. Returns (manifest, available: bool)."""
    settings = load_settings()
    url = url or settings.get("manifest_url")
    manifest = fetch_manifest(url)
    settings["manifest_url"] = url
    settings["last_check"] = datetime.datetime.now(
        datetime.timezone.utc).replace(microsecond=0).isoformat()
    try:
        save_settings(settings)
    except UpdateError:
        pass                      # a read-only folder must not block checking
    return manifest, is_newer(manifest["version"])


# ── downloading ────────────────────────────────────────────────────────────
def download(manifest, progress=None):
    """Fetch the build next to the app and prove it is the promised bytes."""
    digest = hashlib.sha256()
    total = 0
    kind = _check_location(manifest["url"], "The download address")
    fd, staged = tempfile.mkstemp(prefix=".fbvenom-update-", dir=APP_DIR)
    os.close(fd)
    try:
        if kind == "local":
            stream = open(_local_path(manifest["url"]), "rb")
            size = os.path.getsize(_local_path(manifest["url"]))
        else:
            stream = urllib.request.urlopen(manifest["url"], timeout=60)  # nosec B310 - scheme checked
            _require_https_final(stream)
            try:
                size = int(stream.headers.get("Content-Length") or 0)
            except Exception:
                size = 0
        with stream as response, open(staged, "wb") as out:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD:
                    raise UpdateError("The download is far larger than a build "
                                      "should be - refusing it.")
                digest.update(chunk)
                out.write(chunk)
                if progress:
                    progress(total, size)
        if digest.hexdigest() != manifest["sha256"]:
            raise UpdateError(
                "The download does not match the checksum in the manifest, so "
                "it was discarded. Re-publish the build and try again.")
        return staged
    except Exception:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise


# ── installing and rolling back ────────────────────────────────────────────
def previous_build_path():
    stem, ext = os.path.splitext(APP_PATH)
    return stem + ".prev" + ext


def previous_build_version():
    """The version recorded when the kept build was replaced, if known."""
    return load_settings().get("last_installed") or ""


def can_downgrade():
    return os.path.isfile(previous_build_path())


def _swap_script(current, incoming, keep_as):
    """A script that waits for this process to let go, then rotates the files.

    One shape serves both directions:
        current -> keep_as,  incoming -> current
    For an update `incoming` is the download and `keep_as` becomes the rollback
    target. For a downgrade `incoming` IS the rollback target, and the build
    being removed takes its place - so you can roll forward again.
    """
    if os.name == "nt":
        path = os.path.join(APP_DIR, "fbvenom-swap.bat")
        body = (
            "@echo off\r\n"
            "setlocal\r\n"
            f'set "CUR={current}"\r\n'
            f'set "NEW={incoming}"\r\n'
            f'set "KEEP={keep_as}"\r\n'
            'set "TMP=%CUR%.swapping"\r\n'
            ":wait\r\n"
            "timeout /t 1 /nobreak >nul\r\n"
            # The move only succeeds once the old process has released the
            # file, so this doubles as "wait for exit" without guessing a PID.
            'move /Y "%CUR%" "%TMP%" >nul 2>&1\r\n'
            "if errorlevel 1 goto wait\r\n"
            'move /Y "%NEW%" "%CUR%" >nul 2>&1\r\n'
            'move /Y "%TMP%" "%KEEP%" >nul 2>&1\r\n'
            'start "" "%CUR%"\r\n'
            'del "%~f0" >nul 2>&1\r\n')
    else:
        path = os.path.join(APP_DIR, "fbvenom-swap.sh")
        body = (
            "#!/bin/sh\n"
            f'CUR="{current}"\nNEW="{incoming}"\nKEEP="{keep_as}"\n'
            'TMP="$CUR.swapping"\n'
            "sleep 1\n"
            'mv -f "$CUR" "$TMP" 2>/dev/null || exit 1\n'
            'mv -f "$NEW" "$CUR"\n'
            'mv -f "$TMP" "$KEEP"\n'
            'chmod +x "$CUR" 2>/dev/null\n'
            '"$CUR" &\n'
            'rm -f "$0"\n')
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    if os.name != "nt":
        os.chmod(path, 0o755)
    return path


def _launch_detached(script):
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0) |
                 getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        subprocess.Popen(["cmd", "/c", script], close_fds=True,
                         creationflags=flags)
    else:
        subprocess.Popen(["/bin/sh", script], close_fds=True,
                         start_new_session=True)


def install(staged_path, restart=True):
    """Put a verified download in place and relaunch into it.

    Returns the script that will do the swap. The caller must quit promptly -
    the script is waiting for this process to release the executable.
    """
    if not os.path.isfile(staged_path):
        raise UpdateError("The downloaded build went missing before install.")
    settings = load_settings()
    settings["last_installed"] = APP_VERSION      # what the rollback goes back to
    try:
        save_settings(settings)
    except UpdateError:
        pass
    script = _swap_script(APP_PATH, staged_path, previous_build_path())
    if restart:
        _launch_detached(script)
    return script


def downgrade(restart=True):
    """Go back to the kept build. The current one becomes the rollback target."""
    prev = previous_build_path()
    if not os.path.isfile(prev):
        raise UpdateError("There is no previous build on this PC to go back to.")
    script = _swap_script(APP_PATH, prev, prev)
    if restart:
        _launch_detached(script)
    return script
