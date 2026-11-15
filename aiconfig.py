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
import json
import os
import sys
import threading
import urllib.request

# A one-file PyInstaller build imports modules from its temporary _MEIPASS
# directory. Settings must live beside the real EXE instead, or every launch
# reads a throwaway copy and the AI Settings dialog cannot persist changes.
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "venom_ai.json")

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

_lock = threading.Lock()
_cache = None


def load(force=False):
    """Read venom_ai.json, filling anything missing from DEFAULTS."""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return dict(_cache)
        cfg = dict(DEFAULTS)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                for k, v in saved.items():
                    if k in cfg and v is not None:
                        cfg[k] = v
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"  [ai-config] {CONFIG_PATH} unreadable ({e}); using defaults")
        # env var still wins when no key was saved, so a shared machine can
        # keep the key out of the file entirely
        if not str(cfg.get("api_key") or "").strip():
            cfg["api_key"] = os.environ.get("VENOM_API_KEY", "") \
                or os.environ.get("GROQ_API_KEY", "")
        _cache = dict(cfg)
        return dict(cfg)


def save(cfg):
    """Persist and return the merged config."""
    global _cache
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in (cfg or {}).items() if k in DEFAULTS})
    with _lock:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, indent=2)
        except Exception as e:
            print(f"  [ai-config] could not write {CONFIG_PATH}: {e}")
        _cache = dict(merged)
    return dict(merged)


def apply_preset(name, keep_key=True):
    """Switch provider, keeping the existing key unless told otherwise."""
    p = PRESETS.get(name)
    if not p:
        return load()
    cur = load()
    cfg = dict(cur)
    cfg.update({"provider": name, "url": p["url"], "model": p["model"],
                "fallback_model": p["fallback"], "min_gap": p["min_gap"]})
    if not keep_key:
        cfg["api_key"] = ""
    return save(cfg)


def describe():
    cfg = load()
    key = str(cfg.get("api_key") or "")
    shown = (key[:6] + "..." + key[-4:]) if len(key) > 12 else ("(none)" if not key else "set")
    t = str(cfg.get("tone") or "neutral")
    tone = "" if t == "neutral" else f"  tone={t}"
    return (f"{PRESETS.get(cfg['provider'], {}).get('label', cfg['provider'])}"
            f"  model={cfg['model']}  key={shown}{tone}")


def is_ready():
    """Local endpoints need no key; hosted ones do."""
    cfg = load()
    if not str(cfg.get("url") or "").strip():
        return False
    if cfg.get("provider") == "local":
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
