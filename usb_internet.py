"""Owned, USB-only reverse tethering; no Qt or application-core dependencies."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import socket
import subprocess
import threading
import time
import uuid


PACKAGE = "com.genymobile.gnirehtet"
SOCKET_NAME = "localabstract:gnirehtet"
MAX_DISCOVERY = 1024 * 1024
VPN_RECHECK_SECONDS = 20.0
_STATE_NAMES = {3: "unauthorized", 4: "no permissions", 6: "offline", 8: "device", 10: "recovery", 11: "sideload"}
_HINTS = {
    "TETHER_NO_USB_DEVICES": "Connect phones with USB data cables, enable USB debugging, and accept the computer's debugging key.",
    "TETHER_DEVICE_UNAUTHORIZED": "Unlock this phone and accept its USB debugging authorization, then retry.",
    "TETHER_DEVICE_OFFLINE": "Reconnect the USB data cable and wait for this phone to become ready.",
    "TETHER_TRANSPORT_UNKNOWN": "Use the bundled current Android platform-tools; this ADB server cannot confirm the USB transport.",
    "TETHER_DISCOVERY_FAILED": "Check that Android platform-tools can run and the local ADB server is available.",
    "TETHER_ASSETS_INVALID": "Restore the verified Gnirehtet files supplied with this application.",
    "TETHER_UNSUPPORTED_ANDROID": "This phone needs Android 5.0 (API21) or newer.",
    "TETHER_VPN_CONFLICT": "Review the phone's current or always-on VPN before enabling USB internet; it has not been disabled.",
    "TETHER_MAPPING_IN_USE": "Another tool owns this phone's Gnirehtet tunnel. Stop it there before retrying.",
    "TETHER_START_FAILED": "Check the phone's installation and VPN approval prompts, USB connection, and available storage.",
    "TETHER_COMMAND_TIMEOUT": "Check this phone's USB connection and prompts, then retry.",
    "TETHER_VPN_APPROVAL_REQUIRED": "Accept Android's VPN connection request on this phone. The app cannot approve it for you.",
    "TETHER_RELAY_FAILED": "Check the saved USB internet log, then stop and restart USB internet.",
    "TETHER_DEVICE_DISCONNECTED": "Reconnect this phone to restore its USB tunnel.",
    "TETHER_STOP_FAILED": "Reconnect this phone and stop its Gnirehtet VPN manually if it remains active.",
    "TETHER_OWNERSHIP_CHANGED": "Another tool changed this tunnel; its connection has been left untouched.",
    "TETHER_WIFI_NOT_DEFAULT": "Make WiFi the computer's active internet connection before sharing it with phones.",
    "TETHER_INSTALL_FAILED": "Check the phone's storage and any installation prompt, then start USB internet again.",
    "TETHER_VPN_PREAPPROVE_FAILED": "Accept the VPN connection request on the phone once; Android remembers it.",
    "TETHER_HELPERS_MISSING": "Reinstall this FBVenom build; the USB internet helper files are missing.",
}

RELAY_NAME = "gnirehtet.exe" if os.name == "nt" else "gnirehtet"


def find_helpers(extra_dirs=()):
    """Locate the bundled relay and Android client without running them.

    Order: FBVENOM_GNIREHTET_DIR, any caller-supplied directories, the
    PyInstaller bundle, then a ``gnirehtet`` folder beside the program.
    """
    import sys
    candidates = []
    if os.environ.get("FBVENOM_GNIREHTET_DIR"):
        candidates.append(Path(os.environ["FBVENOM_GNIREHTET_DIR"]).expanduser())
    candidates.extend(Path(d) for d in extra_dirs if d)
    bundle = getattr(sys, "_MEIPASS", "")
    if bundle:
        candidates.append(Path(bundle) / "gnirehtet")
    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    candidates.append(here / "gnirehtet")
    for directory in candidates:
        relay, apk = directory / RELAY_NAME, directory / "gnirehtet.apk"
        if relay.is_file() and apk.is_file():
            return str(relay), str(apk)
    raise FileNotFoundError("USB internet helper files (gnirehtet relay and APK) were not found")


class _Failure(Exception):
    def __init__(self, code, message, detail=""):
        super().__init__(message)
        self.code, self.detail = code, detail


def _clean(value, limit=800):
    text = " ".join(str(value or "").split())
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)([?&](?:token|api_key|access_token|key)=)[^&#\s]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)\b((?:api[_ -]?key|access[_ -]?token|password)\s*[:=]\s*)[^\s,;]+", r"\1[redacted]", text)
    return text[:limit]


def _valid_serial(serial):
    return isinstance(serial, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", serial) is not None


def parse_proto_devices(text):
    """Parse the bounded, flat Device records in ADB protobuf text format."""
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_DISCOVERY:
        raise ValueError("ADB device snapshot is too large")
    records, position = [], 0
    while True:
        match = re.search(r"\bdevice\s*\{", text[position:])
        if not match:
            break
        begin = position + match.end()
        index, quoted, escaped = begin, False, False
        while index < len(text):
            character = text[index]
            if escaped:
                escaped = False
            elif quoted and character == "\\":
                escaped = True
            elif character == '"':
                quoted = not quoted
            elif character == "}" and not quoted:
                break
            index += 1
        if index == len(text):
            raise ValueError("Incomplete ADB device snapshot")
        fields = {}
        for field in re.finditer(r'(\w+)\s*:\s*("(?:\\.|[^"\\])*"|[A-Za-z_][A-Za-z0-9_]*|-?\d+)', text[begin:index]):
            name, value = field.groups()
            fields[name] = json.loads(value) if value.startswith('"') else value
        serial = fields.get("serial")
        if not _valid_serial(serial):
            raise ValueError("Invalid ADB device serial")
        state = fields.get("state", "UNKNOWN")
        state = _STATE_NAMES.get(int(state), "unknown") if state.isdigit() else state.lower()
        connection = fields.get("connection_type", "UNKNOWN")
        if connection == "1":
            connection = "USB"
        elif connection == "2":
            connection = "SOCKET"
        records.append({"serial": serial, "adb_state": state, "model": _clean(fields.get("model", ""), 100),
                        "connection_type": connection.upper()})
        if len(records) > 256:
            raise ValueError("Too many ADB devices")
        position = index + 1
    return records


def _read_exact(sock, length):
    result = bytearray()
    while len(result) < length:
        chunk = sock.recv(length - len(result))
        if not chunk:
            raise OSError("ADB server closed the device snapshot")
        result.extend(chunk)
    return bytes(result)


def _read_adb_proto():
    # This reads one initial snapshot and closes the tracker, not a remote ADB
    # connection. Old servers return FAIL and fall back to positive USB paths.
    request = b"host:track-devices-proto-text"
    with socket.create_connection(("127.0.0.1", 5037), timeout=2) as sock:
        sock.sendall(f"{len(request):04x}".encode("ascii") + request)
        if _read_exact(sock, 4) != b"OKAY":
            raise OSError("ADB server does not support transport metadata")
        size = int(_read_exact(sock, 4), 16)
        if size > MAX_DISCOVERY:
            raise ValueError("ADB device snapshot is too large")
        return parse_proto_devices(_read_exact(sock, size))


def _default_network_check():
    from usb_network import check_pc_internet
    return check_pc_internet()


class USBInternetManager:
    def __init__(self, adb_path, relay_path, apk_path, *, on_event=None, log_dir=None,
                 network_check=None, run=None, popen=None, auto_approve_vpn=True,
                 vpn_wait=8.0):
        self.adb_path, self.relay_path, self.apk_path = map(str, (adb_path, relay_path, apk_path))
        self._run = run or subprocess.run
        self._popen = popen or subprocess.Popen
        self._network_check = network_check or _default_network_check
        # Pre-approving Android's VPN prompt over USB debugging (appops) is
        # what makes one click enough for a whole fleet; off = tap OK per phone.
        self.auto_approve_vpn = bool(auto_approve_vpn)
        self._vpn_wait = max(0.0, float(vpn_wait))
        self.log_path = ""
        self._lock, self._operation_lock = threading.RLock(), threading.RLock()
        self._devices, self._owned = {}, {}
        self._vpn_checked = {}       # serial -> monotonic time of last VPN check
        self._relay, self._port, self._busy = None, None, False
        self._last_error, self._network = "", {}
        self._closed = False
        self._monitor_stop = threading.Event()
        self._monitor = None
        self._reader = None
        self._session = uuid.uuid4().hex[:12]
        self._callback, self._events = on_event, queue.Queue(maxsize=512)
        self._logger = logging.getLogger("fbvenom.usb." + self._session)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        # Beside the app first (where users look), then the profile folder
        # when the app folder is read-only. Never silently log nowhere unless
        # both are unwritable.
        directories = [Path(log_dir)] if log_dir is not None else []
        directories.append(Path.home() / ".fbvenom" / "logs")
        for directory in directories:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(directory / "venom_usb_internet.log", maxBytes=1024 * 1024,
                                              backupCount=2, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(message)s"))
                self._logger.addHandler(handler)
                if os.name != "nt":
                    os.chmod(handler.baseFilename, 0o600)
                self.log_path = handler.baseFilename
                break
            except OSError:
                continue
        else:
            self._logger.addHandler(logging.NullHandler())
        if self._callback:
            threading.Thread(target=self._deliver_events, name="usb-internet-events", daemon=True).start()

    def _deliver_events(self):
        while True:
            event = self._events.get()
            try:
                if event is None:
                    return
                try:
                    self._callback(event)
                except Exception:
                    pass
            finally:
                self._events.task_done()

    def _emit(self, code, message, serial="", detail="", severity="info", kind="diagnostic", hint=""):
        event = {"time": time.strftime("%H:%M:%S"), "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 "kind": kind, "severity": severity, "code": _clean(code, 64), "serial": _clean(serial, 200),
                 "message": _clean(message), "detail": _clean(detail), "hint": _clean(hint or _HINTS.get(code, "")),
                 "session": self._session}
        if severity == "error":
            with self._lock:
                self._last_error = event["code"] + ": " + event["message"]
        try:
            self._logger.info(json.dumps(event, ensure_ascii=False))
        except Exception:
            pass
        if self._callback and not self._closed:
            try:
                self._events.put_nowait(deepcopy(event))
            except queue.Full:
                pass  # Persisted logs retain events even if a UI consumer stalls.
        return event

    def _device_state(self, serial, state, detail="", adb_state=None):
        with self._lock:
            row = self._devices.setdefault(serial, {"serial": serial, "model": "", "adb_state": "unknown"})
            changed = row.get("state") != state or row.get("detail") != detail
            row.update(state=state, detail=_clean(detail))
            if adb_state is not None:
                row["adb_state"] = adb_state
        if changed:
            self._emit("TETHER_DEVICE_STATE", state.replace("_", " "), serial, detail, kind="status")

    @contextmanager
    def _operation(self):
        with self._operation_lock:
            with self._lock:
                self._busy = True
            try:
                yield
            finally:
                with self._lock:
                    self._busy = False

    def _environment(self):
        environment = dict(os.environ)
        environment.update(ADB=self.adb_path, GNIREHTET_APK=self.apk_path)
        return environment

    def _command(self, arguments, timeout=8):
        # A windowed EXE would flash a console for every ADB/relay call.
        options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
        try:
            result = self._run(list(arguments), stdin=subprocess.DEVNULL, capture_output=True,
                               encoding="utf-8", errors="replace", timeout=timeout, env=self._environment(),
                               **options)
        except subprocess.TimeoutExpired:
            raise _Failure("TETHER_COMMAND_TIMEOUT", "USB internet command timed out", Path(arguments[0]).name) from None
        except OSError as exc:
            raise _Failure("TETHER_START_FAILED", "USB internet helper could not run", type(exc).__name__) from None
        if isinstance(result, tuple):
            code, out, err = result
        else:
            code, out, err = result.returncode, result.stdout or "", result.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return code, str(out), str(err)

    def _adb(self, serial, arguments, timeout=8):
        return self._command([self.adb_path, "-s", serial, *arguments], timeout)

    def _discover_usb(self):
        code, output, error = self._command([self.adb_path, "devices", "-l"])
        if code:
            raise _Failure("TETHER_DISCOVERY_FAILED", "Could not list Android devices", error)
        if len(output.encode("utf-8")) > MAX_DISCOVERY:
            raise _Failure("TETHER_DISCOVERY_FAILED", "ADB returned an oversized device list")
        candidates = {}
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 2 or not _valid_serial(parts[0]) or parts[0] == "List" or parts[0].startswith("*"):
                continue
            tags = dict(part.split(":", 1) for part in parts[2:] if ":" in part)
            candidates[parts[0]] = {"serial": parts[0], "adb_state": parts[1], "model": tags.get("model", ""),
                                    "usb_marker": any(part.startswith("usb:") for part in parts[2:])}
        metadata = {}
        if candidates:
            try:
                metadata = {row["serial"]: row for row in _read_adb_proto()}
            except (OSError, ValueError, UnicodeError):
                pass
        found = []
        for serial, candidate in candidates.items():
            known = metadata.get(serial)
            if known and known["connection_type"] == "SOCKET":
                continue
            usb = bool(known and known["connection_type"] == "USB") or candidate["usb_marker"]
            if not usb:
                if serial.startswith("emulator-") or ":" in serial or "_adb-tls-" in serial:
                    continue
                code, path, _ = self._adb(serial, ["get-devpath"], timeout=4)
                usb = code == 0 and path.strip().startswith("usb:")
                if not usb and not (known and known["connection_type"] == "SOCKET"):
                    # Windows ADB reports no device path, and an older ADB
                    # server cannot report the transport at all. A serial with
                    # no host:port, emulator or TLS marker can only be a cable.
                    usb = True
                    self._logger.info(json.dumps({"time": time.strftime("%H:%M:%S"), "kind": "transport",
                                                  "session": self._session, "serial": _clean(serial, 200),
                                                  "detail": "USB assumed from a cable-style serial"}))
            if not usb:
                self._emit("TETHER_TRANSPORT_UNKNOWN", "Skipped a device whose USB transport could not be confirmed",
                           serial, severity="warning")
                continue
            row = {"serial": serial, "model": _clean((known or candidate).get("model", ""), 100),
                   "adb_state": (known or candidate)["adb_state"]}
            found.append(row)
            with self._lock:
                previous = self._devices.get(serial, {})
                self._devices[serial] = {**previous, **row}
                owned = serial in self._owned
            if row["adb_state"] != "device":
                state = "unauthorized" if row["adb_state"] == "unauthorized" else "offline"
                self._device_state(serial, state, "USB debugging authorization is required" if state == "unauthorized" else "Phone is not ready")
            elif not owned:
                self._device_state(serial, "ready", "Ready to enable USB internet")
        present = {row["serial"] for row in found}
        with self._lock:
            missing = [serial for serial in self._owned if serial not in present]
        for serial in missing:
            self._device_state(serial, "disconnected", "USB phone disconnected", adb_state="missing")
        return found

    def discover_usb(self):
        with self._operation():
            try:
                return self._discover_usb()
            except _Failure as exc:
                self._emit(exc.code, str(exc), detail=exc.detail, severity="error")
                return []

    def _check_network(self):
        try:
            result = self._network_check()
            if not isinstance(result, dict):
                raise ValueError
        except Exception:
            result = {"ok": False, "code": "TETHER_PC_NETWORK_FAILED", "message": "Could not check the computer's internet connection",
                      "detail": "", "hint": "Check the computer's WiFi and retry.", "interface": "", "kind": "unknown"}
        result = {name: (_clean(value) if isinstance(value, str) else deepcopy(value)) for name, value in result.items()}
        with self._lock:
            self._network = deepcopy(result)
        ok = bool(result.get("ok"))
        # Sharing Ethernet works; it is only worth a heads-up because the
        # user asked for Wi-Fi. A missing connection is the real error.
        severity = ("info" if result.get("kind") == "wifi" else "warning") if ok else "error"
        self._emit(result.get("code", "TETHER_PC_NETWORK_FAILED"), result.get("message", "Computer network checked"),
                   detail=result.get("detail", ""), hint=result.get("hint", "") if severity != "info" else "",
                   severity=severity, kind="status" if ok else "diagnostic")
        return deepcopy(result)

    def check_network(self):
        with self._operation():
            return self._check_network()

    def _verify_helpers(self):
        from tools_prepare_usb_internet import verify_assets
        checked = verify_assets(Path(self.relay_path).parent)
        if any(os.path.normcase(os.path.realpath(given)) != os.path.normcase(os.path.realpath(checked[name]))
               for given, name in ((self.relay_path, "relay"), (self.apk_path, "apk"))):
            raise ValueError("Unexpected USB internet asset path")

    @staticmethod
    def _free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def _read_relay(self, process):
        if process.stdout is None:
            return
        try:
            for line in process.stdout:
                # Details stay in the rotated log; never publish packet/flow
                # logs into the desktop. Bound each line before persistence.
                if "ERROR" in line.upper() or "WARN" in line.upper():
                    self._logger.info(json.dumps({"time": time.strftime("%H:%M:%S"), "kind": "relay",
                                                  "session": self._session, "detail": _clean(line, 1000)}))
        except (OSError, ValueError):
            pass
        finally:
            try:
                process.stdout.close()
            except OSError:
                pass

    def _wait_relay(self, process, port):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self._monitor_stop.is_set():
            if process.poll() is not None:
                return False
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return True
            except OSError:
                time.sleep(0.05)
        return False

    def _start_relay(self):
        if self._relay is not None and self._relay.poll() is None:
            return
        port = self._free_port()
        options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                   "encoding": "utf-8", "errors": "replace", "env": self._environment(),
                   "cwd": str(Path(self.relay_path).parent)}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = self._popen([self.relay_path, "relay", "-p", str(port)], **options)
        with self._lock:
            self._relay, self._port = process, port
        self._reader = threading.Thread(target=self._read_relay, args=(process,), name="usb-internet-relay-log", daemon=True)
        self._reader.start()
        if not self._wait_relay(process, port):
            self._stop_relay()
            raise _Failure("TETHER_RELAY_FAILED", "The USB internet relay did not become ready")
        self._emit("TETHER_RELAY_READY", "USB internet relay is ready", detail=f"loopback port {port}", kind="status")

    def _reverse_target(self, serial):
        code, output, error = self._adb(serial, ["reverse", "--list"], timeout=5)
        if code:
            raise _Failure("TETHER_START_FAILED", "Could not inspect this phone's USB tunnel", error)
        for line in output.splitlines():
            parts = line.split()
            if SOCKET_NAME in parts:
                index = parts.index(SOCKET_NAME)
                return parts[index + 1] if index + 1 < len(parts) else "unknown"
        return None

    def _vpn_active(self, serial):
        code, services, _ = self._adb(serial, ["shell", "dumpsys", "activity", "services", PACKAGE], timeout=4)
        if code or not re.search(r"ServiceRecord\{[^\n]*com\.genymobile\.gnirehtet/", services):
            return False
        code, connectivity, _ = self._adb(serial, ["shell", "dumpsys", "connectivity"], timeout=4)
        return code == 0 and self._has_vpn(connectivity)

    @staticmethod
    def _has_vpn(output):
        return bool(re.search(r"Transports:\s*VPN\b|\bTRANSPORT_VPN\b|NetworkAgentInfo[^\n]*\bVPN\b[^\n]*\bCONNECTED\b", output))

    @staticmethod
    def _port_listening(target):
        """True when something accepts connections on a tcp:PORT target."""
        match = re.fullmatch(r"tcp:(\d{1,5})", str(target or ""))
        if not match:
            return True              # not a plain local port: never assume stale
        try:
            with socket.create_connection(("127.0.0.1", int(match.group(1))), timeout=0.5):
                return True
        except OSError:
            return False

    def _gnirehtet_service(self, serial):
        code, services, _ = self._adb(serial, ["shell", "dumpsys", "activity", "services", PACKAGE], timeout=4)
        return code == 0 and bool(re.search(r"ServiceRecord\{[^\n]*com\.genymobile\.gnirehtet/", services))

    def _clear_stale(self, serial):
        """Remove only leftovers of an earlier FBVenom session whose relay is gone.

        A crash or a closed laptop lid leaves the phone with a reverse tunnel
        to a dead port and Gnirehtet's VPN still up - which used to make every
        later start refuse the phone forever. A tunnel whose port still
        answers belongs to a live relay (another tool) and is left untouched.
        """
        target = self._reverse_target(serial)
        if target is not None:
            if self._port_listening(target):
                raise _Failure("TETHER_MAPPING_IN_USE", "An existing Gnirehtet tunnel has been left untouched", target)
            self._adb(serial, ["reverse", "--remove", SOCKET_NAME], timeout=5)
            self._emit("TETHER_STALE_TUNNEL_REMOVED", "Removed a USB tunnel left by an earlier session",
                       serial, target, kind="status")
        code, connectivity, _ = self._adb(serial, ["shell", "dumpsys", "connectivity"], timeout=4)
        if code == 0 and self._has_vpn(connectivity):
            if not self._gnirehtet_service(serial):
                raise _Failure("TETHER_VPN_CONFLICT", "An active VPN has been left unchanged on this phone")
            # Gnirehtet's own VPN with no relay behind it: stop it first.
            self._command([self.relay_path, "stop", serial], timeout=8)
            self._emit("TETHER_STALE_VPN_STOPPED", "Stopped a USB internet VPN left by an earlier session",
                       serial, kind="status")

    def _preapprove_vpn(self, serial):
        """Install the client if needed, then pre-approve its VPN over ADB.

        ``appops set ... ACTIVATE_VPN allow`` is Android's own pre-consent
        switch (what VpnService.prepare() checks), so no OK tap is needed on
        each phone. If a ROM refuses it, the phone simply shows its prompt.
        """
        code, path, _ = self._adb(serial, ["shell", "pm", "path", PACKAGE], timeout=8)
        if code != 0 or "package:" not in path:
            self._device_state(serial, "installing", "Installing the USB internet client")
            code, output, error = self._command([self.relay_path, "install", serial], timeout=120)
            if code:
                raise _Failure("TETHER_INSTALL_FAILED", "The USB internet client could not be installed", error or output)
        code, output, error = self._adb(serial, ["shell", "appops", "set", PACKAGE, "ACTIVATE_VPN", "allow"], timeout=8)
        said = (output + " " + error).lower()
        if code or any(word in said for word in ("error", "exception", "unknown", "denied", "not allowed")):
            self._emit("TETHER_VPN_PREAPPROVE_FAILED", "Could not pre-approve the VPN on this phone",
                       serial, error or output, severity="warning")
            return False
        self._emit("TETHER_VPN_PREAPPROVED", "VPN permission pre-approved over USB debugging", serial, kind="status")
        return True

    def _wait_vpn(self, serial):
        deadline = time.monotonic() + self._vpn_wait
        while True:
            if self._vpn_active(serial):
                return True
            if time.monotonic() >= deadline or self._monitor_stop.is_set():
                return False
            time.sleep(1.0)

    def _start_device(self, row):
        serial = row["serial"]
        try:
            self._device_state(serial, "starting", "Checking the USB phone")
            code, sdk, _ = self._adb(serial, ["shell", "getprop", "ro.build.version.sdk"])
            if code or not sdk.strip().isdigit() or int(sdk.strip()) < 21:
                raise _Failure("TETHER_UNSUPPORTED_ANDROID", "This phone's Android version is unsupported")
            code, always_on, _ = self._adb(serial, ["shell", "settings", "get", "secure", "always_on_vpn_app"])
            if code == 0 and always_on.strip() not in ("", "null", PACKAGE):
                raise _Failure("TETHER_VPN_CONFLICT", "Another always-on VPN is configured on this phone")
            self._clear_stale(serial)
            if self.auto_approve_vpn:
                self._preapprove_vpn(serial)
            self._device_state(serial, "starting", "Starting the USB internet client")
            code, output, error = self._command([self.relay_path, "start", serial, "-p", str(self._port)], timeout=120)
            if code:
                raise _Failure("TETHER_START_FAILED", "USB internet could not start on this phone", error or output)
            if self._reverse_target(serial) != f"tcp:{self._port}":
                raise _Failure("TETHER_START_FAILED", "The phone's USB tunnel was not established")
            with self._lock:
                self._owned[serial] = self._port
            if self._wait_vpn(serial):
                self._device_state(serial, "vpn_active", "Online through the PC's internet")
                self._emit("TETHER_DEVICE_ONLINE", "Phone is using the PC's internet over USB", serial, kind="status")
            else:
                self._device_state(serial, "approval_required", "Waiting for Android VPN approval")
                self._emit("TETHER_VPN_APPROVAL_REQUIRED", "Approve the VPN connection on this phone", serial, severity="warning")
        except _Failure as exc:
            self._device_state(serial, "failed", str(exc))
            self._emit(exc.code, str(exc), serial, exc.detail, severity="error")
            # A failed helper may already have made our mapping. Claim only
            # that exact random port before attempting any cleanup.
            try:
                if self._reverse_target(serial) == f"tcp:{self._port}":
                    with self._lock:
                        self._owned[serial] = self._port
                    self._stop_device(serial)
                    self._device_state(serial, "failed", str(exc))
            except _Failure:
                pass

    def start_all(self):
        with self._operation():
            if self._closed:
                return self.snapshot()
            self._monitor_stop.clear()
            with self._lock:
                self._last_error = ""
            try:
                rows = self._discover_usb()
                eligible = []
                for row in rows:
                    if row["adb_state"] == "device":
                        with self._lock:
                            if row["serial"] not in self._owned:
                                eligible.append(row)
                    else:
                        code = "TETHER_DEVICE_UNAUTHORIZED" if row["adb_state"] == "unauthorized" else "TETHER_DEVICE_OFFLINE"
                        self._emit(code, "USB phone is not ready", row["serial"], severity="warning")
                if not eligible:
                    with self._lock:
                        owned = bool(self._owned)
                    if not owned:
                        self._emit("TETHER_NO_USB_DEVICES", "No authorized USB phones are available", severity="warning")
                    return self.snapshot()
                if not self._check_network().get("ok"):
                    return self.snapshot()
                try:
                    self._verify_helpers()
                except (OSError, ValueError, ImportError):
                    self._emit("TETHER_ASSETS_INVALID", "USB internet helper assets are missing or failed verification", severity="error")
                    return self.snapshot()
                self._start_relay()
                # Eight at once: ADB multiplexes fine and a 19-phone rack
                # comes up in about a minute instead of three.
                with ThreadPoolExecutor(max_workers=min(8, len(eligible))) as pool:
                    list(pool.map(self._start_device, eligible))
                with self._lock:
                    owned = bool(self._owned)
                if owned:
                    self._start_monitor()
                else:
                    self._stop_relay()
            except (_Failure, OSError) as exc:
                self._emit(getattr(exc, "code", "TETHER_RELAY_FAILED"), "USB internet could not start",
                           detail=getattr(exc, "detail", type(exc).__name__), severity="error")
            return self.snapshot()

    def _stop_device(self, serial):
        with self._lock:
            port = self._owned.get(serial)
        if port is None:
            return
        try:
            target = self._reverse_target(serial)
            if target not in (None, f"tcp:{port}"):
                self._emit("TETHER_OWNERSHIP_CHANGED", "Changed USB tunnel left untouched", serial, severity="warning")
                return
            code, output, error = self._command([self.relay_path, "stop", serial], timeout=8)
            if code:
                raise _Failure("TETHER_STOP_FAILED", "The phone's VPN could not be stopped", error or output)
            if self._reverse_target(serial) == f"tcp:{port}":
                code, _, error = self._adb(serial, ["reverse", "--remove", SOCKET_NAME], timeout=5)
                if code:
                    raise _Failure("TETHER_STOP_FAILED", "The phone's USB tunnel could not be removed", error)
            self._device_state(serial, "stopped", "USB internet stopped")
        except _Failure as exc:
            self._device_state(serial, "stop_failed", str(exc))
            self._emit("TETHER_STOP_FAILED", str(exc), serial, exc.detail, severity="error")
        finally:
            with self._lock:
                self._owned.pop(serial, None)
            self._vpn_checked.pop(serial, None)

    def _stop_relay(self):
        with self._lock:
            process, self._relay, self._port = self._relay, None, None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            self._emit("TETHER_RELAY_FAILED", "The owned USB relay did not stop cleanly", severity="error")

    def stop_all(self):
        self._monitor_stop.set()
        with self._operation():
            with self._lock:
                serials = list(self._owned)
            if serials:
                with ThreadPoolExecutor(max_workers=min(8, len(serials))) as pool:
                    list(pool.map(self._stop_device, serials))
            self._stop_relay()
            return self.snapshot()

    def _start_monitor(self):
        if self._monitor is not None and self._monitor.is_alive():
            return
        self._monitor = threading.Thread(target=self._monitor_loop, name="usb-internet-monitor", daemon=True)
        self._monitor.start()

    def _monitor_loop(self):
        while not self._monitor_stop.wait(2):
            if not self._operation_lock.acquire(blocking=False):
                continue
            try:
                self._monitor_once()
            except Exception as exc:
                self._emit("TETHER_RELAY_FAILED", "USB internet status check failed", detail=type(exc).__name__, severity="warning")
            finally:
                self._operation_lock.release()

    def _monitor_once(self):
        with self._lock:
            process, serials = self._relay, list(self._owned)
        if process is None:
            return
        if process.poll() is not None:
            self._emit("TETHER_RELAY_FAILED", "The USB internet relay exited unexpectedly", severity="error")
            for serial in serials:
                if self._monitor_stop.is_set():
                    break
                self._stop_device(serial)
            self._stop_relay()
            self._monitor_stop.set()
            return
        previous = self.snapshot()
        rows = self._discover_usb()
        present = {row["serial"]: row for row in rows}
        old = {row["serial"]: row for row in previous["devices"]}
        for serial in serials:
            if self._monitor_stop.is_set():
                break
            if serial not in present or present[serial]["adb_state"] != "device":
                if old.get(serial, {}).get("state") != "disconnected":
                    self._emit("TETHER_DEVICE_DISCONNECTED", "USB phone disconnected or is not ready", serial, severity="warning")
                continue
            target = self._reverse_target(serial)
            restored = False
            if target is None:
                code, _, error = self._command([self.relay_path, "tunnel", serial, "-p", str(self._port)], timeout=8)
                if code:
                    self._emit("TETHER_START_FAILED", "Could not restore the USB tunnel", serial, error, severity="warning")
                    continue
                restored = True
            elif target != f"tcp:{self._port}":
                self._emit("TETHER_OWNERSHIP_CHANGED", "Changed USB tunnel left untouched", serial, severity="warning")
                with self._lock:
                    self._owned.pop(serial, None)
                self._device_state(serial, "unmanaged", "Another tool owns this USB tunnel")
                continue
            # The tunnel check above is one cheap call and runs every pass. The
            # VPN check is two `dumpsys` runs ON the phone; doing them every
            # pass for every phone kept low-end phones busy the whole session,
            # right while Facebook Lite was loading posts on them.
            now = time.monotonic()
            if not restored and now - self._vpn_checked.get(serial, float("-inf")) < VPN_RECHECK_SECONDS:
                continue
            self._vpn_checked[serial] = now
            active = self._vpn_active(serial)
            self._device_state(serial, "vpn_active" if active else "approval_required",
                               "Online through the PC's internet" if active else "Waiting for Android VPN approval")

    def snapshot(self):
        with self._lock:
            return {"running": self._relay is not None and self._relay.poll() is None,
                    "busy": self._busy, "relay_port": self._port, "devices": deepcopy(list(self._devices.values())),
                    "last_error": self._last_error, "network": deepcopy(self._network)}

    def close(self):
        self.stop_all()
        self._closed = True
        if self._callback:
            try:
                self._events.put_nowait(None)
            except queue.Full:
                pass
        for handler in self._logger.handlers:
            handler.close()
