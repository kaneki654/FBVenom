# -*- coding: utf-8 -*-
"""AI endpoint configuration for FBVenom.

FBPoison hard-codes one Groq URL, key and model in core.py. This app is the
commenting tool, so the AI is the whole point and it must be changeable
WITHOUT editing source: the endpoint, the key, the model and the fallback all
live in venom_ai.json next to the program and are editable from the UI.

Every provider here speaks the OpenAI chat-completions shape
(POST {"model", "messages", "temperature", "max_tokens"} -> choices[0].message
.content), which is what core._groq_chat already sends, so switching provider
is genuinely just a URL + key + model.
"""
from copy import deepcopy
import http.client
import ipaddress
import json
import math
import os
import socket
import stat
import sys
import tempfile
import time
import threading
import urllib.request
from urllib.parse import urlsplit, urlunsplit

# A one-file PyInstaller build imports modules from its temporary _MEIPASS
# directory. Settings must live beside the real EXE instead, or every launch
# reads a throwaway copy and the AI Settings dialog cannot persist changes.
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "venom_ai.json")

LAN_DEFAULT_URL = "http://192.168.100.129:7499/v1/chat/completions"
LOCAL_LAN_KEY_FILE = "~/.config/aigw/lan.key"

# Known endpoints, all OpenAI-compatible. "label" is what the UI shows.
PRESETS = {
    "groq": {
        "label": "Groq",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "groq/compound-mini",
        "fallback": "qwen/qwen3.8-27b",
        "keys_url": "https://console.groq.com/keys",
        # 19 phones share ONE free-tier key: this is the fleet-wide gap
        # between AI calls, and too small a value just buys 429s.
        "min_gap": 4.0,
    },
    "openai": {
        "label": "OpenAI",
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "fallback": "gpt-4o-mini",
        "keys_url": "https://platform.openai.com/api-keys",
        "min_gap": 1.0,
    },
    "openrouter": {
        "label": "OpenRouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "meta-llama/llama-3.3-70b-instruct",
        "fallback": "meta-llama/llama-3.1-8b-instruct",
        "keys_url": "https://openrouter.ai/keys",
        "min_gap": 1.5,
    },
    "together": {
        "label": "Together AI",
        "url": "https://api.together.xyz/v1/chat/completions",
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "fallback": "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "keys_url": "https://api.together.ai/settings/api-keys",
        "min_gap": 1.5,
    },
    "deepseek": {
        "label": "DeepSeek",
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "fallback": "deepseek-chat",
        "keys_url": "https://platform.deepseek.com/api_keys",
        "min_gap": 1.0,
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        # Anthropic's OpenAI-compatibility endpoint: same {"model","messages",
        # "max_tokens"} body and choices[0].message.content reply as every other
        # provider here, and it authenticates with "Authorization: Bearer <key>"
        # exactly like _groq_chat already sends. The native /v1/messages API is
        # NOT OpenAI-shaped, so this compat URL is the one that works unchanged.
        "url": "https://api.anthropic.com/v1/chat/completions",
        "model": "claude-sonnet-5",
        "fallback": "claude-haiku-4-5-20251001",
        "keys_url": "https://console.anthropic.com/settings/keys",
        "min_gap": 1.0,
    },
    "local": {
        "label": "Local (Ollama / LM Studio)",
        "url": "http://127.0.0.1:11434/v1/chat/completions",
        "model": "llama3.1",
        "fallback": "llama3.1",
        "keys_url": "",
        "min_gap": 0.0,          # your own machine: no rate limit needed
    },
    "lan": {
        "label": "LAN gateway",
        "url": LAN_DEFAULT_URL,
        "model": "llama-sea-lion",
        "fallback": "llama-sea-lion",
        "keys_url": "",
        "min_gap": 1.0,
    },
    "custom": {
        "label": "Custom endpoint",
        "url": "",
        "model": "",
        "fallback": "",
        "keys_url": "",
        "min_gap": 1.5,
    },
}

DEFAULTS = {
    "server_mode": "primary",
    "primary_profile": {},
    "lan_profile": {},
    "provider": "groq",
    "url": PRESETS["groq"]["url"],
    "api_key": "",
    "model": PRESETS["groq"]["model"],
    "fallback_model": PRESETS["groq"]["fallback"],
    "min_gap": PRESETS["groq"]["min_gap"],
    # Emotion the comment is delivered with. "neutral" keeps the
    # example's own feeling and is a true no-op; "random" picks a
    # different one per comment. Names come from core.COMMENT_TONES.
    "tone": "neutral",
    # Read the post before commenting on it: the caption off the phone screen
    # plus a look at the picture, once per link for the whole fleet. Long
    # captions may require multiple frames. Off
    # Post reading was REMOVED. The example comments are now the subject as
    # well as the voice, so read_post / vision_model / post_crop /
    # max_caption_frames are gone from both the config and the UI. The
    # vision code in core.py is left intact but unreachable.
    "temperature": 0.9,
    "max_tokens": 40,
    # A self-hosted model answers a real comment prompt in 2-8s once warm but
    # 40-120s cold. 20s made every cold spell a wall of timeouts.
    "timeout": 90,
    # Optional Pollinations token (auth.pollinations.ai, free registration).
    # Anonymous is one request every 15s; a token moves you to the Seed tier at
    # one every 5s. Kept separate from api_key so it survives switching provider
    # - the keyless Pollinations path is the fallback for every provider.
    "pollinations_token": "",
}

