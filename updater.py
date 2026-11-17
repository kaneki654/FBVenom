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
import email.utils
import json
import ntpath
import os
import subprocess
import ssl
import sys
import tempfile
import time
import urllib.error
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
MAX_HTTP_ATTEMPTS = 3
_RETRY_HTTP_STATUSES = frozenset((429, 500, 502, 503, 504))

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
    # URL parsing treats an absolute Windows drive path as a one-letter
    # scheme ("C:"). Check native paths before applying the network policy.
    # A drive-relative path such as C:updates remains unsupported.
    drive, tail = ntpath.splitdrive(text)
    if (text.startswith("\\\\") or
            (len(drive) == 2 and drive[0].isalpha() and drive[1] == ":"
             and tail.startswith(("\\", "/")))):
        return "local"
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme in ("", "file"):
        return "local"
    if parsed.scheme == "https":
        return "https"
    raise UpdateError(
        f"{what} must be an https:// address or a local/network path "
        f"(got '{parsed.scheme}://'). Plain http is refused.")


def _local_path(url):
    return url[7:] if str(url).startswith("file://") else str(url)


def _safe_location(url):
    """Show a useful address without userinfo, query tokens or fragments."""
    try:
        parsed = urllib.parse.urlsplit(str(url))
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            return "the local update file"
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        try:
            if parsed.port is not None:
                host += f":{parsed.port}"
        except ValueError:
            pass
        safe = urllib.parse.urlunsplit(("https", host, parsed.path or "/", "", ""))
        return "".join(c for c in safe if ord(c) >= 32 and ord(c) != 127)
    except ValueError:
        return "the configured update address"


class _HTTPSOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        if urllib.parse.urlsplit(target).scheme.lower() != "https":
            # urllib normally closes this response only after constructing the
            # redirected request. A refusal must close it here instead.
            fp.close()
            raise UpdateError("The update was redirected to a non-HTTPS address and was refused.")
        return super().redirect_request(req, fp, code, msg, headers, target)


_HTTPS_OPENER = urllib.request.build_opener(_HTTPSOnlyRedirect())


