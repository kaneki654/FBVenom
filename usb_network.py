"""Read-only verification of the PC Wi-Fi route used by USB reverse tethering.

Gnirehtet uses the computer's normal routes. Merely having Wi-Fi connected is
insufficient when Ethernet or a VPN has the preferred default route. This
module never changes adapters, route metrics, proxy settings, or DNS.
"""

import json
import math
import os
import platform
import queue
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request

_ROUTE_TIMEOUT = 4.0
_PROBE_TIMEOUT = 4.0
_CHECK_TIMEOUT = 9.0
_PROBE_URL = "https://www.gstatic.com/generate_204"
_probe_lock = threading.Lock()

# Route selection is RouteMetric + InterfaceMetric on Windows. Only adapter
# metadata is requested: no wireless profiles, SSIDs, or passwords are read.
_WINDOWS_ROUTES = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$interfaces = @(Get-NetIPInterface -AddressFamily IPv4)
$adapters = @(Get-NetAdapter -IncludeHidden)
$rows = @(foreach ($route in @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -PolicyStore ActiveStore)) {
    $ipif = $interfaces | Where-Object { $_.InterfaceIndex -eq $route.InterfaceIndex } | Select-Object -First 1
    $adapter = $adapters | Where-Object { $_.ifIndex -eq $route.InterfaceIndex } | Select-Object -First 1
    [pscustomobject]@{
        InterfaceAlias = [string]$route.InterfaceAlias
        InterfaceIndex = [int]$route.InterfaceIndex
        RouteMetric = [int]$route.RouteMetric
        InterfaceMetric = $(if ($null -ne $ipif) { [int]$ipif.InterfaceMetric } else { [int]$route.InterfaceMetric })
        ConnectionState = [string]$ipif.ConnectionState
        AdapterStatus = [string]$adapter.Status
        NdisPhysicalMedium = [string]$adapter.NdisPhysicalMedium
        PhysicalMediaType = [string]$adapter.PhysicalMediaType
        InterfaceDescription = [string]$adapter.InterfaceDescription
        Virtual = [bool]$adapter.Virtual
    }
})
ConvertTo-Json -InputObject $rows -Depth 4 -Compress
"""


class _CheckFailure(Exception):
    def __init__(self, code, message, detail="", hint=""):
        self.code, self.message, self.detail, self.hint = code, message, detail, hint
        super().__init__(message)


def _result(ok, code, message, detail="", hint="", interface="", kind=""):
    return {"ok": bool(ok), "code": code, "message": message, "detail": detail,
            "hint": hint, "interface": interface, "kind": kind}


def _interface_name(value):
    return "".join(c for c in str(value or "") if c.isprintable())[:80]


def _metric(value):
    if isinstance(value, bool):
        raise ValueError("Invalid route metric")
    number = float(value)
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        raise ValueError("Invalid route metric")
    return int(number)


def _windows_kind(row):
    if row.get("Virtual") is True:
        return "other"
    medium = str(row.get("NdisPhysicalMedium") or "").lower()
    physical = str(row.get("PhysicalMediaType") or "").lower()
    if medium in ("1", "9") or any(value in medium + " " + physical for value in
            ("802.11", "802_11", "wirelesslan", "wireless lan")):
        return "wifi"
    if medium == "14" or any(value in medium + " " + physical for value in ("802.3", "802_3", "ethernet")):
        return "ethernet"
    # Older drivers sometimes omit the physical-medium property. Accept Wi-Fi
    # adapter names/descriptions, while excluding mobile and virtual adapters.
    label = " ".join(str(row.get(name) or "") for name in ("InterfaceAlias", "InterfaceDescription")).lower()
    if any(word in label for word in ("vpn", "virtual", "wwan", "cellular", "mobile broadband")):
        return "other"
    if re.search(r"wi[-_ ]?fi|wlan|802[._]11|wireless", label):
        return "wifi"
    if re.search(r"ethernet|802[._]3", label):
        return "ethernet"
    return "unknown"


def _windows_routes(payload):
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("Invalid route snapshot")
    routes = []
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError("Invalid route record")
        name = _interface_name(row.get("InterfaceAlias"))
        state = str(row.get("ConnectionState") or "").lower()
        status = str(row.get("AdapterStatus") or "").lower()
        active = (state in ("connected", "1") or status == "up") and state not in (
            "disconnected", "0") and status not in ("down", "disabled", "disconnected", "not present")
        routes.append({"interface": name, "kind": _windows_kind(row), "active": active,
                       "metric": _metric(row.get("RouteMetric")) + _metric(row.get("InterfaceMetric"))})
    return routes


def _linux_kind(name):
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", name):
        return "unknown"
    if os.path.isdir(os.path.join("/sys/class/net", name, "wireless")) or name.lower().startswith(("wl", "wifi")):
        return "wifi"
    if name.lower().startswith(("en", "eth")):
        return "ethernet"
    return "other"


def _linux_routes(payload):
    if not isinstance(payload, list):
        raise ValueError("Invalid route snapshot")
    routes = []
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError("Invalid route record")
        if row.get("dst", "default") not in ("default", "0.0.0.0/0"):
            continue
        if row.get("type", "unicast") not in ("unicast", ""):
            continue
        nexthops = row.get("nexthops") or [row]
        if not isinstance(nexthops, list):
            raise ValueError("Invalid multipath route")
        for hop in nexthops:
            if not isinstance(hop, dict):
                raise ValueError("Invalid route next hop")
            name = _interface_name(hop.get("dev", row.get("dev")))
            flags = row.get("flags", []) + hop.get("flags", [])
            routes.append({"interface": name, "kind": _linux_kind(name),
                           "metric": _metric(row.get("metric", 0)),
                           "active": "linkdown" not in flags and "dead" not in flags})
    return routes


def _route_snapshot():
    """Return normalized default routes; subprocess time is bounded to four seconds."""
    system = platform.system()
    if system == "Windows":
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_ROUTES]
        options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    elif system == "Linux":
        command = ["ip", "-j", "-4", "route", "show", "default"]
        options = {}
    else:
        raise _CheckFailure("TETHER_PC_OS_UNSUPPORTED", "Wi-Fi route checking is unavailable on this operating system.",
                            hint="Use Windows or Linux for USB Internet sharing.")
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8-sig",
                                   errors="replace", timeout=_ROUTE_TIMEOUT, check=False, **options)
    except subprocess.TimeoutExpired:
        raise _CheckFailure("TETHER_PC_ROUTE_TIMEOUT", "The PC network route check timed out.",
                            hint="Check the network adapters and try again.") from None
    except OSError:
        raise _CheckFailure("TETHER_PC_ROUTE_UNAVAILABLE", "The PC network route information could not be read.",
                            hint="Check that PowerShell networking tools or the Linux ip command are available.") from None
    if completed.returncode != 0:
        raise _CheckFailure("TETHER_PC_ROUTE_UNAVAILABLE", "The PC network route information could not be read.",
                            hint="Check the network adapters and try again.")
    try:
        if len(completed.stdout) > 256 * 1024:
            raise ValueError("Oversized route snapshot")
        text = completed.stdout.lstrip("\ufeff").strip()
        payload = json.loads(text) if text else []
        routes = _windows_routes(payload) if system == "Windows" else _linux_routes(payload)
    except (TypeError, ValueError, KeyError):
        raise _CheckFailure("TETHER_PC_ROUTE_UNAVAILABLE", "The PC network route information was incomplete.",
                            hint="Check the network adapters and try again.") from None
    return {"platform": system.lower(), "routes": routes}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _probe_failure(error):
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    if isinstance(error, socket.gaierror):
        return _result(False, "TETHER_PC_DNS_FAILED", "The Wi-Fi Internet check could not resolve its HTTPS test address.",
                       hint="Check the Wi-Fi connection and its DNS settings, then try again.")
    if isinstance(error, ssl.SSLError):
        return _result(False, "TETHER_PC_TLS_FAILED", "The HTTPS Internet check could not be verified securely.",
                       hint="Check the PC date, time, and network sign-in. Certificate verification remains enabled.")
    if isinstance(error, (TimeoutError, socket.timeout)):
        return _result(False, "TETHER_PC_INTERNET_TIMEOUT", "The Wi-Fi Internet check timed out.",
                       hint="Check the Wi-Fi connection or complete its browser sign-in, then try again.")
    return _result(False, "TETHER_PC_INTERNET_UNAVAILABLE", "The Wi-Fi Internet connection could not be verified.",
                   hint="Open a browser on the PC, check Internet access, and complete any network sign-in.")


def _perform_probe(timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(_PROBE_URL, headers={"User-Agent": "FBVenom-USB-Internet-check",
                                                        "Cache-Control": "no-cache"})
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            final = response.geturl()
            if status == 204 and final == _PROBE_URL:
                return _result(True, "TETHER_PC_WIFI_READY", "The PC Wi-Fi Internet connection is ready.")
            return _result(False, "TETHER_PC_CAPTIVE_PORTAL", "The Wi-Fi network did not pass the Internet sign-in check.",
                           hint="Open a browser on the PC and complete any Wi-Fi sign-in, then try again.")
    except urllib.error.HTTPError as error:
        if error.code in (200, 301, 302, 303, 307, 308, 401, 403, 511):
            return _result(False, "TETHER_PC_CAPTIVE_PORTAL", "The Wi-Fi network may require browser sign-in.",
                           hint="Open a browser on the PC and complete any Wi-Fi sign-in, then try again.")
        return _probe_failure(error)
    except (OSError, ValueError, urllib.error.URLError) as error:
        return _probe_failure(error)


def _internet_probe(timeout=_PROBE_TIMEOUT):
    """Bound even stalled OS DNS resolution by waiting on a daemon worker."""
    timeout = min(_PROBE_TIMEOUT, max(0.01, float(timeout)))
    if not _probe_lock.acquire(blocking=False):
        return _result(False, "TETHER_PC_CHECK_BUSY", "A previous Internet check is still finishing.",
                       hint="Wait a moment and check the PC connection again.")
    answers = queue.Queue(maxsize=1)
    def work():
        try:
            try:
                answer = _perform_probe(timeout)
            except Exception:
                answer = _probe_failure(None)
            answers.put_nowait(answer)
        finally:
            _probe_lock.release()
    threading.Thread(target=work, daemon=True).start()
    try:
        return answers.get(timeout=timeout)
    except queue.Empty:
        return _probe_failure(TimeoutError())


def check_pc_internet():
    """Return a sanitized diagnostic dict without changing the PC network."""
    deadline = time.monotonic() + _CHECK_TIMEOUT
    interface, kind = "", ""
    try:
        snapshot = _route_snapshot()
        routes = snapshot["routes"]
        active = [route for route in routes if route.get("active")]
        if not active:
            return _result(False, "TETHER_PC_NO_DEFAULT_ROUTE", "The PC has no active IPv4 Internet default route.",
                           hint="Connect the PC to Wi-Fi with Internet access, then check again.")
        if any(not route.get("interface") for route in active):
            return _result(False, "TETHER_PC_INTERFACE_UNKNOWN", "The Internet route's network adapter could not be identified.",
                           hint="Check the PC network adapters and try again.")
        active.sort(key=lambda route: (route["metric"], route["interface"]))
        preferred = [route for route in active if route["metric"] == active[0]["metric"]]
        # Phones share whatever route the PC really uses. Wi-Fi is what the
        # user asked for, but Ethernet works just as well (usually better), so
        # a non-Wi-Fi route is reported - never refused.
        selected = next((route for route in preferred if route["kind"] != "wifi"), preferred[0])
        interface, kind = selected["interface"], selected["kind"]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _result(False, "TETHER_PC_INTERNET_TIMEOUT", "The PC Internet check timed out.", interface=interface, kind=kind)
        result = _internet_probe(timeout=min(_PROBE_TIMEOUT, remaining))
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ValueError("Invalid probe result")
        result = {**_result(False, "TETHER_PC_INTERNET_UNAVAILABLE", "The PC Internet connection could not be verified."),
                  **result, "interface": interface, "kind": kind}
        if result["ok"] and kind != "wifi":
            label = {"ethernet": "Ethernet"}.get(kind, "a non-Wi-Fi adapter")
            result.update(code="TETHER_PC_INTERNET_READY",
                          message=f"The PC Internet connection is ready ({label}: {interface}).",
                          detail="Phones will use this connection. " +
                                 ("Multiple default routes share the preferred metric. " if len(preferred) > 1 else ""),
                          hint="To share Wi-Fi instead, make Wi-Fi the PC's preferred connection or unplug "
                               "Ethernet, then start USB internet again. FBVenom never changes route priorities.")
        elif result["ok"]:
            result["message"] = f"The PC Wi-Fi Internet connection is ready ({interface})."
        return result
    except _CheckFailure as error:
        return _result(False, error.code, error.message, error.detail, error.hint, interface, kind)
    except Exception:
        return _result(False, "TETHER_PC_CHECK_FAILED", "The PC Wi-Fi connection could not be checked.",
                       hint="Check the network adapters and try again.", interface=interface, kind=kind)