_CONNECTION_FIELDS = ("provider", "url", "api_key", "model", "fallback_model", "min_gap", "timeout")
_PRIVATE_V4 = tuple(ipaddress.ip_network(value) for value in
                    ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_lock = threading.RLock()
_cache = None


def _connection(cfg):
    return {name: deepcopy(cfg.get(name, DEFAULTS[name])) for name in _CONNECTION_FIELDS}


def get_server_mode(cfg=None):
    cfg = load() if cfg is None else cfg
    return "lan" if cfg.get("server_mode") == "lan" else "primary"


def get_lan_profile(cfg=None):
    cfg = load() if cfg is None else cfg
    profile = {"provider": "lan", "url": LAN_DEFAULT_URL, "api_key": "",
               "model": "llama-sea-lion", "fallback_model": "llama-sea-lion",
               "min_gap": 1.0, "timeout": 300}
    saved = cfg.get("lan_profile")
    if not isinstance(saved, dict) or not saved:
        candidate = cfg.get("model")
        if (isinstance(candidate, str) and candidate.strip()
                and all(33 <= ord(c) <= 126 for c in candidate.strip())):
            profile["model"] = profile["fallback_model"] = candidate.strip()
    if isinstance(saved, dict):
        profile.update({name: deepcopy(saved[name]) for name in _CONNECTION_FIELDS if name in saved})
    if get_server_mode(cfg) == "lan":
        profile.update({name: deepcopy(cfg[name]) for name in _CONNECTION_FIELDS if name in cfg})
    profile["provider"] = "lan"
    if not profile.get("fallback_model"):
        profile["fallback_model"] = profile.get("model") or "llama-sea-lion"
    if not str(profile.get("api_key") or "").strip():
        profile["api_key"] = get_local_lan_key(profile.get("url"))
    return profile


def get_local_lan_key(url):
    """Read this computer's private gateway key only for its own LAN address.

    A local bind checks ownership of the numeric address without connecting to
    the gateway or sending a packet. Remote LAN computers must supply their
    own key explicitly. Unsupported permission checks fail closed.
    """
    if not all(hasattr(os, name) for name in ("getuid", "O_NOFOLLOW", "O_NONBLOCK")):
        return ""
    try:
        checked = build_lan_profile(url, "x" * 32, "local-key-check")
        address = urlsplit(checked["url"]).hostname
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((address, 0))
        path = os.path.expanduser(LOCAL_LAN_KEY_FILE)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1
                    or info.st_size > 4096):
                return ""
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        if len(raw) > 4096:
            return ""
        token = raw.decode("ascii").strip()
        if len(token) < 32 or any(not 33 <= ord(c) <= 126 for c in token):
            return ""
        return token
    except (OSError, ValueError, TypeError):
        return ""


def build_lan_profile(url, key, model, fallback_model=""):
    """Normalize a private numeric LAN address without borrowing any hosted key."""
    try:
        parsed = urlsplit(str(url or "").strip())
        address = ipaddress.ip_address(parsed.hostname or "")
        parsed_port = parsed.port
        path = parsed.path.rstrip("/")
        if (parsed.scheme not in ("http", "https") or address.version != 4
                or not (address.is_loopback or any(address in network for network in _PRIVATE_V4))
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or path not in ("", "/v1", "/chat/completions", "/v1/chat/completions")
                or (parsed_port is not None and not 1 <= parsed_port <= 65535)):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Enter an http(s) URL with a private numeric IPv4 address and optional port") from None
    if (not isinstance(key, str) or len(key.strip()) < 32
            or any(not 33 <= ord(character) <= 126 for character in key.strip())):
        raise ValueError("Enter the separate LAN API key (at least 32 visible ASCII characters)")
    def model_name(value):
        if not isinstance(value, str) or not value.strip() or any(not 33 <= ord(c) <= 126 for c in value.strip()):
            raise ValueError("Choose a nonempty model ID returned by the LAN gateway")
        return value.strip()
    model = model_name(model)
    fallback_model = model_name(fallback_model) if fallback_model else model
    authority = str(address) + (f":{parsed_port}" if parsed_port is not None else "")
    canonical = urlunsplit((parsed.scheme, authority, "/v1/chat/completions", "", ""))
    return {"provider": "lan", "url": canonical, "api_key": key.strip(),
            "model": model, "fallback_model": fallback_model, "min_gap": 1.0, "timeout": 300}