def _retry_delay(headers, attempt):
    fallback = min(0.25 * (2 ** (attempt - 1)), 2.0)
    value = headers.get("Retry-After") if headers else None
    if value is None:
        return fallback
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            until = email.utils.parsedate_to_datetime(value)
            if until.tzinfo is None:
                until = until.replace(tzinfo=datetime.timezone.utc)
            delay = (until - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return fallback
    # NaN and infinity must not become unbounded or invalid sleeps.
    if not 0 <= delay <= 2:
        return 2.0 if delay > 2 else 0.0 if delay < 0 else fallback
    return delay


def _open_https(url, timeout, accept):
    """Retry transient HTTP failures at this exact channel, keeping TLS enabled."""
    location = _safe_location(url)
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        request = urllib.request.Request(url, headers={
            "Accept": accept, "User-Agent": "FBVenom-Updater/" + APP_VERSION})
        try:
            response = _HTTPS_OPENER.open(request, timeout=timeout)
            try:
                _require_https_final(response)
            except BaseException:
                response.close()
                raise
            return response
        except urllib.error.HTTPError as exc:
            code = exc.code
            delay = _retry_delay(exc.headers, attempt)
            exc.close()
            if code in _RETRY_HTTP_STATUSES and attempt < MAX_HTTP_ATTEMPTS:
                time.sleep(delay)
                continue
            if code == 404:
                advice = "The requested update file is not published at that address."
            elif code in _RETRY_HTTP_STATUSES:
                advice = f"The server is temporarily unavailable after {attempt} attempts; try again later."
            else:
                advice = "Check the update address and access to its published release."
            raise UpdateError(f"Update server returned HTTP {code} at {location}. {advice}") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                message = "TLS certificate verification failed; the update connection was refused"
            elif isinstance(exc.reason, TimeoutError):
                message = "The update connection timed out"
            else:
                message = "Could not reach the update server; check the network connection"
            raise UpdateError(f"{message} at {location}.") from None
        except TimeoutError:
            raise UpdateError(f"The update connection timed out at {location}.") from None
        except ssl.SSLCertVerificationError:
            raise UpdateError(f"TLS certificate verification failed at {location}; the update was refused.") from None
        except OSError:
            raise UpdateError(f"Could not reach the update server at {location}; check the network connection.") from None


def _read(url, limit):
    if _check_location(url, "The update address") == "local":
        with open(_local_path(url), "rb") as fh:
            return fh.read(limit + 1)
    with _open_https(url, timeout=20, accept="application/json") as response:
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
    except Exception:
        raise UpdateError(f"Could not read the update manifest at {_safe_location(url)}; check that the file is available.") from None
    if len(raw) > MAX_MANIFEST:
        raise UpdateError("That address did not answer with an update manifest.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise UpdateError(f"The update manifest at {_safe_location(url)} is not valid JSON.") from None
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
    try:
        fd, staged = tempfile.mkstemp(prefix=".fbvenom-update-", dir=APP_DIR)
    except OSError:
        raise UpdateError("Could not stage the update; check that the application folder is writable and has free space.") from None
    os.close(fd)
    try:
        if kind == "local":
            stream = open(_local_path(manifest["url"]), "rb")
            size = os.path.getsize(_local_path(manifest["url"]))
        else:
            stream = _open_https(manifest["url"], timeout=60, accept="application/octet-stream")
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
    except Exception as exc:
        try:
            os.unlink(staged)
        except OSError:
            pass
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(f"Could not download the update from {_safe_location(manifest['url'])}; check the connection and available disk space.") from None


# ── installing and rolling back ────────────────────────────────────────────
def previous_build_path():
    stem, ext = os.path.splitext(APP_PATH)
    return stem + ".prev" + ext


def previous_build_version():
    """The version recorded when the kept build was replaced, if known."""
    return load_settings().get("last_installed") or ""


def can_downgrade():
    return os.path.isfile(previous_build_path())


def _swap_script(current, incoming, keep_as, relaunch=True):
    """A script that waits for this process to let go, then rotates the files.

    One shape serves both directions:
        current -> keep_as,  incoming -> current
    For an update `incoming` is the download and `keep_as` becomes the rollback
    target. For a downgrade `incoming` IS the rollback target, and the build
    being removed takes its place - so you can roll forward again.
    `relaunch=False` rotates the files without starting the replacement app.
    """
    if os.name == "nt":
        path = os.path.join(APP_DIR, "fbvenom-swap.bat")
        body = (
            "@echo off\r\n"
            "setlocal\r\n"
            f'set "CUR={current}"\r\n'
            f'set "NEW={incoming}"\r\n'
            f'set "KEEP={keep_as}"\r\n'
            'set "SWAP_TEMP=%CUR%.swapping"\r\n'
            ":wait\r\n"
            # timeout requires an interactive console and fails with the
            # detached updater's redirected input. Loopback ping works there.
            "ping -n 2 127.0.0.1 >nul 2>&1\r\n"
            # The move only succeeds once the old process has released the
            # file, so this doubles as "wait for exit" without guessing a PID.
            'move /Y "%CUR%" "%SWAP_TEMP%" >nul 2>&1\r\n'
            "if errorlevel 1 goto wait\r\n"
            'move /Y "%NEW%" "%CUR%" >nul 2>&1\r\n'
            'move /Y "%SWAP_TEMP%" "%KEEP%" >nul 2>&1\r\n'
            + ('start "" "%CUR%"\r\n' if relaunch else "")
            # Keep the helper: CMD may reread its executing batch after each
            # command. The next update overwrites this file safely.
            + 'exit /b 0\r\n')
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
            + ('"$CUR" &\n' if relaunch else "")
            + 'rm -f "$0"\n')
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
    script = _swap_script(APP_PATH, staged_path, previous_build_path(), relaunch=restart)
    if restart:
        _launch_detached(script)
    return script


def downgrade(restart=True):
    """Go back to the kept build. The current one becomes the rollback target."""
    prev = previous_build_path()
    if not os.path.isfile(prev):
        raise UpdateError("There is no previous build on this PC to go back to.")
    script = _swap_script(APP_PATH, prev, prev, relaunch=restart)
    if restart:
        _launch_detached(script)
    return script