def _prepared(cfg, previous=None, strict=True):
    merged = deepcopy(DEFAULTS)
    merged.update({name: deepcopy(value) for name, value in (cfg or {}).items()
                   if name in DEFAULTS and value is not None})
    mode = "lan" if merged.get("provider") == "lan" else "primary"
    merged["server_mode"] = mode
    if mode == "lan":
        if previous and get_server_mode(previous) == "primary":
            primary = _connection(previous)
        else:
            primary = merged.get("primary_profile")
            primary = _connection(primary) if isinstance(primary, dict) and primary else _connection(DEFAULTS)
        profile = _connection(merged)
        if strict:
            validated = build_lan_profile(profile["url"], profile["api_key"], profile["model"], profile["fallback_model"])
            if primary.get("api_key") and validated["api_key"] == primary["api_key"]:
                raise ValueError("Use the separate LAN API key; the primary provider key cannot be reused")
            for name in ("min_gap", "timeout"):
                value = profile.get(name, validated[name])
                try:
                    number = float(value)
                    if not math.isfinite(number) or (number < 0 if name == "min_gap" else number <= 0):
                        raise ValueError
                except (TypeError, ValueError):
                    raise ValueError("LAN request timing must be finite and positive") from None
                validated[name] = number
            profile = validated
        merged.update(deepcopy(profile))
        merged["primary_profile"] = deepcopy(primary)
        merged["lan_profile"] = deepcopy(profile)
    else:
        lan = merged.get("lan_profile")
        prior_lan = get_lan_profile(previous) if previous else {}
        lan_key = lan.get("api_key") if isinstance(lan, dict) else None
        if strict and merged.get("api_key") and merged["api_key"] in (
                lan_key, prior_lan.get("api_key")):
            raise ValueError("Enter the primary provider's API key; the separate LAN key cannot be reused")
        merged["primary_profile"] = _connection(merged)
        if not isinstance(merged.get("lan_profile"), dict):
            merged["lan_profile"] = {}
    return merged


def load(force=False):
    """Return independent copies; credentials for LAN never fall back to hosted environment keys."""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return deepcopy(_cache)
        cfg = deepcopy(DEFAULTS)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                cfg.update({name: deepcopy(value) for name, value in saved.items()
                            if name in DEFAULTS and value is not None})
        except FileNotFoundError:
            pass
        except Exception:
            print("  [ai-config] Settings are unreadable; using defaults")
        cfg = _prepared(cfg, strict=False)
        if get_server_mode(cfg) == "primary" and not str(cfg.get("api_key") or "").strip():
            cfg["api_key"] = os.environ.get("VENOM_API_KEY", "") or os.environ.get("GROQ_API_KEY", "")
            cfg["primary_profile"] = _connection(cfg)
        _cache = deepcopy(cfg)
        return deepcopy(cfg)


def save(cfg):
    """Atomically persist private settings before making them active in this process."""
    global _cache
    with _lock:
        previous = load()
        merged = _prepared(cfg, previous=previous)
        directory = os.path.dirname(os.path.abspath(CONFIG_PATH))
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".venom-ai-", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, CONFIG_PATH)
            temporary = None
        except Exception:
            raise RuntimeError("Could not save AI settings; check that the settings directory is writable") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        _cache = deepcopy(merged)
        return deepcopy(merged)


def set_server_mode(mode, lan_profile=None):
    with _lock:
        if mode not in ("primary", "lan"):
            raise ValueError("Server mode must be primary or lan")
        cfg = load()
        if mode == "lan":
            profile = get_lan_profile(cfg) if lan_profile is None else deepcopy(lan_profile)
            if not isinstance(profile, dict):
                raise ValueError("LAN profile must be an object")
            profile = build_lan_profile(profile.get("url"), profile.get("api_key"),
                                        profile.get("model"), profile.get("fallback_model", ""))
            if get_server_mode(cfg) == "primary":
                cfg["primary_profile"] = _connection(cfg)
            cfg.update(deepcopy(profile))
            cfg["lan_profile"] = deepcopy(profile)
        elif get_server_mode(cfg) == "lan":
            primary = cfg.get("primary_profile")
            cfg.update(_connection(primary if isinstance(primary, dict) and primary else DEFAULTS))
        cfg["server_mode"] = mode
        return save(cfg)



def _lan_json_get(url, key, deadline):
    """Numeric private host, direct socket, no proxies, and no redirect following."""
    parsed = urlsplit(url)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(parsed.hostname, parsed.port, timeout=remaining)
    headers = {"Accept": "application/json", "User-Agent": "FBVenom-LAN-check"}
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        connection.request("GET", parsed.path, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            if response.status in (401, 403):
                raise RuntimeError("LAN gateway rejected the API key or this client's network address")
            raise RuntimeError(f"LAN gateway check failed (HTTP {response.status})")
        content = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            # The response can retain the socket after HTTPConnection clears
            # its reference for a Connection: close response.
            sock = connection.sock or getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(remaining)
            chunk = response.read1(min(16384, 1024 * 1024 + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > 1024 * 1024:
                raise RuntimeError("LAN gateway returned an oversized response")
        return json.loads(content.decode("utf-8"))
    finally:
        connection.close()


def validate_lan_connection(profile):
    if not isinstance(profile, dict):
        raise ValueError("LAN profile must be an object")
    checked = build_lan_profile(profile.get("url"), profile.get("api_key"),
                                profile.get("model"), profile.get("fallback_model", ""))
    parts = urlsplit(checked["url"])
    base = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    deadline = time.monotonic() + 15.0
    try:
        health = _lan_json_get(base + "/health", "", deadline)
        if not isinstance(health, dict) or not (health.get("status") == "ok" or health.get("ok") is True):
            raise RuntimeError("LAN model gateway is not ready")
        data = _lan_json_get(base + "/v1/models", checked["api_key"], deadline)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise RuntimeError("LAN gateway returned an invalid model list")
        ids = list(dict.fromkeys(item["id"] for item in data["data"]
                   if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]))
        if not ids:
            raise RuntimeError("LAN gateway returned no models")
        return ids
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("Cannot validate the LAN gateway; check its address, network, certificate and API key") from None


def apply_preset(name, keep_key=True):
    """Apply endpoint defaults while keeping primary and LAN credentials separate."""
    p = PRESETS.get(name)
    if not p:
        return load()
    if name == "lan":
        profile = get_lan_profile()
        if not keep_key:
            profile["api_key"] = ""
        return set_server_mode("lan", profile)
    cur = load()
    cfg = deepcopy(cur)
    cfg.update({"provider": name, "url": p["url"], "model": p["model"],
                "fallback_model": p["fallback"], "min_gap": p["min_gap"]})
    if get_server_mode(cur) == "lan":
        primary = cur.get("primary_profile") or {}
        cfg["api_key"] = primary.get("api_key", "") if keep_key and name == primary.get("provider") else ""
        cfg["timeout"] = primary.get("timeout", DEFAULTS["timeout"])
    elif not keep_key:
        cfg["api_key"] = ""
    return save(cfg)


def describe():
    cfg = load()
    mode = "LAN" if get_server_mode(cfg) == "lan" else "Primary"
    label = PRESETS.get(cfg.get("provider"), {}).get("label", cfg.get("provider", ""))
    tone = str(cfg.get("tone") or "neutral")
    suffix = "" if tone == "neutral" else f"  tone={tone}"
    return f"{mode} · {label}  model={cfg.get('model', '')}{suffix}"


def is_ready():
    cfg = load()
    if not str(cfg.get("url") or "").strip():
        return False
    if cfg.get("provider") == "local" and get_server_mode(cfg) != "lan":
        return True
    return bool(str(cfg.get("api_key") or "").strip())


def _models_url(chat_url):
    """Turn a .../chat/completions URL into the sibling .../models URL."""
    u = str(chat_url or "").strip()
    if u.endswith("/chat/completions"):
        return u[: -len("/chat/completions")] + "/models"
    if u.endswith("/"):
        return u + "models"
    return u.rsplit("/", 1)[0] + "/models"


def list_models(cfg=None, timeout=20):
    """Ask the endpoint which models it serves and return their IDs.

    Every OpenAI-compatible /v1/models endpoint answers {"data":[{"id":...}]}
    to a Bearer request. Anthropic is the exception: its models endpoint wants
    x-api-key + anthropic-version headers instead of Bearer, so branch on that.
    Returns a list of model-id strings (may be empty); raises on a hard error so
    the caller can show why the list is unavailable.
    """
    cfg = cfg or load()
    if cfg.get("provider") == "lan" or get_server_mode(cfg) == "lan":
        return validate_lan_connection(cfg)
    url = str(cfg.get("url") or "").strip()
    key = str(cfg.get("api_key") or "").strip()
    provider = str(cfg.get("provider") or "")
    if not url:
        raise RuntimeError("set the API URL first")
    models_url = _models_url(url)
    if provider == "anthropic" or "api.anthropic.com" in url:
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        headers = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"
    headers["User-Agent"] = "Mozilla/5.0"
    req = urllib.request.Request(models_url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    items = data.get("data") if isinstance(data, dict) else data
    ids = []
    for m in (items or []):
        mid = m.get("id") if isinstance(m, dict) else m
        if mid:
            ids.append(str(mid))
    return ids
