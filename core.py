# fbPoison/fb_core.py
# Core Facebook automation logic, ADB helpers, AI generation, and screenshot.
# Screenshot now uses multiple fallbacks + detailed logging.

import subprocess
import os
import sys
import time
import glob
import re
import html
import random
import json
import threading
import concurrent.futures
import http.client
import io
import socket
import ssl
import urllib.request
import urllib.error
import urllib.parse
import urllib.response
from typing import Any
import xml.etree.ElementTree as ET
import uiautomator2 as u2

def _utf8_console():
    """Make stdout/stderr survive emoji.

    Account names and captions routinely contain emoji, and this console is
    cp1252. Printing a name like "Ana 💀" raised UnicodeEncodeError from inside
    print(), which killed that device's worker thread mid-run. Replace rather
    than raise: a log line is never worth losing a phone over.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_utf8_console()


def _silence_subprocess_windows():
    if sys.platform != "win32" or getattr(subprocess, "_no_window_patched", False):
        return
    _orig_init = subprocess.Popen.__init__
    def _init(self, *args, **kwargs):
        if "creationflags" not in kwargs:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        _orig_init(self, *args, **kwargs)
    subprocess.Popen.__init__ = _init
    subprocess._no_window_patched = True

_silence_subprocess_windows()

if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_DIR = getattr(sys, "_MEIPASS", HERE)

def resource_path(*parts):
    for base in (HERE, BUNDLE_DIR):
        path = os.path.join(base, *parts)
        if os.path.exists(path):
            return path
    return os.path.join(HERE, *parts)

def _find_adb():
    import shutil
    if sys.platform != "win32":
        onpath = shutil.which("adb")
        if onpath:
            return onpath
    cands = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands.append(os.path.join(meipass, "platform-tools", "adb.exe"))
    cands.append(os.path.join(os.path.dirname(sys.executable),
                              "platform-tools", "adb.exe"))
    cands.append(os.path.join(HERE, "platform-tools", "adb.exe"))
    for c in cands:
        if c and os.path.exists(c):
            return c
    onpath = shutil.which("adb")
    return onpath if onpath else "adb"

ADB = _find_adb()
FB_PACKAGE = "com.facebook.lite"
FB_ACTIVITY = "com.facebook.lite/.MainActivity"
RECOVER_RETRIES = 3

# ── AI comment generation ──
POLLINATIONS_TEXT_URL = "https://text.pollinations.ai/{prompt}"
# The endpoint is NOT hard-coded in this app. FBVenom exists to comment, so the
# AI is the product: URL, key, model and fallback come from venom_ai.json and
# are editable in the UI. aiconfig ships OpenAI-compatible presets (Groq,
# OpenAI, OpenRouter, Together, DeepSeek, local Ollama/LM Studio), which all
# speak the same chat-completions shape this module already sends.
import aiconfig


def ai_cfg():
    return aiconfig.load()


def _ai(key, default=None):
    return ai_cfg().get(key, default)


class _AISetting:
    """Reads venom_ai.json on each use, so editing settings takes effect
    immediately instead of needing a restart. Kept name-compatible with
    FBPoison so the shared comment code below needs no changes."""

    def __init__(self, key, default=None):
        self._key = key
        self._default = default

    def _v(self):
        return _ai(self._key, self._default)

    def __bool__(self):
        return bool(str(self._v() or "").strip())

    def __str__(self):
        return str(self._v() or "")

    def strip(self):
        return str(self).strip()


GROQ_API_KEY = _AISetting("api_key", "")

def groq_url():
    return str(_ai("url", "") or "")

def groq_model():
    return str(_ai("model", "") or "")

def groq_fallback_model():
    return str(_ai("fallback_model", "") or "")

# This is a PARAPHRASE job, not a "write your own comment" job. The old prompt
# assigned a random tone and "angle" (be excited / share a personal experience /
# add a detail), which made the model invent feelings and facts that were never
# in the example — e.g. "omg ang laki nito" came back as "ang saya saya sabay
# laki naman ito". Same meaning, different words is the whole requirement.
# The "same feeling" half of this is the one thing a chosen TONE may take over,
# so it lives in its own piece: with a tone set the feeling comes from the tone
# instead of from the example, and the two clauses that forbid that are swapped
# out rather than argued with. Stating both at once is what made every tone
# come back as a plain neutral reword - the model reads five "never add a
# feeling" lines against one "be angry", and plays safe. Subject, opinion,
# facts and language hold either way.
_BASE_HEAD = "You rewrite one Facebook comment using different words.\n"

_BASE_FEELING_NEUTRAL = (
    "Your output MUST mean EXACTLY the same thing as the example comment: "
    "the same subject, the same opinion, the same feeling.\n"
    "NEVER add a feeling, reason, event, person, place, number or detail that is "
    "not already in the example. Do not explain and do not react to it.\n"
)

_BASE_FEELING_TONED = (
    "Your output MUST say EXACTLY the same thing about the same subject as the "
    "example comment: the same subject, the same opinion. The FEELING is the "
    "one exception - it comes from the TONE below, NOT from the example, so "
    "the mood is ALLOWED to differ from the example's.\n"
    "NEVER add a reason, event, person, place, number or detail that is not "
    "already in the example. Do not explain it.\n"
)

_BASE_RULES = (
    "Do not change what is being said about the subject, and never say the "
    "opposite of it.\n"
    "Keep the SAME quality being described. If the example says something is "
    "big, your version must still say it is big — not beautiful (ganda), not "
    "many (dami), not strong (lakas), not funny. Only the words may change, "
    "never the quality being talked about.\n"
    "Keep the SAME THING being talked about. If the example is about fire "
    "trucks, your version is still about fire trucks — never an ambulance, "
    "never a police car, never a road. Never swap the object, the person or "
    "the place for a different one. Names stay exactly as written.\n"
)

_BASE_WORDING_NEUTRAL = (
    "Change ONLY the wording: different words, different word order, "
    "slightly different length.\n"
)

_BASE_WORDING_TONED = (
    "Change the wording AND the feeling it is delivered with - nothing else: "
    "different words, different word order, slightly different length.\n"
)

_BASE_LANG = (
    "Use the exact same language mix as the example "
    "(English stays English; Tagalog/Taglish stays Taglish). Never translate.\n"
)

_COMMENT_SYSTEM_BASE = (_BASE_HEAD + _BASE_FEELING_NEUTRAL + _BASE_RULES
                        + _BASE_WORDING_NEUTRAL + _BASE_LANG)


def comment_base_for(tone=None):
    """The meaning rules, with the feeling clauses set by whether a tone is on."""
    toned = bool(COMMENT_TONES.get(tone or "neutral", ""))
    return (_BASE_HEAD
            + (_BASE_FEELING_TONED if toned else _BASE_FEELING_NEUTRAL)
            + _BASE_RULES
            + (_BASE_WORDING_TONED if toned else _BASE_WORDING_NEUTRAL)
            + _BASE_LANG)

# One-liner examples ("grabe ang ganda nito") should stay one-liners, and read
# like a phone-typed comment - hence no punctuation at all.
_COMMENT_SYSTEM_SHORT_TAIL = (
    "Keep it about as short as the example — one short sentence at most.\n"
    "Do NOT use any punctuation marks or symbols at all — no periods, commas, "
    "exclamation points, question marks, quotes, dashes, hashtags, or emoji.\n"
    "Output ONLY the rewritten comment on one line: no quotes, labels, "
    "explanations, or extra lines."
)

# A written-out example (several sentences, an opinion plus a hope) has to come
# back as a PARAGRAPH of the same shape. The short rules above would crush it
# into one punctuation-less line, which is what made long examples come back
# looking nothing like what was typed in.
_COMMENT_SYSTEM_PARAGRAPH_TAIL = (
    "Match the LENGTH of the example: roughly the same number of words, "
    "covering every idea it contains. Never compress it into one short line "
    "and never drop one of its ideas — all of them must still be there, "
    "reworded.\n"
    "Use YOUR OWN words. Do not reuse the example's words: swap every content "
    "word (nouns, verbs, adjectives) for a different word that means the same "
    "thing. Only unavoidable little words (ang, ng, sa, ay, at, na, mga) may "
    "stay the same. The meaning must be identical; the wording must not.\n"
    "Do NOT use any punctuation marks or symbols at all — no periods, commas, "
    "exclamation points, question marks, quotes, dashes, hashtags, or emoji. "
    "Write it the way someone types quickly on a phone.\n"
    "Output ONLY the rewritten comment on one line: no quotes, labels, "
    "explanations, or extra lines."
)

# -- Voice --------------------------------------------------------------
# Everything above fixes WHAT the comment says. This fixes HOW it sounds.
# It is a style layer only: every meaning rule above outranks it, so a trait
# may change word choice, register and spelling, but never a fact, a feeling,
# the subject or the length.
#
# The percentages ARE the dial - tune the numbers, not the sentences. 0 turns a
# trait off completely and it stops being sent at all; 1-33 picks the low
# wording, 34-66 the middle, 67-100 the high one.
COMMENT_PERSONA = {
    "Human thinking":    100,
    "Overthinking":      100,
    "Common sense":      100,
    "Gen Z":             100,
    "Taglish":           100,
    "Filipino":           70,
    "Bisaya":            100,
    "Brain cells":         3,
    "Kaalaman":          100,
    "Humor":             100,
    "Critical thinking":  40,
    "Emotional damage":  100,
    "Sarcastic":         100,
    "Supportive":        100,
}

# (low, middle, high) wording per trait. Each one is phrased so it can only
# reach the WORDING: "overthinking" buys a hedge, not an extra sentence, and
# "emotional damage" buys tired word choice, not a feeling the example never
# had - otherwise the persona would fight _COMMENT_SYSTEM_BASE and the result
# would be rejected by _good_variation and burn a retry (which is what pushes
# the shared free-tier key into 429s).
_PERSONA_BANDS = {
    "Human thinking": (
        "it may read a little flat",
        "it should read like a person typed it",
        "it must read exactly like a real person thumb-typing on a phone, "
        "never like generated text",
    ),
    "Overthinking": (
        "say it straight out",
        "a small hesitation in the wording is fine",
        "word it like someone who second-guessed how to say it - one hedge or "
        "unsure word. Wording only: no extra thought and no extra length",
    ),
    "Common sense": (
        "the wording may be a bit random",
        "keep the wording ordinary and sensible",
        "say it the plainest, most obvious way",
    ),
    "Gen Z": (
        "one casual word at most",
        "some casual internet slang",
        "full Gen Z texting voice: all lowercase, short chatty words, texting "
        "spellings and slang (grabe, sheesh, fr, ngl, lowkey, tas, char, "
        "ganern). No emoji - they are stripped before posting",
    ),
    "Taglish": (
        "mostly one language, at most a stray word from the other",
        "mix Tagalog and English the way people text - only if the example "
        "already mixes them, never translate it",
        "heavy Tagalog-English mixing - only if the example already mixes "
        "them, never translate it",
    ),
    "Filipino": (
        "a faint Filipino flavour, only where the example already has it",
        "use Filipino fillers and particles (naman, talaga, grabe, sana, po) "
        "where the example's own language allows - never translate it",
        "strongly Filipino phrasing, only if the example is already Filipino - "
        "never translate it",
    ),
    "Bisaya": (
        "at most a stray Bisaya word, and only where the example already "
        "has one",
        "mix in everyday Bisaya words (kaayo, nindot, gyud, bitaw, lagi) "
        "where the example is already Bisaya - never turn a Tagalog "
        "example into Bisaya",
        "full Bisaya/Cebuano voice, but ONLY if the example is already "
        "Bisaya. Bisaya and Tagalog are different languages: whichever one "
        "the example is written in, stay in that one and never switch it "
        "to the other",
    ),
    "Brain cells": (
        "you barely thought about it: the simplest, dumbest, most obvious "
        "words, no big words, no analysing, no explaining",
        "everyday words, nothing clever",
        "sharp, well-chosen words",
    ),
    "Kaalaman": (
        "do not sound informed - you know nothing beyond what the example "
        "says, so never add a fact, a term or an explanation",
        "sound normally clued in, still adding no facts of your own",
        "sound well informed in wording only, still adding no facts of your own",
    ),
    "Humor": (
        "straight-faced",
        "a light playful edge in the word choice",
        "funny wording - playful, teasing, a bit exaggerated. The joke is in "
        "HOW it is worded, never a new joke or punchline of your own",
    ),
    "Critical thinking": (
        "do not weigh or judge anything",
        "a slight weighing of words is fine, but never a new opinion",
        "choose the words carefully, still never a new opinion",
    ),
    "Emotional damage": (
        "a faint tired hugot undertone in the word choice only - never a new "
        "feeling, never sadder or happier than the example",
        "a clear tired hugot undertone in the word choice only, never a new "
        "feeling",
        "heavy hugot undertone in the word choice only, never a new feeling",
    ),
    "Sarcastic": (
        "a faint dry edge, barely there",
        "a dry teasing edge, said with a bit of a smirk",
        "heavy sarcasm - dry, mocking, fake-innocent. The edge is in the "
        "WORDING: it still makes the same claim about the same subject and "
        "never flips to saying the opposite",
    ),
    "Supportive": (
        "not unkind, just not going out of your way",
        "on their side, a little encouragement in the word choice",
        "clearly on their side and rooting for them. Together with Sarcastic "
        "that is affectionate teasing - you tease because you are WITH "
        "them, never to put them down",
    ),
}

_PERSONA_HEADER = (
    "HOW IT MUST SOUND - voice only. These change word choice, register and "
    "spelling. They never change the meaning, the facts or the length:\n"
)

# Without this the voice is silently cancelled: the model reads the
# "add nothing that is not in the example" rule as covering slang too, and
# every trait above then comes back as a plain, flat reword.
_PERSONA_GUARD = (
    "Slang, fillers, particles and texting spellings ARE wording, not new "
    "content - they are expected here and do not count as adding anything. "
    "A new fact, feeling, reason or detail still does.\n"
    "If any of that would change the meaning, add something the example does "
    "not already say, or make the comment longer than the example, drop it - "
    "the rules above always win.\n"
)


def _persona_band(pct):
    """Which of the three wordings a percentage selects (None = trait off)."""
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return None
    if pct <= 0:
        return None
    if pct <= 33:
        return 0
    if pct <= 66:
        return 1
    return 2


def persona_block(persona=None):
    """Render COMMENT_PERSONA as prompt lines ("" when every trait is 0).

    The percentage is kept in the line itself: "Brain cells 2%" calibrates the
    model far better than the adjective alone does.
    """
    persona = COMMENT_PERSONA if persona is None else persona
    lines = []
    for trait, pct in (persona or {}).items():
        band = _persona_band(pct)
        if band is None:
            continue
        text = (_PERSONA_BANDS.get(trait) or ("", "", ""))[band]
        if text:
            lines.append(f"- {trait} {int(float(pct))}%: {text}.")
    if not lines:
        return ""
    return _PERSONA_HEADER + "\n".join(lines) + "\n" + _PERSONA_GUARD


def persona_line(persona=None):
    """One-line version of the dial, for the END of the user prompt.

    The system prompt alone loses this fight: several paragraphs of
    "add nothing" follow it, so the model plays safe and returns a flat
    reword with none of the voice. Restating the dial last, right before
    the output instruction, is what actually makes it show up.
    """
    persona = COMMENT_PERSONA if persona is None else persona
    parts = [f"{t} {int(float(p))}%" for t, p in (persona or {}).items()
             if _persona_band(p) is not None]
    if not parts:
        return ""
    return ("The person typing this is: " + ", ".join(parts)
            + ". Sound exactly like them - in the WORDING only, never in "
            "what is being said. Write it in the example's own language. ")


# The only two traits that can beat a rule above them, so they are the only
# two that get filtered per example rather than just worded carefully.
_LANGUAGE_TRAITS = ("Taglish", "Filipino", "Bisaya")

# Traits that set a feeling, and so cannot run alongside a chosen tone.
_MOOD_TRAITS = ("Emotional damage", "Sarcastic")


def persona_for(example, tone=None, lang=None):
    """The dial as it applies to THIS example.

    Sending Taglish and Filipino on an English example makes the model
    translate it - "this is really nice" came back as "grabe ang ganda nito
    talaga", which is a different comment, not a reworded one. They apply
    only when the example is already Filipino.

    `lang` overrides what the example says, for the one case where something
    else owns the language: a sampled style that rolled Bisaya has to keep the
    Bisaya trait even though the examples are Tagalog, or the dial argues with
    the style block and the comment comes back Tagalog anyway.
    """
    persona = dict(COMMENT_PERSONA)
    if lang == "bisaya":
        persona.pop("Taglish", None)
        persona.pop("Filipino", None)
    elif lang == "tl":
        persona.pop("Bisaya", None)
    elif _detect_lang(example) != "tl":
        for trait in _LANGUAGE_TRAITS:
            persona.pop(trait, None)
    elif _is_bisaya(example):
        # Taglish and Filipino both read as "write Tagalog" and translated
        # the example. On a Bisaya comment only the Bisaya trait belongs.
        persona.pop("Taglish", None)
        persona.pop("Filipino", None)
    else:
        persona.pop("Bisaya", None)
    # These are feelings, and once a tone is chosen the feeling is the
    # tone's job: sending both asks for two moods at once, and Emotional
    # damage's "never a new feeling" wording cancels the tone outright.
    # Supportive is deliberately NOT here - it is a stance rather than a
    # mood, and it composes with any tone.
    if tone == TONE_RAGE:
        # Supportive sits at 100% and its high band reads "clearly on their
        # side and rooting for them ... never to put them down". Against
        # sarcastic anger that is not a stance, it is a cancellation: it is
        # what turned this tone into straight compliments ("ANG GALING NAMAN
        # NG PINK SUKUNA", "SUKUNA MASTERPIECE TALAGA"). It goes.
        # Sarcastic is the opposite case - it is the one trait this tone
        # actually wants, so unlike every other tone it is KEPT.
        persona.pop("Supportive", None)
        persona.pop("Emotional damage", None)
    elif COMMENT_TONES.get(tone or "neutral", ""):
        for trait in _MOOD_TRAITS:
            persona.pop(trait, None)
    return persona


# -- Tone --------------------------------------------------------------
# The persona above is a VOICE and may never touch the feeling, because
# _COMMENT_SYSTEM_BASE forbids adding one. Tone is the deliberate exception:
# choosing one IS you saying "deliver it angrily", so it is the single thing
# allowed to override that rule. Everything else still holds - an angry
# rewrite of "ang ganda nito" is still saying it is beautiful, just said by
# someone who is pissed off. Subject, opinion and facts never move.
#
# "neutral" is the default and is a true no-op: the prompt goes out exactly as
# it did before tones existed.
COMMENT_TONES = {
    "neutral":      "",
    "sarcastic":    "dry and mocking, fake-sweet, the way someone types when "
                    "they do not mean it straight",
    "angry":        "fed up and sharp, short hard words, someone typing while "
                    "they are pissed off",
    "annoyed":      "mildly irritated and impatient, like someone who has seen "
                    "this one too many times",
    "happy":        "warm and pleased, light, smiling while typing",
    "excited":      "hyped and full of energy, like they just saw it and had "
                    "to type fast",
    "amazed":       "genuinely stunned, like they cannot believe what they are "
                    "looking at",
    "sad":          "quiet and heavy, low energy",
    "disappointed": "let down, expected better, more tired than angry",
    "caring":       "gentle and supportive, talking to someone they care about",
    "joking":       "clowning around, teasing, treating the whole thing as a "
                    "joke",
    "proud":        "proud of it and a little boastful, claiming it as ours",
    "confused":     "genuinely lost, not following what is going on",
    "bored":        "flat and uninterested, typing because they may as well",
    "hugot":        "heavy Filipino hugot - personal, dramatic, taking it to "
                    "heart",
    # The only tone that samples a style per comment instead of describing one
    # mood. See TONE_RAGE and sample_rage_style() below.
    "sarcastic_anger":
                    "sarcastic anger - Filipino outrage comedy, furious and "
                    "funny at the same time, measuring the thing against "
                    "something small and personal",
}

# Not a tone but a selection: pick a different real one for every comment, so a
# fleet of ~19 phones does not post the same mood under the same post.
TONE_RANDOM = "random"

# Tones whose whole point is that the commenter is NOT pleased. The offline
# fallback must never answer one of these out of a praise pool - see the guard
# in unique_comment(). Listed by stance, not by intensity: "bored" is mild and
# still cannot be served by "Absolute perfection!".
_NO_PRAISE_TONES = frozenset({
    "sarcastic", "angry", "annoyed", "sad", "disappointed", "bored",
    "sarcastic_anger",
})

_TONE_HEADER = (
    "TONE - this is the ONE thing that overrides the same-feeling rule above. "
    "The subject, the opinion and the facts still cannot change; only the "
    "emotion it is typed with does. You MAY add the words that carry this "
    "feeling - an interjection, a particle, an emotional word or two: those "
    "ARE the tone, not new content. A new fact, reason, event, person or "
    "place is still forbidden, and so is insulting anyone, slurs and "
    "swearing:\n"
)


def tone_names():
    """Every selectable tone, in menu order."""
    return list(COMMENT_TONES.keys()) + [TONE_RANDOM]


def tone_label(name):
    """Menu text for a tone name."""
    if name == TONE_RANDOM:
        return "Random (a different one each comment)"
    if name == "neutral":
        return "Neutral (keep the example's own feeling)"
    if name == "sarcastic_anger":
        return "Sarcastic anger (Gen Z / millennial, sampled style)"
    return str(name).title()


def resolve_tone(tone=None):
    """Turn a stored setting into the tone to use for ONE comment.

    Resolved once and then handed to both prompts: resolving TONE_RANDOM
    separately in each would ask for two different feelings in the same call.
    Passing an already-resolved name back in is a no-op, so it is safe to call
    twice. None means "whatever venom_ai.json says".
    """
    if tone is None:
        tone = _ai("tone", "neutral")
    name = str(tone or "").strip().lower()
    if name == TONE_RANDOM:
        real = [t for t, text in COMMENT_TONES.items() if text]
        return random.choice(real) if real else "neutral"
    return name if name in COMMENT_TONES else "neutral"


def tone_block(tone):
    """System-prompt paragraph for an already-resolved tone ("" = neutral)."""
    text = COMMENT_TONES.get(tone or "neutral", "")
    if not text:
        return ""
    return _TONE_HEADER + "- " + str(tone).title() + ": " + text + ".\n"


def tone_line(tone):
    """One-line restatement for the END of the user prompt.

    Same reason as persona_line(): stated only once at the top, the tone is
    buried under the meaning rules and comes back neutral.
    """
    text = COMMENT_TONES.get(tone or "neutral", "")
    if not text:
        return ""
    return ("The feeling it is typed with is " + str(tone) + " - " + text
            + ". Use the words that carry that feeling. It changes the "
              "delivery only: what it says about the subject does not "
              "change. ")


# ── Sarcastic anger: sampled style ─────────────────────────────────────────
# Every other tone is one sentence of description and the model picks its own
# delivery, which is why they all come back sounding like the same careful
# person. This one is sampled instead: a comment is a POINT IN A STYLE SPACE,
# and the axes below get rolled per comment so nineteen accounts arrive as
# nineteen different people rather than one person nineteen times.
#
# Only this tone samples. Everything else keeps the old behaviour exactly.
TONE_RAGE = "sarcastic_anger"

# How the caps land. Not decoration - caps profile is most of what makes a
# comment read as a specific human being.
RAGE_CAPS = {
    "all_caps": "TYPE THE WHOLE COMMENT IN CAPITALS. Not one word of "
                "lowercase.",
    "caps_burst": "Type normally, then SHOUT ONE CLAUSE IN THE MIDDLE in "
                  "capitals and drop back to lowercase after it. Exactly one "
                  "burst, and put it on the part that actually stings.",
    "lower_flat": "everything lowercase - proper nouns lowercase, i lowercase, "
                  "and no full stop at the end. it should read tired and "
                  "deadpan, like someone who has stopped bothering.",
    "lower_exhale_then_caps": "Start lowercase and flat, like an exhale, then "
                              "let the LAST PART GO INTO CAPITALS as it builds. "
                              "The break has to land mid-thought, not at a "
                              "tidy sentence boundary.",
    "sentence_sloppy": "Normal capitalisation, but no full stop at the end and "
                       "no tidying up.",
}

# What kind of angry it is. These are social positions, not synonyms - each one
# is a different person in the comment section.
RAGE_REGISTER = {
    "galit": "straight furious. short hard words, no jokes, the anger is the "
             "whole comment.",
    # Deliberately does NOT contain the words "sana all". Handing the model a
    # catchphrase gets that catchphrase pasted into every comment - it turned
    # up in four out of nine under one post. Describe the stance, not the line.
    # "Congratulate them, admire it" on its own produced SINCERE PRAISE, so the
    # sting is now stated as the requirement it is: the compliment has to be
    # aimed at whatever is wrong, or it is not this register at all.
    "pang_asar": "needling and fake-congratulatory: you are not complaining, "
                 "you are pointedly happy for them, which is worse. The "
                 "praise must be OBVIOUSLY insincere and aimed squarely at "
                 "the thing that is wrong - congratulate them ON the flaw, "
                 "admire the very part that fails, act impressed by exactly "
                 "what should embarrass them. If it could be read as a real "
                 "compliment, you have written the wrong comment.",
    "pagod": "past anger and into exhaustion. resigned, flat, done arguing. "
             "hays, wala na talaga, bahala na.",
    "tawa": "laughing at it because it is genuinely absurd. the joke IS the "
            "criticism.",
    "gulat": "caught off guard by how bad it is. grabe, jusko, hala - "
             "disbelief first, anger underneath.",
    "tanong": "asking a rhetorical question you already know the answer to. "
              "bakit ganon, sino ba nag-approve nito, ano ba yan.",
    "depensa": "arguing back at someone who defended it, as if mid-thread. "
               "you are correcting a take nobody typed.",
    "balewala": "a two-word dismissal. you cannot be bothered to write more "
                "than that, and that is the insult.",
}

# The coupling the user called out: an exhausted person does not type in
# capitals, and a furious one rarely types flat lowercase. Weights, not rules,
# so the unusual combination still shows up occasionally - real comment
# sections have those too. A 0 means genuinely never.
_RAGE_CAPS_WEIGHTS = {
    "galit":     {"all_caps": 4, "caps_burst": 4, "lower_exhale_then_caps": 3,
                  "sentence_sloppy": 1, "lower_flat": 0},
    "pang_asar": {"caps_burst": 3, "sentence_sloppy": 3, "all_caps": 2,
                  "lower_exhale_then_caps": 2, "lower_flat": 1},
    "pagod":     {"lower_flat": 7, "sentence_sloppy": 2,
                  "lower_exhale_then_caps": 1, "caps_burst": 0, "all_caps": 0},
    "tawa":      {"caps_burst": 3, "sentence_sloppy": 3, "all_caps": 2,
                  "lower_flat": 1, "lower_exhale_then_caps": 1},
    "gulat":     {"all_caps": 4, "caps_burst": 3, "lower_exhale_then_caps": 3,
                  "sentence_sloppy": 1, "lower_flat": 0},
    "tanong":    {"sentence_sloppy": 3, "caps_burst": 2, "lower_flat": 2,
                  "lower_exhale_then_caps": 2, "all_caps": 1},
    "depensa":   {"caps_burst": 3, "all_caps": 3, "sentence_sloppy": 2,
                  "lower_exhale_then_caps": 2, "lower_flat": 0},
    "balewala":  {"lower_flat": 5, "sentence_sloppy": 3, "caps_burst": 1,
                  "all_caps": 0, "lower_exhale_then_caps": 0},
}

RAGE_LENGTH = {
    "fragment": "2 to 5 words. A fragment, not a sentence.",
    "one_liner": "6 to 15 words. One line.",
    "short": "16 to 30 words.",
    "rant": "30 to 60 words in ONE unbroken chain of comma splices. No full "
            "stops anywhere in it, no paragraph breaks - it runs on because "
            "the person is not stopping to breathe.",
}
_RAGE_LENGTH_WORDS = {"fragment": (2, 5), "one_liner": (6, 15),
                      "short": (16, 30), "rant": (30, 60)}

# Length is a property of the register too. A two-word dismissal IS balewala;
# a 50-word run-on is what galit and depensa actually look like.
_RAGE_LENGTH_WEIGHTS = {
    "galit":     {"one_liner": 3, "short": 3, "rant": 3, "fragment": 1},
    "pang_asar": {"one_liner": 4, "short": 3, "fragment": 2, "rant": 1},
    "pagod":     {"fragment": 3, "one_liner": 4, "short": 2, "rant": 1},
    "tawa":      {"one_liner": 4, "short": 3, "fragment": 2, "rant": 1},
    "gulat":     {"fragment": 2, "one_liner": 4, "short": 3, "rant": 1},
    "tanong":    {"one_liner": 4, "short": 3, "fragment": 1, "rant": 1},
    "depensa":   {"short": 4, "rant": 4, "one_liner": 2, "fragment": 0},
    "balewala":  {"fragment": 9, "one_liner": 1, "short": 0, "rant": 0},
}

RAGE_LAUGH = {
    "none": "",
    "hahaha": "hahaha",
    "HAHAHA": "HAHAHA",
    "HAHAHAHAHA": "HAHAHAHAHA",
    "emoji": "\U0001F923\U0001F923\U0001F923",
    "hays": "hays",
}
# Laughter length tracks intensity, so it is drawn against the caps profile
# rather than at random: nobody types HAHAHAHAHA in a flat lowercase comment,
# and "hays" is the exhausted person's laugh.
_RAGE_LAUGH_WEIGHTS = {
    "all_caps":               {"HAHAHAHAHA": 3, "HAHAHA": 3, "none": 3,
                               "emoji": 2, "hahaha": 1, "hays": 0},
    "caps_burst":             {"HAHAHA": 3, "hahaha": 2, "none": 4,
                               "emoji": 2, "HAHAHAHAHA": 1, "hays": 0},
    "lower_flat":             {"none": 6, "hays": 4, "hahaha": 1,
                               "emoji": 1, "HAHAHA": 0, "HAHAHAHAHA": 0},
    "lower_exhale_then_caps": {"none": 4, "HAHAHA": 2, "hahaha": 2,
                               "hays": 2, "emoji": 1, "HAHAHAHAHA": 1},
    "sentence_sloppy":        {"none": 5, "hahaha": 3, "emoji": 2,
                               "HAHAHA": 2, "hays": 1, "HAHAHAHAHA": 0},
}

RAGE_PUNCT = {
    "none": "No punctuation at all.",
    "ellipsis": "Use ... where the thought trails off. Nothing else.",
    "bang": "End it with !!! - three of them, not one.",
    "comma_splice": "Commas only, joining clause after clause. Never a full "
                    "stop.",
}
# A terminal full stop reads as a formal older person, so the tidy options are
# deliberately rare and there is no "proper punctuation" value at all.
_RAGE_PUNCT_WEIGHTS = {
    "all_caps":               {"none": 4, "bang": 4, "comma_splice": 2,
                               "ellipsis": 1},
    "caps_burst":             {"none": 4, "comma_splice": 3, "bang": 2,
                               "ellipsis": 2},
    "lower_flat":             {"none": 4, "ellipsis": 4, "comma_splice": 2,
                               "bang": 0},
    "lower_exhale_then_caps": {"none": 3, "comma_splice": 3, "ellipsis": 2,
                               "bang": 2},
    "sentence_sloppy":        {"none": 4, "comma_splice": 3, "ellipsis": 2,
                               "bang": 1},
}

RAGE_SWITCH = {
    "tagalog_heavy": "Mostly Tagalog, with English only for the words people "
                     "genuinely never translate.",
    "balanced": "Real Taglish - switching language mid-sentence, the way it is "
                "actually typed.",
    "english_heavy": "Mostly English with Tagalog particles thrown in. This is "
                     "a different social character: the may-pinag-aralan "
                     "complainer, more articulate and more condescending for "
                     "it.",
    "bisaya": "Bisaya, not Tagalog - gyud, kaayo, unsa, murag, lagi, uy, sus, "
              "ambot, pastilan. Mixed with English the same way.",
}
_RAGE_SWITCH_WEIGHTS = {"tagalog_heavy": 4, "balanced": 4,
                        "english_heavy": 2, "bisaya": 2}

# The comparison anchor. Filipino outrage-comedy works by measuring the huge
# thing against something small, personal and domestic - that is the move that
# turns generic indignation into an actual joke, and it is the single biggest
# quality lever here. Sampled and named explicitly rather than left to the
# model, so nineteen accounts reach for nineteen different yardsticks instead
# of all landing on the same one.
# A yardstick only lands when it is comparable ALONG THE SAME DIMENSION as the
# complaint. Drawing one at random from a flat list is what produced
# "mas malabo pa sa sahod" and "mas malniaw pa lotto jackpot" - a salary is not
# blurry and a jackpot is not clear, so the joke is a category error and the
# comment reads as a canned line stapled to a post it has nothing to do with.
#
# So the dimension is read off the post FIRST, and the yardstick is drawn only
# from that dimension. A post with no readable dimension gets NO comparison at
# all - forcing one is worse than going without.
RAGE_ANCHOR_DIMENSIONS = {
    "money": {
        "cues": r"billion|bilyon|million|milyon|peso|php|budget|cost|costs|"
                r"funds?|funding|price|priced|expensive|mahal|gastos|pondo|"
                r"salary|worth|spent|spending|allocation|contract",
        # No brands and no institutions - see _OFF_ENTITY_RE. A yardstick has
        # to be something the commenter OWNS or LIVES, not a named third party
        # dragged into a post it has nothing to do with.
        "anchors": ("your sahod", "the kuryente bill", "presyo ng bigas",
                    "your baon or allowance", "the lotto jackpot",
                    "your 13th month pay", "tuition", "the load you top up",
                    "your e-wallet balance", "your utang sa 5-6",
                    "the price of sibuyas", "a can of sardinas",
                    "your monthly contribution", "the pamasahe"),
    },
    "speed": {
        "cues": r"slow|fast|quick|speed|bilis|bagal|instant|delay|delayed|"
                r"rushed|overnight|immediately|agad",
        "anchors": ("your internet connection", "your wifi at home",
                    "how long a delivery rider takes",
                    "how fast your load runs out",
                    "how fast your battery drains",
                    "the traffic on your way home"),
    },
    "waiting": {
        "cues": r"years?|months?|taon|buwan|waiting|waited|hinintay|pending|"
                r"since|finally|delayed|backlog|queue",
        "anchors": ("the queue you lined up in last week",
                    "waiting for a jeep to fill up",
                    "waiting for your laundry to dry",
                    "waiting for your sahod",
                    "waiting for a reply you never got"),
    },
    "clarity": {
        "cues": r"blurr?y|unclear|hard to see|low quality|pixel|grainy|dark|"
                r"malabo|hindi malinaw|out of focus|cannot be seen|obscured",
        "anchors": ("a 240p video", "CCTV footage from a sari-sari store",
                    "a screenshot of a screenshot",
                    "your eyesight without your glasses",
                    "a photo taken through a jeepney window",
                    "a video call on one bar of signal"),
    },
    "durability": {
        "cues": r"crack|cracked|broken|broke|collapse|collapsed|damaged|"
                r"sira|nasira|bumagsak|falling apart|worn|leaking|rusted|"
                r"washed away|nawasak",
        "anchors": ("pandesal left out overnight", "your tsinelas",
                    "a phone case from the tiangge", "a 20-peso umbrella",
                    "the plastic chair at every birthday party",
                    "a charger cable from the sidewalk"),
    },
    "size": {
        "cues": r"small|tiny|maliit|little|narrow|shallow|thin|short|"
                r"huge|malaki|massive|giant",
        "anchors": ("your studio unit", "the aircon in a jeep",
                    "a sachet of shampoo", "your baon container",
                    "the space between seats on a bus"),
    },
}

# Every yardstick, flattened - used only to ration them across the fleet.
RAGE_ANCHORS = tuple(a for d in RAGE_ANCHOR_DIMENSIONS.values()
                     for a in d["anchors"])

_DIM_RES = {name: re.compile(spec["cues"], re.I)
            for name, spec in RAGE_ANCHOR_DIMENSIONS.items()}


def post_dimensions(ctx):
    """Which dimensions this post can actually be complained about along.

    Empty is a real and common answer, and it means NO comparison: a post that
    is not about a cost, a speed, a wait, a clarity, a breakage or a size has
    nothing a domestic yardstick can be held against, and forcing one is how
    "mas malabo pa sa sahod" happened.
    """
    text = str(ctx or "")
    return [name for name, rx in _DIM_RES.items() if rx.search(text)]


# HOW the comparison is made. Sampling only the yardstick was not enough: nine
# accounts picked nine different anchors and then poured all of them into the
# same "mas ___ pa sa ___" mould, which is what made a real run read as one
# comment posted nine times. The rhetorical MOVE has to vary too.
RAGE_COMPARE = {
    "mas_pa": "the plain comparison - mas ___ pa ang/sa ___.",
    "parang": "call the thing in the post BY the yardstick's name - it simply "
              "is that thing now, no comparison word at all.",
    "ako_nga": "put yourself in it first - ako nga ___, tapos ito pa kaya. "
               "Your own situation is the setup, the post is the punchline.",
    "kahit": "concede it - kahit ___ ___, and even that beats this.",
    "arithmetic": "do the maths out loud. Work out how many of the small thing "
                  "the big number buys, and let the number land on its own.",
    "swap": "say you would rather have the small thing, and mean it.",
    "timeline": "compare how LONG, not how much - how long the small thing "
                "lasts against how fast this one failed.",
    "receipt": "talk about it like a receipt or a bill you are reading out, "
               "itemised and disbelieving.",
    "question": "ask which of the two is supposed to make sense, without "
                "answering.",
}

# Catchphrases a model reaches for again and again once it has seen one. They
# are not banned - they are real - but the prompt is told which ones the fleet
# has already spent under this post.
_RAGE_CATCHPHRASES = (
    "sana all", "hays", "sheesh", "jusko", "grabe", "ang lupit", "ang galing",
    "bahala na", "wala na talaga", "the audacity", "ganern", "charot",
    "sus", "pastilan", "ambot", "awit", "wala tayong magagawa",
)


def _weighted(weights):
    """Pick one key by weight. A weight of 0 is never picked."""
    live = [(k, w) for k, w in (weights or {}).items() if w > 0]
    if not live:
        return None
    total = sum(w for _, w in live)
    roll = random.uniform(0, total)
    for k, w in live:
        roll -= w
        if roll <= 0:
            return k
    return live[-1][0]


def sample_rage_style(anchor_prob=0.80, fragment_anchor_prob=0.35,
                      avoid_anchors=(), avoid_compare=(), dimensions=None):
    """Roll one point in the style space, coupled so the person is coherent.

    Register is drawn first because everything else hangs off it: it decides
    how the caps land, how long the comment is, and that in turn decides the
    laughter and the punctuation. Rolling the axes independently produced
    exhausted people SHOUTING IN CAPITALS, which is the specific thing that
    reads as generated.
    """
    register = random.choice(list(RAGE_REGISTER))
    caps = _weighted(_RAGE_CAPS_WEIGHTS[register]) or "sentence_sloppy"
    length = _weighted(_RAGE_LENGTH_WEIGHTS[register]) or "one_liner"
    laugh = _weighted(_RAGE_LAUGH_WEIGHTS[caps]) or "none"
    punct = _weighted(_RAGE_PUNCT_WEIGHTS[caps]) or "none"
    switch = _weighted(_RAGE_SWITCH_WEIGHTS) or "balanced"
    # A rant is a chain of comma splices by definition, so it takes the commas
    # whatever the punctuation axis rolled.
    if length == "rant":
        punct = "comma_splice"
    # Commas join clauses, and a fragment has one clause. "ok, sige" is not a
    # comma splice, it is a comma sitting on its own.
    if length == "fragment" and punct == "comma_splice":
        punct = "none"
    # The comparison is the single biggest quality lever, so a fragment gets one
    # too - it is short BECAUSE the yardstick is the whole joke ("parang sahod
    # ko"). Gating it on length alone dropped the overall anchor rate to ~54%.
    anchor, compare, dim = "", "", ""
    prob = fragment_anchor_prob if length == "fragment" else anchor_prob
    # No readable dimension means no comparison. This is not a failure - most
    # posts are not about a cost or a speed, and a comment that just reacts is
    # far better than one holding the post against an unrelated yardstick.
    dims = list(dimensions or [])
    if dims and random.random() < prob:
        # Never the same yardstick twice under one post while unused ones are
        # left, and never the same rhetorical move either. Both pools reset per
        # run and fall back to the full list once exhausted, so a long run
        # degrades to random instead of running out.
        dim = random.choice(dims)
        in_dim = RAGE_ANCHOR_DIMENSIONS[dim]["anchors"]
        pool = [a for a in in_dim if a not in set(avoid_anchors)]
        anchor = random.choice(pool or list(in_dim))
        moves = [c for c in RAGE_COMPARE if c not in set(avoid_compare)]
        compare = random.choice(moves or list(RAGE_COMPARE))
        # Counting only works when there is a number to count, and a two-word
        # fragment has no room to itemise anything.
        if length == "fragment" and compare in ("arithmetic", "receipt",
                                                "timeline"):
            compare = random.choice(("mas_pa", "parang", "swap"))
    if length == "fragment" and laugh in ("HAHAHAHAHA", "emoji"):
        laugh = "none"
    return {"register": register, "caps": caps, "length": length,
            "laugh": laugh, "punct": punct, "switch": switch,
            "anchor": anchor, "compare": compare, "dimension": dim}


# The "80-year-old comment" problem. Textbook Tagalog is what a model reaches
# for by default, and it is exactly what nobody types. Naming the dead
# constructions is far more effective than asking for "casual" - a model given
# only a positive instruction still writes "napakaganda nito talaga".
_RAGE_DEAD_TAGALOG = (
    "NEVER write like a textbook or like someone's lolo. Banned outright: "
    "'ito ay', 'iyan ay', 'sadyang', 'lubos', 'ngunit', 'subalit', 'datapwat', "
    "'aking', 'iyong', 'nawa', 'kahanga-hanga', 'napaka-' anything, 'po', "
    "'opo', 'maraming salamat', 'mabuhay', and any greeting or sign-off. "
    "No hashtags. No jejemon spelling (eEoW pOw, d2, cnu) - that is 2009, not "
    "now.\n"
)
_RAGE_LIVE_TAGALOG = (
    "Type the way people actually type online now - late twenties, phone in "
    "hand, mid-scroll. Contractions and casual spelling are the default: yung, "
    "ung, tas, nalang, sakin, sayo, dun, ganto, ganun, di, wag, san, pano, "
    "ano ba yan. Particles carry the attitude: nga, naman, kasi, eh, ba, daw, "
    "pala, talaga, pa rin, na lang, di ba.\n"
    "The English half is Gen Z internet English, not school English: no "
    "because, not the, the way na, istg, fr, lowkey, deadass, the audacity, "
    "bestie, girl, as in, hello???, excuse me??, period.\n"
    "Filipino slang that belongs here: sheesh, awit, lupit, solid, bet, pak, "
    "ganern, charot, chariz, gigil, jusko, hays, grabe, tol, bes, mars, idol, "
    "sana all, alam mo yun, echos, ick.\n"
    "STRUCTURE is what separates this from a formal comment, more than any "
    "word: start mid-thought, run clauses together, ask questions you already "
    "know the answer to, and stop without tidying the ending.\n"
)
_STAY_ON_POST = (
    "STAY ON THE POST. The only things that exist are what the post shows and "
    "says, and the slang in the example comments. Do NOT bring in a brand, a "
    "company, an app, a team, a place or a PERSON that the post did not "
    "mention - no naming anybody the post did not name. If you want a concrete "
    "detail, take it from the post.\n"
    "This applies to the GOVERNMENT above all: do not mention the government, "
    "an agency, a mayor, a senator, an official, taxes or corruption unless "
    "the post or the example comments raised it first. If the post is not "
    "about the government, it is not about the government - do not make it "
    "about the government.\n"
    "The one exception is the comparison in the STYLE block, if there is one: "
    "that yardstick is your OWN everyday life (your sahod, your wifi, your "
    "load), never a named third party.\n"
)

_RAGE_GUARD = (
    "Angry at the situation, never abusive at a person: no slurs, no threats, "
    "no sexual content, and do not call anyone a name.\n"
    "No hard swearing either - not tangina, putangina, gago, tanga, bobo, "
    "ulol, punyeta, yawa or their spellings. Exasperation words carry it "
    "further anyway: jusko, hays, grabe, bwisit, leche, hala, awit, sus, "
    "pastilan.\n"
)

# Enforced, not merely requested: a prompt line alone still let "TANGINA NAMAN"
# through, and a comment removed for profanity - or an account flagged for it -
# costs far more than the retry. Loosen this list if you want the harder
# register; it is the only place it is decided.
# Matched on WORD BOUNDARIES, not as substrings: "tanga" sits inside
# "natangay ng baha", which is exactly the phrasing a flood post attracts, and
# a substring check threw those away as swearing.
_HARD_PROFANITY_RE = re.compile(
    r"\b(?:t+a+ng+i+na\w*|tang\s*ina|puta\w*|gag[oa]|tanga|bobo|ulol|"
    r"punyeta|yawa|leche\s*ka|fuck\w*|shit|bitch)\b", re.I)


def style_lang(style):
    """Which language the sampled style is actually in.

    english_heavy is still Taglish - English-dominant Taglish is a register,
    not a different language - so only Bisaya changes what the persona dial and
    the prompt should say.
    """
    if not style:
        return None
    return "bisaya" if style.get("switch") == "bisaya" else "tl"


def rage_style_block(style):
    """The sampled style as system-prompt lines."""
    if not style:
        return ""
    lines = [
        "You are ANNOYED at this post. Every comment you write carries a "
        "complaint - something here is stupid, cheap, fake, overdone, badly "
        "done, or not worth what it took. Find it and go at it.",
        "You are NOT a supporter and NOT a fan. Never encourage, never cheer, "
        "never wish them well, never say it is nice. Praise appears ONLY as "
        "sarcasm pointed at the flaw - if a line could be screenshotted as a "
        "genuine compliment, it is wrong and you must rewrite it.",
        "If the post looks fine to you, that IS the annoyance: it is trying "
        "too hard, it is showing off, everyone has seen it before, or you are "
        "sick of it being posted. Be sarcastic about that. Never resolve it "
        "into approval.",
        "STYLE - this comment is ONE specific person, and these are their "
        "habits. Follow every one of them exactly; they matter more than "
        "sounding polished:",
        f"- Register: {RAGE_REGISTER[style['register']]}",
        f"- Capitalisation: {RAGE_CAPS[style['caps']]}",
        f"- Length: {RAGE_LENGTH[style['length']]}",
        f"- Punctuation: {RAGE_PUNCT[style['punct']]}",
        f"- Language: {RAGE_SWITCH[style['switch']]}",
    ]
    laugh = RAGE_LAUGH.get(style["laugh"], "")
    if laugh:
        lines.append(f"- Laughter: include {laugh} exactly as written, once, "
                     f"where it falls naturally.")
    else:
        lines.append("- Laughter: none. Do not laugh in this one.")
    if style.get("anchor"):
        dim = style.get("dimension") or ""
        lines.append(
            f"- COMPARISON: you are complaining about the {dim} of the thing "
            f"in the post. Hold it against {style['anchor']}, which is small, "
            f"personal and domestic - and which can be judged on {dim} too. "
            "That shared dimension is the whole joke.")
        lines.append(
            "  It must LINE UP. Compare a cost to a cost, a wait to a wait, a "
            "blur to something you genuinely cannot see. If the yardstick "
            "above does not line up with what this post is actually about, "
            "DROP IT and just react - a forced comparison is worse than none, "
            "and 'mas malabo pa sa sahod' is not a joke, a salary is not "
            "blurry.")
        move = RAGE_COMPARE.get(style.get("compare"))
        if move:
            lines.append(f"- How to make it: {move} Do NOT fall back on "
                         f"'mas ___ pa sa ___' unless that is the move named "
                         f"here.")
    else:
        lines.append("- Comparison: none. Nothing in this post can be held "
                     "against a household yardstick, so do not invent one - "
                     "react straight at what is actually there.")
    return ("\n".join(lines) + "\n" + _STAY_ON_POST + _RAGE_DEAD_TAGALOG
            + _RAGE_LIVE_TAGALOG + _RAGE_GUARD)


def rage_style_line(style):
    """One-line restatement for the END of the user prompt.

    Same reason persona_line() exists: stated once at the top, the style loses
    to the paragraphs of rules after it and the comment comes back tidy.
    """
    if not style:
        return ""
    bits = [f"{style['register']}", f"{style['caps']}", f"{style['length']}",
            f"{style['punct']}", f"{style['switch']}"]
    laugh = RAGE_LAUGH.get(style["laugh"], "")
    if laugh:
        bits.append(f"laugh={laugh}")
    out = ("Style to hit exactly: " + ", ".join(bits) + ". ")
    if style.get("anchor"):
        out += (f"Build it around holding the post's {style.get('dimension')} "
                f"against {style['anchor']} - and drop that comparison "
                f"entirely if it does not honestly line up. ")
    else:
        out += ("No comparison in this one - do not reach for the price of "
                "bigas or your sahod or a queue somewhere unless the post is "
                "genuinely about that. ")
    return out


COMMENT_SYSTEM = _COMMENT_SYSTEM_BASE + persona_block() + _COMMENT_SYSTEM_SHORT_TAIL


def _example_shape(example):
    """(words, sentences, is_paragraph) for the example comment."""
    ex = (example or "").strip()
    words = len(ex.split())
    sents = [s for s in re.split(r"(?<=[.!?])\s+", ex) if s.strip()]
    sent_n = max(1, len(sents))
    # Either several sentences, or simply too long to be a one-liner.
    return words, sent_n, (sent_n > 1 or words >= 12)


def comment_system_for(example, tone=None):
    """The system prompt that fits THIS example's shape and tone.

    `tone` must already be through resolve_tone(); None means neutral,
    which leaves every prompt exactly as it was before tones existed.
    """
    _, _, paragraph = _example_shape(example)
    tail = (_COMMENT_SYSTEM_PARAGRAPH_TAIL if paragraph
            else _COMMENT_SYSTEM_SHORT_TAIL)
    # Rebuilt per call rather than cached, so editing COMMENT_PERSONA takes
    # effect on the very next comment instead of at the next restart.
    return (comment_base_for(tone)
            + persona_block(persona_for(example, tone))
            + tone_block(tone) + tail)

# Meaning-preserving variations only — none of these may introduce new content.
_COMMENT_ANGLES = [
    "Use different words for the same idea.",
    "Keep the same words' meaning but change the word order.",
    "Swap the intensifier for another one that means the same.",
    "Say it a bit more casually, same meaning.",
    "Say it slightly shorter, same meaning.",
    "Say it slightly longer, same meaning.",
    "Begin with a different word than the example does.",
    "Use the plainest everyday phrasing for the same idea.",
    "Use a common texting-style spelling, same meaning.",
]

STOP_REQUESTED = None
# Per-THREAD, because every device runs in its own thread. A single global was
# overwritten by whichever device started last, so _is_reel_context() could read
# another phone's URL and send this one down the reel path (or the reverse). It
# was also stale for links 2+ of a mass-links run.
_tls = threading.local()


def set_current_post_url(url):
    _tls.post_url = url


def current_post_url():
    return getattr(_tls, "post_url", None)

def stop_requested():
    try:
        return bool(STOP_REQUESTED and STOP_REQUESTED())
    except Exception:
        return False

# ── Abort support ──
# Abort must be felt immediately, not at the next loop boundary. Every long
# wait in the automation goes through abort_sleep(), so a running action bails
# out within ~0.1s instead of finishing its retry budget.
def abort_sleep(seconds, step=0.1):
    """time.sleep() that returns False the instant Abort is requested."""
    end = time.time() + seconds
    while True:
        if stop_requested():
            return False
        remain = end - time.time()
        if remain <= 0:
            return True
        time.sleep(min(step, remain))

def close_fb(serial):
    """Force-stop Facebook Lite on one device."""
    try:
        run(["shell", "am", "force-stop", FB_PACKAGE], serial=serial, timeout=15)
        print(f"    [{serial}] Facebook Lite closed.")
        return True
    except Exception as e:
        print(f"    [{serial}] could not close Facebook Lite: {e}")
        return False

def abort_devices(serials):
    """Force-stop Facebook Lite on every given device, in parallel."""
    threads = []
    for s in serials or []:
        t = threading.Thread(target=close_fb, args=(s,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=12)

REACTIONS = {"like", "love", "care", "haha", "wow", "sad", "angry"}
REACTION_ORDER = ["like", "love", "care", "haha", "wow", "sad", "angry"]

REACTION_LABELS = {
    "like":  ["like", "like reaction", "thumbs up"],
    "love":  ["love", "love reaction", "heart"],
    "care":  ["care", "care reaction"],
    "haha":  ["haha", "haha reaction", "ha ha"],
    "wow":   ["wow", "wow reaction"],
    "sad":   ["sad", "sad reaction"],
    "angry": ["angry", "angry reaction"],
}

BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
ACCOUNT_SKIP_TEXT = ("dismiss", "switch accounts", "log in to another account",
                     "log into another account", "add account", "create new account",
                     # Buttons that sit in the account-switcher sheet but are NOT
                     # accounts — clicking them navigates away from the task.
                     "accounts center", "account center", "see all profiles",
                     "manage accounts", "add another account", "log out", "logout",
                     # menu chrome sitting behind the switcher sheet
                     "switch account", "view your profile", "see more",
                     "switch accounts")
SUSPENDED_MARKERS = (
    "account suspended",
    "your account has been suspended",
    "we suspended your account",
    "account disabled",
    "your account has been disabled",
    "we disabled your account",
    "disagree with decision",
    "appeal",
)

_AI_SSL_CONTEXT = [None]
_AI_SSL_LOCK = threading.Lock()


def _ai_ssl_context():
    with _AI_SSL_LOCK:
        if _AI_SSL_CONTEXT[0] is None:
            _AI_SSL_CONTEXT[0] = ssl.create_default_context()
        return _AI_SSL_CONTEXT[0]


class _KeepAliveAIHandler(urllib.request.BaseHandler):
    """Reuse TCP/TLS connections for AI chat requests, and only for those.

    urllib opens a brand-new connection for every request. Against the
    Cloudflare-fronted endpoint that is DNS + TCP + TLS before the prompt is
    even sent - measured at ~0.8s of a ~1.1s round trip - and one comment often
    needs two or three calls once the quality gates reject a wording. Requests
    opt in by carrying ``_ai_keepalive``; every other request on these openers
    (link resolution, Pollinations, update checks) falls through to the stock
    handlers untouched. The body is read in full before the connection goes
    back to the pool, so a caller can never leave a half-read response on a
    socket another phone is about to reuse.
    """

    handler_order = 450          # ahead of the stock HTTP(S)Handler (500)
    MAX_IDLE = 8

    def __init__(self):
        self._lock = threading.Lock()
        self._idle = {}
        self._epoch = 0

    def reset(self):
        """Drop idle sockets, e.g. after the endpoint or its key changed."""
        with self._lock:
            idle, self._idle = self._idle, {}
            self._epoch += 1
        for pool in idle.values():
            for conn in pool:
                try:
                    conn.close()
                except Exception:
                    pass

    def idle_count(self):
        with self._lock:
            return sum(len(pool) for pool in self._idle.values())

    def _checkout(self, key, timeout):
        with self._lock:
            pool = self._idle.get(key)
            if pool:
                return pool.pop(), True, self._epoch
            epoch = self._epoch
        scheme, host, port = key
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout,
                                               context=_ai_ssl_context())
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        return conn, False, epoch

    def _checkin(self, key, conn, epoch):
        with self._lock:
            if epoch == self._epoch:
                pool = self._idle.setdefault(key, [])
                if len(pool) < self.MAX_IDLE:
                    pool.append(conn)
                    return
        conn.close()

    def _open(self, req, scheme):
        if not getattr(req, "_ai_keepalive", False):
            return None          # not ours: let the stock handler take it
        parts = urllib.parse.urlsplit(req.full_url)
        if not parts.hostname:
            raise urllib.error.URLError("no host given")
        key = (scheme, parts.hostname, parts.port or (443 if scheme == "https" else 80))
        timeout = req.timeout
        if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
            timeout = None
        headers = dict(req.header_items())
        for attempt in (0, 1):
            conn, reused, epoch = self._checkout(key, timeout)
            try:
                if reused and conn.sock is not None:
                    conn.sock.settimeout(timeout)
                try:
                    conn.request(req.get_method(), req.selector, body=req.data,
                                 headers=headers)
                except (BrokenPipeError, ConnectionResetError,
                        ConnectionAbortedError, http.client.CannotSendRequest):
                    raise
                except OSError as exc:
                    # Same shape as urllib's own handler for a failed send.
                    raise urllib.error.URLError(exc) from None
                response = conn.getresponse()
                data = response.read()
            except (http.client.RemoteDisconnected, http.client.CannotSendRequest,
                    BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                conn.close()
                # The server closed an idle keep-alive socket between requests.
                # Nothing was processed, so one fresh connection is safe; a
                # timeout is NOT retried - the server may still be generating.
                if reused and attempt == 0:
                    continue
                raise
            except BaseException:
                conn.close()
                raise
            if response.will_close:
                conn.close()
            else:
                self._checkin(key, conn, epoch)
            result = urllib.response.addinfourl(io.BytesIO(data), response.headers,
                                                req.full_url, response.status)
            result.msg = response.reason
            return result
        raise urllib.error.URLError("AI connection closed")   # pragma: no cover

    def http_open(self, req):
        return self._open(req, "http")

    def https_open(self, req):
        return self._open(req, "https")


_AI_KEEPALIVE = _KeepAliveAIHandler()
_AI_LAN_KEEPALIVE = _KeepAliveAIHandler()
NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}), _AI_KEEPALIVE)


class _NoLANRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # LAN mode sends credentials and comments only to the saved endpoint.
        # Even a misconfigured gateway cannot redirect them to a public host.
        return None


LAN_NOPROXY = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoLANRedirect(), _AI_LAN_KEEPALIVE)

# Rate limit comes from the provider preset: a hosted endpoint needs spacing,
# a local Ollama does not. Read live so changing provider takes effect at once.
def ai_min_gap():
    try:
        return float(_ai("min_gap", 2.5))
    except (TypeError, ValueError):
        return 2.5


def set_ai_gap(seconds):
    """Override the pacing between AI calls for this process.

    Deliberately a function, not a module constant. FBPoison had AI_MIN_GAP and
    code that assigned to it; here the value lives in venom_ai.json, so leaving
    that constant in place would look settable while silently doing nothing.
    """
    cfg = aiconfig.load()
    cfg["min_gap"] = float(seconds)
    aiconfig.save(cfg)
    return cfg["min_gap"]
AI_REFERRER = "fbreactor"
# Held for the duration of an actual AI HTTP call, so only one is ever in
# flight for the whole fleet - see _pollinations_post().
_ai_http_lock = threading.Lock()

# Pollinations' own API docs: the ANONYMOUS tier is "one request every 15s"
# (Seed, via free registration, is one every 5s). venom_ai.json ships min_gap
# 1.0, i.e. ~15x too fast, which is what produced the endless 402/429s. This is
# enforced here rather than in the config because it is a property of the
# endpoint, not a user preference - and the AI SETTINGS "TEST" button rewrites
# that file regularly.
POLLINATIONS_MIN_INTERVAL = 15.5
_poll_last_call = [0.0]
_ai_slot_lock = threading.Lock()
_ai_next_slot = [0.0]
def _ai_wait_slot():
    with _ai_slot_lock:
        start_at = max(time.time(), _ai_next_slot[0])
        _ai_next_slot[0] = start_at + ai_min_gap()
    delay = start_at - time.time()
    if delay > 0:
        # abort_sleep, not time.sleep: with many devices this queue can be
        # seconds deep, and PURGE has to cut through it immediately.
        return abort_sleep(delay)
    return True

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002300-\U000023FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D\U0000202A-\U0000202E]",
    flags=re.UNICODE)

_TAGALOG_MARKERS = {
    "ang", "ng", "mga", "naman", "talaga", "ganda", "maganda", "grabe", "sana",
    "ako", "ikaw", "siya", "kami", "tayo", "kayo", "sila", "natin", "namin",
    "ninyo", "po", "opo", "yung", "kasi", "lang", "dito", "diyan", "ganyan",
    "ganito", "astig", "lupit", "petmalu", "idol", "salamat", "pangit", "kain",
    "pwede", "hindi", "wala", "meron", "ba", "nga", "raw", "daw", "ito", "iyan",
    "sobrang", "napaka", "ansaya", "saya", "bongga", "wagi", "keri", "sulit",
    "talaga", "tama", "oo", "oo nga", "eh", "di ba", "ano", "mismo", "sakto",
    "lodi", "paps", "sis", "bes", "mhie", "chika", "chismis", "nakakaloka",
    # Common everyday words that were missing, so short Taglish examples like
    # "wow its big, super laki" were misread as pure English and the model got
    # an ambiguous language instruction.
    "laki", "malaki", "napakalaki", "liit", "maliit", "sobra", "sobrang",
    "ganito", "ganda", "astig", "ang", "yan", "ito", "nito", "sya", "siya",
    "akin", "atin", "grabe", "parang", "medyo", "tapos", "pala", "yata",
    # Function words - the ones that actually carry the language. Without these
    # a perfectly good Tagalog line built from "sa/ng/na/para" scored as English
    # and was thrown away by the translation guard ("pasasalamat sa mga bagong
    # trake para sa apoy" was rejected as English). Deliberately excludes words
    # that are ALSO English ("at", "an", "no", "so") so an English example is
    # never mistaken for Tagalog.
    "sa", "ng", "na", "ay", "para", "ko", "mo", "niya", "nila", "namin",
    "kung", "dahil", "pag", "kaya", "nang", "ni", "kay", "upang", "nasa",
    "mula", "hanggang", "bawat", "lahat", "ganitong", "ganyang", "ating",
    "aming", "inyong", "kanilang", "bagong", "mas", "pinaka",
}

# _detect_lang's "tl" means "Filipino-family", not Tagalog specifically.
# Without these a Bisaya example scores zero markers, is read as English,
# and persona_for() then strips the Bisaya trait that was meant to handle
# it - the dial did nothing on exactly the comments it exists for. Same
# rule as the list above: nothing that is also an English word.
_BISAYA_MARKERS = {
    "nindot", "kaayo", "gwapo", "gwapa", "gwapaha", "kanindot", "grabeha",
    "unsa", "kinsa", "ngano", "pila", "dili", "daghan", "gamay", "dako",
    "maayo", "palihug", "nako", "nimo", "namo", "nato", "kamo", "ka",
    "gyud", "jud", "gud", "bitaw", "baya", "kuan", "murag", "mura", "mao",
    "naa", "duna", "aduna", "tanan", "karon", "ganina", "ugma", "gahapon",
    "diri", "diha", "didto", "buang", "lami", "bugnaw", "kusog", "hinay",
    "tarong", "padayon", "bisan", "amping", "pud", "sab", "unya", "og",
    "ug",
}
# Words the Bisaya list claims that Tagalog uses just as much. Without them
# "ka" alone made every Tagalog comment carrying the ordinary second-person
# pronoun ("grabe ka naman idol angas nito") register as Bisaya, so the
# translation guard threw it out and burned a retry. Listing them here rather
# than deleting them from _BISAYA_MARKERS keeps them working as Filipino
# markers for _detect_lang; it only stops them counting as proof of Bisaya.
_TAGALOG_MARKERS |= {"ka", "mura", "pila"}
# Telling the two apart needs the words only Bisaya has, so this is taken
# before the merge below folds them together.
_BISAYA_ONLY = _BISAYA_MARKERS - _TAGALOG_MARKERS
_TAGALOG_MARKERS |= _BISAYA_MARKERS

def _is_bisaya(text):
    """True when the text is Bisaya rather than Tagalog.

    _detect_lang lumps both under "tl" - they are both Filipino, and the
    translation guard needs that so a Tagalog rewrite of a Tagalog example
    is not thrown away. Bisaya still has to be told apart from Tagalog, or
    the Taglish and Filipino traits quietly turn "nindot kaayo ni oy" into
    "ganda nito grabe talaga" - another language, so another comment.
    """
    words = re.findall(r"[a-z]+", (text or "").lower())
    return any(w in _BISAYA_ONLY for w in words)


def _detect_lang(text):
    t = (text or "").strip().lower()
    if not t:
        return "tl"
    if any(ord(c) > 127 and c.isalpha() for c in t):
        return "other"
    words = re.findall(r"[a-z]+", t)
    tagalog_score = sum(1 for w in words if w in _TAGALOG_MARKERS)
    if tagalog_score >= 2:
        return "tl"
    if len(words) > 0 and tagalog_score / len(words) < 0.2:
        return "en"
    return "tl" if tagalog_score > 0 else "en"

def _plain_text(s, keep_sentence_punct=False):
    """Strip punctuation so a comment reads as phone-typed.

    `keep_sentence_punct` keeps periods and commas: a written-out paragraph
    needs its sentence breaks, and stripping them turned a two-sentence comment
    into one run-on line that no longer resembled the example.
    """
    import unicodedata
    apostrophes = "'’‘`´ʼ"
    keep = ".,!?" if keep_sentence_punct else ""
    out = []
    for ch in (s or ""):
        if ch in apostrophes:
            continue
        if ch in keep:
            out.append(ch)
            continue
        out.append(" " if unicodedata.category(ch)[0] in ("P", "S") else ch)
    txt = re.sub(r"\s+", " ", "".join(out)).strip()
    if keep_sentence_punct:
        txt = re.sub(r"\s+([.,!?])", r"\1", txt)      # no space before . or ,
        txt = re.sub(r"([.,!?])(?=\S)", r"\1 ", txt)  # one space after
        txt = re.sub(r"([.,!?])\s*(?=[.,!?])", "", txt)   # no doubled marks
    return txt.strip()

# The laugh emoji a sampled style is allowed to keep. Everything else still
# goes: an emoji the style did not ask for is the model decorating.
_LAUGH_EMOJI = "\U0001F923\U0001F602\U0001F480\U0001F62D\U0001F644"
# What each punctuation axis is allowed to leave on the page.
_STYLE_PUNCT_KEEP = {"none": "", "ellipsis": ".", "bang": "!",
                     "comma_splice": ","}


def _style_punct(text, style):
    """Strip punctuation down to what the sampled style actually uses.

    _plain_text takes everything, which is right for a normal comment and
    fatal for this one: the commas ARE the rant, the !!! IS the shouting, and
    the ... IS the exhaustion. So keep exactly the marks the axis asked for.
    """
    keep = _STYLE_PUNCT_KEEP.get(style.get("punct"), "")
    # A rhetorical question needs its question mark whatever the punctuation
    # axis rolled - "bakit ganon" with no mark reads as a statement.
    if style.get("register") == "tanong":
        keep += "?"
    if style.get("laugh") == "emoji":
        keep += _LAUGH_EMOJI
    # Hyphens hold Tagalog affixes together (mag-approve, pa-cute); turning
    # them into spaces is a spelling error, not a cleanup.
    out = []
    for i, ch in enumerate(text or ""):
        prev = text[i - 1] if i else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if ch in keep or ch.isalnum() or ch.isspace():
            out.append(ch)
        elif ch == "-" and prev.isalpha() and nxt.isalpha():
            out.append(ch)
        # A separator inside a number is part of the number: without this
        # "1.2 billion" arrives as "1 2 billion", which reads as a typo rather
        # than a figure.
        elif ch in ".," and prev.isdigit() and nxt.isdigit():
            out.append(ch)
        else:
            out.append(" ")
    txt = re.sub(r"\s+", " ", "".join(out)).strip()
    txt = re.sub(r"\s+([.,!?])", r"\1", txt)          # no space before a mark
    if "." in keep:
        txt = re.sub(r"\.{2,}", "...", txt)           # every run becomes ...
        # A lone full stop is out - it reads formal - but not the one holding a
        # decimal together.
        txt = re.sub(r"(?<![\d.])\.(?![\d.])", "", txt)
    if "!" in keep:
        txt = re.sub(r"!{2,}", "!!!", txt)
    if "?" in keep:
        txt = re.sub(r"\?{4,}", "???", txt)
    return txt.strip()


def _style_char_cap(style):
    """Character ceiling for a sampled length band, with room to land on."""
    _, hi = _RAGE_LENGTH_WORDS.get(style.get("length"), (6, 15))
    # Filipino words average ~6 characters plus the space, and a rant needs the
    # whole 60 - the old flat 200-char cap cut one in half mid-clause.
    return max(60, int(hi * 8) + 40)


def sanitize_comment(text, fallback, style=None):
    """Clean a model reply into a postable comment.

    With `style` set (sarcastic anger) the cleanup runs in style-preserving
    mode. It has to: the default path lowercases ALL CAPS, deletes every
    comma, dot and exclamation mark and strips emoji, so a sampled style
    arrives at the phone with its capitalisation, its comma splices, its !!!
    and its laugh already destroyed - every axis flattened into the same
    punctuation-free lowercase line. Without `style` this behaves exactly as
    it always has.
    """
    if not text:
        return fallback
    text = html.unescape(text).strip()
    for tag in ("</s>", "<|end|>", "<|assistant|>", "<|user|>",
                "<|system|>", "assistant", "Comment:", "comment:"):
        text = text.replace(tag, " ")
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]
    # "i " is a preamble marker only in the default path. A lowercase styled
    # comment legitimately opens with it ("i cant with this"), and dropping
    # that line threw away the comment and kept the model's own preamble.
    skip = ("here", "sure", "this response", "here's", "note",
            "explanation", "as an", "okay", "ok,")
    if not style:
        skip += ("i ",)
    pick = ""
    for ln in lines:
        low = ln.lower()
        if low.startswith(skip):
            continue
        pick = ln
        break
    if not pick:
        pick = lines[0] if lines else fallback
    pick = pick.strip().strip('"').strip("'").strip("`").strip()
    # Strip a LIST MARKER the model prefixed ("- ", "* ", "1. ", "2) ") - not a
    # leading number. The old character class ate any run of digits and dots at
    # the front, so "1.2 billion tapos sira agad" was posted as "billion tapos
    # sira agad" and "500 million" as "million": the figure the comment is
    # about, deleted. A marker needs the separator AND the space after it.
    pick = re.sub(r"^(?:[-*•·]+\s*|\d{1,2}[.)]\s+)+", "", pick)
    pick = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", pick)
    if style:
        if style.get("laugh") == "emoji":
            pick = "".join(c for c in pick
                           if c in _LAUGH_EMOJI or not _EMOJI_RE.match(c))
        else:
            pick = _EMOJI_RE.sub("", pick)
        pick = _style_punct(pick, style)
        # A missing laugh is added, not rejected. Insisting on it in the gate
        # cost three API calls per comment and then dropped the whole thing to
        # the paraphrase path, which posts a reworded example - an OFF-TOPIC
        # comment - as the punishment for a missing "hays". Appending it is
        # what the style asked for anyway.
        laugh = RAGE_LAUGH.get(style.get("laugh"), "")
        if laugh and pick and laugh not in pick:
            letters = [c for c in pick if c.isalpha()]
            shouting = bool(letters) and all(c.isupper() for c in letters)
            pick = f"{pick} {laugh.upper() if shouting else laugh}".strip()
        # No all-caps flattening here - ALL CAPS is a sampled axis, and
        # lowercasing it deletes the loudest person in the comment section.
        cap = _style_char_cap(style)
    else:
        pick = _EMOJI_RE.sub("", pick)
        # Every comment goes out punctuation-free, long or short: no dots, no
        # commas, no symbols - that is what a phone-typed comment looks like.
        pick = _plain_text(pick)
        # "MALAKAS MALAKAS NYA" style shouting reads as generated, not typed.
        letters = [c for c in pick if c.isalpha()]
        if len(letters) > 5 and all(c.isupper() for c in letters):
            pick = pick.lower()
        # Cap relative to the example: a flat 200 chars truncated any paragraph
        # longer than that mid-sentence.
        cap = max(200, len(str(fallback or "")) * 2)
    if len(pick) > cap:
        pick = pick[:cap].rsplit(" ", 1)[0].strip()
    # A two-word dismissal IS the balewala register, so the floor drops to one
    # word when a style asked for a fragment.
    floor = 1 if (style and style.get("length") == "fragment") else 2
    if len(pick.split()) < floor or pick.lower() in ("nice", "good", "great",
                                                     "ok", "wow", "love"):
        return fallback
    return pick or fallback

# Models that answered 400 because of the sampling parameters. Remembered for
# the rest of the run so the 400 is paid ONCE per model, not once per comment.
_no_sampling = {}


def _sampling_rejected(detail):
    """True when a 400 body blames temperature / top_p rather than the request.

    Newer Claude models refuse them outright ("`temperature` is deprecated for
    this model", "`temperature` and `top_p` cannot both be specified"). Before
    this check every comment 400'd on the configured model and silently landed
    on the fallback - the model set in AI SETTINGS never ran once.
    """
    low = str(detail or "").lower()
    return "temperature" in low or "top_p" in low


# A credential or billing failure is PERMANENT: every phone in the fleet gets
# the identical answer, so retrying it 15 times per comment across 19 threads
# only delays the run and buries the cause. The generic "endpoint unreachable"
# message made a dead key look like a network blip - meanwhile the offline
# fallback quietly posted praise under an angry post. Detect it, say WHY once,
# and switch the AI off immediately.
_AI_FATAL_RE = re.compile(
    r"credit balance|billing|quota|insufficient|payment required|"
    r"invalid[_ ]?api[_ ]?key|authentication|unauthorized|expired", re.I)
_ai_fatal_said = [False]


def _fatal_ai_error(code, detail):
    """True when retrying cannot possibly help - bad key, no credit, no quota."""
    if code in (401, 403):
        return True
    return code == 400 and bool(_AI_FATAL_RE.search(detail or ""))


def _groq_chat(prompt_text, timeout=None, system=None, max_tokens=60, model=None,
               cfg=None, _probe=False):
    # Try the good model first; fall back to the small one only if the big one
    # is unavailable (rate limit / decommissioned), so a bad key or a busy
    # endpoint degrades instead of failing outright.
    # `prompt_text` may be a plain string or an OpenAI content-block list, which
    # is how an image rides along on the vision call - both go straight into the
    # user message.
    # `_probe` marks the health checks (session start, warm-up, the breaker's
    # own re-check): they always reach the endpoint, everything else is turned
    # away without a request while the shared breaker is open.
    if not _probe and _ai_breaker["open"]:
        raise AIEndpointCooling("AI endpoint is resting after failures: "
                                + (_ai_breaker["reason"] or "not answering"))
    cfg = ai_cfg() if cfg is None else cfg
    # venom_ai.json's "timeout" was ignored on the two call sites that matter -
    # generate_comment and _generate_context_comment pass no timeout, so every
    # comment used the 20s default no matter what the file said. A self-hosted
    # model answers a real prompt in 2-8s warm but 40-120s cold, so 20s turned
    # every cold spell into a wall of timeouts.
    if timeout is None:
        try:
            timeout = float(cfg.get("timeout") or 90)
        except Exception:
            timeout = 90.0
    models = [model] if model else [m for m in (cfg.get("model"),
                                                cfg.get("fallback_model")) if m]
    # FIX 3: model and fallback_model are routinely set to the SAME name
    # (gemma-sea-lion-9b twice). Retrying an identical model after a timeout
    # just pays the timeout again - 2 models x 2 attempts x 20s = 80s before
    # the fallback provider was even reached. De-duplicate, keeping order.
    models = list(dict.fromkeys([m for m in models if m]))
    if not models:
        raise RuntimeError("no AI model configured - set one in AI SETTINGS")
    headers = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0"}
    key = str(cfg.get("api_key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = str(cfg.get("url") or "")
    last = None
    for mdl in models:
        base = {
            "model": mdl,
            "messages": [
                {"role": "system", "content": system or COMMENT_SYSTEM},
                {"role": "user",   "content": prompt_text},
            ],
            "max_tokens": max_tokens,
        }
        sampling = {"temperature": float(cfg.get("temperature", 0.85)),
                    "top_p": 0.9}
        # Anthropic's OpenAI-compat endpoint rejects sending temperature and
        # top_p together (400 invalid_request_error); every other provider here
        # tolerates both, so only drop top_p for Claude.
        if str(cfg.get("provider") or "") == "anthropic":
            sampling.pop("top_p", None)
        if _no_sampling.get(mdl):
            sampling = {}
        # Two shots at most: the body as configured, then - only if the endpoint
        # names the sampling params as the problem - the same model again
        # without them.
        for attempt in (0, 1):
            body = dict(base)
            body.update(sampling)
            req = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"), headers=headers)
            req._ai_keepalive = True
            started = time.time()
            try:
                opener = LAN_NOPROXY if aiconfig.get_server_mode(cfg) == "lan" else NOPROXY
                with opener.open(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                _ai_breaker_success(None if _probe == "warmup" else time.time() - started)
                return content
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "replace")
                except Exception:
                    detail = ""
                finally:
                    e.close()
                last = RuntimeError(f"HTTP {e.code} on {mdl}: {detail[:200]}"
                                    if detail else f"HTTP {e.code} on {mdl}")
                if e.code >= 500:
                    # Overloaded, crashed or unreachable behind a proxy (502/
                    # 503/504/524): the endpoint itself is the problem, and
                    # the fallback model lives on the same server.
                    # At session start a 5xx is a definite answer: open now
                    # rather than letting every phone find out on its own.
                    _ai_breaker_failure(last, http_code=e.code, force=_probe == "start")
                    if _ai_breaker["open"] and not _probe:
                        raise last
                # No key, no credit, no quota: the fallback model shares the
                # same key, so trying it just repeats the same 400.
                if _fatal_ai_error(e.code, detail):
                    if not _ai_fatal_said[0]:
                        _ai_fatal_said[0] = True
                        try:
                            said = json.loads(detail)["error"]["message"]
                        except Exception:
                            said = (detail or "").strip() or f"HTTP {e.code}"
                        print(f"  [ai] !! {mdl}: {said[:180]}")
                        print("  [ai] !! this is a KEY/BILLING problem, not a "
                              "network one. Fix it in AI Settings - the AI is "
                              "off for this run.")
                        report_error("AI_AUTH_FAILED", detail=f"HTTP {e.code} on {mdl}: {said}",
                                     severity="error")
                    _ai_off[0] = True
                    raise last
                if 400 <= e.code < 500 and e.code != 429 and not (
                        e.code == 400 and _sampling_rejected(detail)):
                    _ai_say_once(f"rejected-{e.code}", "AI_REQUEST_REJECTED",
                                 f"HTTP {e.code} on {mdl}: {detail[:200]}")
                # The MODEL is fine, the sampling params are not. Retry it as-is
                # rather than dropping to the fallback model.
                if (e.code == 400 and sampling and attempt == 0
                        and _sampling_rejected(detail)):
                    _no_sampling[mdl] = True
                    sampling = {}
                    print(f"  [ai] {mdl} refuses temperature/top_p - "
                          f"resending without them")
                    continue
                if e.code == 429:
                    # Free-tier rate limit, shared by every phone. Honour the
                    # server's Retry-After when it sends one; a short wait here
                    # is far cheaper than dropping to the offline fallback.
                    try:
                        delay = float(e.headers.get("retry-after") or 0)
                    except (TypeError, ValueError):
                        delay = 0
                    if not abort_sleep(min(max(delay, 2.0), 10.0)):
                        raise
                break
            except Exception as e:
                last = e
                if _ai_transport_error(e):
                    # A refused/unresolvable endpoint at session start is
                    # down; a start-up TIMEOUT may just be a cold model
                    # loading, so that one only counts like any other.
                    _ai_breaker_failure(e, force=_probe == "start" and not _is_timeout(e))
                    if _ai_breaker["open"] and not _probe:
                        raise
                break
    raise last if last else RuntimeError("AI: no model available")

def _pollinations_get(prompt_text, timeout=35, system=None, max_tokens=40,
                      cfg=None):
    # The old plain-text GET (text.pollinations.ai/<prompt>) now answers 402
    # "Payment Required" for anonymous callers - Pollinations deprecated it.
    # The keyless POST /openai endpoint still works, so route through it and
    # leave every existing call site (generate_comment, ai_reply, start_llama)
    # untouched.
    # Single quick shot - the retry/backoff lives in the _pollinations_post
    # call that generate_comment makes right after this, so we don't hammer the
    # same throttled IP twice.
    return _pollinations_post(prompt_text, timeout=timeout, system=system,
                              max_tokens=max_tokens, tries=1, cfg=cfg)

# Pollinations does not fail with an HTTP error when the account is out of
# credits - it answers 200 OK and puts the apology in message.content:
#   "The account behind this API key doesn't have enough credits. Please
#    [top up](https://enter.pollinations.ai/top-up?...) or [complete a quest]"
# Verified 2026-09-25 against the saved token. Nothing checked for it, so that
# sentence became a candidate COMMENT: it got as far as the language gate,
# was rejected as "translated to English", and the attempt was burnt - while
# still paying the fleet-wide lock and its 5.5s gap, twice per generation
# (GET is the same POST with tries=1). That is a provider that can never
# succeed being retried forever by every phone.
_POLL_DEAD_RE = re.compile(
    r"enough credits|top-?up|complete a quest|insufficient|quota|"
    r"payment required|billing|enter\.pollinations\.ai", re.I)
# Latched for the run once seen, so the fleet pays for this discovery ONCE.
_poll_dead = [False]


def _pollinations_post(prompt_text, timeout=20, system=None, max_tokens=60,
                       tries=2, cfg=None):
    cfg = ai_cfg() if cfg is None else cfg
    if aiconfig.get_server_mode(cfg) == "lan":
        raise RuntimeError("External AI fallback is disabled in LAN mode")
    if _poll_dead[0]:
        raise RuntimeError("Pollinations is out of credits for this key")
    # Keyless, free, OpenAI-shaped. Only text.pollinations.ai/openai still
    # answers - api.pollinations.ai/v1/chat/completions returns 522 (down), so
    # it was dropped. Anonymous IPs are throttled to one in-flight request, so
    # a burst gets 429 "Queue full"; retry with a short backoff rather than
    # failing the comment. Sending a referrer identifies the app for a slightly
    # kinder limit. `tries` is capped low so a throttled device falls back to
    # the pool in seconds instead of stalling the whole run.
    # Two different APIs, picked by whether we have a key:
    #   * with a key  -> the CURRENT API at gen.pollinations.ai. The legacy host
    #     answers "402 ... legacy text API is being deprecated for authenticated
    #     users. Please migrate to https://enter.pollinations.ai" for any keyed
    #     request, so a token MUST go here or it can never work.
    #   * without one -> the legacy anonymous host, one request per 15s.
    token = str(cfg.get("pollinations_token", "") or "").strip() or \
        os.environ.get("POLLINATIONS_TOKEN", "").strip()
    if token:
        url = "https://gen.pollinations.ai/v1/chat/completions"
        # gen.pollinations.ai/v1/models does not list "openai-fast"; "openai"
        # is the general-purpose alias there.
        model_name = "openai"
    else:
        url = "https://text.pollinations.ai/openai"
        # text.pollinations.ai/models lists exactly ONE model available to
        # anonymous callers: openai-fast (GPT-OSS 20B, tier=anonymous).
        model_name = "openai-fast"
    last = None
    for attempt in range(max(1, tries)):
        payload = json.dumps({
            "model": model_name,
            "messages": [
                {"role": "system", "content": system or COMMENT_SYSTEM},
                {"role": "user", "content": prompt_text},
            ],
            "temperature": 0.65,
            "top_p": 0.9,
            "max_tokens": max_tokens,
            "seed": random.randint(1, 1_000_000),
            "referrer": AI_REFERRER,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "User-Agent": "Mozilla/5.0",
                   "Referer": AI_REFERRER}
        # Key set above; with none we stay on the keyless anonymous path.
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            # ONE request in flight at a time, fleet-wide. Pollinations answers
            # "Queue full for IP: 1 requests already queued (max: 1)", and with
            # 18 phones calling at once every extra request is a guaranteed 429
            # - the phones were throttling themselves. _ai_wait_slot() only
            # spaces the STARTS; a call runs for seconds, so without this lock
            # they still overlap.
            with _ai_http_lock:
                # Respect the anonymous tier's one-request-every-15s rule for
                # the whole fleet before opening the connection.
                # A token moves us to the Seed tier: one request every 5s.
                gap = 5.5 if token else POLLINATIONS_MIN_INTERVAL
                wait = gap - (time.time() - _poll_last_call[0])
                if wait > 0 and not abort_sleep(wait):
                    raise RuntimeError("stopped")
                try:
                    with NOPROXY.open(req, timeout=timeout) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                finally:
                    _poll_last_call[0] = time.time()
            said = data["choices"][0]["message"]["content"]
            # A 200 that is really a billing notice. Say it once, then stop
            # asking - retrying cannot make credits appear, and every retry
            # holds the fleet-wide lock that all the other phones queue on.
            if said and _POLL_DEAD_RE.search(said):
                if not _poll_dead[0]:
                    _poll_dead[0] = True
                    print(f"  [ai] !! Pollinations returned a BILLING notice "
                          f"instead of a comment: {said.strip()[:120]}")
                    print("  [ai] !! disabling Pollinations for this run - "
                          "top up the token or clear it in AI Settings.")
                raise RuntimeError("Pollinations is out of credits for this key")
            return said
        except urllib.error.HTTPError as e:
            last = e
            # 429 = per-IP queue full; 402 = anonymous throttle (both are
            # dynamic and clear once the IP goes idle for a moment); 5xx/522 =
            # upstream hiccup. Back off and retry. Any other 4xx will not clear,
            # so stop early.
            if e.code in (402, 429) or 500 <= e.code < 600:
                if attempt + 1 >= tries or not abort_sleep(2.0 * (attempt + 1)):
                    break
                continue
            break
        except Exception as e:
            last = e
            if attempt + 1 >= tries or not abort_sleep(1.5):
                break
            continue
    raise last if last else RuntimeError("all Pollinations POST endpoints failed")

# Little words that carry no meaning of their own. A rewrite is allowed to keep
# these - Tagalog cannot be written without "ang/ng/sa" - so they are excluded
# when measuring how much of the example's wording was actually reused.
_STOPWORDS = {
    # Tagalog / Filipino
    "ang", "ng", "mga", "sa", "ay", "at", "na", "ni", "kay", "nang", "para",
    "may", "kung", "dahil", "pero", "ito", "iyan", "yan", "yun", "nito", "niyan",
    "ako", "ikaw", "siya", "sya", "kami", "tayo", "kayo", "sila", "namin",
    "natin", "ninyo", "nila", "niya", "ko", "mo", "din", "rin", "lang", "naman",
    "kasi", "din", "dito", "diyan", "doon", "ba", "po", "opo", "pa", "nga",
    "wala", "meron", "isang", "isa", "hindi", "oo",
    # English
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "to", "of",
    "in", "on", "for", "and", "or", "but", "it", "its", "this", "that", "with",
    "will", "can", "so", "as", "by", "from", "we", "you", "they", "he", "she",
    "our", "your", "their", "his", "her", "not", "have", "has", "had", "do",
    "does", "did", "all", "very", "too", "if", "then", "than", "there", "here",
}


def _content_words(text):
    """Meaning-carrying words only, for overlap scoring."""
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) >= 3 and w not in _STOPWORDS}


# How many times one word may appear under a single post before it is retired.
# Without this an example's stand-out word ("paldo") rode into almost every
# comment, because the prompt asked for the examples' slang and the quality
# gate REQUIRED some of it back.
WORD_USE_LIMIT = 3


def _word_key(word):
    """Collapse a word to the form the repetition cap counts.

    Filipino glues the linker -ng onto vowel-final words (paldo -> paldong),
    and people stretch letters for emphasis (paldooo, hahaha). Counted apart,
    those are how one word reached fifteen comments. Merging a few unrelated
    -ng words into one budget is a cheap price for catching that.
    """
    w = str(word or "").lower()
    w = re.sub(r"([aeiou])\1+", r"\1", w)      # paldooo / paldoo -> paldo
    w = re.sub(r"(.)\1{2,}", r"\1", w)         # grabeee, pffff
    if w.endswith("ng") and len(w) >= 6 and w[-3] in "aeiou":
        w = w[:-2]
    return w


def _content_keys(text):
    """The capped forms of a comment's meaning-carrying words."""
    return {_word_key(w) for w in _content_words(text)}


def _good_variation(out, example):
    """A usable paraphrase: not a copy, not a fragment, not a ramble.

    Length is the cheap signal for drift — once the model runs long it starts
    inventing content that was never in the example.
    """
    if not out:
        return False
    if out.lower() == example.lower():
        return False
    words = out.split()
    if len(words) < 2:
        return False
    # Same language as the example. A Taglish example must not come back as
    # plain English ("we now feel secure in our areas") - that is a translation,
    # not a rewording, and it reads nothing like the other comments.
    if _detect_lang(example) == "tl" and _detect_lang(out) == "en":
        print(f"  [ai] rejected (translated to English, example is Taglish): "
              f"{out[:60]}")
        return False
    # The mirror image, which the Filipino traits in COMMENT_PERSONA can
    # provoke: an English example must not come back in Tagalog either.
    if _detect_lang(example) == "en" and _detect_lang(out) == "tl":
        print(f"  [ai] rejected (translated to Tagalog, example is "
              f"English): {out[:60]}")
        return False
    # Bisaya and Tagalog are both "tl", so the checks above cannot see a
    # swap between them. A Bisaya example that comes back with no Bisaya
    # left in it has been translated, same as any other translation.
    if _is_bisaya(example) != _is_bisaya(out):
        print(f"  [ai] rejected (swapped between Bisaya and Tagalog): "
              f"{out[:60]}")
        return False
    ex_words, ex_sents, ex_paragraph = _example_shape(example)
    limit = max(6, ex_words * 2 + 3)
    if len(words) > limit:
        print(f"  [ai] rejected (too long, {len(words)}w > {limit}w): {out[:60]}")
        return False
    if ex_paragraph:
        # A written-out example must not come back as a one-liner: that is the
        # model summarising instead of rewording, and it drops whole ideas.
        floor = max(4, int(ex_words * 0.6))
        if len(words) < floor:
            print(f"  [ai] rejected (too short for a {ex_words}w example, "
                  f"{len(words)}w < {floor}w): {out[:60]}")
            return False
        # Different WORDS, same logic. Sentence counting cannot be used here -
        # the output is punctuation-free by design - so measure how much of the
        # example's own vocabulary came back instead.
        # Proper nouns are NOT wording to be varied - "Gov Vilma" has to stay
        # "Gov Vilma" or the comment is about someone else. Anything capitalised
        # mid-text is treated as a name and excluded from the score, otherwise
        # keeping the subject looked like copying and good rewrites were thrown
        # away (which then fell back to posting the example verbatim).
        head, _, rest = example.strip().partition(" ")
        proper = {w.lower() for w in re.findall(r"\b[A-Z][A-Za-z]+", rest)}
        ex_content = _content_words(example) - proper
        if ex_content:
            shared = ex_content & _content_words(out)
            # 0.75, not 0.5: the SUBJECT itself ("fire trucks") has to survive a
            # faithful rewrite, so a legitimate one still keeps several words. A
            # near-copy keeps ~90-100% of them, which this still catches.
            ratio = len(shared) / len(ex_content)
            if ratio > 0.75:
                print(f"  [ai] rejected (reuses {len(shared)}/{len(ex_content)} "
                      f"of the example's words: "
                      f"{', '.join(sorted(shared)[:6])}): {out[:60]}")
                return False
            # The other end: sharing NOTHING at all means the model stopped
            # talking about the same thing ("fire trucks" -> "ambulansya").
            # Only zero, deliberately: a good rewrite legitimately keeps just
            # one word when it finds Filipino equivalents for the rest (e.g.
            # "trak ng bumbero" for fire trucks), and rejecting those burned a
            # retry each time - which is what pushed Groq into 429s.
            if not shared:
                print(f"  [ai] rejected (drifted off the subject, no words in "
                      f"common): {out[:60]}")
                return False
    # The model sometimes leaks the transformation itself, e.g.
    # "ang laki nito becomes whew ang laki nito".
    low = f" {out.lower()} "
    for meta in (" becomes ", " rewritten ", " rewrite ", " paraphrase ",
                 " version ", " original ", " example ", " output ",
                 " same meaning ", " translates "):
        if meta in low:
            print(f"  [ai] rejected (leaked instruction text): {out[:60]}")
            return False
    return True

# ── Reading the post itself ────────────────────────────────────────────────
# Everything above answers "say this differently". None of it ever looks at the
# post, which is why an example that does not match the link produces comments
# that read as nonsense: nine accounts reword one sentence about something else.
# This section gives the AI the post - the caption it can read off the screen,
# and the picture - so the comment can be ABOUT the link, with the examples
# demoted from "the thing to reword" to "the voice to copy".

# Chrome that sits on every post view and is never part of a caption.
_UI_CHROME_EXACT = {
    "like", "gusto", "comment", "komento", "magkomento", "share", "ibahagi",
    "send", "reply", "tugon", "sagot", "most relevant", "all comments",
    "top comments", "lahat ng komento", "view more comments",
    "view previous comments", "be the first to comment", "no comments yet",
    "home", "search", "menu", "notifications", "marketplace", "back",
    "facebook", "see more", "see less", "follow", "following", "add friend",
    "message", "photo", "video", "profile picture", "options", "more options",
    "save", "hide", "report", "translate", "see translation",
    "tingnan ang salin", "others", "iba pa", "view", "all", "and", "at",
    "write a comment", "write a public comment",
}
# Substrings that mark a line as interface rather than content.
_UI_CHROME_SUB = ("write a comment", "comment as", "reply to", "sumagot kay",
                  "magsulat ng komento", "sumulat ng komento",
                  "view more comments", "view previous comments",
                  "all reactions", "lahat ng reaksyon", "most relevant",
                  "people reacted", "reacted with", "sponsored", "suggested for you")
# Facebook writes its own description of a photo into content-desc. It is the
# single most useful line on the screen and costs nothing to read.
_ALT_PREFIXES = ("may be an image of", "may be a", "image may contain",
                 "maaaring larawan", "maaaring imahe", "no photo description")
# The Like/Comment/Share row: the boundary between the POST and other people's
# comments. uiautomator dumps nodes in layout order, so everything before this
# row is the post and everything after it belongs to somebody else.
_ACTION_ROW = ("like", "gusto", "comment", "komento", "magkomento",
               "share", "ibahagi")
_COUNT_RE = re.compile(r"^[\d.,]+\s*[kKmM]?$")
_AGO_RE = re.compile(
    r"^\d+\s*(s|m|h|d|w|y|sec|secs|min|mins|hr|hrs|hour|hours|day|days|"
    r"week|weeks|oras|araw|minuto|linggo|segundo)\b\.?$", re.I)


# An expanded Facebook caption can easily be several thousand characters.
# Keeping only 1,200 chars cut off the conclusion (often the actual request,
# allegation or punchline) even when the accessibility tree exposed all of it.
# This is a safety ceiling, not a target: the vision pass condenses it later.
CAPTION_CHAR_CAP = 6000


def _caption_worthy(line):
    """True when a dumped line looks like post text rather than interface."""
    low = line.lower().strip(" .,:;·•")
    if len(line) < 3 or low in _UI_CHROME_EXACT:
        return False
    if any(s in low for s in _UI_CHROME_SUB):
        return False
    if _COUNT_RE.match(low) or _AGO_RE.match(low):
        return False
    # A caption is prose. One short word is a label, a name or a counter.
    if len(line.split()) < 2 and len(line) < 14:
        return False
    return True


def read_post_text(d):
    """The post's caption and Facebook's own photo alt-text, from the UI dump.

    Best effort, and on Facebook Lite that effort comes back empty: measured on
    a real post view, the dump is 48 KB with 59 Facebook nodes and exactly ONE
    of them carries text - the "Write a comment…" placeholder. Lite draws the
    caption, the poster and the counts without any accessibility text, so there
    is nothing here to read. The screenshot is what actually carries the caption,
    and the vision call reads it straight off the pixels.

    Kept because it costs nothing (the dump is already taken), because a client
    that does expose its caption gets it for free, and because a caption in hand
    is a better prompt than a caption the model has to find. Nodes are filtered
    to the Facebook package first: without that the status bar answers instead,
    and "8:35 AM Lite notification: Wifi signal full" gets sent to the AI as the
    post's caption - which is exactly what happened before this line existed.

    Stopping at the Like/Comment/Share row keeps the caption separate from the
    comments below it. Returns (caption, alt); either may be "".
    """
    root = hierarchy_root(d)
    if root is None:
        return "", ""
    caption, alt, seen = [], [], set()
    for node in root.iter("node"):
        if node.attrib.get("package") != FB_PACKAGE:
            continue
        for attr in ("text", "content-desc"):
            raw = html.unescape(node.attrib.get(attr) or "").strip()
            if not raw:
                continue
            low = raw.lower().strip(" .,:;")
            if any(low.startswith(p) for p in _ALT_PREFIXES):
                if low not in seen:
                    seen.add(low)
                    alt.append(raw)
                continue
            # Past the action row we are into the comment list: stop.
            if low in _ACTION_ROW:
                return (" ".join(caption)[:CAPTION_CHAR_CAP],
                        " | ".join(alt[:2])[:300])
            if low in seen or not _caption_worthy(raw):
                continue
            seen.add(low)
            caption.append(raw)
    return (" ".join(caption)[:CAPTION_CHAR_CAP], " | ".join(alt[:2])[:300])


# A 720x1612 screenshot is ~220 KB of PNG. Down to a 768 px long edge and saved
# as JPEG it is ~32 KB and ~350 image tokens, which is what makes looking at
# every link affordable.
POST_IMAGE_MAX_PX = 768
POST_IMAGE_QUALITY = 75


# How far down the screen the POST ends and the comment list begins. A phone
# screenshot of an opened post is roughly: status bar, poster header, caption,
# photo, the Like/Comment/Share row, then everybody else's comments - each with
# its own little round profile photo.
#
# Sending the whole screen is what produced comments about a "blurry profile
# picture", a Lakers jersey and Universal Studios: on a post whose own image is
# not visually dominant, the most photograph-like things on screen are the
# COMMENTERS' AVATARS, and the model described those instead. Worse, the
# comment list also contains this tool's own earlier comments, so the fleet was
# reading itself. Cutting the bottom off removes both in one step.
POST_CROP_BOTTOM = 0.62


def _screen_jpeg_b64(serial, crop_bottom=None):
    """The phone's current screen as base64 JPEG, or "" if it cannot be had.

    Cropped to the post itself - see POST_CROP_BOTTOM.
    """
    try:
        import io
        import base64
        from PIL import Image
    except Exception as e:
        print(f"    [post-ctx] Pillow unavailable ({e}); caption only")
        return ""
    try:
        cap = subprocess.run([ADB, "-s", serial, "exec-out", "screencap", "-p"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=25)
        raw = cap.stdout or b""
        if not raw.startswith(b"\x89PNG"):
            return ""
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if crop_bottom is None:
            try:
                crop_bottom = float(_ai("post_crop", POST_CROP_BOTTOM))
            except (TypeError, ValueError):
                crop_bottom = POST_CROP_BOTTOM
        frac = min(1.0, max(0.25, crop_bottom))
        if frac < 1.0:
            w, h = im.size
            im = im.crop((0, 0, w, int(h * frac)))
        im.thumbnail((POST_IMAGE_MAX_PX, POST_IMAGE_MAX_PX))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=POST_IMAGE_QUALITY)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        print(f"    [post-ctx] screenshot failed on {serial}: {e}")
        return ""


# Text that hides the rest of a long caption behind a tap.
_SEE_MORE_LABELS = ("see more", "see more...", "…see more", "... see more",
                    "tingnan pa", "magpakita pa", "see translation",
                    "continue reading", "read more")


def _expand_caption(serial, d):
    """Tap 'See more' so a long caption is on screen before the screenshot.

    Facebook collapses a long caption after a few lines, and in Filipino posts
    the point is usually at the END - the ask, the punchline, the thing worth
    being annoyed about. Reading only the visible head produces a comment about
    the setup and not about the post.

    Returns True when something was expanded. Best effort: on Facebook Lite the
    label is usually not in the accessibility tree at all, which is what the
    scroll-and-second-frame path in post_context() is for.
    """
    root = hierarchy_root(d)
    if root is None:
        return False
    for node in root.iter("node"):
        if node.attrib.get("package") != FB_PACKAGE:
            continue
        for attr in ("text", "content-desc"):
            label = html.unescape(node.attrib.get(attr) or "").strip().lower()
            # Facebook sometimes exposes only the label and sometimes exposes
            # the whole collapsed caption ending in that label. Normalize the
            # ellipsis and accept either shape. Do not act on "See
            # translation": that changes language instead of revealing text.
            label = re.sub(r"[.\u2026]+\s*", " ", label)
            label = re.sub(r"\s+", " ", label).strip()
            revealers = ("see more", "tingnan pa", "magpakita pa",
                         "continue reading", "read more")
            if not label or not any(label == more or
                                    label.endswith(" " + more)
                                    for more in revealers):
                continue
            b = parse_bounds(node.attrib.get("bounds"))
            if not b:
                continue
            try:
                d.click(*center(b))
                abort_sleep(1.2)
                print("    [post-ctx] expanded a collapsed caption")
                return True
            except Exception:
                return False
    return False


def _scroll_for_more(serial, d, frac=0.55, direction="down"):
    """Move through a long post while keeping overlap between frames.

    ``direction='down'`` reveals later caption text (finger swipes upward).
    ``direction='up'`` restores the original position (finger swipes down).
    Keeping this explicit avoids the old negative-fraction restore, whose end
    coordinate was 133% of the display and was ignored by some phones.
    """
    try:
        info = d.info
        w, h = info["displayWidth"], info["displayHeight"]
        amount = min(0.62, max(0.25, abs(float(frac))))
        if direction == "up":
            y1, y2 = int(h * 0.24), int(h * (0.24 + amount))
        else:
            y1, y2 = int(h * 0.80), int(h * (0.80 - amount))
        d.swipe(w // 2, y1, w // 2, y2, 0.25)
        return abort_sleep(1.0)
    except Exception as e:
        print(f"    [post-ctx] could not scroll for the rest of the post: {e}")
        return False


CAPTION_EXPAND_SYSTEM = (
    "Locate the caption-expansion control in this Facebook post screenshot. "
    "Find only the text at the end of the POST CAPTION that says See more, "
    "Tingnan pa, Magpakita pa, Continue reading, or Read more. Ignore the "
    "three-dot menu, comments, buttons and any other use of the word more. "
    "Coordinates use 0 to 1000 from the image's left/top to right/bottom. "
    "Reply with exactly {\"x\":number,\"y\":number} for the center of that "
    "caption control, or exactly NONE if it is not visible."
)


def _caption_expand_point(image_b64):
    """Vision-locate an inaccessible ``See more`` control, normalized 0..1000."""
    if not image_b64 or not _ai_wait_slot():
        return None
    payload = [
        {"type": "text", "text": "Find the post caption's expansion control."},
        {"type": "image_url",
         "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
    ]
    vision = str(_ai("vision_model", "") or "").strip() or None
    try:
        raw = _groq_chat(payload, timeout=int(_ai("timeout", 20)) + 25,
                         system=CAPTION_EXPAND_SYSTEM, max_tokens=80,
                         model=vision)
    except Exception as e:
        print(f"    [post-ctx] could not locate See more: {e}")
        return None
    text = str(raw or "").strip()
    if text.upper() == "NONE":
        return None
    mx = re.search(r'["\']?x["\']?\s*[:=]\s*(\d+(?:\.\d+)?)', text, re.I)
    my = re.search(r'["\']?y["\']?\s*[:=]\s*(\d+(?:\.\d+)?)', text, re.I)
    if not (mx and my):
        return None
    x, y = float(mx.group(1)), float(my.group(1))
    if not (0 <= x <= 1000 and 0 <= y <= 1000):
        return None
    return x, y


def _expand_caption_from_image(serial, d, image_b64, crop_bottom=0.85):
    """Tap a visually located caption expander when UI automation cannot see it."""
    point = _caption_expand_point(image_b64)
    if point is None:
        return False
    try:
        info = d.info
        w, h = info["displayWidth"], info["displayHeight"]
        x = int((point[0] / 1000.0) * w)
        y = int((point[1] / 1000.0) * h * crop_bottom)
        if not (0 <= x < w and 0 <= y < h):
            return False
        d.click(x, y)
        if not abort_sleep(1.2):
            return False
        print(f"    [post-ctx] visually expanded a long caption at ({x},{y})")
        return True
    except Exception as e:
        print(f"    [post-ctx] could not tap See more: {e}")
        return False


POST_CONTEXT_SYSTEM = (
    "You look at one Facebook post and state plainly what it is about.\n"
    "Work in this order: FIRST read the post's caption - the text the poster "
    "wrote, above or below the picture - then look at the picture itself.\n"
    "You may be given more than one screenshot. They are consecutive parts of "
    "the SAME post, scrolled down - read them as one continuous post, not as "
    "several posts.\n"
    "If the caption is cut off - a 'See more' or 'Tingnan pa' link, or text "
    "that runs off the bottom edge mid-sentence - finish your answer with the "
    "single word CONTINUES on its own. Do not guess what the rest says.\n"
    "Report ONLY what you can actually see or read. Never guess, never invent "
    "a name, never add an opinion and never write a comment.\n"
    "Answer in English as one factual paragraph: what the caption says, then "
    "what the picture shows and who or what it is (name it only if the caption "
    "or the image itself says so). Use at most 50 words for a short caption. "
    "For a long caption use up to 160 words: preserve its named people and "
    "organizations, dates, numbers, claims, reasons, request or complaint, and "
    "its final conclusion. Remove repetition and padding, not meaning. What "
    "the post is ASKING FOR or COMPLAINING ABOUT matters more than how it "
    "opens.\n"
    "WHAT IS NOT THE POST, and must never be described: the small round "
    "profile photo beside a name, anybody's avatar, a cover photo, other "
    "people's comments, the status bar, the buttons, the sticker tray and the "
    "comment box. Those belong to the app and to other users, not to the post. "
    "The post is the caption the poster wrote and the large picture directly "
    "under it.\n"
    "If you cannot see a post - only interface, avatars or comments - reply "
    "with exactly: NO POST VISIBLE.\n"
    "If the post's own picture is unclear or still loading, describe the "
    "caption and end with: picture unclear."
)

# A description that is really about the app rather than the post. When one of
# these lands, every account under that link writes about a profile photo,
# because the reading is cached fleet-wide - so it is treated as a failed read
# instead of being used.
_CTX_JUNK_MARKERS = (
    "no post visible", "profile picture", "profile photo", "profile pic",
    "avatar", "cover photo", "comment section", "comments section",
    "list of comments", "facebook interface", "user interface",
    "screenshot shows a facebook", "sticker", "notification",
)


_CONTINUES_RE = re.compile(r"\bcontinues\b\.?\s*$", re.I)
# Internal result used only between post_context() and the run loop. It is
# cached per link so every phone makes the same safe decision: skip instead of
# commenting from a partial caption whose conclusion was never read.
POST_CONTEXT_INCOMPLETE = "__FBVENOM_INCOMPLETE_CAPTION__"


def _ctx_continues(desc):
    """True when the reading says the caption ran off the bottom of the screen."""
    return bool(_CONTINUES_RE.search(str(desc or "").strip()))


def _strip_continues(desc):
    """Drop the CONTINUES marker before the reading is used or cached."""
    return _CONTINUES_RE.sub("", str(desc or "").strip()).strip(" .,;-")


def _context_is_junk(desc):
    """True when the vision call described the app instead of the post."""
    low = " " + str(desc or "").lower().strip() + " "
    if len(low.split()) < 4:
        return True
    return any(m in low for m in _CTX_JUNK_MARKERS)


def _post_context_prompt(caption, alt, n_frames=1):
    parts = []
    # Facebook Lite puts none of this in the accessibility tree, so the usual
    # case is no caption in hand and the model reading it off the screenshot.
    if caption:
        parts.append(f'Text read off the post:\n"""{caption}"""')
    if alt:
        parts.append(f"Facebook's own description of the photo: {alt}")
    if n_frames > 1:
        parts.append(f"These are {n_frames} screenshots of ONE Facebook post, "
                     "taken in order as the screen was scrolled down - the "
                     "second continues where the first ends, and they may "
                     "overlap. Read the caption across all of them, then say "
                     "what the picture shows.")
    else:
        parts.append("This is a screenshot of one Facebook post. Read the "
                     "post's caption in it first, then look at the picture "
                     "and say what it shows.")
    return "\n\n".join(parts)


def _describe_post(caption, alt, image_b64):
    """One vision call: caption first, then the picture.

    `image_b64` may be a single frame or a list of consecutive frames of the
    same post - a long caption does not fit on one screen, so it is read as two
    scrolled-down shots in one call rather than two calls.
    """
    frames = ([f for f in image_b64 if f] if isinstance(image_b64, (list, tuple))
              else ([image_b64] if image_b64 else []))
    text = _post_context_prompt(caption, alt, len(frames))
    if frames:
        payload = [{"type": "text", "text": text}]
        for f in frames:
            payload.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{f}"}})
    else:
        payload = text
    # Blank means "use the configured comment model", which also keeps the
    # fallback model in play. A thinking model spends part of its budget before
    # it writes anything, so the cap is generous - only what is produced bills.
    vision = str(_ai("vision_model", "") or "").strip() or None
    raw = _groq_chat(payload, timeout=int(_ai("timeout", 20)) + 25,
                     system=POST_CONTEXT_SYSTEM, max_tokens=512, model=vision)
    out = " ".join(str(raw or "").split())
    # Preserve the continuation signal even if a verbose model reaches the
    # defensive character ceiling; losing it would make a partial caption look
    # complete and poison every account through the fleet-wide cache.
    continues = _ctx_continues(out)
    if len(out) > 2400:
        out = out[:2400].rsplit(" ", 1)[0].strip()
        if continues and not _ctx_continues(out):
            out += " CONTINUES"
    return out


# url -> description. "" is a cached failure: a link that could not be read is
# not retried by the next eighteen phones.
_post_ctx = {}
_post_ctx_wait = {}
_post_ctx_lock = threading.Lock()


def reset_post_context():
    with _post_ctx_lock:
        _post_ctx.clear()
        _post_ctx_wait.clear()


def post_context(serial, d, url):
    """What this post actually is, read ONCE per link for the whole fleet.

    Nineteen phones commenting under one link do not each need to look at it:
    the first to arrive reads the caption and screen; the rest wait for that
    result and reuse it. A normal post costs one vision call per LINK, not one
    per account. A caption spanning several screens needs additional batched
    reads, still only once for the whole fleet.

    Returns "" when the post could not be read at all, which puts the caller
    back on the plain paraphrase path - so a bad link is never worse than before.
    """
    # Post reading was removed: the AI Settings toggle is gone and the run loop
    # no longer calls this. The default flips to False so that nothing can
    # quietly switch the vision path back on through a stale venom_ai.json.
    # The machinery below is left intact rather than ripped out, so restoring
    # the feature is a one-line change rather than an archaeology exercise.
    if not _ai("read_post", False):
        return ""
    try:
        key = normalize_url(url) or url
    except Exception:
        key = url
    with _post_ctx_lock:
        if key in _post_ctx:
            return _post_ctx[key]
        ev = _post_ctx_wait.get(key)
        mine = ev is None
        if mine:
            ev = threading.Event()
            _post_ctx_wait[key] = ev
    if not mine:
        # Another phone is reading this same link right now.
        ev.wait(timeout=120)
        with _post_ctx_lock:
            return _post_ctx.get(key, "")
    desc = ""
    try:
        caption, alt = read_post_text(d)
        expanded = False
        # The soft keyboard covers half the post whenever the composer has
        # focus, and half a post is half a description. This drops it and is
        # guarded against navigating away.
        if not stop_requested():
            prepare_post_view_for_shot(serial, d)
            # A long caption is collapsed behind "See more"; expanding it is
            # free when the label is in the accessibility tree.
            expanded = _expand_caption(serial, d)
            if expanded:
                caption, alt = read_post_text(d)
        shot = "" if stop_requested() else _screen_jpeg_b64(serial)
        if caption or alt or shot:
            if _ai_wait_slot():
                desc = _describe_post(caption, alt, shot)

        # Facebook Lite often renders "See more" but omits it from the
        # accessibility tree. Once vision confirms the caption is incomplete,
        # use a slightly taller screenshot to locate that exact caption control
        # and tap it. Scrolling a collapsed caption cannot reveal hidden text.
        if (desc and shot and _ctx_continues(desc) and not expanded and
                not stop_requested()):
            locator_shot = _screen_jpeg_b64(serial, crop_bottom=0.85)
            if (locator_shot and
                    _expand_caption_from_image(serial, d, locator_shot, 0.85)):
                expanded = True
                caption, alt = read_post_text(d)
                shot = _screen_jpeg_b64(serial)
                if shot and _ai_wait_slot():
                    refreshed = _describe_post(caption, alt, shot)
                    if refreshed:
                        desc = refreshed

        # A caption too long for one screen is read across overlapping frames
        # rather than guessed at. In Filipino posts the point is usually at the END - the
        # ask, the punchline, the thing worth being annoyed about - so a
        # comment written off the visible head is about the setup and not
        # about the post.
        if (desc and shot and _ctx_continues(desc) and not stop_requested()):
            print("  [post-ctx] caption runs past the screen - reading on")
            frames, scrolled = [shot], 0
            try:
                max_frames = int(_ai("max_caption_frames", 8))
            except (TypeError, ValueError):
                max_frames = 8
            max_frames = min(12, max(2, max_frames))

            # Add two overlapping screenfuls at a time, then ask whether the
            # caption is complete. This needs at most six vision calls even for
            # a truly huge 12-screen caption, while a normal post still uses
            # exactly one. Each re-read receives every frame accumulated so
            # far, so repeated lines at the overlap are reconciled in context.
            while (_ctx_continues(desc) and len(frames) < max_frames and
                   not stop_requested()):
                before = len(frames)
                for _ in range(min(2, max_frames - len(frames))):
                    if not _scroll_for_more(serial, d, direction="down"):
                        break
                    scrolled += 1
                    nxt = _screen_jpeg_b64(serial)
                    if not nxt:
                        break
                    frames.append(nxt)
                if len(frames) == before or not _ai_wait_slot():
                    break
                more = _describe_post(caption, alt, frames)
                if more:
                    desc = more

            # Once the caption ends, take one more overlapping frame when
            # possible. An expanded long caption pushes its attached picture
            # below the fold; without this extra frame we would read all the
            # words but never perform the picture-analysis half of the job.
            if (not _ctx_continues(desc) and len(frames) < max_frames and
                    not stop_requested() and
                    _scroll_for_more(serial, d, direction="down")):
                scrolled += 1
                nxt = _screen_jpeg_b64(serial)
                if nxt:
                    frames.append(nxt)
                    if _ai_wait_slot():
                        more = _describe_post(caption, alt, frames)
                        if more:
                            desc = more

            # Put the phone back where the rest of the run expects it - the
            # screenshot proof and the composer search both start from the top
            # of the post, not from wherever the caption ran out.
            for _ in range(scrolled):
                _scroll_for_more(serial, d, direction="up")
            if _ctx_continues(desc):
                print(f"  [post-ctx] caption is still incomplete after "
                      f"{len(frames)} frames - skipping this post rather than "
                      f"commenting from a partial caption")
                desc = POST_CONTEXT_INCOMPLETE
        if desc != POST_CONTEXT_INCOMPLETE:
            desc = _strip_continues(desc)
        # One bad reading is posted by every account under this link, so a
        # description that is plainly about avatars or the comment list is
        # thrown away rather than cached. Retry once on a tighter crop - the
        # usual cause is a post whose own image sits high on the screen.
        if (desc and desc != POST_CONTEXT_INCOMPLETE and
                _context_is_junk(desc) and shot and not stop_requested()):
            print(f"  [post-ctx] reading was about the app, not the post "
                  f"({desc[:70]}) - retrying on a tighter crop")
            tight = _screen_jpeg_b64(serial, crop_bottom=0.45)
            if tight and _ai_wait_slot():
                desc = _describe_post(caption, alt, tight)
        if (desc and desc != POST_CONTEXT_INCOMPLETE and
                _context_is_junk(desc)):
            print(f"  [post-ctx] still not the post ({desc[:70]}) - refusing "
                  f"to comment off a bad reading")
            desc = ""
        if desc == POST_CONTEXT_INCOMPLETE:
            pass  # the precise reason was logged above
        elif desc:
            print(f"  [post-ctx] {key}\n              -> {desc}")
        else:
            print(f"  [post-ctx] nothing readable on {key} - "
                  f"falling back to rewording the example")
    except Exception as e:
        print(f"  [post-ctx] read failed ({e}); falling back to rewording "
              f"the example")
        desc = ""
    finally:
        with _post_ctx_lock:
            _post_ctx[key] = desc
            _post_ctx_wait.pop(key, None)
        ev.set()
    return desc


# ── Commenting ON the post, in the examples' voice ─────────────────────────
_CTX_SYSTEM_HEAD = (
    "You write ONE short Facebook comment reacting to a specific post.\n"
)
_CTX_SYSTEM_BODY = (
    "You are given what the post really is, and some example comments.\n"
    "The examples are there for their VOICE ONLY: copy their slang, their "
    "language mix, their length, their spelling and their energy. Do NOT copy "
    "what they are ABOUT - they were written under a different post.\n"
    "Reuse the examples' own slang words wherever they fit, and aim them at "
    "what is really in THIS post.\n"
    "Your comment must be about the post described below and nothing else. "
    "Never mention a person, place, number, event or detail that is not in "
    "that description. If the description says the picture is unclear, react "
    "to the caption only and say nothing about the picture.\n"
    "Write as a real Filipino commenter typing fast on a phone, not as an "
    "assistant. Never explain, never greet, never ask a question, never "
    "address the poster, and never describe the post back - react to it.\n"
)
_CTX_SYSTEM_TAIL = (
    "Keep it to ONE line, about as long as the example comments.\n"
    "Do NOT use any punctuation marks or symbols at all - no periods, commas, "
    "exclamation points, question marks, quotes, dashes, hashtags, or emoji.\n"
    "Output ONLY the comment on one line: no quotes, labels, explanations or "
    "extra lines."
)


# The styled tail replaces the plain one. _CTX_SYSTEM_TAIL bans every mark of
# punctuation and pins the length to the examples; under a sampled style the
# punctuation and the length ARE the style, so those two rules have to go or
# they cancel the axes they contradict.
_CTX_SYSTEM_STYLED_TAIL = (
    "The examples show you the KIND of person and the kind of slang - how "
    "casual, how online, how blunt. Everything mechanical comes from the STYLE "
    "above and overrides them: the length, the capitalisation, the punctuation "
    "AND the language mix. If the style says 60 words in capitals in Bisaya, "
    "write that, even though the examples are short lowercase Tagalog.\n"
    "Output ONLY the comment: no quotes, no labels, no explanation, and never "
    "a second line."
)

# Rejections that mean the model narrated the style instead of using it. It
# does this often enough to be worth a check of its own - "in a sarcastic tone,
# ang bilis naman" is a comment nobody would type.
_STYLE_LEAK = (" register ", " caps ", " all caps ", " comparison ",
               " sarcastic tone ", " in a tone ", " style ", " comma splice ",
               " rhetorical ", " tagalog-heavy ", " english-heavy ",
               " code-switch ", " punctuation ")


def _good_styled_comment(out, words, style, ctx="", examples=(), feedback=None):
    """Gate for a sampled sarcastic-anger comment.

    Checks what the style actually promised: the length band it drew, the
    capitalisation profile, and the laugh - the three things the model
    silently ignores most often. It deliberately does NOT check language
    against the examples: bisaya is a value on the code-switch axis, and an
    english-heavy comment is a different social character rather than a
    translation error.
    """
    lo, hi = _RAGE_LENGTH_WORDS.get(style.get("length"), (2, 60))
    # Room at both ends: word bands are guidance, and rejecting a 16-word
    # one-liner just burns a call to get a 15-word one.
    if not (max(1, lo - 2) <= len(words) <= hi + 6):
        print(f"  [ai] rejected ({len(words)}w outside the "
              f"{style['length']} band {lo}-{hi}): {out[:60]}")
        return False
    letters = [c for c in out if c.isalpha()]
    upper = sum(1 for c in letters if c.isupper())
    ratio = (upper / len(letters)) if letters else 0.0
    caps = style.get("caps")
    if caps == "all_caps" and ratio < 0.9:
        print(f"  [ai] rejected (asked for ALL CAPS, got {ratio:.0%}): "
              f"{out[:60]}")
        return False
    if caps == "lower_flat" and ratio > 0.05:
        print(f"  [ai] rejected (asked for flat lowercase, got {ratio:.0%} "
              f"caps): {out[:60]}")
        return False
    if caps in ("caps_burst", "lower_exhale_then_caps") and not (
            0.04 <= ratio <= 0.85):
        print(f"  [ai] rejected ({caps} needs part of it shouted, got "
              f"{ratio:.0%}): {out[:60]}")
        return False
    # No laugh check here: sanitize_comment appends a missing one rather than
    # spending a retry on it.
    low = f" {out.lower()} "
    stray = _foreign_entity(out, ctx, examples)
    if stray:
        print(f"  [ai] rejected (brings in '{stray}' - not in the post or the "
              f"examples): {out[:60]}")
        if feedback is not None:
            feedback.setdefault("avoid", []).append(str(stray).lower())
        return False
    # Talking about a profile photo is only wrong when the POST is not about
    # one. Plenty of posts are exactly that ("new pfp"), and rejecting those
    # threw away the only on-topic comment available.
    if not _OFFTOPIC_RE.search(str(ctx or "")):
        off = _OFFTOPIC_RE.search(out)
        if off:
            print(f"  [ai] rejected (about a profile photo, not the post - "
                  f"'{off.group(0)}'): {out[:60]}")
            return False
    hit = _HARD_PROFANITY_RE.search(out)
    if hit:
        print(f"  [ai] rejected (hard swearing '{hit.group(0)}' - gets the "
              f"comment pulled): {out[:60]}")
        if feedback is not None:
            feedback.setdefault("avoid", []).append(hit.group(0).lower())
        return False
    for meta in _STYLE_LEAK:
        if meta in low:
            print(f"  [ai] rejected (narrated the style instead of using it): "
                  f"{out[:60]}")
            return False
    for meta in (" the post ", " this post ", " the caption ",
                 " the screenshot ", " the description ",
                 " the image shows ", " the photo shows "):
        if meta in low:
            print(f"  [ai] rejected (describes the post instead of reacting): "
                  f"{out[:60]}")
            return False
    return True


def comment_system_context(examples, tone=None, style=None):
    """System prompt for a comment ABOUT the post, voiced like the examples.

    With a sampled style the tail changes: _CTX_SYSTEM_TAIL forbids all
    punctuation and pins the length to the examples, and both of those are
    axes the style is supposed to own.
    """
    joined = " ".join(examples)
    if style:
        return (_CTX_SYSTEM_HEAD + _CTX_SYSTEM_BODY
                + persona_block(persona_for(joined, tone,
                                            lang=style_lang(style)))
                + rage_style_block(style) + _CTX_SYSTEM_STYLED_TAIL)
    return (_CTX_SYSTEM_HEAD + _CTX_SYSTEM_BODY
            + persona_block(persona_for(joined, tone))
            + tone_block(tone) + _CTX_SYSTEM_TAIL)


def _note_avoid(feedback, out, keys):
    """Record which of `out`'s words got it rejected, for the next attempt.

    `keys` are capped forms (_word_key); the surface words the model actually
    typed are what it needs to hear back, in the order it used them.
    """
    if feedback is None or not keys:
        return
    typed = [w for w in re.findall(r"[a-z0-9]+", str(out or "").lower())
             if _word_key(w) in keys]
    feedback.setdefault("avoid", []).extend(dict.fromkeys(typed or sorted(keys)))


def _retry_avoid_line(words):
    """Tell a retry exactly which words got the last attempt(s) rejected.

    The retry used to resend the identical prompt, so the model mostly made
    the identical choice: measured through the live endpoint on a two-example
    post, all three styled attempts leaned on "sana"/"view"/"makabalik" and the
    comment fell through to the plain reword path, ~4s of wasted calls each.
    """
    words = [w for w in dict.fromkeys(words) if w][:10]
    if not words:
        return ""
    return ("\n\nYOUR LAST TRY WAS REJECTED for reusing these words: "
            + ", ".join(f'"{w}"' for w in words)
            + ". Write a different comment that contains NONE of them - not "
              "even another form of them. Reply with only the comment.")


def _good_context_comment(out, examples, ctx, style=None, feedback=None):
    """Quality gate for a context comment.

    `feedback`, when given, collects the words a rejection was about under
    "avoid", so the next attempt can be told to steer clear of them. The
    verdict itself does not depend on it.

    _good_variation is the wrong test here - it rejects anything that does not
    mean what the example means, and meaning something NEW about the post is the
    entire point. What still has to hold is the VOICE: the language, the length,
    and that the model did not simply hand an example back.

    With a sampled style most of those tests move: the length comes from the
    sampled band rather than the examples, Bisaya is a legitimate choice rather
    than drift, and the voice is carried by the style block instead of by
    borrowed words - a 50-word rant about a kuryente bill shares nothing with a
    three-word example and is still exactly right.
    """
    # Punctuation is content when a style is on, so it is not normalised away
    # before counting - the commas of a rant are the rant.
    out = (str(out or "").strip() if style
           else _plain_text(str(out or "")).strip())
    words = out.split()
    if len(words) < (1 if style and style.get("length") == "fragment" else 2):
        return False
    joined = " ".join(examples)
    if style:
        return _good_styled_comment(out, words, style, ctx, examples, feedback=feedback)
    # _detect_lang is a marker-word count, and gamer slang is not in its list:
    # of "angas ng sukuna wallpaper" / "angas bangis tol" / "sheeesh iba talaga
    # tol kapg sukuna", it calls the last two ENGLISH. At comment length it is
    # a coin flip, so the language gate that guards the paraphrase path would
    # throw away perfectly good comments here. What holds instead is the VOICE
    # BAND below: the comment has to carry some of the examples' own words
    # (that is what "same slang, new subject" means) without carrying all of
    # them. It is a sharper test than the detector at three to six words, and
    # a comment that answers in plain English fails it on the low end anyway.
    # Bisaya is a fixed word list, so drifting INTO it is still worth catching.
    if not _is_bisaya(joined) and _is_bisaya(out):
        print(f"  [ai] rejected (drifted into Bisaya, examples are not): "
              f"{out[:60]}")
        return False
    ex_words = [len(e.split()) for e in examples if e.split()] or [4]
    limit = max(6, max(ex_words) * 2 + 3)
    if len(words) > limit:
        print(f"  [ai] rejected (too long, {len(words)}w > {limit}w): {out[:60]}")
        return False
    # Handing an example straight back is exactly the failure this path exists
    # to fix - the examples were written under a DIFFERENT post.
    key = _norm_comment(out)
    out_content = _content_words(out)
    voice = set()
    for e in examples:
        if key and key == _norm_comment(e):
            print(f"  [ai] rejected (copied the example back): {out[:60]}")
            return False
        voice |= _content_words(e)
    # The examples are an IDEA and a register, not a word bank. Requiring one
    # of their words back is what put "paldo" in fifteen of twenty comments, so
    # that rule is gone: borrowing nothing is now a good comment, not a reject.
    # What is still caught is leaning on them - echoing several of their
    # stand-out words is the same comment wearing a hat.
    # Compared on the capped forms, or "paldo" in the example and "paldong" in
    # the comment would read as two different words and slip through.
    echoed = {_word_key(w) for w in voice} & _content_keys(out)
    if len(echoed) > 1:
        print(f"  [ai] rejected (leans on the example's words "
              f"{sorted(echoed)}): {out[:60]}")
        _note_avoid(feedback, out, echoed)
        return False
    # A word that has had its allowance under this post is retired. This is
    # what actually stops one word riding into every comment.
    burnt = _spent_words() & _content_keys(out)
    if burnt:
        print(f"  [ai] rejected (word already used {WORD_USE_LIMIT}x under "
              f"this post: {sorted(burnt)}): {out[:60]}")
        _note_avoid(feedback, out, burnt)
        return False
    if voice and not (out_content - voice):
        print(f"  [ai] rejected (says nothing the examples did not - it is "
              f"the example again): {out[:60]}")
        return False
    low = f" {out.lower()} "
    for meta in (" becomes ", " rewritten ", " rewrite ", " paraphrase ",
                 " version ", " original ", " example ", " output ",
                 " same meaning ", " translates "):
        if meta in low:
            print(f"  [ai] rejected (leaked instruction text): {out[:60]}")
            return False
    # Narrating the post instead of reacting to it.
    for meta in (" the post ", " this post ", " the caption ",
                 " the screenshot ", " the description ", " the image shows ",
                 " in the picture ", " the photo shows "):
        if meta in low:
            print(f"  [ai] rejected (describes the post instead of reacting): "
                  f"{out[:60]}")
            return False
    return True


# What to react TO. Newer Claude models refuse temperature, so an identical
# prompt gives an identical comment - nine accounts posting one sentence. These
# are the variation, the same way _COMMENT_ANGLES varies a paraphrase: not what
# the comment says, but which part of the post it is aimed at.
_CONTEXT_ANGLES = [
    "React to the main subject of the picture.",
    "React to how it looks - the art, the colours, the quality of it.",
    "Hype it the way a fan of that subject would.",
    "Agree with what the caption says.",
    "React with disbelief at how good it is.",
    "Say you want it or are taking it, in slang.",
    "Compliment whoever posted it, in slang.",
    "React to one specific detail visible in the picture.",
    "Say it beats the others you have seen, in slang.",
]


# Words the DESCRIPTION uses about itself rather than about the post. They are
# the most frequent words in every reading and would otherwise be handed back
# as the post's subject matter.
_CTX_META_WORDS = {
    "post", "posts", "caption", "captions", "picture", "pictures", "photo",
    "photos", "image", "images", "shows", "showing", "reads", "says", "text",
    "screenshot", "facebook", "appears", "visible", "background", "unclear",
    "the", "and", "with", "that", "this", "from", "into", "over", "under",
}


def post_subjects(ctx, limit=6):
    """The handful of things the post is actually about.

    `limit` defaulted to None while the loop below compared `len(out) >= limit`
    unconditionally, so EVERY call raised
    `TypeError: '>=' not supported between instances of 'int' and 'NoneType'`.
    The one caller passes no limit, so the context path - the path taken
    whenever "Read the post" is ticked - crashed on every single comment. The
    exception was swallowed by unique_comment's bare `except Exception`, so it
    looked exactly like the model returning a bad answer, and every comment
    quietly fell through to the offline praise pool.

    Pulled out of the reading and named in the prompt so "be about the post"
    stops being a hope. Numbers come first - on a post whose whole point is a
    figure, the figure IS the subject, and it is the one token that survives
    into a Tagalog comment unchanged.
    """
    text = str(ctx or "")
    nums = re.findall(r"\d[\d.,]*\s*(?:billion|million|thousand|bilyon|milyon"
                      r"|libo|k|m|b)?", text, re.I)
    nums = [n.strip() for n in nums if n.strip()]
    # Document order, not longest-first. The reading is written caption-first,
    # so its own order already ranks the subjects - and sorting by length
    # promoted "government" and "province" over "flood" and "canal", which are
    # the words the post is actually about.
    content = _content_words(text)
    seen, words = set(), []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in content and w not in _CTX_META_WORDS and w not in seen:
            seen.add(w)
            words.append(w)
    out = []
    for item in nums[:2] + words:
        if item not in out:
            out.append(item)
        if limit and len(out) >= limit:
            break
    return out


# The failure that started all this: comments about the commenters' avatars
# instead of the post. The reading is fixed upstream by cropping, but if one
# still slips through, the comment itself is caught here.
_OFFTOPIC_RE = re.compile(
    r"\b(profile\s*(pic\w*|photo)?|avatar|cover\s*photo|dp|"
    r"blurry\s+(pic|photo|profile))\b", re.I)

# Things a comment may only name when the POST or the EXAMPLES named them
# first. Each entry is one CONCEPT with every surface form it takes in both
# languages, because the post description is written in English while the
# comment is written in Taglish: matching bare words would block "gobyerno" on
# a post that plainly says "government", and let it through on a post that says
# nothing of the kind. The concept is allowed when the post or the examples
# mention ANY of its forms.
#
# Government is here by explicit instruction: a comment must not blame the
# government, an official or corruption unless the post itself raised it.
# Blaming the state on an unrelated post is both off-topic and the kind of
# thing that gets a comment reported.
_GATED_CONCEPTS = {
    "government": {"government", "governments", "gobyerno", "gubyerno",
                   "goverment", "lgu", "administration", "administrasyon",
                   "gov", "state", "bayan", "publiko", "public"},
    "official":   {"official", "officials", "opisyal", "opisyales",
                   "politician", "politicians", "politiko", "pulitiko",
                   "politics", "pulitika", "kandidato", "candidate"},
    "mayor":      {"mayor", "alkalde"},
    "governor":   {"governor", "gobernador"},
    "senator":    {"senator", "senators", "senador", "senate", "senado"},
    "congress":   {"congressman", "kongresista", "congress", "kongreso"},
    "barangay":   {"barangay", "brgy", "kapitan", "kagawad"},
    "cityhall":   {"munisipyo", "munisipal", "municipal", "cityhall",
                   "capitol", "kapitolyo"},
    "tax":        {"tax", "taxes", "taxpayer", "taxpayers", "buwis",
                   "kabang", "yaman"},
    "corruption": {"corruption", "corrupt", "korap", "korapsyon", "kurakot",
                   "magnanakaw", "nakaw", "ninakaw", "plunder"},
    "agency":     {"nbi", "lto", "sss", "pagibig", "philhealth", "dpwh",
                   "dswd", "comelec", "deped", "doh", "dilg"},
    "utility":    {"meralco", "maynilad"},
    "transit":    {"lrt", "mrt", "edsa"},
    "wallet":     {"gcash", "paymaya"},
    "delivery":   {"grab", "angkas", "lalamove", "foodpanda"},
    "shopping":   {"shopee", "lazada", "puregold"},
    "socialapp":  {"tiktok", "spotify", "youtube", "messenger"},
    "streaming":  {"netflix", "disney"},
    "foodchain":  {"jollibee", "mcdo", "mcdonalds", "chowking"},
    "telco":      {"globe", "smart", "pldt", "converge", "starlink"},
    "team":       {"lakers", "nba", "pba", "ginebra"},
    "game":       {"valorant", "codm", "roblox", "minecraft", "dota"},
    "themepark":  {"universal", "disneyland", "enchanted"},
}

# One regex per concept, built from its surface forms.
_GATED_RES = {
    name: re.compile(r"\b(?:" + "|".join(
        sorted((re.escape(f) for f in forms), key=len, reverse=True)) + r")\b",
        re.I)
    for name, forms in _GATED_CONCEPTS.items()
}

# Filipino marks a person by name with si/ni/kay and their plurals. A name
# sitting after one of these is the model inventing somebody - which is the
# single thing most likely to make a comment read as belonging under a
# different post.
_NAMED_PERSON_RE = re.compile(
    r"\b(?:si|ni|kay|sina|nina|kina)\s+([A-Za-z][A-Za-z'.-]{2,})", re.I)

# Ordinary Filipino words that follow si/ni/kay without naming anybody.
_NOT_A_NAME = {
    "ako", "ikaw", "siya", "kami", "kayo", "sila", "tayo", "akin", "iyo",
    "kanya", "amin", "inyo", "kanila", "ate", "kuya", "nanay", "tatay",
    "mama", "papa", "lola", "lolo", "bes", "tol", "idol", "mars", "sis",
    "boss", "madam", "sir", "pre", "bro", "friend", "kaibigan", "lahat",
    "kanino", "sino", "ganyan", "ganto", "ganun", "iba", "isa",
}


def _allowed_vocab(ctx, examples):
    """Every word the post and the examples actually put on the table."""
    text = str(ctx or "") + " " + " ".join(examples or [])
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _foreign_entity(out, ctx, examples):
    """The first thing in `out` that the post and examples never raised.

    This is the mechanical form of "stick to what is on the post": a comment
    may react however it likes, but it may not introduce a brand, an agency, a
    place, the government, or a person out of nowhere. A concept counts as
    raised when the post or the examples use ANY of its surface forms in either
    language. Returns the offending text, or "".
    """
    text = str(out or "")
    allowed = _allowed_vocab(ctx, examples)
    for name, rx in _GATED_RES.items():
        hit = rx.search(text)
        if not hit:
            continue
        # Raised by the post or the examples in any of its forms? Then it is
        # fair game - "gobyerno" is allowed on a post about a government
        # project, and blocked on a post about a wallpaper.
        if allowed & _GATED_CONCEPTS[name]:
            continue
        return hit.group(0)
    for m in _NAMED_PERSON_RE.finditer(text):
        name = m.group(1)
        low = re.sub(r"[^a-z0-9]+", "", name.lower())
        if not low or low in _NOT_A_NAME or low in allowed:
            continue
        # Only treat it as a name when it is written like one. An ALL CAPS
        # comment capitalises everything, so capitalisation cannot be the
        # test there - fall back to "the post never said this word".
        return name
    return ""


def _flavor_taken():
    with _used_lock:
        p = _pool()
        return set(p["anchors"]), set(p["compare"]), set(p["phrases"])


def _claim_flavor(style):
    """Reserve this comment's yardstick and move before anyone else rolls.

    Claimed at SAMPLE time, not on success: nineteen device threads roll their
    style within milliseconds of each other, so waiting until a comment passes
    the gate lets several of them pick the same anchor first.
    """
    if not style:
        return
    with _used_lock:
        p = _pool()
        if style.get("anchor"):
            p["anchors"].add(style["anchor"])
        if style.get("compare"):
            p["compare"].add(style["compare"])


def _claim_phrases(text):
    with _used_lock:
        spent = _pool()["phrases"]
        low = " " + str(text or "").lower() + " "
        for p in _RAGE_CATCHPHRASES:
            if p in low:
                spent.add(p)


def _claim_words(text):
    """Charge this comment's words against the per-post repetition budget."""
    with _used_lock:
        words = _pool()["words"]
        for key in _content_keys(text):
            words[key] = words.get(key, 0) + 1


def _spent_words(url=None):
    """Words that have had their allowance under this post and are now banned."""
    with _used_lock:
        words = _pool(url)["words"]
        return {k for k, n in words.items() if n >= WORD_USE_LIMIT}


def _generate_context_comment(examples, ctx, retries=3, avoid_openings=None,
                              tone=None, cfg=None):
    """A comment about THIS post, typed in the examples' voice."""
    examples = [e for e in examples if e.strip()][:8]
    if not examples:
        return ""
    joined = " ".join(examples)
    lang_hint = {
        "tl": "the same Filipino mix the examples use - Tagalog or Bisaya, "
              "whichever one they are, never swapped for the other",
        "en": "the exact same language as the examples",
        "other": "the exact same language as the examples",
    }[_detect_lang(joined)]
    ex_words = [len(e.split()) for e in examples if e.split()] or [4]
    target = max(3, sum(ex_words) // len(ex_words))
    angle = random.choice(_CONTEXT_ANGLES)
    # Sarcastic anger is the one tone that draws a fresh style per comment, so
    # this is rolled here - once per comment, shared by that comment's retries,
    # and different on the next account.
    used_anchors, used_compare, used_phrases = _flavor_taken()
    style = None
    if tone == TONE_RAGE:
        # The yardstick is drawn from what this post can actually be measured
        # on. No matching dimension means no comparison at all.
        style = sample_rage_style(avoid_anchors=used_anchors,
                                  avoid_compare=used_compare,
                                  dimensions=post_dimensions(ctx))
        _claim_flavor(style)
    system_prompt = comment_system_context(examples, tone, style)
    avoid = ""
    if avoid_openings:
        sample = [o for o in avoid_openings if o][-12:]
        if sample:
            avoid = (" Other people already began their comments with: "
                     + "; ".join(f'"{o}"' for o in sample)
                     + ". Start with clearly different words than those.")
    ex_block = "\n".join(f"- {e}" for e in examples)
    # "Be about the post" is not an instruction a model can check itself
    # against, so the post's actual subjects are named. Nine comments about a
    # profile picture is what happens when this is left implicit.
    subjects = post_subjects(ctx)
    subj_line = ""
    if subjects:
        subj_line = ("THIS POST IS ABOUT: " + ", ".join(subjects)
                     + ". Your comment must be recognisably about that - name "
                       "it, or react to it so plainly that anyone scrolling "
                       "past knows which post it is under. Write those things "
                       "naturally in your own language; do not copy them as "
                       "English labels.\n\n")
    spent = ""
    # Words that have had their allowance under this post. Naming them is what
    # lets the model route around them instead of being rejected and retried.
    burnt = sorted(_spent_words())[:40]
    if used_phrases or burnt:
        parts = []
        if used_phrases:
            parts.append(", ".join(sorted(used_phrases)))
        if burnt:
            parts.append(", ".join(burnt))
        spent = (" These are USED UP under this post: " + "; ".join(parts)
                 + " - your comment must not contain any of them, or any "
                   "word built on them. Find another way to say it.")
    if style:
        lo, hi = _RAGE_LENGTH_WORDS[style["length"]]
        shape = (f"{lo} to {hi} words. The STYLE block decides the length, the "
                 "capitals, the punctuation and the language - the examples do "
                 "not.")
        aim = rage_style_line(style)
        target = hi
        # The style owns the language axis, so the examples must not also
        # claim it - two instructions asking for different languages is how a
        # Bisaya roll came back in Tagalog.
        lang_hint = RAGE_SWITCH[style["switch"]]
    else:
        shape = f"About {target} words, one line, no punctuation."
        aim = f"Aim it here: {angle} "
    prompt_text = (
        "THE POST you are commenting on:\n"
        f'"""{ctx}"""\n\n'
        + subj_line
        + "EXAMPLE COMMENTS - take the IDEA and the way these people type.\n"
          "Do NOT reuse their words:\n"
        f"{ex_block}\n\n"
        f"Write ONE new comment about THE POST above, in {lang_hint}, at the "
        "same level of slang and the same way of typing as those examples - "
        "but in YOUR OWN WORDS. Say the same kind of thing about the same "
        "topic using different wording; a reader must not be able to tell "
        "which example you were given. Reusing their stand-out words is the "
        f"one thing that gives it away. {shape} "
        + aim
        + avoid + spent + " "
        + persona_line(persona_for(joined, tone, lang=style_lang(style)))
        + (tone_line(tone) if not style else "")
        + " Reply with only the comment, and nothing else."
    )
    max_tok = max(60, min(400, target * 6 + 60))
    # Words earlier attempts were rejected for, in the order they came up.
    # Each retry is told them by name, and the list only grows, so attempt 3
    # cannot walk back into what sank attempt 1.
    rejected_words = []
    for attempt in range(retries):
        # Checked before taking a pacing slot: a phone must not queue behind
        # the fleet only to learn the endpoint is resting.
        if _ai_is_off():
            return ""
        if not _ai_wait_slot():
            return ""
        try:
            raw = _groq_chat(prompt_text + _retry_avoid_line(rejected_words),
                             system=system_prompt, max_tokens=max_tok, cfg=cfg)
            out = sanitize_comment(raw, "", style=style)
            feedback = {}
            if _good_context_comment(out, examples, ctx, style=style,
                                     feedback=feedback):
                _claim_phrases(out)
                return out
            for word in feedback.get("avoid", ()):
                if word not in rejected_words:
                    rejected_words.append(word)
            continue
        except Exception as e:
            print(f"  [ai] context attempt {attempt+1} failed: {e}")
    return ""


def comment_examples_as_context(example):
    """The example comments, flattened into the "what this is about" string.

    The post is no longer read, so the examples carry BOTH jobs: they are the
    voice AND they are the subject. Feeding them in as the context is what
    keeps the whole styled path - the per-comment style roll (length band, caps
    profile, code-switch, personal comparison) that makes fifteen accounts
    sound like fifteen people, plus _foreign_entity(), which rejects anything
    the examples did not put there.

    The plain reword path cannot do this job: it has no style roll and
    _good_variation() holds every output to the example's meaning, so fifteen
    comments off a two-word example came back as fifteen ways of saying
    "kawawang pilipinas". Measured on those two examples, this path gives
    15/15 distinct.
    """
    return " ".join(_split_examples(example)).strip()


def generate_comment(example, retries=3, avoid_openings=None, tone=None,
                     ctx="", examples=None):
    cfg = ai_cfg()
    lan_only = aiconfig.get_server_mode(cfg) == "lan"
    example = example.strip()
    # `ctx` is what the post actually is, read once per link by post_context().
    # With it the job changes completely: the examples stop being the sentence
    # to reword and become the voice to copy, and the comment is about the post.
    # Without it - unreadable link, reading switched off, endpoint down - every
    # line below runs exactly as it did before, so nothing regresses.
    if ctx:
        tone = resolve_tone(tone)
        out = _generate_context_comment(examples or [example], ctx,
                                        retries=retries,
                                        avoid_openings=avoid_openings,
                                        tone=tone, cfg=cfg)
        if out:
            return out
        if _ai_is_off():
            # The endpoint is resting (or off): no style retry and no reword
            # pass - the caller's offline wordings take over immediately.
            return example
        # A style that could not be satisfied must not cost us the POST. Drop
        # the style, keep the post: an on-topic comment in a plainer voice
        # beats a perfectly-styled reword of an example written under some
        # other link, which is what the paraphrase fallback produces.
        if tone == TONE_RAGE:
            print("  [ai] style could not be met - retrying on the post "
                  "without it")
            out = _generate_context_comment(examples or [example], ctx,
                                            retries=2,
                                            avoid_openings=avoid_openings,
                                            tone="annoyed", cfg=cfg)
            if out:
                return out
        print("  [ai] context comment failed - rewording the example instead")
    lang_hint = {
        "tl": "the same Filipino mix the example already uses - Tagalog or "
              "Bisaya, whichever one it is, never swapped for the other",
        "en": "the exact same language as the example (do not translate it)",
        "other": "the exact same language as the example (do not translate it)",
    }[_detect_lang(example)]
    # A tone is honoured only when one was actually chosen. The old random
    # tone is still gone for good: it forced a mood the example never had
    # and produced off-meaning comments. This one is opt-in, and neutral
    # (the default) leaves the prompt exactly as it was.
    tone = resolve_tone(tone)
    angle = random.choice(_COMMENT_ANGLES)
    # Everything about length has to follow the EXAMPLE, not a fixed one-liner
    # rule: a 2-sentence, 25-word example must come back as 2 sentences of ~25
    # words, not squeezed into a short line.
    ex_words, ex_sents, ex_paragraph = _example_shape(example)
    if ex_paragraph:
        shape_rule = (
            f"The example is about {ex_words} words long. Match that: write "
            f"roughly {ex_words} words covering every idea in the example. Do "
            "NOT shorten it into one line. Use DIFFERENT words from the "
            "example - replace its nouns, verbs and adjectives with other "
            "words meaning the same thing; only small connecting words (ang, "
            "ng, sa, ay, at, na, mga) may repeat. Also BUILD THE SENTENCE "
            "DIFFERENTLY: start from a different idea, and phrase it the way "
            "another person would say the same thing - it must not read as the "
            "example with a few words swapped. Write it with NO punctuation "
            "at all, like someone typing fast on a phone.")
    else:
        # Pinning a 4-word example to exactly 4 words leaves no room for a
        # voice at all, so the persona comes back as a plain reword. A tone
        # needs more room again - anger or sarcasm is carried by words the
        # example does not have. Both stay inside _good_variation's ceiling
        # of ex_words * 2 + 3.
        room = ("a few extra words if the voice or the tone needs them"
                if tone != "neutral" else
                "at most two extra slang words or particles if the voice "
                "needs them")
        shape_rule = ("Keep it to one short sentence, about as long as the "
                      "example plus " + room + ".")
    system_prompt = comment_system_for(example, tone)
    # Token budget scaled to the example - the old flat 40 truncated any
    # paragraph mid-sentence (Tagalog words cost ~2 tokens each).
    max_tok = max(40, min(320, ex_words * 4 + 24))
    avoid = ""
    if avoid_openings:
        sample = [o for o in avoid_openings if o][-12:]
        if sample:
            avoid = (" Other people already began their comments with: "
                     + "; ".join(f'"{o}"' for o in sample)
                     + ". Start with clearly different words than those, but do"
                       " NOT change the meaning to do it.")
    prompt_text = (
        "Example Facebook comment (between the triple quotes):\n"
        f'"""{example}"""\n\n'
        f"Rewrite that comment in {lang_hint}. It must mean EXACTLY the same "
        "thing as the example — same subject, same opinion, same feeling — but "
        "worded differently, as if a different person typed the same reaction. "
        f"Variation to apply: {angle} "
        "Add NOTHING that is not already in the example: no new feelings, no "
        "reasons, no extra details, no greetings. Slang, fillers and "
        "texting spellings are wording, not details, so those are welcome. "
        "Never say the opposite of the "
        "example. "
        + shape_rule
        + avoid + " " + persona_line(persona_for(example, tone)) + tone_line(tone) +
        " Reply with only the rewritten comment, on one line."
    )

    for attempt in range(retries):
        # A bad key or an empty balance answers every phone identically, so
        # once _groq_chat has said so there is nothing left to retry. Without
        # this the fleet still burned retries x providers x comments x 19
        # threads against an endpoint that had already given its final answer.
        # Checked BEFORE the pacing slot, so a resting endpoint costs no wait.
        if _ai_is_off():
            return example
        if not _ai_wait_slot():
            return example          # PURGE: don't start a fresh AI call
        if lan_only or str(cfg.get("api_key") or "").strip():
            try:
                raw = _groq_chat(prompt_text, system=system_prompt,
                                 max_tokens=max_tok, cfg=cfg)
                out = sanitize_comment(raw, example)
                if _good_variation(out, example):
                    return out
                # The endpoint WORKS, this particular wording just failed the
                # checks. Ask it again rather than falling through to the
                # keyless fallback, which is slow and usually unavailable.
                continue
            except Exception as e_groq:
                print(f"  [ai] Groq attempt {attempt+1} failed: {e_groq}")
        if lan_only:
            continue
        try:
            raw = _pollinations_get(prompt_text, system=system_prompt,
                                    max_tokens=max_tok, cfg=cfg)
            out = sanitize_comment(raw, example)
            if _good_variation(out, example):
                return out
        except Exception as e_get:
            print(f"  [ai] GET attempt {attempt+1} failed: {e_get}")
        try:
            raw = _pollinations_post(prompt_text, system=system_prompt,
                                     max_tokens=max_tok, cfg=cfg)
            out = sanitize_comment(raw, example)
            if _good_variation(out, example):
                return out
        except Exception as e_post:
            print(f"  [ai] POST attempt {attempt+1} failed: {e_post}")

    print("  [ai] all AI attempts failed, using original example as comment")
    return example

def ai_reply(prompt_text, system=None, max_tokens=90, retries=2):
    cfg = ai_cfg()
    lan_only = aiconfig.get_server_mode(cfg) == "lan"
    for _ in range(retries):
        if not _ai_wait_slot():
            return ""               # PURGE: don't start a fresh AI call
        if lan_only or str(cfg.get("api_key") or "").strip():
            try:
                r = _groq_chat(prompt_text, system=system, max_tokens=max_tokens,
                               cfg=cfg)
                if r and r.strip():
                    return r
            except Exception:
                pass
        if lan_only:
            continue
        try:
            r = _pollinations_get(prompt_text, system=system, cfg=cfg)
            if r and r.strip():
                return r
        except Exception:
            pass
        try:
            r = _pollinations_post(prompt_text, system=system, max_tokens=max_tokens,
                                   cfg=cfg)
            if r and r.strip():
                return r
        except Exception:
            pass
    return ""

def start_llama():
    cfg = ai_cfg()
    lan_only = aiconfig.get_server_mode(cfg) == "lan"
    if lan_only or str(cfg.get("api_key") or "").strip():
        if lan_only:
            print("  [ai] Checking LAN AI gateway...")
            try:
                _groq_chat("hi", timeout=15, cfg=cfg, _probe="start")
                print("  [ai] LAN AI gateway ready.")
                return True
            except Exception as e:
                print(f"  [ai] LAN gateway check failed ({e}); external fallback is disabled.")
                return False
        print("  [ai] Checking Groq AI (free tier)...")
        try:
            _groq_chat("hi", timeout=15, cfg=cfg, _probe="start")
            print("  [ai] Groq AI ready.")
            return True
        except Exception as e:
            if ai_endpoint_open():
                # Phones skip every AI path while the breaker is open, so a
                # healthy Pollinations would only make this check lie.
                print(f"  [ai] AI endpoint is down ({e}); comments use offline "
                      f"wordings until it answers again.")
                return False
            print(f"  [ai] Groq check failed ({e}); falling back to Pollinations...")
    import urllib.parse
    print("  [ai] Checking Pollinations AI (plain-text endpoint)...")
    try:
        req = urllib.request.Request(
            "https://text.pollinations.ai/hi?model=openai-fast",
            headers={"User-Agent": "Mozilla/5.0"})
        with NOPROXY.open(req, timeout=12) as resp:
            resp.read()
        print("  [ai] Pollinations AI ready.")
        return True
    except Exception as e:
        print(f"  [ai] GET endpoint failed ({e}), trying POST fallback...")
        try:
            _pollinations_post("hi", timeout=15, cfg=cfg)
            print("  [ai] Pollinations POST fallback ready.")
            return True
        except Exception as e2:
            print(f"  [ai] Pollinations AI fully unreachable: {e2}")
            return False

def stop_llama():
    pass

# Expanded comment pools (fallback only)
COMMENT_POOL = [
    "Grabe ang ganda!", "Sobrang ganda nito!", "Ang galing naman!", "Nice one!",
    "Astig nito!", "Ganda ah!", "Love it!", "Solid to!", "Petmalu talaga!",
    "Angas nito!", "Sana all", "Ang cute naman!", "Grabe idol!", "Perfect to!",
    "Napakaganda!", "Wow ang ganda!", "The best talaga!", "Sobrang lupit!",
    "Ganda ng vibes!", "Idol talaga!", "Bongga!", "Ang saya nito!", "Goals to!",
    "Napa-wow ako!", "Ang husay!", "Ansaya naman!", "Legit ang ganda!",
    "Love this!", "Ang ganda ng shot!", "Panalo to!", "Sobrang cute!", "Grabe ka!",
    "Ganda ganda!", "Good vibes!", "Nice nice!", "Napakaganda talaga!", "Superb!",
    "Ang ganda ha!", "Wagi to!", "Sulit na sulit!", "Ang ganda ng kuha!",
    "Fave ko to!", "Grabe sa ganda!", "Keri to!", "Ganda promise!",
    "Two thumbs up!", "Ang ganda ng araw!", "Wow lang!", "Ang ganda talaga!",
    "Ang lupit nito!", "Grabeng ganda naman!", "Sobrang worth it!", "Ang refreshing!",
    "Nakakatuwa!", "Ang galing!", "Panalo ang ganda!", "Walang kupas!",
    "Gandang-ganda ako!", "Sobrang nakaka-inspire!", "Ang saya-saya!",
    "Ganda ng pagkakakuha!", "Solid na solid!", "Ang ganda ng kulay!",
    "Napaka-creative!", "Ang galing ng shots!", "Sobrang aesthetic!",
    "Gandang tanawin!", "Ang sarap balikan!", "Ang ganda ng place!",
]

ENGLISH_POOL = [
    "This is amazing!", "So good!", "Love this!", "Absolutely stunning!",
    "Incredible shot!", "This looks great!", "Wow, beautiful!", "Nailed it!",
    "So cool!", "This is the best!", "Looking good!", "Perfect!",
    "That's awesome!", "Big fan of this!", "Superb!", "Well done!",
    "Love the vibe!", "This made my day!", "Pure gold!", "Top notch!",
    "Simply the best!", "So impressive!", "This is fire!", "Gorgeous!",
    "Amazing work!", "Keep it up!", "So inspiring!", "This is everything!",
    "Beautiful capture!", "Really nice!", "Outstanding!", "Loving this!",
    "Way to go!", "This hits different!", "Stunning as always!", "So clean!",
    "Great job!", "This is a whole vibe!", "Total goals!", "Absolute perfection!",
    "Obsessed with this!", "You crushed it!", "Right on point!", "Legendary!",
    "This deserves more love!", "Instant favorite!", "So worth it!",
    "Can't get enough!", "This is perfection!", "Masterpiece!",
    "I'm blown away!", "This is incredible!", "So happy for you!",
    "Fantastic!", "Brilliant!", "What a gem!", "This is gold!",
    "So proud!", "Amazing effort!", "This is next level!", "Inspiring!",
]

# ── Offline rewording ──────────────────────────────────────────────────────
# Drop-in synonyms for the vocabulary these comments actually use. Each list
# must mean the SAME thing as its key and fit the same slot in the sentence, so
# swapping is safe without any grammar work. This is what gives every account a
# visibly different comment when no AI endpoint is reachable.
_SYNONYMS = {
    # thanks / praise
    "salamat": ["maraming salamat", "salamat po", "thank you", "thankyou"],
    "maraming": ["sobrang", "napaka"],
    "maganda": ["ganda", "napakaganda", "ang ganda"],
    "ganda": ["maganda", "ang ganda"],
    "astig": ["angas", "lupit"],
    "galing": ["husay", "angas"],
    "husay": ["galing", "angas"],
    # size / degree
    "malaking": ["malaki na", "napakalaking", "sobrang laking"],
    "malaki": ["malaking", "napakalaki"],
    "sobrang": ["napaka", "grabeng", "talagang"],
    "napaka": ["sobrang", "talagang"],
    "grabe": ["sobra", "talaga"],
    # opportunity / help
    "oportunidad": ["pagkakataon", "tsansa", "opportunity"],
    "pagkakataon": ["oportunidad", "tsansa"],
    "tulong": ["saklolo", "suporta", "ayuda"],
    "suporta": ["tulong", "support"],
    "mapalakas": ["mapaigting", "palakasin", "mapabuti"],
    "mapaigting": ["mapalakas", "palakasin"],
    "mapabuti": ["mapaganda", "mapalakas"],
    # community / place
    "barangay": ["komunidad", "lugar namin", "community"],
    "komunidad": ["barangay", "lugar", "community"],
    "communities": ["komunidad", "mga lugar", "barangay"],
    "community": ["komunidad", "barangay"],
    "lugar": ["komunidad", "area"],
    "bayan": ["lungsod", "munisipyo"],
    # programs / projects / money
    "programa": ["proyekto", "programs", "mga programa"],
    "proyekto": ["programa", "project"],
    "pondo": ["budget", "salapi", "pera"],
    "sentimo": ["piso", "sentimos"],
    "budget": ["pondo", "salapi"],
    # safety
    "safe": ["ligtas", "panatag", "secure"],
    "ligtas": ["safe", "panatag"],
    "panatag": ["ligtas", "safe"],
    "protektado": ["ligtas", "safe"],
    # feeling / opinion
    "feeling": ["pakiramdam", "feeling talaga"],
    "pakiramdam": ["feeling", "damang"],
    "sana": ["hopefully", "sana talaga", "nawa"],
    "hopefully": ["sana", "sana talaga"],
    # correctness / result
    "tama": ["wasto", "maayos", "sakto"],
    "wasto": ["tama", "maayos"],
    "maayos": ["tama", "wasto"],
    # Replacements must fit the SAME slot: this word is usually followed by
    # "na", so "klarong"/"clear na" would produce "klarong na"/"clear na na".
    "malinaw": ["maliwanag", "klaro"],
    "resulta": ["bunga", "kinalabasan", "results"],
    "bunga": ["resulta", "kinalabasan"],
    "magamit": ["gamitin", "mapakinabangan"],
    "gamitin": ["magamit", "mapakinabangan"],
    # time / novelty
    "finally": ["sa wakas", "at last", "sa wakas talaga"],
    "bagong": ["panibagong", "bago na", "brand new na"],
    "bago": ["bagong", "panibago"],
    "ngayon": ["sa wakas", "sa ngayon"],
    # common English filler in Taglish
    "new": ["bagong", "brand new"],
    "great": ["astig", "ang galing"],
    "good": ["maganda", "okay"],
    "thanks": ["salamat", "maraming salamat"],
    # NOTE: no entry for nouns that are half of a fixed phrase ("fire trucks").
    # Swapping one word of a pair produced "fire fire trucks" / "fire sasakyan".
    "people": ["mga tao", "residente"],
    "everyone": ["lahat", "lahat ng tao"],
    "lahat": ["bawat isa", "lahat ng tao"],
    "bawat": ["kada", "lahat ng"],
}


def _syn_key(word):
    return re.sub(r"[^a-z0-9]", "", (word or "").lower())


def _match_case(original, replacement):
    """Keep the original word's capitalisation on its replacement."""
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _offline_rewordings(text, count=10):
    """Same-meaning rewrites built by swapping known words.

    No network, no model: this is what stops seven accounts posting the same
    sentence under one post when the AI endpoint cannot be reached.
    """
    words = text.split()
    slots = [i for i, w in enumerate(words) if _syn_key(w) in _SYNONYMS]
    if not slots:
        return []
    outs = []
    seen = {text.lower()}
    for _ in range(count * 8):
        if len(outs) >= count:
            break
        k = random.randint(1, min(len(slots), 4))
        new = list(words)
        for i in random.sample(slots, k):
            new[i] = _match_case(words[i],
                                 random.choice(_SYNONYMS[_syn_key(words[i])]))
        cand = " ".join(new)
        low = cand.lower()
        # A multi-word replacement can repeat the word beside it ("sa wakas" put
        # next to "wakas", "brand new na" next to "new"). Drop those.
        toks = low.split()
        if any(a == b for a, b in zip(toks, toks[1:])):
            continue
        if low not in seen:
            seen.add(low)
            outs.append(cand)
    return outs


def _example_variants(example):
    e = example.strip()
    if _example_shape(e)[2]:
        # Paragraph: doubling it reads as broken text and the "!"/"..." tricks
        # do nothing for prose. Reordering whole sentences is the only safe
        # offline variation that keeps every idea intact.
        parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", e) if s.strip()]
        outs = [e]
        if len(parts) >= 2:
            outs.append(" ".join(parts[1:] + parts[:1]))
            outs.append(" ".join(parts[::-1]))
    else:
        stem = e.rstrip(" .!?…")
        outs = [e, stem + "!", stem + "!!", stem + "...", stem + " " + stem]
    seen, res = set(), []
    for o in outs:
        k = o.strip().lower()
        if o.strip() and k not in seen:
            seen.add(k)
            res.append(o.strip())
    return res

def _opening_key(text):
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return " ".join(words[:2])

# PER POST, not per run. A duplicate only matters where a reader can SEE it:
# two identical comments under one post is the tell, the same wording under two
# different posts is invisible to everyone. This was one fleet-wide set, so a
# wording spent on link 1 was banned on links 2-10 as well - with 10 links that
# is roughly a tenth of the usable space, and it ran out. Measured on a real
# run: 3787 distinct comments posted, then 121 "no unused wording left" skips
# as the hour went on and comments/hour fell 879 -> 179.
#
# Keyed by the link the calling thread is working on (current_post_url()), so
# nothing needed a new argument. An empty key - legacy/CLI callers with no
# thread-local set - all share one pool, exactly as before.
_pool_by_link = {}
_example_turn = [0]          # rotates through multi-example input
_used_lock = threading.Lock()
_ai_fails = [0]
_ai_off = [False]            # hard off: a key/billing answer never self-heals
# Soft cooldown after transient failures (timeouts). Unlike _ai_off this
# EXPIRES, so one cold-start storm at the top of a run no longer costs the
# whole run its AI - which is what the old one-way latch did.
_ai_cold_until = [0.0]
AI_COOLDOWN = 120.0

# ── Shared AI endpoint breaker ────────────────────────────────────────────
# A stalled endpoint used to cost EVERY phone the full timeout on EVERY try:
# three context attempts, two style retries and three rewording attempts, each
# up to the 90s timeout - about ten minutes per comment per phone - and every
# abandoned request stayed queued on the server, deepening its backlog (590
# requests in flight on a gateway with 18 live clients, measured). One breaker
# now decides for the whole fleet. Two transport failures in a row (timeout,
# refused/reset connection, HTTP 5xx) open it; phones then use the offline
# wordings at once instead of queueing, and ONE background probe decides when
# the AI is back. Any success closes it. Cleared at every session start.
AI_BREAKER_THRESHOLD = 2
AI_PROBE_DELAYS = (10.0, 20.0, 40.0, 60.0)   # then every 60s while open
AI_PROBE_TIMEOUT = 60.0
AI_SLOW_SECONDS = 25.0
_ai_breaker_lock = threading.Lock()
_ai_breaker = {"failures": 0, "open": False, "since": 0.0, "reason": "",
               "generation": 0, "probing": False, "said": set()}


class AIEndpointCooling(RuntimeError):
    """Raised without sending anything while the shared AI breaker is open."""


def ai_endpoint_open():
    """True while the fleet is skipping the AI endpoint after failures."""
    return bool(_ai_breaker["open"])


def _is_timeout(exc):
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        exc = exc.reason
    return isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in str(exc).lower()


def _ai_transport_error(exc):
    """True when a failure says the endpoint itself is down or stalled."""
    if isinstance(exc, AIEndpointCooling):
        return False
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return isinstance(exc, (OSError, http.client.HTTPException))


def _ai_say_once(key, code, detail="", severity="warning"):
    """One diagnostic per outage and kind - never one per phone."""
    with _ai_breaker_lock:
        if key in _ai_breaker["said"]:
            return
        _ai_breaker["said"].add(key)
    try:
        report_error(code, detail=detail, severity=severity)
    except Exception:
        pass


def _ai_breaker_success(elapsed=None):
    with _ai_breaker_lock:
        was_open = _ai_breaker["open"]
        down_for = time.time() - _ai_breaker["since"] if was_open else 0.0
        _ai_breaker["failures"] = 0
        _ai_breaker["open"] = False
        _ai_breaker["reason"] = ""
        if was_open:
            _ai_breaker["said"].discard("outage")   # a later outage is news
    try:
        if was_open:
            took = f" in {elapsed:.1f}s" if elapsed is not None else ""
            print(f"  [ai] endpoint answering again{took} after {down_for:.0f}s "
                  f"- AI comments resume")
            report_error("AI_RECOVERED", severity="info",
                         detail=f"Answered{took} after {down_for:.0f}s of failures.")
        if elapsed is not None and elapsed >= AI_SLOW_SECONDS:
            _ai_say_once("slow", "AI_SLOW",
                         f"A reply took {elapsed:.0f}s; a healthy server answers in a few seconds.")
    except Exception:
        pass


def _ai_breaker_failure(exc, http_code=None, force=False):
    """Count one endpoint failure; open the breaker once it is clearly down."""
    if http_code is not None:
        code = "AI_SERVER_ERROR"
    elif _is_timeout(exc):
        code = "AI_TIMEOUT"
    else:
        code = "AI_UNREACHABLE"
    with _ai_breaker_lock:
        _ai_breaker["failures"] += 1
        opening = not _ai_breaker["open"] and (
            force or _ai_breaker["failures"] >= AI_BREAKER_THRESHOLD)
        if not opening:
            return
        _ai_breaker["open"] = True
        _ai_breaker["since"] = time.time()
        reason = f"HTTP {http_code}" if http_code is not None else type(exc).__name__
        _ai_breaker["reason"] = _diagnostic_text(f"{reason}: {exc}", 180)
        start_probe = not _ai_breaker["probing"]
        if start_probe:
            _ai_breaker["probing"] = True
        generation = _ai_breaker["generation"]
        say = "outage" not in _ai_breaker["said"]
        _ai_breaker["said"].add("outage")
        reason = _ai_breaker["reason"]
    print(f"  [ai] endpoint failing ({reason}) - phones use offline wordings "
          f"while it is re-checked in the background")
    if say:
        try:
            report_error(code, detail=reason, severity="warning")
        except Exception:
            pass
    if start_probe:
        _start_ai_probe(generation)


def _start_ai_probe(generation):
    threading.Thread(target=_ai_probe_loop, args=(generation,),
                     name="ai-endpoint-probe", daemon=True).start()


def _ai_probe_loop(generation):
    """Re-check a failing endpoint with one tiny request - never from a phone."""
    def stale():
        return (_ai_breaker["generation"] != generation or not _ai_breaker["open"]
                or _ai_off[0])
    try:
        attempt = 0
        while True:
            delay = AI_PROBE_DELAYS[min(attempt, len(AI_PROBE_DELAYS) - 1)]
            attempt += 1
            deadline = time.time() + delay
            while time.time() < deadline:
                if stale():
                    return
                time.sleep(min(0.5, max(0.0, deadline - time.time())))
            if stale():
                return
            try:
                cfg = ai_cfg()
                try:
                    limit = float(cfg.get("timeout") or AI_PROBE_TIMEOUT)
                except (TypeError, ValueError):
                    limit = AI_PROBE_TIMEOUT
                _groq_chat("hi", timeout=min(AI_PROBE_TIMEOUT, limit),
                           system="Reply with one word.", max_tokens=4, cfg=cfg,
                           _probe="recheck")
                return               # the success already closed the breaker
            except Exception as exc:
                nxt = AI_PROBE_DELAYS[min(attempt, len(AI_PROBE_DELAYS) - 1)]
                print(f"  [ai] endpoint still failing ({type(exc).__name__}) - "
                      f"next check in {nxt:.0f}s")
    finally:
        with _ai_breaker_lock:
            if _ai_breaker["generation"] == generation:
                _ai_breaker["probing"] = False


def _pool(url=None):
    """This link's used-wording sets. Call with _used_lock held."""
    key = url if url is not None else (current_post_url() or "")
    p = _pool_by_link.get(key)
    if p is None:
        p = {"comments": set(), "openings": set(),
             "anchors": set(), "compare": set(), "phrases": set(),
             # word key -> how many posted comments have used it
             "words": {}}
        _pool_by_link[key] = p
    return p


def _ai_is_off():
    """True while the AI must not be called: hard off, cooling down, or the
    shared breaker is open because the endpoint stopped answering."""
    return _ai_off[0] or time.time() < _ai_cold_until[0] or bool(_ai_breaker["open"])


def reset_ai_connection_state():
    """Forget endpoint failures/pacing after an idle settings switch.

    This does not generate a warm-up request or clear per-post comment history.
    The GUI calls it only while no commenting session is running.
    """
    # A previous session's background probe can still hold _ai_http_lock
    # during a slow request. Settings changes run on the GUI thread and must
    # not wait for that network operation to finish.
    with _used_lock:
        _ai_fails[0] = 0
        _ai_off[0] = False
        _ai_cold_until[0] = 0.0
        _ai_fatal_said[0] = False
        _poll_dead[0] = False
        _no_sampling.clear()
    _poll_last_call[0] = 0.0
    with _ai_slot_lock:
        _ai_next_slot[0] = 0.0
    with _ai_breaker_lock:
        _ai_breaker.update(failures=0, open=False, since=0.0, reason="",
                           probing=False, said=set())
        # A probe left over from the previous session sees the new generation
        # and exits without touching this one.
        _ai_breaker["generation"] = _ai_breaker.get("generation", 0) + 1
    # A changed endpoint or key must never ride an old socket.
    _AI_KEEPALIVE.reset()
    _AI_LAN_KEEPALIVE.reset()


def reset_comment_pool():
    with _used_lock:
        _pool_by_link.clear()
        _example_turn[0] = 0
    reset_ai_connection_state()
    # A new run means new links, and an edited post gets read again.
    reset_post_context()
    # FIX 1b: wake the endpoint up BEFORE the phones need it. A cold
    # llama.cpp/Ollama takes 40-120s to answer its first real prompt (measured:
    # 120s timeout, then 105.6s, 41.9s, 14.6s, 2.1s) while every phone is
    # asking at once, so the run opened with a burst of timeouts. In a thread:
    # the fleet starts immediately and the model loads alongside it.
    threading.Thread(target=warm_ai_endpoint, daemon=True).start()


def warm_ai_endpoint():
    """One throwaway generation so the model is resident before the run needs it."""
    try:
        t0 = time.time()
        # A cold load legitimately takes a minute, so the warm-up is never
        # judged as "slow" - only real comment replies are.
        _groq_chat("hi", timeout=180, system="Reply with one word.", max_tokens=4,
                   _probe="warmup")
        print(f"  [ai] endpoint warm in {time.time() - t0:.0f}s")
    except Exception as e:
        print(f"  [ai] warm-up failed ({type(e).__name__}: {e}) - "
              f"the first comments may be slow")

# Occasional light misspellings on purpose — they make the comments read like a
# real person typing on a phone rather than generated text.
TYPO_PROB = 0.30

def humanize_typo(text, prob=None):
    prob = TYPO_PROB if prob is None else prob
    if not text or len(text) < 6 or random.random() >= prob:
        return text
    words = text.split(" ")
    idxs = [i for i, w in enumerate(words) if len(w) >= 4]
    if not idxs:
        return text
    i = random.choice(idxs)
    w = words[i]
    j = random.randint(1, len(w) - 2)
    kind = random.choice(("swap", "swap", "drop", "double"))
    if kind == "swap":
        w = w[:j] + w[j + 1] + w[j] + w[j + 2:]
    elif kind == "drop":
        w = w[:j] + w[j + 1:]
    else:
        w = w[:j] + w[j] + w[j:]
    words[i] = w
    return " ".join(words)

def _norm_comment(text):
    """Dedupe key: letters and digits only, so spacing, punctuation and case
    differences cannot smuggle the same sentence through twice."""
    return re.sub(r"\s+", " ",
                  re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower())).strip()


def _claim_comment(text):
    """Reserve a wording for this run. False if it was already posted.

    Every path that returns a comment goes through here, so a sentence can be
    posted at most once per run no matter which path produced it.
    """
    key = _norm_comment(text)
    if not key:
        return False
    with _used_lock:
        used = _pool()["comments"]
        if key in used:
            return False
        used.add(key)
        return True


def _split_examples(text):
    """Several example comments in one field, separated by | or newlines.

    One example makes every account paraphrase the SAME sentence, so the whole
    post ends up with one idea worded 7 ways. Several examples give the AI
    genuinely different things to say.
    """
    parts = [p.strip() for p in re.split(r"[|\n\r]+", str(text or "")) if p.strip()]
    return parts


def pick_example(text):
    """Next example in rotation, so all of them get used evenly."""
    parts = _split_examples(text)
    if len(parts) <= 1:
        return parts[0] if parts else ""
    with _used_lock:
        i = _example_turn[0]
        _example_turn[0] = i + 1
    return parts[i % len(parts)]


def unique_comment(example="", tone=None, ctx=""):
    """Pick a wording, then charge its words against this post's budget.

    Done in one wrapper because the inner function returns from several places
    - the AI path and every offline fallback - and a word that rides out on a
    fallback counts against the cap exactly like one the model chose.
    """
    text = _unique_comment_inner(example, tone=tone, ctx=ctx)
    if text:
        _claim_words(text)
    return text


def prefetch_comment(tgt):
    """Start composing a link's comment while the phone is still opening it.

    Opening a post takes the phone several seconds; the comment used to be
    composed only AFTER that, so every AI round trip (and every quality-gate
    retry) was added on top. Composing alongside the open hides it. One
    pending comment per phone thread: retries of the same open reuse it, so a
    flaky open does not burn extra wordings.
    """
    try:
        if not tgt.get("do_comment"):
            return None
        url = str(tgt.get("url") or "")
        example = str(tgt.get("comment") or "")
    except AttributeError:
        return None
    if not url:
        return None
    pending = getattr(_tls, "prefetch", None)
    if pending and pending[0] == url and pending[1] == example:
        return pending[2]
    future = concurrent.futures.Future()

    def work():
        if not future.set_running_or_notify_cancel():
            return
        set_current_post_url(url)        # the per-post wording pool
        try:
            ctx = comment_examples_as_context(example) if example.strip() else ""
            future.set_result(unique_comment(example, ctx=ctx))
        except BaseException as exc:
            future.set_exception(exc)

    _tls.prefetch = (url, example, future)
    threading.Thread(target=work, name="comment-prefetch", daemon=True).start()
    return future


def take_prefetched_comment(url, example):
    """The prefetched wording for this link, or None to compose it inline.

    Returns "" when Abort is pressed while waiting; the caller checks
    stop_requested() before treating an empty result as anything else.
    """
    pending = getattr(_tls, "prefetch", None)
    if not pending or pending[0] != url or pending[1] != str(example or ""):
        return None
    _tls.prefetch = None
    future = pending[2]
    while True:
        try:
            return future.result(timeout=0.25)
        except concurrent.futures.TimeoutError:
            if stop_requested():
                return ""
        except Exception as exc:
            print(f"  [ai] prefetched comment failed ({type(exc).__name__}: {exc}) "
                  f"- composing it now")
            return None


def _unique_comment_inner(example="", tone=None, ctx=""):
    # Round-robin across the examples the user typed, then reword the one we
    # landed on. Done here so every caller gets it for free.
    all_examples = _split_examples(example)
    example = pick_example(example)
    # With a post context the examples are a STYLE SHEET, so every one of them
    # goes to the model, not just the rotated one - a single sample is not
    # enough to pin a voice down. The rotated one leads, so each account leans
    # on a different example and the nine comments do not converge.
    style = [example] + [e for e in all_examples if e != example]
    # Resolved once per comment rather than per retry: with TONE_RANDOM
    # that keeps one comment's two attempts in the same mood, while the
    # next comment - and the next phone - still gets a different one.
    tone = resolve_tone(tone)
    if example and not _ai_is_off():
        with _used_lock:
            avoid = set(_pool()["openings"])
        c = ""
        for _try in range(2):
            try:
                # More attempts when a real endpoint is configured: a Groq call
                # is ~2s, and the quality gates (drift, translation, near-copy)
                # legitimately reject a fair share of wordings. Too few retries
                # and a good endpoint still ends up posting the fallback.
                # Keep this small: every retry is another API call, and with a
                # fleet of ~19 phones sharing one free-tier key the request rate
                # is the scarce resource, not the wording.
                _tries = 3 if GROQ_API_KEY else 2
                c = (generate_comment(example, retries=_tries,
                                      avoid_openings=avoid,
                                      tone=tone, ctx=ctx,
                                      examples=style) or "").strip()
            except Exception as e:
                # SAY IT. A crash in here used to look exactly like the model
                # returning a bad wording - both just produced an offline
                # comment - which is how a TypeError in post_subjects() hid for
                # an entire release while every comment came out of the praise
                # pool. A generation bug must never be silent again.
                print(f"  [ai] generation crashed: {type(e).__name__}: {e}")
                c = ""
            if not c or c.lower() == example.lower():
                c = ""
                break
            op = _opening_key(c)
            with _used_lock:
                pool_now = _pool()
                fresh_text = _norm_comment(c) not in pool_now["comments"]
                fresh_open = op not in pool_now["openings"]
                if fresh_text and (fresh_open or _try == 1):
                    _ai_fails[0] = 0
                    _ai_cold_until[0] = 0.0
                    pool_now["comments"].add(_norm_comment(c))
                    if op:
                        pool_now["openings"].add(op)
                    return c
            avoid = avoid | {op}
        with _used_lock:
            _ai_fails[0] += 1
            # Give up on the AI sooner when it is plainly unreachable. Each
            # failed comment burns several HTTP attempts with backoff, so
            # waiting for 6 of them cost minutes at the start of every run
            # before the fallback took over. Reset per run by
            # reset_comment_pool(), so a working endpoint is still picked up.
            # This counter is FLEET-WIDE. With ~19 phones a couple of transient
            # rate-limit blips happen in the first seconds, so a low threshold
            # switched the AI off for the whole run and every account fell back
            # to the same handful of offline wordings - which is how duplicates
            # ended up under the post. Any success resets it to 0, so this only
            # trips when the endpoint is genuinely down.
            # FIX 5: a COOLDOWN, not a latch. This used to set _ai_off for the
            # rest of the run, so one cold-start storm - 18 phones all asking
            # while the model was still loading - permanently demoted the whole
            # run to the offline pool, which then ran dry. Now the AI is rested
            # for AI_COOLDOWN seconds and tried again; any success clears it.
            if _ai_fails[0] >= 15 and time.time() >= _ai_cold_until[0]:
                _ai_cold_until[0] = time.time() + AI_COOLDOWN
                _ai_fails[0] = 0
                print(f"  [ai] endpoint failing - resting it for "
                      f"{AI_COOLDOWN:.0f}s, then trying again "
                      f"(offline fallback until then)")
    lang = _detect_lang(example) if example else "tl"
    paragraph = bool(example) and _example_shape(example)[2]
    # COMMENT_POOL and ENGLISH_POOL are PRAISE pools ("Ang ganda talaga!",
    # "Absolute perfection!"). Under an angry post they do not merely miss the
    # voice - they say the opposite of what the run is for, which is a louder
    # bot signal than any wording problem. That is exactly what shipped: with
    # the endpoint down, sarcastic_anger examples ("kawawang pilipinas",
    # "puro mandarambong mga naka upo") posted as "Absolute perfection!".
    #
    # _detect_lang("kawawang pilipinas") returning "en" is what picked the
    # ENGLISH pool specifically, but the Tagalog praise pool would have been
    # just as wrong, so this guards the tone rather than the detector. For a
    # not-pleased tone the only safe offline source is the user's OWN examples:
    # they carry both the register and the subject. If those run out, post
    # nothing - silence beats a compliment here.
    no_praise = str(tone or "") in _NO_PRAISE_TONES
    if no_praise and not example:
        print(f"  [comment] endpoint down and no example to reword for a "
              f"'{tone}' comment - skipping rather than posting praise")
        return ""
    if no_praise:
        # _example_variants ends with the example said TWICE, which reads as
        # emphasis for praise ("Ang ganda! Ang ganda!") and as a stutter for a
        # statement ("kawawang pilipinas kawawang pilipinas"). Drop it here.
        _base = _plain_text(example).strip().lower()
        _orders = [v for v in _example_variants(example)
                   if not (_base and
                           _plain_text(v).strip().lower() == f"{_base} {_base}")]
        base_pool = list(_orders)
        for v in _orders:
            base_pool.extend(_offline_rewordings(v, count=10))
    elif paragraph:
        # The canned pools are one-liners ("Grabe ang ganda!"). Answering a
        # written-out, several-sentence example with one of those says something
        # completely different, so stay with the example's own meaning instead:
        # reorder its clauses AND swap known words, so each account posts a
        # visibly different comment rather than the same sentence nine times.
        orders = _example_variants(example)
        base_pool = list(orders)
        for v in orders:
            base_pool.extend(_offline_rewordings(v, count=10))
    elif lang == "en":
        base_pool = ENGLISH_POOL
    elif lang == "other" and example:
        base_pool = _example_variants(example)
    else:
        base_pool = COMMENT_POOL
    pool = [_plain_text(x) for x in base_pool if _plain_text(x)]
    random.shuffle(pool)
    for c in pool:
        if _claim_comment(c):
            return c

    # Pool exhausted. Manufacture NEW wordings instead of repeating one - the
    # old code ended here with random.choice(), which is exactly how the same
    # sentence appeared under the post two and three times.
    if example:
        for _ in range(6):
            extra = []
            for v in _example_variants(example):
                extra.extend(_offline_rewordings(v, count=25))
            random.shuffle(extra)
            for c in extra:
                c = _plain_text(c)
                if c and _claim_comment(c):
                    return c

    # Pasting two pool lines together only reads naturally for the praise
    # one-liners this was written for ("Ang ganda! Solid!"). An angry example is
    # a whole thought, so concatenating gives "kawawang pilipinas kawawang
    # pilipinas" - worse than saying nothing. For a not-pleased tone, the pool
    # running dry means this account has nothing left to say, and the skip
    # below is the right answer.
    if not paragraph and not no_praise:
        src = pool if pool else [_plain_text(x) for x in COMMENT_POOL]
        for _ in range(200):
            combo = f"{random.choice(src)} {random.choice(src)}"
            if _claim_comment(combo):
                return combo

    # Nothing unused is left. Post NOTHING rather than repeat a comment that is
    # already sitting under this post - a duplicate is what makes a run obvious.
    print("  [comment] no unused wording left - skipping this account")
    return ""

# ── IMPROVED Screenshot function with triple fallback ──
# Shots are saved to the PC (screenshots/<serial>/account_<n>/) AND kept on the
# phone itself (DEVICE_SHOT_DIR, which shows up in the Gallery) when
# keep_on_device is set.
DEVICE_SHOT_DIR = "/sdcard/Pictures/FBReactor"

def _scan_device_media(serial, device_path):
    # Nudge the phone so a freshly-saved image appears in Gallery/Photos.
    try:
        subprocess.run([ADB, "-s", serial, "shell", "am", "broadcast",
                        "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                        "-d", f"file://{device_path}"],
                       capture_output=True, timeout=10)
    except Exception:
        pass

def _push_shot_to_device(serial, pc_path, filename):
    # Copy a PC-side screenshot back onto the phone so it also lives in the gallery.
    try:
        device_path = f"{DEVICE_SHOT_DIR}/{filename}"
        subprocess.run([ADB, "-s", serial, "shell", "mkdir", "-p", DEVICE_SHOT_DIR],
                       capture_output=True, timeout=10)
        push = subprocess.run([ADB, "-s", serial, "push", pc_path, device_path],
                              capture_output=True, timeout=20)
        if push.returncode == 0:
            _scan_device_media(serial, device_path)
            print(f"    [SCREENSHOT] also saved on device: {device_path}")
            return True
        print(f"    [SCREENSHOT] push to device failed (code {push.returncode})")
    except Exception as e:
        print(f"    [SCREENSHOT] push-to-device exception: {e}")
    return False

def take_screenshot(serial, account_num, action, d=None, keep_on_device=True):
    try:
        # Serials for TCP/wifi/emulator devices look like "127.0.0.1:5555" — the
        # ':' (and other reserved chars) are illegal in Windows paths, so makedirs
        # would raise WinError 267 and the shot would silently fail. Sanitize the
        # serial for filesystem use while still passing the real serial to adb.
        safe_serial = re.sub(r'[<>:"/\\|?*]', '_', str(serial)).strip() or "device"
        shot_dir = os.path.join(HERE, "screenshots", safe_serial, f"account_{account_num}")
        os.makedirs(shot_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"fb_{safe_serial}_acc{account_num}_{action}_{timestamp}.png"
        filepath = os.path.join(shot_dir, filename)
        device_path = f"{DEVICE_SHOT_DIR}/{filename}"

        # ── Method 1: capture straight to a file ON THE PHONE, then pull a copy to
        # the PC. This is the only method that leaves the shot in the phone's
        # gallery, so it runs first whenever keep_on_device is requested.
        if keep_on_device:
            print(f"    [SCREENSHOT] capturing on device for {serial}...")
            try:
                subprocess.run([ADB, "-s", serial, "shell", "mkdir", "-p", DEVICE_SHOT_DIR],
                               capture_output=True, timeout=10)
                cap = subprocess.run([ADB, "-s", serial, "shell", "screencap", "-p", device_path],
                                     capture_output=True, timeout=15)
                if cap.returncode == 0:
                    pull = subprocess.run([ADB, "-s", serial, "pull", device_path, filepath],
                                          capture_output=True, timeout=20)
                    _scan_device_media(serial, device_path)
                    if pull.returncode == 0 and os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                        print(f"    [SCREENSHOT] OK saved on device ({device_path}) and PC ({filepath})")
                        return filepath
                    print(f"    [SCREENSHOT] device pull failed (code {pull.returncode}): "
                          f"{pull.stderr.decode(errors='replace')}")
                else:
                    print(f"    [SCREENSHOT] on-device screencap failed: "
                          f"{cap.stderr.decode(errors='replace')}")
            except Exception as e:
                print(f"    [SCREENSHOT] device-save method exception: {e}")

        # ── Method 2: exec-out screencap (binary-safe, PC only). ──
        print(f"    [SCREENSHOT] trying screencap (binary safe) for {serial}...")
        try:
            cmd = [ADB, "-s", serial, "exec-out", "screencap", "-p"]
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            if result.returncode == 0 and len(result.stdout) > 0:
                with open(filepath, "wb") as f:
                    f.write(result.stdout)
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    print(f"    [SCREENSHOT] OK (exec-out) saved to {filepath}")
                    if keep_on_device:
                        _push_shot_to_device(serial, filepath, filename)
                    return filepath
            print(f"    [SCREENSHOT] exec-out failed(size {len(result.stdout)})")
        except Exception as e:
            print(f"    [SCREENSHOT] exec-out exception: {e}")
        print(f"    trying another method + pull for {serial}...")
        try:
            temp_path = "/sdcard/temp_screenshot.png"
            capture_cmd = [ADB, "-s", serial, "shell", "screencap", "-p", temp_path]
            cap_result = subprocess.run(capture_cmd, capture_output=True, timeout=10)
            if cap_result.returncode == 0:
                pull_cmd = [ADB, "-s", serial, "pull", temp_path, filepath]
                pull_result = subprocess.run(pull_cmd, capture_output=True, timeout=10)
                subprocess.run([ADB, "-s", serial, "shell", "rm", temp_path], capture_output=True, timeout=5)
                if pull_result.returncode == 0 and os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    print(f"    [SCREENSHOT] Ok (pull) saved to {filepath}")
                    if keep_on_device:
                        _push_shot_to_device(serial, filepath, filename)
                    return filepath
                else:
                    print(f"    [SCREENSHOT] pull failed (code {pull_result.returncode}): "
                        f"{pull_result.stderr.decode(errors='replace')}")
                    if os.path.exists(filepath):
                        os.remove(filepath)
            else:
                print(f"    [SCREENSHOT] screencap failed: {cap_result.stderr.decode(errors='replace')}")
        except Exception as e:
            print(f"    [SCREENSHOT pull-method exception: {e}]")
        if d is not None:
            print(f"    [SCREENSHOT] trying uiautomator2 for {serial}...")
            try:
                img = d.screenshot()
                if img is not None:
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    img.save(filepath, "PNG")
                    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                        print(f"    [SCREENSHOT] OK (u2) saved to {filepath}")
                        if keep_on_device:
                            _push_shot_to_device(serial, filepath, filename)
                        return filepath
                    else:
                        print(f"    [SCREENSHOT] u2 saved empty file")
                        if os.path.exists(filepath):
                            os.remove(filepath)
                else:
                    print(f"    [SCREENSHOT] u2 returned no image")
            except Exception as e:
                print(f"    [SCREENSHOT] u2 failed: {e}")

        print(f"    [SCREENSHOT] all methods failed for {serial}")
        return None
    except Exception as e:
        print(f"  [screenshot] {serial} error: {e}")
        return None

def _keyboard_shown(serial):
    # True when the soft keyboard/IME is currently covering part of the screen.
    try:
        _, out, _ = run(["shell", "dumpsys", "input_method"], serial=serial)
        return "mInputShown=true" in out
    except Exception:
        return False

def prepare_post_view_for_shot(serial, d):
    # After posting a comment the soft keyboard usually still covers half the
    # screen. Drop it (a BACK press, only while the keyboard is actually up) so a
    # single screenshot can show BOTH the reaction on the post and the comment
    # that was just posted. The is-shown guard prevents an extra BACK from
    # navigating away from the post.
    try:
        for _ in range(2):
            if not _keyboard_shown(serial):
                break
            run(["shell", "input", "keyevent", "KEYCODE_BACK"], serial=serial)
            time.sleep(0.8)
    except Exception:
        pass
    try:
        poll_until(lambda: post_ready(d), 3)
    except Exception:
        pass
    time.sleep(0.6)
# ── ADB helpers ──

def run(args, serial=None, timeout=60):
    cmd = [ADB]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    try:
        # encoding must be pinned: text=True decodes with the Windows locale
        # (cp1252), and ADB output routinely carries emoji and non-Latin text -
        # names, captions, dumpsys. That raised UnicodeDecodeError inside
        # subprocess' reader thread and handed back EMPTY output, which then
        # looked like "the switcher would not open" or "got the system window".
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except subprocess.TimeoutExpired:
        if getattr(_link_check, "active", False):
            return 1, "", "timeout"        # heal_hung_link's own probe
        report_error("ADB_TIMEOUT", serial, detail=f"command={args[0] if args else '(empty)'}")
        # The command is NOT retried: a tap that timed out may still have
        # landed, and a Send must never go twice. Only the link is repaired,
        # so the caller's own next step reaches the phone again.
        if serial and args and args[0] in ("shell", "exec-out"):
            heal_hung_link(serial)
        return 1, "", "timeout"
    except OSError as e:
        report_error("ADB_UNAVAILABLE", serial, exc=e)
        return 1, "", "adb-unavailable"
    except UnicodeError as e:
        print(f"  [adb] output decode failed: {e}")
        return 1, "", "decode-error"


# A phone's USB link can hang while ADB still lists the phone as "device".
# Measured on a Redmi 9 phone box (cascaded USB 2.0 hubs, scrcpy mirroring every
# phone): two phones on one sub-hub stopped answering at the same moment -
# `adb shell echo ok` timed out on both for 7+ minutes, with no USB reset in the
# kernel log, while `adb devices` kept saying "device". wait_online() only
# repairs phones that are NOT "device", so nothing ever looked wrong: every
# command ran into its timeout, again and again, and the phone sat there doing
# nothing for the rest of the run. `adb -s <serial> reconnect` healed both at
# once - the shell answered within a second, and the run carried on.
HUNG_LINK_PROBE = 5.0          # an `echo` answers in ~40 ms on a healthy link
HUNG_LINK_RETRY = 30.0         # at most one reconnect per phone per 30 s
HUNG_LINK_RECOVER = 15.0       # how long a reconnected phone gets to answer
_link_healed_at = {}
_link_heal_lock = threading.Lock()
_link_check = threading.local()     # set while heal_hung_link() runs


def _link_answers(serial):
    rc, out, _ = run(["shell", "echo", "ok"], serial=serial,
                     timeout=HUNG_LINK_PROBE)
    return rc == 0 and "ok" in (out or "")


def heal_hung_link(serial):
    """Reconnect a listed phone whose USB link stopped answering.

    Only a phone ADB lists as "device" can have a hung link - a missing,
    offline or unauthorized phone is wait_online()'s job. It checks first: a
    slow command on a healthy phone is not a hung link, and then nothing is
    touched. Only a phone that cannot even echo within HUNG_LINK_PROBE seconds
    gets `adb reconnect` - the same as unplugging and replugging that one
    phone. Its USB-internet tunnel, if any, is restored by the tunnel monitor
    on its next pass. Returns True when the phone answers.
    """
    if not serial or stop_requested() or getattr(_link_check, "active", False):
        return False
    _link_check.active = True
    try:
        if device_state(serial) != "device":
            return False
        if _link_answers(serial):
            return True
        now = time.time()
        with _link_heal_lock:
            if now - _link_healed_at.get(serial, float("-inf")) < HUNG_LINK_RETRY:
                return False
            _link_healed_at[serial] = now
        print(f"  [adb] {serial} is listed but stopped answering (hung USB "
              f"link) - reconnecting it")
        dbg(serial, "usb-link-hung", "reconnecting")
        run(["reconnect"], serial=serial, timeout=15)
        deadline = time.time() + HUNG_LINK_RECOVER
        while time.time() < deadline:
            if stop_requested():
                return False
            if _link_answers(serial):
                print(f"  [adb] {serial} answers again after the reconnect.")
                # MIUI raises "Use USB for" on every (re)connect, a moment later.
                time.sleep(1.5)
                dismiss_system_popup(serial)
                return True
            time.sleep(1.0)
        print(f"  [adb] {serial} still not answering - check its cable and hub.")
        return False
    finally:
        _link_check.active = False

def list_devices():
    _, out, _ = run(["devices"])
    devices = {}
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        serial, _, state = line.partition("\t")
        serial = serial.strip()
        state  = state.strip()
        if serial:
            devices[serial] = state or "unknown"
    return devices

_RECOVERABLE_STATES = ("offline", "unknown")


def ensure_online():
    """Device list after trying to bring offline phones back.

    Only offline/unknown phones can be recovered from the PC. An UNAUTHORIZED
    phone waits for its owner to accept the debugging prompt, yet it used to
    trigger up to three ADB server restarts on every rescan and every workspace
    Start - each one disconnecting every other phone and, with USB internet on,
    cutting every phone's internet just as the run began.
    """
    devices = list_devices()
    for attempt in range(1, RECOVER_RETRIES + 1):
        bad = {s: st for s, st in devices.items() if st in _RECOVERABLE_STATES}
        if not bad:
            break
        print(f"[recover] attempt {attempt}: {len(bad)} phone(s) not ready, trying to recover...")
        run(["reconnect", "offline"])
        time.sleep(2)
        devices = list_devices()
        if any(st in _RECOVERABLE_STATES for st in devices.values()):
            if usb_internet_active():
                # The USB internet tunnels live inside the ADB server.
                print("[recover] not restarting ADB while USB internet is on - "
                      "every phone would lose its connection.")
                break
            run(["kill-server"])
            run(["start-server"])
            time.sleep(3)
            devices = list_devices()
    return devices

def has_fb(serial):
    _, out, _ = run(["shell", "pm", "list", "packages", FB_PACKAGE], serial=serial)
    return FB_PACKAGE in out

def find_apk():
    candidates = glob.glob(os.path.join(HERE, "*.apk"))
    if not candidates:
        return None
    for c in candidates:
        name = os.path.basename(c).lower()
        if "lite" in name or "facebook" in name:
            return c
    return candidates[0]

_FB_WEB_HOST_RE = re.compile(r"(?:^|\.)facebook\.com$", re.I)
# Redirect-only hosts: kept intact here and resolved once per run. fb.gg is
# Facebook Gaming's short host and is how most game LIVE links are shared.
_FB_SHORT_HOSTS = {"fb.watch", "www.fb.watch", "fb.me", "www.fb.me",
                   "fb.gg", "www.fb.gg"}
# Facebook's own alias domain. Measured 2026-11-18: fb.com/<path> and
# www.fb.com/<path> answer 301 to www.facebook.com/<the same path>, so they
# are rewritten locally instead of being rejected as "not Facebook".
_FB_ALIAS_HOSTS = {"fb.com", "www.fb.com"}
_FB_TRACKING_KEYS = {
    "fbclid", "mibextid", "sfnsn", "rdid", "extid", "ref", "refid",
    "refsrc", "paipv", "eav", "locale", "acontext", "app", "app_id",
    "notif_id", "notif_t", "hc_ref", "__cft__", "__tn__", "_rdr",
    # Added by Facebook's own share redirects (seen 2026-11-18 on /share/v/
    # and /reel/ answers): "?rdid=..&share_url=.." and "?extid=reels&wtsid=..".
    "share_url", "wtsid",
}


def _facebook_host(host):
    host = str(host or "").lower().rstrip(".")
    return bool(_FB_WEB_HOST_RE.search(host) or host in _FB_SHORT_HOSTS
                or host in _FB_ALIAS_HOSTS)


def _url_from_pasted_text(raw):
    """Extract one Facebook link from copied text, preserving a bare URL."""
    text = html.unescape(str(raw or "")).replace("\\/", "/").strip()
    text = text.strip('"').strip("'").strip()
    if not text:
        return ""
    # An entirely encoded copied URL must be decoded before searching for a
    # hostname, otherwise '%2Fm.facebook.com' can look like a bare hostname.
    if re.match(r"(?i)^(?:https?|facebook|fb)%3a", text):
        text = urllib.parse.unquote(text)
    if re.match(r"(?i)^https?://", text):
        try:
            if not _facebook_host(urllib.parse.urlsplit(text).hostname):
                return text
        except ValueError:
            return ""
    # Messenger/Facebook copy often includes a sentence before or after the
    # link. Accept every official web host form, with or without a scheme.
    # Do not extract the facebook.com suffix of an unrelated hostname, or a
    # nested Facebook-looking path in an external URL.
    pattern = (
        r"(?i)(?<![\w.@/:-])(?:(?:facebook|fb)://[^\s<>]+|"
        r"(?:https?://|//)?(?:[a-z0-9-]+\.)*facebook\.com(?:[/?#][^\s<>]*)?|"
        r"(?:https?://|//)?(?:www\.)?(?:fb\.watch|fb\.me|fb\.gg|fb\.com)"
        r"(?:[/?#][^\s<>]*)?)(?![\w.@:-])")
    m = re.search(pattern, text)
    if m:
        text = m.group(0)
    # Sentence punctuation copied immediately after a URL is not part of it.
    return text.rstrip(".,;!)]}>\"'”’")


def _drop_fb_tracking(query):
    """Keep routing/query IDs and unknown semantics; remove known trackers."""
    kept = []
    for key, value in urllib.parse.parse_qsl(query or "", keep_blank_values=True):
        low = key.lower()
        if low.startswith("utm_") or low in _FB_TRACKING_KEYS or low.startswith("__"):
            continue
        kept.append((key, value))
    return urllib.parse.urlencode(kept, doseq=True)


def normalize_url(raw, _depth=0):
    """Deterministically normalize any official Facebook URL.

    This stage never touches the network, so it is safe to call from the GUI
    while the user types. Redirect-only links (share tokens, fb.watch, fb.me)
    are resolved once by normalize_phone_url() when a run starts.
    """
    if _depth > 5:
        return ""
    url = _url_from_pasted_text(raw)
    if not url:
        return ""
    scheme = re.match(r"(?i)^(facebook|fb)://", url)
    if scheme:
        try:
            native = urllib.parse.urlsplit(url)
            if not native.netloc or native.username or native.password:
                return ""
            pairs = urllib.parse.parse_qs(native.query, keep_blank_values=True)
            # The facewebmodal wrapper often arrives from a copied app intent.
            if native.netloc.lower() == "facewebmodal":
                nested = (pairs.get("href") or pairs.get("url") or [""])[0]
                return normalize_url(nested, _depth + 1) if nested else ""
            return urllib.parse.urlunsplit((
                scheme.group(1).lower(), native.netloc.lower(), native.path,
                _drop_fb_tracking(native.query), ""))
        except (ValueError, UnicodeError):
            return ""
    if url.startswith("//"):
        url = "https:" + url
    elif not re.match(r"(?i)^https?://", url):
        url = "https://" + url
    try:
        parts = urllib.parse.urlsplit(url)
        # Credentials and custom ports are not Facebook share links.
        if parts.username or parts.password or parts.port not in (None, 80, 443):
            return ""
    except Exception:
        return ""
    host = (parts.hostname or "").lower().rstrip(".")
    if not _facebook_host(host):
        return ""

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    query_pairs = urllib.parse.parse_qs(parts.query, keep_blank_values=True)

    # Facebook outbound and share wrappers sometimes contain another Facebook
    # URL in `u=`. Unwrap only when the destination is itself Facebook; never
    # turn this tool into a generic external-link opener.
    if (_FB_WEB_HOST_RE.search(host) and
            path.lower().rstrip("/") in (
                "/l.php", "/share.php", "/sharer.php", "/sharer/sharer.php",
                "/flx/warn")):
        nested = (query_pairs.get("u") or query_pairs.get("href") or [""])[0]
        if nested:
            return normalize_url(nested, _depth + 1)
        return ""

    # Every Facebook web/mobile/basic/touch hostname routes through the same
    # stable HTTPS host. Short-link hosts must remain intact until resolved.
    clean_host = host if host in _FB_SHORT_HOSTS else "www.facebook.com"
    if clean_host == "www.fb.watch":
        clean_host = "fb.watch"
    elif clean_host == "www.fb.me":
        clean_host = "fb.me"
    elif clean_host == "www.fb.gg":
        clean_host = "fb.gg"

    # /amp is a presentation variant, not part of the post identity.
    if path.lower().endswith("/amp"):
        path = path[:-4] or "/"
    if path != "/":
        path = path.rstrip("/")
    query = _drop_fb_tracking(parts.query)
    return urllib.parse.urlunsplit(("https", clean_host, path, query, ""))

def parse_action_mode(mode):
    raw     = (mode or "").strip().lower()
    compact = re.sub(r"[^a-z]+", "", raw)
    if compact in ("", "r", "react", "reaction"):
        return True, False
    if compact in ("c", "comment", "comments"):
        return False, True
    if compact in ("b", "both", "rc", "cr",
                   "reactcomment", "commentreact",
                   "reactandcomment", "commentandreact",
                   "reactcomments", "commentsreact"):
        return True, True
    return False, False

def install_fb(serial, apk):
    print(f"  [install] {serial} <- {os.path.basename(apk)} ...")
    rc, out, err = run(["install", "-r", apk], serial=serial, timeout=300)
    ok = "Success" in out or "Success" in err
    return ok and has_fb(serial)

def connect(serial):
    try:
        return u2.connect(serial)
    except Exception as e:
        print(f"  [!] {serial} u2 connect failed: {e}")
        return None

def device_state(serial):
    return list_devices().get(serial, "missing")

def wait_online(serial):
    delay    = 2
    announced = False
    while True:
        if stop_requested():
            return False
        st = device_state(serial)
        if st == "device":
            if announced:
                print(f"    [{serial}] back online.")
            return True
        if st == "unauthorized":
            return False
        if not announced:
            print(f"    [{serial}] {st} - waiting for it to come back...")
            announced = True
        run(["reconnect", "offline"])
        run(["reconnect"], serial=serial)
        run(["wait-for-device"], serial=serial, timeout=20)
        if device_state(serial) == "device":
            if announced:
                print(f"    [{serial}] back online.")
            return True
        time.sleep(delay)
        delay = min(delay + 2, 15)

def wake_and_unlock(serial):
    for attempt in range(3):
        # Screen-wake retries used to hold Abort up for ~9s; bail out at once.
        if stop_requested():
            return
        _, out, _ = run(["shell", "dumpsys", "display"], serial=serial)
        is_off = "mScreenState=OFF" in out
        if is_off:
            run(["shell", "input", "keyevent", "224"], serial=serial)
            if not abort_sleep(1.5):
                return
        _, out, _ = run(["shell", "dumpsys", "window"], serial=serial)
        # Only a keyguard that is really SHOWING gets the unlock swipe.
        #
        # The old test asked whether the strings "mKeyguard" and
        # "mKeyguardDrawComplete" merely APPEARED in the dump - both appear on
        # an UNLOCKED phone too, and "mKeyguardOccluded=true" is absent exactly
        # when the phone is unlocked, so the whole condition was true for a
        # phone that was already unlocked. wake_and_unlock() then fired a
        # full-screen bottom-to-top swipe into whatever app was open - three
        # times. That is the "phones just scroll the feed" behaviour: this runs
        # on every device when a run starts (pre_wake) and again right before
        # each account's post is opened.
        #
        # Test the VALUES instead. If nothing says the lockscreen is up, assume
        # it is not: a missed unlock shows up as an honest failure, while a
        # wrong swipe silently scrolls the feed and ruins the run.
        keyguard_active = any(m in out for m in (
            "mDreamingLockscreen=true",
            "isStatusBarKeyguard=true",
            "mKeyguardShowing=true",
            "keyguardShowing=true",
            "mShowingLockscreen=true",
        ))
        if not keyguard_active:
            return
        w, h = 720, 1600
        for line in out.splitlines():
            if "Display" in line and "init=" in line:
                m = re.search(r'init=\((\d+),(\d+)\)', line)
                if m:
                    w, h = int(m.group(1)), int(m.group(2))
        if stop_requested():
            return
        run(["shell", "input", "swipe",
             str(w // 2), str(h - 120),
             str(w // 2), str(h // 4)], serial=serial)
        if not abort_sleep(1.5):
            return
        run(["shell", "input", "keyevent", "82"], serial=serial)
        if not abort_sleep(1):
            return

U2_JAR_ON_DEVICE = "/data/local/tmp/u2.jar"


def repair_u2_jar(serial):
    """Fix the one device fault that leaves a phone sitting there doing nothing.

    uiautomator2 pushes its server jar to /data/local/tmp/u2.jar. If a
    DIRECTORY of that name exists - one interrupted `adb push` is enough to
    create it - every later push silently lands INSIDE it as u2.jar/u2.jar, and
    the launcher's CLASSPATH then points at a directory:

        E appproc: ERROR: could not find class 'com.wetest.uia2.Main'
        u2 connect failed: ('server quit unexpectly', 'Aborted')

    uiautomator2 cannot recover on its own, because its integrity check md5s
    the PATH, and a directory hashes as the EMPTY file - which never matches
    the jar, so it re-pushes into the directory every time, forever.
    connect_resilient() then retries for the whole run while the phone does
    nothing, with no message that names the real cause.

    Found on a TECNO KM4 (Android 15) whose `ls -l` looked like a normal 3.7 MB
    file because it was really listing the directory's contents.

    Returns True when something was actually repaired.
    """
    try:
        _, out, err = run(["shell", "ls", "-ld", U2_JAR_ON_DEVICE],
                          serial=serial)
    except Exception:
        return False
    line = ((out or "") + (err or "")).strip()
    if not line.startswith("d"):
        return False
    print(f"  [{serial}] {U2_JAR_ON_DEVICE} is a DIRECTORY - uiautomator can "
          f"never start on this phone. Removing it so the jar can be pushed.")
    run(["shell", "rm", "-rf", U2_JAR_ON_DEVICE], serial=serial)
    return True


def connect_resilient(serial):
    repaired = False
    while True:
        if stop_requested():
            return None
        if not wait_online(serial):
            return None
        d = connect(serial)
        if d is not None:
            try:
                d.window_size()
                return d
            except Exception:
                pass
        # Most failed connects are transient and worth simply retrying, but the
        # u2.jar-is-a-directory fault never heals by itself and would otherwise
        # burn the entire run. Try the repair once, then go back to retrying.
        if not repaired:
            repaired = True
            if repair_u2_jar(serial):
                continue
        time.sleep(2)

def ensure_device(serial, d):
    wait_online(serial)
    if d is not None:
        try:
            d.window_size()
            return d
        except Exception:
            pass
    while True:
        if stop_requested():
            return d
        if device_state(serial) == "unauthorized":
            return d
        wait_online(serial)
        nd = connect(serial)
        if nd is not None:
            try:
                nd.window_size()
                return nd
            except Exception:
                nd = None
        time.sleep(2)

def safe_dump(dev):
    """UI hierarchy, retrying until it actually contains the Facebook window.

    uiautomator dumps the ACTIVE window, and on some phones it hands back the
    SystemUI window instead — a tree holding nothing but the status bar, nav bar
    and notifications. The old version returned that first dump happily, so
    every detector saw an empty screen: no menu button, no switcher, no account
    rows. That is why account switching reported "Could not open account
    switcher" on a phone whose switcher was perfectly fine.

    Only retried while Facebook really is in the foreground; if it genuinely is
    not showing, the dump is correct and returned as-is.
    """
    # A Snapshot is already a finished, validated dump - re-running the retry
    # logic on it would re-do the ~330 ms fb_foreground() check for nothing.
    if getattr(dev, "_frozen", False):
        return dev.dump_hierarchy()
    serial = getattr(dev, "serial", None)
    last = ""
    for attempt in range(6):
        try:
            xml = dev.dump_hierarchy()
        except Exception:
            if serial:
                # A listed phone whose link hung looks "online" to
                # wait_online(); check it can still answer, reconnect if not.
                heal_hung_link(serial)
                wait_online(serial)
            else:
                time.sleep(1.0)
            continue
        if not xml:
            time.sleep(0.4)
            continue
        last = xml
        if FB_PACKAGE in xml:
            return xml                     # got the Facebook window
        if not serial or not fb_foreground(serial):
            return xml                     # can't verify, or FB really isn't up
        # A pop-up that never goes away by itself ("Use USB for") would make
        # the wait below run out every time: close it and read again.
        if dismiss_system_popup(serial):
            continue
        if attempt == 0:
            print("    [dump] got the system window instead of Facebook - "
                  "waiting for Facebook to take input focus")
            # Wait for focus instead of re-dumping SystemUI six times in three
            # seconds and then reporting an empty screen to the caller.
            poll_until(lambda: fb_has_focus(serial), 8.0)
        else:
            time.sleep(0.5)
    return last


# ── Live progress ──────────────────────────────────────────────────────────
# Called the MOMENT an account actually reacts or comments. The GUI used to
# count doses only in mark_done(), which fires when a whole phone has finished
# every account - so the counter sat still and then jumped in chunks. Set this
# to a callable to receive each dose as it lands.
PROGRESS_HOOK = None


def report_progress(serial, kind, account_num=None, link_no=None, url=""):
    hook = PROGRESS_HOOK
    if not hook:
        return
    try:
        hook({"serial": serial, "kind": kind, "account": account_num,
              "link": link_no, "url": url})
    except Exception:
        pass


# ── Debug trace ────────────────────────────────────────────────────────────
# Records what each phone is actually looking at, so a run that wanders off the
# task leaves evidence instead of just looking wrong. Lines go to the console
# and to venom_debug.log beside the program.
DEBUG_TRACE = True
DEBUG_LOG = os.path.join(HERE, "venom_debug.log")
_dbg_lock = threading.Lock()
ERROR_HOOK = None

# Stable codes make the console, desktop diagnostics and exported logs useful
# even when a phone only returns a terse status such as "comment-no-box".
_ERROR_HELP = {
    "INVALID_URL": ("Facebook link is invalid or unsupported.",
                    "Paste a Facebook post, photo, reel, video or live broadcast link."),
    "DEVICE_UNAUTHORIZED": ("USB debugging is not authorized.",
                            "Unlock the phone and accept its USB debugging prompt, then rescan."),
    "DEVICE_OFFLINE": ("The phone is offline or disconnected.",
                      "Check the USB data cable, unlock the phone and rescan devices."),
    "DEVICE_CONNECT_FAILED": ("Could not connect to the phone's automation service.",
                             "Confirm USB debugging is enabled and reconnect the phone."),
    "FACEBOOK_NOT_INSTALLED": ("Facebook Lite is not installed and no installer is available.",
                               "Install Facebook Lite on the phone, then rescan and try again."),
    "FACEBOOK_INSTALL_FAILED": ("Facebook Lite could not be installed on the phone.",
                                "Check storage and installation prompts, or install Facebook Lite manually."),
    "AI_UNAVAILABLE": ("The AI provider could not be reached.",
                       "Check the provider in AI settings; comments will use your example wordings as a fallback."),
    "AI_TIMEOUT": ("The AI server did not answer in time.",
                   "Phones now use your example wordings and the server is re-checked in the "
                   "background. If this repeats, the server is overloaded or running without its GPU."),
    "AI_UNREACHABLE": ("The AI server could not be reached.",
                       "Check the AI address, this computer's internet connection and that the server "
                       "is running. Phones use your example wordings until it answers again."),
    "AI_SERVER_ERROR": ("The AI server returned a server error.",
                        "The server is busy, restarting or misconfigured. Phones use your example "
                        "wordings and AI comments resume automatically when it recovers."),
    "AI_SLOW": ("The AI server is answering slowly.",
                "Comments are delayed while it catches up. Check the server's load and that its "
                "GPU is in use; a healthy server answers in a few seconds."),
    "AI_RECOVERED": ("The AI server is answering again.",
                     "AI-written comments have resumed."),
    "AI_AUTH_FAILED": ("The AI server rejected the API key.",
                       "Enter the correct key in AI settings. The AI is off for this session."),
    "AI_REQUEST_REJECTED": ("The AI server rejected the comment request.",
                            "Check the model name and address in AI settings."),
    "ADB_TIMEOUT": ("An Android device command timed out.",
                    "Check the phone and USB connection; reconnect if it remains unresponsive."),
    "ADB_UNAVAILABLE": ("Android platform tools could not start.",
                        "Install adb or restore the bundled platform-tools files, then restart the app."),
    "TARGET_OPEN_FAILED": ("The requested Facebook content could not be opened.",
                           "Open the link manually with the same account; check access, availability and connection."),
    "SHARE_UNRESOLVED": ("The shared link could not be resolved on this computer.",
                         "The original link will still be tried on the phone; if it fails, copy the content's direct permalink from Facebook."),
    "LINK_NOT_SINGLE_CONTENT": ("This link does not point to a single post, reel, video or photo.",
                                "It will still be tried, but comments need one post: open it in Facebook, "
                                "tap Share > Copy link, and paste that link instead."),
    "LINK_NOT_COMMENTABLE": ("Stories take private replies, not public comments, so this link was skipped.",
                             "Paste the link of a post, reel or video instead."),
    "TARGET_NOT_VERIFIED": ("The requested post view could not be verified.",
                            "Check the phone is on the intended content and has access; this link was skipped. "
                            "The phone's screen is saved in the debug folder."),
    "PHONE_NO_INTERNET": ("The phone says its connection has no internet.",
                          "Check this phone's Wi-Fi or mobile data and the router; the post is retried "
                          "as soon as the connection returns."),
    "USB_VPN_STALE_STOPPED": ("Turned off a leftover USB internet VPN that had nothing behind it.",
                              "The phone is back on its own Wi-Fi. Start USB internet again if you "
                              "want it to use this computer's connection."),
    "COMMENT_EMPTY": ("There is no comment text to enter.",
                      "Add an example comment for this link."),
    "COMMENT_NO_BOX": ("A comment composer could not be found.",
                       "Check that comments are enabled and this account can comment on the content."),
    "COMMENT_TYPE_FAILED": ("Text entry into the comment composer failed.",
                            "Check the phone's keyboard and automation service, then retry the link."),
    "COMMENT_NOT_TYPED": ("The entered comment could not be verified in the composer.",
                          "Check keyboard focus and text entry on the phone before retrying."),
    "COMMENT_SEND_FAILED": ("Comment submission could not be confirmed.",
                            "Inspect the phone and connection, and check the post before retrying to avoid duplicates."),
    "COMMENT_SUBMIT_UNVERIFIED": ("A send was attempted but submission could not be verified.",
                                  "Check the post manually before retrying; another send may duplicate the comment."),
    "COMMENT_WORDING_EXHAUSTED": ("No unused comment wording is available for this link.",
                                 "Add more example comments or check the AI provider; this attempt was skipped."),
    "ACTION_BLOCKED": ("Facebook restricted this account's actions.",
                       "Review the account's restriction in Facebook before running it again."),
    "ACCOUNT_LOGIN_REQUIRED": ("Facebook requires login or account verification.",
                               "Complete login or the checkpoint on the phone."),
    "ACCOUNT_SUSPENDED": ("Facebook reports this account as suspended.",
                          "Review the account status in Facebook."),
    "ACCOUNT_SWITCH_FAILED": ("The next saved account could not be selected.",
                              "Check the account chooser on the phone and confirm the account can log in."),
    "DEVICE_WORKER_FAILED": ("The device worker stopped unexpectedly.",
                             "Review the error details and phone state before starting another session."),
    "SESSION_FAILED": ("The commenting session stopped unexpectedly.",
                       "Review the error details and saved links, then restart the session."),
}


def _diagnostic_text(value, limit=800):
    """Keep diagnostics single-line and redact common credentials in errors."""
    value = " ".join(str(value or "").split())
    value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redacted]", value)
    value = re.sub(
        r"(?i)([?&](?:access_token|token|api_key|key|appsecret_proof)=)[^&#\s]+",
        r"\1[redacted]", value)
    value = re.sub(
        r"(?i)\b((?:api[_ -]?key|access[_ -]?token|password)\s*[:=]\s*)[^\s,;]+",
        r"\1[redacted]", value)
    return value[:limit]


def _append_debug_line(line):
    try:
        with _dbg_lock:
            try:
                if os.path.getsize(DEBUG_LOG) > 5_000_000:
                    os.replace(DEBUG_LOG, DEBUG_LOG + ".1")
            except OSError:
                pass
            with open(DEBUG_LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        # A read-only application directory must not stop a device worker.
        pass


def report_error(code, serial="", detail="", *, account_num=None,
                 link_no=None, url="", exc=None, severity="error"):
    """Print, persist and deliver an actionable error without interrupting work.

    ERROR_HOOK runs on the calling worker thread; GUI callers must queue the
    event onto the UI thread. Error output remains enabled when tracing is off.
    """
    code = re.sub(r"[^A-Z0-9_]", "_", str(code).upper())[:64]
    message, hint = _ERROR_HELP.get(
        code, (code.replace("_", " ").capitalize() + ".",
               "Review the details and check the phone before retrying."))
    if exc is not None:
        detail = f"{detail} {type(exc).__name__}: {exc}".strip()
    event = {"time": time.strftime("%H:%M:%S"), "severity": severity,
             "code": code, "serial": _diagnostic_text(serial, 100),
             "account": account_num, "link": link_no,
             "url": _diagnostic_text(url), "message": message,
             "hint": hint, "detail": _diagnostic_text(detail)}
    context = " ".join(str(part) for part in (
        event["serial"], f"account={account_num}" if account_num is not None else "",
        f"link={link_no}" if link_no is not None else "") if part)
    line = f"[{event['time']}] [{severity.upper()} {code}]"
    if context:
        line += f" [{context}]"
    line += f" {message}"
    if event["detail"]:
        line += f" | {event['detail']}"
    if event["url"]:
        line += f" | target={event['url']}"
    line += f" | Try: {hint}"
    try:
        print(line)
    except Exception:
        pass
    _append_debug_line(line)
    hook = ERROR_HOOK
    if hook:
        try:
            hook(dict(event))
        except Exception:
            pass
    return event


def dbg(serial, event, detail=""):
    if not DEBUG_TRACE:
        return
    line = (f"[{time.strftime('%H:%M:%S')}] [{serial}] {event}"
            + (f" | {detail}" if detail else ""))
    print(f"    [trace] {line}")
    _append_debug_line(line)


def classify_screen(d):
    """Name the surface the phone is on right now, for the debug trace.

    Facebook Lite exposes almost no text, so this is deliberately coarse - the
    only distinction that matters is "the post we were told to act on" versus
    "somewhere else", and "somewhere else" is the bug.
    """
    serial = getattr(d, "serial", None)
    try:
        if serial and not fb_foreground(serial):
            return "facebook-not-running"
    except Exception:
        pass
    try:
        snap = d if getattr(d, "_frozen", False) else Snapshot(d)
    except Exception:
        return "unknown"
    try:
        if dump_is_blind(snap):
            return "unreadable"
    except Exception:
        pass
    try:
        if not is_logged_in(snap):
            return "login"
    except Exception:
        pass
    for name, probe in (("account-switcher", lambda: bool(account_rows(snap))),
                        ("post", lambda: on_post_view(snap, 0.0))):
        try:
            if probe():
                return name
        except Exception:
            pass
    try:
        text = (visible_text(snap) or "").lower()
    except Exception:
        text = ""
    if "search..." in text or "search facebook" in text:
        return "search"
    if any(m in text for m in FEED_MARKERS):
        return "feed"
    try:
        if _has_post_action_row(snap):
            return "feed-or-list"
    except Exception:
        pass
    return "elsewhere"


# Screens the run legitimately passes through. Anything else means the phone
# has WANDERED - Marketplace, a profile, search, the feed, someone else's post
# - and the only safe response is to go back to the link, never to act where
# it happens to be standing.
WORKING_SCREENS = frozenset({"post", "account-switcher", "login", "unreadable"})


def task_log(serial, phase, detail=""):
    """GOOD path: the run doing what it is supposed to be doing.

    One prefix for everything that IS the job - opening the link, switching
    accounts, reacting, commenting - so the operator can read a run at a
    glance and anything tagged [bad] stands out against it. Prints as well as
    traces, because a [trace]-only line is invisible in the GUI log pane.
    """
    line = f"{phase:<9} {detail}".rstrip()
    print(f"    [task] [{serial}] {line}")
    dbg(serial, "task", line)


def bad_log(serial, phase, detail=""):
    """BAD path: the phone somewhere the run never asked it to be."""
    line = f"{phase:<9} {detail}".rstrip()
    print(f"    [bad ] [{serial}] {line}")
    dbg(serial, "bad", line)


def _short_target(url):
    """The recognisable tail of a link, for one-line log messages."""
    text = str(url or "")
    if not text:
        return "(no url)"
    m = re.search(
        r"/(reel|videos?|posts|permalink|photo|story\.php|share/[a-z])/?([^/?&]*)",
        text, re.I)
    if m:
        return f"{m.group(1)}/{m.group(2)}" if m.group(2) else m.group(1)
    # Anything else: drop the scheme and host and keep the path. A blind tail
    # cut ("ps://www.facebook.com/...") reads like corruption in a log.
    path = re.sub(r"(?i)^https?://[^/]+", "", text).lstrip("/")
    return (path or text)[:40]


def ensure_on_target(serial, d, url=None, where=""):
    """Confirm the phone is on the target post, and put it back if it drifted.

    This is the self-realignment step: drifting off the link used to end in a
    refusal ("not on the target post"), which is safe but leaves the account
    unprocessed. Here we say WHERE the phone actually is and WHICH link it
    should be on, then re-open it.

    Everything below prints as well as tracing. Drift used to be visible only
    as a [trace] line, so a run that spent its time on Marketplace looked
    exactly like a run that was working - which is precisely the case an
    operator needs to see.
    """
    url = url or current_post_url()
    target = _short_target(url)
    if on_post_view(d, timeout=2.0):
        return True
    if stop_requested():
        return False
    if dump_is_blind(d):
        # A blind dump usually means Facebook IS foreground but has not taken
        # input focus, which is not evidence of drift - thrashing on it would
        # restart a post that opened fine. But when Facebook is not in the
        # foreground at all (home screen, launcher, another app) the screen is
        # blind precisely BECAUSE the phone left, and "assume still on target"
        # is how a phone sits outside the app for an entire run while every
        # check reports success. Measured: KEYCODE_HOME produced
        # screen='unreadable', ensure_on_target returned True, and
        # on_post_view was False.
        try:
            pkg = foreground_package(serial)
        except Exception:
            pkg = FB_PACKAGE
        if not pkg or pkg == FB_PACKAGE:
            # Nothing focused yet (the cold-start gap) or Facebook itself:
            # the answer is "wait", not "go back".
            dbg(serial, "screen-unreadable", f"{where}; assuming still on target")
            return True
        label, screen = "WANDERED", pkg
    else:
        screen = classify_screen(d)
        label = ("WANDERED" if screen not in WORKING_SCREENS else "OFF-TARGET")
    msg = (f"{where}: phone is on '{screen}', expected {target}"
           + (" - this screen is not part of the run at all"
              if label == "WANDERED" else ""))
    bad_log(serial, label.lower(), f"{msg} - going back")
    if not url:
        bad_log(serial, "no-url", "no target url on this thread - cannot recover")
        return False
    ok = False
    try:
        ok = open_target_post(serial, d, url)
    except Exception as e:
        dbg(serial, "realign-error", f"{type(e).__name__}: {e}")
    if ok:
        task_log(serial, "recovered", f"back on {target}")
    else:
        bad_log(serial, "stuck", f"FAILED to get back to {target}")
    return ok


def dump_is_blind(d):
    """True when we cannot actually see Facebook's screen right now.

    Measured on the itel/CPH phones: when Facebook Lite is in the FOREGROUND but
    does not hold input focus, uiautomator dumps the SystemUI window instead - a
    12-item tree of the status bar and nav bar with no Facebook content at all.
    Every text/geometry detector then reads an empty screen and truthfully
    reports "nothing found".

    That is the dangerous case: "nothing found" must never be read as "the thing
    is not there", because the fallback for that is usually to SCROLL, and what
    actually gets scrolled is whatever is really on screen - the news feed.
    """
    try:
        xml = safe_dump(d)
    except Exception:
        return True
    if xml and FB_PACKAGE in xml:
        return False                      # we can genuinely see Facebook
    serial = getattr(d, "serial", None)
    if serial and fb_foreground(serial):
        return True                       # FB is up but we are reading SystemUI
    return False                          # FB really is not showing


# Screen resolution cannot change during a run, but window_size() is a ~400 ms
# ADB round trip and core.py asks for it from 25 places - several of them
# inside the scroll loop. Measured on real hardware, so it is cached per
# device for the life of the process.
_wsize_cache = {}
_wsize_lock = threading.Lock()


def safe_window_size(dev):
    serial = getattr(dev, "serial", None)
    if serial:
        with _wsize_lock:
            hit = _wsize_cache.get(serial)
        if hit:
            return hit
    for _ in range(6):
        try:
            sz = dev.window_size()
            if sz and serial:
                with _wsize_lock:
                    _wsize_cache[serial] = sz
            return sz
        except Exception:
            if serial:
                heal_hung_link(serial)
                wait_online(serial)
            else:
                time.sleep(2)
    return dev.window_size()


def forget_window_size(serial=None):
    """Drop cached resolution (device reconnected, or rotated)."""
    with _wsize_lock:
        if serial:
            _wsize_cache.pop(serial, None)
        else:
            _wsize_cache.clear()
    with _density_lock:
        if serial:
            _density_cache.pop(serial, None)
            _density_retry_at.pop(serial, None)
        else:
            _density_cache.clear()
            _density_retry_at.clear()


# ── Screen density ─────────────────────────────────────────────────────────
# Every size limit in the screen detectors (button widths and heights, row
# gaps, band tolerances) is in PIXELS, measured on the fleet's 720-px phones.
# Facebook Lite lays its screens out in dp, so one control is bigger in pixels
# on a denser screen. The reel rail's gap proves it: 144 px on the itel
# (720x1612), 126 px on the TECNO KM4 (720x1600), 198 px on a Redmi 9
# (M2004J19C, 1080x2340, density 440) - 72 dp on every one of them.
#
# On that Redmi phone box the rail buttons measured 148x176 px, just past the
# 140x175 limit, so the reel player was never recognised: every reel and video
# link "would not open" and was force-stopped and reopened until it was given
# up, while posts (found by text) still worked. The account switcher's chevron
# (35-90 px) and every other shape test had the same ceiling.
#
# So each detector converts its limits with px(): the pixel size a control
# measured on the fleet has on THIS screen. The fleet's phones draw 1.75-2.0
# px per dp, and on them px(n) == n exactly - the limits there are untouched.
REF_PX_PER_DP = 2.0
_density_cache = {}
_density_lock = threading.Lock()


def _read_px_per_dp(dev):
    """Pixels per dp as the phone reports them, or None.

    uiautomator2 reports the screen in pixels AND in dp (`displaySizeDpX`), so
    a live phone answers exactly. A saved dump or a test double cannot.
    """
    try:
        if not isinstance(dev, u2.Device):
            return None
        info = dev.info or {}
        dp_width = float(info.get("displaySizeDpX") or 0)
        px_width = float(info.get("displayWidth") or 0)
        if dp_width > 0 and px_width > 0:
            return px_width / dp_width
    except Exception:
        pass
    return None


DENSITY_RETRY = 30.0           # after a failed read, judge by the screen meanwhile
_density_retry_at = {}


def px_per_dp(dev):
    """Pixels per dp of this phone's screen (cached per phone)."""
    serial = getattr(dev, "serial", None)
    if serial:
        with _density_lock:
            hit = _density_cache.get(serial)
            retry_at = _density_retry_at.get(serial, 0.0)
        if hit:
            return hit
    else:
        retry_at = 0.0
    if time.time() >= retry_at:
        value = _read_px_per_dp(dev)
        if value is not None and 1.0 <= value <= 6.0:
            if serial:
                with _density_lock:
                    _density_cache[serial] = value
            return value
        if serial and isinstance(dev, u2.Device):
            # The phone was asked and could not say. Every screen check calls
            # this, so do not ask again on each one while uiautomator recovers.
            with _density_lock:
                _density_retry_at[serial] = time.time() + DENSITY_RETRY
    # Nothing to ask (a saved dump, a test double): phones are about 360-410
    # dp across their SHORT side, so judge by that - 720 px reads as 2.0, the
    # fleet itself, in portrait and in landscape (1600x720) alike.
    try:
        short_side = float(min(safe_window_size(dev)[:2]))
    except Exception:
        return REF_PX_PER_DP
    return max(1.0, short_side / 360.0)


def dp_scale(dev):
    """How many times bigger, in pixels, this screen draws the controls than
    the 720-px phones the limits were measured on. Never below 1, so the
    fleet's own phones keep exactly the limits they were tuned with."""
    return max(1.0, px_per_dp(dev) / REF_PX_PER_DP)


def _scaler(dev):
    """px(n): a size of n pixels on the fleet's phones, on this screen."""
    k = dp_scale(dev)
    if k <= 1.0:
        return lambda n: n
    return lambda n: int(round(n * k))

def poll_until(check, timeout, interval=0.4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_requested():
            return False
        try:
            if check():
                return True
        except Exception:
            pass
        if not abort_sleep(interval):
            return False
    return False

def fb_foreground(serial):
    _, out, _ = run(["shell", "dumpsys", "window", "windows"], serial=serial)
    return FB_PACKAGE in out


def foreground_package(serial):
    """The package holding INPUT FOCUS, or "" when nothing does.

    fb_foreground() asks `FB_PACKAGE in dumpsys window windows`, which lists
    EVERY window - so it stays true while Facebook merely has a window
    somewhere, including on the home screen and inside other apps. Measured on
    the TECNO: KEYCODE_HOME and launching Settings both left fb_foreground()
    reporting True, so drift out of the app was invisible.

    mCurrentFocus names the single focused window, which tells the three cases
    apart: Facebook, a different app, or nothing focused yet (the cold-start
    gap, where the answer is "wait", not "go back").
    """
    # This ROM prints SEVERAL mCurrentFocus lines and the first is often
    # `null` (a display with nothing focused). Returning on the first match
    # therefore always answered "" - which reads as "cold-start gap" and made
    # every drift look like settling. Scan them all and take the first that
    # actually names a package.
    _, out, _ = run(["shell", "dumpsys", "window"], serial=serial)
    for line in out.splitlines():
        if "mCurrentFocus" not in line:
            continue
        m = re.search(r"\su0\s+([A-Za-z0-9_.]+)/", line)
        if m:
            return m.group(1)
    return ""


def fb_has_focus(serial):
    """True when Facebook actually holds INPUT focus.

    uiautomator dumps the FOCUSED window, not the foreground one. For the first
    few seconds after a cold start mCurrentFocus is null, so the dump comes back
    as the SystemUI window - a dozen status-bar items - and every detector
    truthfully reports an empty screen. Measured on the CPH phones: focus is
    null at t+1.5s, Facebook at t+3.0s, and the dump only reaches its full size
    at t+6s. That gap is why a healthy phone reported "Could not open account
    switcher" and processed just one account.
    """
    _, out, _ = run(["shell", "dumpsys", "window"], serial=serial)
    for line in out.splitlines():
        if "mCurrentFocus" in line:
            return FB_PACKAGE in line
    return False


# System pop-ups that land on top of Facebook Lite and are safe to close with
# Back (= Cancel, nothing changes). MIUI raises "Use USB for" every time a
# phone's USB link connects - plugging the phone box in, and every adb
# reconnect. Measured on a Redmi 9 (MIUI 12): it holds input focus, so each
# dump read Settings instead of Facebook ("got the system window"), the post
# verified as unreadable, and the run only escaped after its wander check fired.
_DISMISSABLE_POPUPS = ("Settings$UsbDetailsActivity",)


def dismiss_system_popup(serial):
    """Close a known system pop-up holding focus over Facebook Lite.

    Only the exact windows named in _DISMISSABLE_POPUPS are touched - never a
    USB-debugging prompt, a permission dialog or anything else. True when one
    was closed.
    """
    try:
        _, out, _ = run(["shell", "dumpsys", "window"], serial=serial)
    except Exception:
        return False
    for line in out.splitlines():
        if "mCurrentFocus" not in line:
            continue
        if any(name in line for name in _DISMISSABLE_POPUPS):
            run(["shell", "input", "keyevent", "KEYCODE_BACK"], serial=serial)
            print(f"    [{serial}] closed the phone's 'Use USB for' pop-up")
            dbg(serial, "popup-closed", line.strip()[:120])
            time.sleep(0.6)
            return True
    return False

def post_ready(d):
    try:
        if find_reel_like_point(d) is not None:
            return True
        return _post_surface(d)
    except Exception:
        return False

# ── Target-post lock ───────────────────────────────────────────────────────
# post_ready() alone is NOT enough to act: the news feed also has post action
# rows, so if a deep link fails and Facebook lands on the feed, the old code
# would happily react to whatever post was under the cursor. These markers only
# ever render on a single-post (permalink) view — the feed has no comment
# composer and no comments header — so they pin us to the post we were told to
# act on.
POST_VIEW_MARKERS = (
    # English - composer placeholder and comment-list headers only. NOT a bare
    # "comment": the feed's per-post action button is literally "Comment", so a
    # bare word would match the feed and defeat the whole point of this lock.
    "write a comment", "write a public comment", "comment as",
    "add a comment", "most relevant", "all comments", "top comments",
    "view more comments", "view previous comments",
    "be the first to comment", "no comments yet",
    # Tagalog / Filipino locales. Same rule: the feed's button is "Komento" /
    # "Magkomento", so those bare labels are deliberately EXCLUDED - only the
    # composer placeholder ("Magsulat/Sumulat ng komento") and the comments
    # header ("Lahat ng komento") prove we are on the post itself.
    "magsulat ng komento", "sumulat ng komento",
    "lahat ng komento",
)

# Phrases that appear ONLY on the news feed / home, never on a single-post
# permalink view (FB Lite shows the status composer only on the feed and
# timeline). If any of these is on screen we are NOT on the target post, no
# matter what else matched - this is what stops a deep link that quietly
# dropped onto the feed from being mistaken for the post and scrolled.
FEED_MARKERS = (
    "what's on your mind", "whats on your mind", "what’s on your mind",
    "ano ang nasa isip mo", "anong nasa isip mo", "nasa isip mo",
)

def has_comment_composer(d):
    """True when the post's comment box is on screen - by SHAPE, not text.

    Group posts (and any post whose a11y tree carries no text at all - the
    normal case on FB Lite) expose no marker word for on_post_view to match, so
    a perfectly open post verified as "not on the target post" and every opener
    variant was rejected: measured 102s of retries per group link, per account.

    The comment composer is the one widget the FEED never has at the bottom of
    the screen - the feed's status composer sits at the TOP - so an editable
    text widget low on screen means we are on a single post. Measured on a group
    post: android.widget.MultiAutoCompleteTextView at y=1467 of 1612.
    """
    try:
        width, height = safe_window_size(d)
        root = hierarchy_root(d)
        if root is None:
            return False
        for node in root.iter("node"):
            cls = node.attrib.get("class", "")
            if not ("EditText" in cls or "AutoCompleteTextView" in cls
                    or node.attrib.get("editable") == "true"):
                continue
            b = parse_bounds(node.attrib.get("bounds"))
            if not b:
                continue
            if center(b)[1] > int(height * 0.80):
                return True
    except Exception:
        pass
    return False


def _fb_comment_box(d):
    """Bounds of Facebook Lite's comment box wherever it sits, or None.

    Across ~2,000 real dumps from the fleet the ONLY editable field Facebook
    Lite ever showed was its comment box (a MultiAutoCompleteTextView: "Write a
    comment…", "Sumulat ng comment...", "Comment as <name>" or a draft), so an
    editable Facebook Lite field means a comment box is open. Fields of other
    apps (launcher search, Google) are ignored.
    """
    root = hierarchy_root(d)
    if root is None:
        return None
    best = None
    for node in root.iter("node"):
        attrs = node.attrib
        cls = attrs.get("class", "")
        if not ("EditText" in cls or "AutoCompleteTextView" in cls
                or attrs.get("editable") == "true"):
            continue
        package = attrs.get("package")
        if package and package != FB_PACKAGE:
            continue
        bounds = parse_bounds(attrs.get("bounds"))
        if bounds and (best is None or bounds[1] > best[1]):
            best = bounds
    return best


def _one_screen_read(d):
    """A frozen Snapshot of a live phone's screen, or `d` itself.

    Anything already frozen, or that is not a phone at all, is used as it is;
    so is a live phone whose dump comes back empty (each detector then tries
    for itself, exactly as before).
    """
    if (getattr(d, "_frozen", False) or not hasattr(d, "dump_hierarchy")
            or not hasattr(d, "window_size")):
        return d
    try:
        snap = Snapshot(d)
    except Exception:
        return d
    return snap if snap else d


def _reel_sheet_open(d):
    """This thread's reel/video has its comment sheet open on screen.

    With the keyboard up the sheet's box rides up to mid-screen (y=950 of 1612
    measured) and the sheet covers the rail, so neither has_comment_composer()
    (bottom fifth only) nor the rail check can see it - and a comment that had
    just posted there was verified as "left the post". Search boxes sit in the
    top of the screen and are excluded.
    """
    if not _is_reel_context():
        return False
    try:
        box = _fb_comment_box(d)
        if box is None:
            return False
        _, height = safe_window_size(d)
        return center(box)[1] > int(height * 0.30)
    except Exception:
        return False


def on_post_view(d, timeout=0.0, feed_grace=None):
    """True only when a single post (or reel) is on screen — never the feed.

    Waits up to `timeout` seconds for the view to settle. Returns False the
    moment Abort is pressed.

    `feed_grace`: give up early once the NEWS FEED has stayed on screen this
    many seconds. A deep link can flash the feed before it pushes the post, so
    a moment of feed is not proof - but a feed that stays put is, and waiting
    out a long `timeout` there only delays trying the next opener. That is what
    lets the post-open check be patient with a SLOW post without being slow on
    a link that landed on the feed.
    """
    deadline = time.time() + max(0.0, timeout)
    feed_since = None
    while True:
        if stop_requested():
            return False
        # One read of the screen for every detector below. Each used to pull
        # its own ~140 ms dump from the phone - four or five per check - and
        # they could disagree, having looked at different moments.
        view = _one_screen_read(d)
        try:
            text = visible_text(view).lower()
        except Exception:
            text = ""
        # Feed veto first: if the home/feed status composer is on screen we are
        # NOT on the post, so never let a marker (or the reel check) say we are.
        on_feed = any(f in text for f in FEED_MARKERS)
        if on_feed and feed_grace is not None:
            now = time.time()
            feed_since = now if feed_since is None else feed_since
            if now - feed_since >= feed_grace:
                return False
        elif not on_feed:
            feed_since = None
        if not on_feed:
            try:
                if find_reel_like_point(view) is not None:
                    return True      # the reel player is a single-item surface
            except Exception:
                pass
            if any(m in text for m in POST_VIEW_MARKERS):
                return True
            # Text-free fallback: FB Lite usually exposes NO caption or marker
            # words, so the composer's shape is the only proof left.
            if has_comment_composer(view):
                return True
            if _reel_sheet_open(view):
                return True
        if time.time() >= deadline:
            return False
        if not abort_sleep(0.5):
            return False

_url_resolve_cache = {}
_url_resolve_failed_at = {}
_url_resolve_lock = threading.Lock()
SHARE_RETRY_AFTER = 300.0
_FB_CONTENT_ID = r"(?:\d+|pfbid[0-9A-Za-z]+)"
_SHARE_TOKEN_RE = re.compile(r"^/share/(?:[a-z]/)?[^/?#]+/?$", re.I)


def _fb_target_reference(url):
    """Return (surface, exact ID) for a supported single-content URL.

    Match parsed paths and whole IDs, never arbitrary query text. A watch
    feed, album, profile or login redirect that happens to *contain* a post URL
    must not masquerade as the post when resolving a short link.
    """
    clean = normalize_url(url)
    if not clean:
        return None
    parts = urllib.parse.urlsplit(clean)
    path = urllib.parse.unquote(parts.path).rstrip("/")
    query = urllib.parse.parse_qs(parts.query)

    def content_id(*keys):
        for key in keys:
            value = (query.get(key) or [""])[0]
            if re.fullmatch(_FB_CONTENT_ID, value):
                return value
        return None

    if parts.scheme in ("facebook", "fb"):
        route = parts.netloc.lower()
        query_keys = {"story": ("story_fbid", "id"),
                      "photo": ("fbid", "id"),
                      "video": ("v", "video_id", "id"),
                      "watch": ("v", "video_id", "id")}.get(route, ("id",))
        token = path.strip("/") or content_id(*query_keys)
        if not token and route in ("post", "story"):
            compound = (query.get("id") or [""])[0]
            if re.fullmatch(r"[0-9]+_[0-9]+", compound):
                token = compound
        if token and re.fullmatch(_FB_CONTENT_ID, token):
            kind = {"post": "post", "story": "post", "reel": "reel",
                    "video": "video", "watch": "video", "live": "video",
                    "photo": "photo"}.get(route)
            if kind:
                return kind, token
        if route in ("post", "story") and re.fullmatch(r"\d+_\d+", token or ""):
            return "post", token
        return None

    host = (parts.hostname or "").lower()
    if host in ("fb.gg", "www.fb.gg"):
        match = re.fullmatch(r"/v/(\d+)", path, re.I)
        return ("video", match.group(1)) if match else None
    if not _FB_WEB_HOST_RE.search(host):
        return None
    for kind, pattern in (
        ("reel", rf"/reels?/({_FB_CONTENT_ID})"),
        ("post", rf"/(?:posts|permalink)/(?:[^/]+/)?({_FB_CONTENT_ID})"),
        ("video", rf"/(?:videos?|live|live_videos)/(?:[^/]+/)*({_FB_CONTENT_ID})"),
        ("photo", rf"/photos/(?:[^/]+/)*({_FB_CONTENT_ID})"),
        ("story", rf"/stories/[^/]+/({_FB_CONTENT_ID})"),
    ):
        match = re.search(pattern + r"$", path, re.I)
        if match:
            return kind, match.group(1)
    if path.lower() in ("/watch", "/watch/live", "/video.php", "/video"):
        token = content_id("v", "video_id")
        if token:
            return "video", token
    if path.lower() in ("/photo", "/photo.php"):
        token = content_id("fbid")
        if token:
            return "photo", token
    if path.lower() in ("", "/story.php", "/permalink.php"):
        token = content_id("story_fbid")
        if token:
            return "post", token
    if re.fullmatch(r"/groups/[^/]+", path, re.I):
        token = content_id("multi_permalinks")
        if token:
            return "post", token
    return None


def _fb_native_web_url(url):
    """An HTTPS fallback for a recognized native post/reel/video/photo link."""
    reference = _fb_target_reference(url)
    if not reference:
        return ""
    kind, token = reference
    base = "https://www.facebook.com/"
    if kind == "reel":
        return base + "reel/" + token
    if kind == "video":
        return base + "watch?v=" + token
    if kind == "photo":
        return base + "photo.php?fbid=" + token
    if kind == "post":
        if "_" in token:
            owner, post = token.split("_", 1)
            return base + "story.php?story_fbid=" + post + "&id=" + owner
        parts = urllib.parse.urlsplit(url)
        if parts.netloc.lower() == "story":
            query = urllib.parse.parse_qs(parts.query)
            owner = (query.get("id") or [""])[0]
            if query.get("story_fbid") and re.fullmatch(r"[0-9]+", owner):
                return base + "story.php?story_fbid=" + token + "&id=" + owner
        return base + "permalink/" + token
    return ""


# What a link shows on the phone - and so where its comment box is:
#   reel  - Facebook Lite's reel player. Comment is the SECOND button of the
#           rail down the right edge (Like, Comment, Share, More) and opens a
#           sheet with the box at the bottom. 6,593 comments in the fleet's own
#           logs were posted that way.
#   video - the same reel player. Facebook serves every video as a reel on a
#           phone: measured 2026-11-18, /watch/?v=X, /<page>/videos/X and a
#           1920x1080 /share/v/ link all answer an Android browser with
#           https://www.facebook.com/reel/X as their canonical page, and the
#           fleet's screenshots show landscape videos in the reel player too.
#   post, photo, story - a single post; the box sits at its bottom.
# /share/r|v|p/<token> carry their kind in the path even before (or without)
# resolution, and fb.watch / fb.gg are video hosts.
_SHARE_KINDS = {"r": "reel", "v": "video", "p": "post"}
_target_kind_cache = {}


def _target_kind(url):
    """'reel', 'video', 'post', 'photo', 'story' or None (unknown)."""
    key = str(url or "")
    if key in _target_kind_cache:
        return _target_kind_cache[key]
    kind = None
    reference = _fb_target_reference(key)
    if reference:
        kind = reference[0]
    else:
        clean = normalize_url(key)
        if clean:
            parts = urllib.parse.urlsplit(clean)
            host = (parts.hostname or "").lower()
            match = re.fullmatch(r"/share/([a-z])/[^/]+", parts.path, re.I)
            if host in ("fb.watch", "fb.gg"):
                kind = "video"
            elif match:
                kind = _SHARE_KINDS.get(match.group(1).lower())
    if len(_target_kind_cache) > 512:
        _target_kind_cache.clear()
    _target_kind_cache[key] = kind
    return kind


def _shows_in_reel_player(url):
    """True when Facebook Lite opens this link in its reel player."""
    return _target_kind(url) in ("reel", "video")
# Facebook Lite's own user agent shape. A desktop UA is answered with HTTP 400
# on /share/ links, and facebookexternalhit returns the /videos/<slug>/<id>
# permalink instead of the /reel/<id> form the reel player actually wants -
# which is why it is only the fallback (the ID is the same either way).
_SHARE_UA = "Mozilla/5.0 (Linux; Android 10)"
_CRAWLER_UA = "facebookexternalhit/1.1"
# The canonical link lives in <head>; everything after it is app payload.
_SHARE_HTML_LIMIT = 300000
_CANONICAL_HREF_RE = re.compile(
    r"<link\b(?=[^>]*\brel=[\"']canonical[\"'])[^>]*\bhref=[\"']([^\"']+)[\"']",
    re.I)
_OG_URL_RE = re.compile(
    r"<meta\b(?=[^>]*\bproperty=[\"']og:url[\"'])[^>]*\bcontent=[\"']([^\"']+)[\"']",
    re.I)


def _read_until_canonical(resp, limit=_SHARE_HTML_LIMIT, chunk=32768):
    """Read only as far as the canonical link, not the whole page.

    The permalink sits in <head>, but a Facebook share page carries ~80-400 KB
    of app payload after it. Stopping at the tag is what keeps the pre-run link
    resolution quick instead of a full page download per link, and the run
    starts as soon as the last link is known.
    """
    parts, total = [], 0
    while total < limit:
        block = resp.read(min(chunk, limit - total))
        if not block:
            break
        parts.append(block)
        total += len(block)
        text = b"".join(parts).decode("utf-8", "replace")
        if _canonical_from_html(text):
            return text
    return b"".join(parts).decode("utf-8", "replace")


def _canonical_from_html(body):
    """The real permalink Facebook prints into a /share/ page's <head>."""
    for pattern in (_CANONICAL_HREF_RE, _OG_URL_RE):
        m = pattern.search(body or "")
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def _canonical_candidates_from_html(body):
    """Try both metadata tags when one of them names an unusable page."""
    candidates = []
    for pattern in (_CANONICAL_HREF_RE, _OG_URL_RE):
        for match in pattern.finditer(body or ""):
            value = html.unescape(match.group(1)).strip()
            if value and value not in candidates:
                candidates.append(value)
    return candidates


def _accept_resolved_url(candidate):
    """The normalized candidate, but only when it really names a post.

    A login wall, a checkpoint or another /share/ token is no better than the
    link we already hold, so those are rejected and the original is kept.
    """
    candidate = normalize_url(candidate)
    if not candidate:
        return ""
    try:
        parts = urllib.parse.urlsplit(candidate)
    except Exception:
        return ""
    if (parts.scheme != "https" or
            not _FB_WEB_HOST_RE.search(parts.hostname or "") or
            re.match(r"^/(?:login|checkpoint|recover)(?:[/.]|$)", parts.path, re.I) or
            not _fb_target_reference(candidate)):
        return ""
    return candidate


def _canonicalize_fb_url(url):
    """Turn a share/fb.watch redirector into the real post URL.

    Share tokens (/share/p|r|v/<token>) and fb.watch links carry NO post id, so
    Facebook Lite has to resolve them itself and very often lands on the feed
    instead of the post. Resolving them here on the PC turns them into a
    canonical facebook.com URL that _fb_uri_variants can read a proper
    fb://reel/<id> or fb://post/<id> deep link out of.

    Two resolution paths, tried in order:
      1. the HTTP redirect chain - what fb.watch/fb.me still use, and
      2. the <link rel="canonical"> / og:url in the page body. /share/* links
         now answer 200 with NO redirect, so geturl() reports the share token
         straight back and path 1 alone can never resolve a reel again.

    Cached, never raises, and only accepts a result that still looks like a post
    (so a login/checkpoint page can never make things worse - we just keep the
    original link).
    """
    try:
        parts = urllib.parse.urlsplit(url)
        is_share = bool(_SHARE_TOKEN_RE.fullmatch(parts.path))
        is_short = (parts.hostname or "").lower() in _FB_SHORT_HOSTS
    except Exception:
        return url
    if not is_share and not is_short:
        return url
    with _url_resolve_lock:
        if url in _url_resolve_cache:
            # A failure is only remembered briefly: it used to stick for the
            # life of the app, so one network blip at Start sent the raw share
            # link - which Facebook Lite often opens on the feed - to every
            # phone in every later run until the app was restarted.
            failed_at = _url_resolve_failed_at.get(url)
            if failed_at is None or time.monotonic() - failed_at < SHARE_RETRY_AFTER:
                return _url_resolve_cache[url]
    resolved = url
    last_failure = "Facebook exposed no usable single-content permalink."
    # A share token carries its answer in the body, so HEAD can never resolve
    # one - skip straight to GET and save the wasted round trip.
    attempts = ([("GET", _SHARE_UA)] if is_share
                else [("HEAD", _SHARE_UA), ("GET", _SHARE_UA)])
    # A second opinion when the phone-browser answer names no post (a login or
    # consent page, a rate limit, a network blip): Facebook's own link-preview
    # crawler. Measured 2026-11-18, it is REDIRECTED straight to the content -
    # /share/v/<t> -> /<page>/videos/<id>/?rdid=.., /share/r/<t> -> /reel/<id>
    # - so even an unreadable page body still resolves. A video ID is also its
    # reel ID, and _fb_uri_variants() adds the reel openers for it.
    attempts.append(("GET", _CRAWLER_UA))
    for method, agent in attempts:
        body = ""
        try:
            req = urllib.request.Request(
                url, method=method,
                headers={"User-Agent": agent,
                         "Accept-Language": "en-US,en;q=0.9"})
            with NOPROXY.open(req, timeout=10) as resp:
                final = resp.geturl() or ""
                if method == "GET":
                    body = _read_until_canonical(resp)
        except Exception as exc:
            last_failure = f"{method} resolution failed: {type(exc).__name__}: {exc}"
            continue
        good = _accept_resolved_url(final)
        if not good:
            for canonical in _canonical_candidates_from_html(body):
                good = _accept_resolved_url(urllib.parse.urljoin(url, canonical))
                if good:
                    break
        if good:
            resolved = good
            break
    with _url_resolve_lock:
        _url_resolve_cache[url] = resolved
        if resolved == url:
            _url_resolve_failed_at[url] = time.monotonic()
        else:
            _url_resolve_failed_at.pop(url, None)
    if resolved != url:
        print(f"  [link] resolved share/redirect -> {resolved[:80]}")
    else:
        report_error("SHARE_UNRESOLVED", url=url, detail=last_failure,
                     severity="warning")
    return resolved


def normalize_phone_url(raw):
    """One stable target URL shared by every phone in the run.

    Local cleanup handles mobile/basic hosts and trackers. Redirect resolution
    handles opaque /share/*, fb.watch and fb.me links once on the PC, before
    devices fan out. Failure is safe: the cleaned original remains usable as a
    fallback and each opener variant is still verified on the phone.
    """
    url = normalize_url(raw)
    if not url or url.startswith(("facebook://", "fb://")):
        return url
    resolved = _canonicalize_fb_url(url)
    return normalize_url(resolved) or url


def _fb_uri_variants(https_url):
    """Ordered, deduplicated openers that preserve one content target."""
    target = normalize_phone_url(https_url)
    if not target:
        return []
    native = target.startswith(("facebook://", "fb://"))
    reference = _fb_target_reference(target)
    web = _fb_native_web_url(target) if native else target
    variants = [target]
    if native:
        if not web:
            return variants
        variants.append(web)
    if reference:
        kind, token = reference
        if kind == "reel":
            variants.append(f"fb://reel/{token}")
        elif kind in ("post", "video", "photo"):
            # Video/live presentation paths sometimes miss the Lite permalink
            # alias; the same globally unique video ID also has a watch URL.
            # Keep the exact input first and verify every alternate on-device.
            if kind == "video":
                if token.isdigit():
                    # A video's ID is its reel ID, and /reel/<id> is the page
                    # Facebook itself names for any video on a phone (measured
                    # 2026-11-18) - the reel player, whose comment button the
                    # run knows how to find.
                    variants.append("https://www.facebook.com/reel/" + token)
                    variants.append(f"fb://reel/{token}")
                variants.append("https://www.facebook.com/watch?v=" + token)
            elif kind == "photo":
                variants.append("https://www.facebook.com/photo.php?fbid=" + token)
            variants.append(f"fb://post/{token}")
            if token.isdigit():
                variants.append(f"facebook://post?id={token}")
    if web:
        variants.append("fb://facewebmodal/f?href=" +
                        urllib.parse.quote(web, safe=""))
    return list(dict.fromkeys(variants))

# Facebook Lite routes web links through dedicated deep-link ALIAS activities,
# not through MainActivity. Measured on FB Lite 526 (itel A6611L):
#
#   * -n com.facebook.lite/.MainActivity  - MainActivity is the plain app entry
#     point. Handed a post URL it ignores it and opens the NEWS FEED. This is
#     exactly the "opened Facebook but not the post" failure: the link was
#     right, the activity was wrong.
#   * -p com.facebook.lite                - TWO aliases in the package claim
#     facebook.com URLs, so Android raises an "Open with" chooser listing
#     "Lite" twice. The chooser holds focus, fb_foreground() is False, the
#     variant is rejected, and open_post() force-stops before anything can
#     dismiss it.
#   * -n <permalink alias>                - opens the post itself. A reel URL
#     lands directly in the reel player, action rail and all.
#
# Named explicitly so neither trap can come back. Builds without these aliases
# make `am start` fail with "does not exist", which open_post() already treats
# as "try the next attempt", so the old openers still cover them.
FB_LINK_ALIASES = (
    "com.facebook.lite.deeplinking.activities.PermalinkPossiblePatternsActivityAlias",
    "com.facebook.deeplinking.aliasactivity.CatchAllUriAliasActivity",
)


_SHELL_SAFE_ARG = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")


def _device_shell_quote(value):
    """Quote one argument for the phone's own shell.

    `adb shell a b c` joins its arguments with spaces and hands that line to the
    phone's /system/bin/sh WITHOUT escaping anything (adb does this on purpose,
    "just like ssh"). A link such as

        https://www.facebook.com/permalink.php?story_fbid=1&id=2

    was therefore cut at the `&`: the phone ran `am start ... story_fbid=1` in
    the background and then tried to run `id=2 -f 0x14000000` as a command
    ("-f: not found"). Every https opener for permalink.php, story.php and
    photo?fbid=..&set=.. links failed that way, before Facebook Lite ever saw
    the whole link. Single quotes keep the link one argument.
    """
    text = str(value)
    if _SHELL_SAFE_ARG.fullmatch(text):
        return text
    return "'" + text.replace("'", "'\\''") + "'"


def _post_view_attempts(uri):
    data = _device_shell_quote(uri)
    is_fb_scheme = uri.startswith(("fb://", "facebook://"))
    if is_fb_scheme:
        return [
            ["shell", "am", "start", "-a", "android.intent.action.VIEW",
             "-d", data, "-p", FB_PACKAGE, "-f", "0x14000000"],
            ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", data],
        ]
    attempts = [
        ["shell", "am", "start", "-n", f"{FB_PACKAGE}/{alias}",
         "-a", "android.intent.action.VIEW", "-d", data, "-f", "0x14000000"]
        for alias in FB_LINK_ALIASES
    ]
    # Package-scoped VIEW before MainActivity: MainActivity is measured to
    # ignore the link and open the news feed, and open_post() only judges the
    # FIRST opener that starts without an error, so with MainActivity ahead of
    # it this one could never run when a build lacks both aliases. With the
    # aliases present (the normal case) neither of these is ever reached.
    attempts.append(
        ["shell", "am", "start", "--activity-single-top",
         "-a", "android.intent.action.VIEW",
         "-d", data, "-p", FB_PACKAGE, "-f", "0x14000000"])
    attempts.append(
        ["shell", "am", "start", "-n", FB_ACTIVITY,
         "-a", "android.intent.action.VIEW", "-d", data, "-f", "0x14000000"])
    return attempts

# How long one opener may take to show the post once Facebook is up. Facebook
# Lite draws its screens from the server, so on a slow or congested connection
# a cold start can need well over 6 s; the old 6 s check force-stopped the app
# just before the post arrived and started it again - over and over, which is
# what "it can't open Facebook Lite" looks like on the phone. A link that lands
# on the news feed still fails fast: the feed only gets OPEN_FEED_GRACE.
OPEN_VERIFY_SECONDS = 15.0
OPEN_FEED_GRACE = 4.0
WARM_SETTLE = 1.5

# ── Phone connectivity ─────────────────────────────────────────────────────
# Android's status bar names a connection without internet in the icon's
# description; the fleet's own dumps show "Wifi signal full.,No internet".
_NO_INTERNET_RE = re.compile(
    r'content-desc="[^"]*\b(?:no internet|walang internet)\b', re.I)
PHONE_OFFLINE_WAIT = 120.0
GNIREHTET_PACKAGE = "com.genymobile.gnirehtet"
GNIREHTET_SOCKET = "localabstract:gnirehtet"
# Set by the GUI: a callable that is True while this app's USB internet is
# sharing the PC's connection. Its tunnels live inside the ADB server and its
# VPNs belong to it, so nothing here restarts that server or stops those VPNs
# while it runs.
USB_INTERNET_ACTIVE = None


def usb_internet_active():
    hook = USB_INTERNET_ACTIVE
    if not hook:
        return False
    try:
        return bool(hook())
    except Exception:
        return False


def phone_reports_no_internet(d):
    """True when the phone's own status bar says its connection has no internet."""
    try:
        xml = d.dump_hierarchy()
    except Exception:
        return False
    return bool(xml) and bool(_NO_INTERNET_RE.search(xml))


def wait_for_phone_internet(serial, d, max_wait=None):
    """Hold off while the phone says it has no internet, instead of reopening.

    Every reopen force-stops Facebook Lite, and a phone without internet cannot
    load any post, so retrying only burns the link's attempts (and then the
    account's) while saying nothing useful. Returns True when the phone is
    online - or its status bar cannot be read - and False when it stayed
    offline for `max_wait` seconds or Abort was pressed.
    """
    if not phone_reports_no_internet(d):
        return True
    limit = PHONE_OFFLINE_WAIT if max_wait is None else float(max_wait)
    report_error("PHONE_NO_INTERNET", serial, severity="warning",
                 detail=f"Waiting up to {int(limit)}s for the connection to return.")
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if not abort_sleep(5.0):
            return False
        if not phone_reports_no_internet(d):
            print(f"    [{serial}] the phone's internet is back.")
            return True
    return False


def _usb_tunnel_target(serial):
    """The PC end of this phone's Gnirehtet tunnel ("tcp:PORT"), or None."""
    rc, out, _ = run(["reverse", "--list"], serial=serial, timeout=10)
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split()
        if GNIREHTET_SOCKET in parts:
            index = parts.index(GNIREHTET_SOCKET)
            return parts[index + 1] if index + 1 < len(parts) else "unknown"
    return None


def _local_port_answers(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except (OSError, ValueError):
        return False


def stop_stale_usb_vpn(serial):
    """Turn off a USB-internet VPN that no live relay is serving.

    Gnirehtet's VPN sends ALL of the phone's traffic down the USB tunnel. When
    the relay behind it is gone - FBVenom crashed or was killed, the PC slept,
    or the ADB server restarted and took the tunnel with it - the phone keeps
    the VPN up with nothing on the other end: Wi-Fi still looks connected but
    Facebook Lite cannot load anything, so every post "fails to open".

    Left alone: this app's own running USB internet (it manages its VPNs), and
    any tunnel whose PC port still answers (a live relay, maybe another tool).
    Returns True when a stale VPN was stopped.
    """
    if usb_internet_active():
        return False
    rc, out, _ = run(["shell", "dumpsys", "activity", "services", GNIREHTET_PACKAGE],
                     serial=serial, timeout=15)
    if rc != 0 or not re.search(r"ServiceRecord\{[^\n]*com\.genymobile\.gnirehtet/", out):
        return False
    target = _usb_tunnel_target(serial)
    if target is not None:
        match = re.fullmatch(r"tcp:(\d{1,5})", target)
        if not match or _local_port_answers(match.group(1)):
            return False
    run(["shell", "am", "start", "-a", "com.genymobile.gnirehtet.STOP",
         "-n", f"{GNIREHTET_PACKAGE}/.GnirehtetActivity"], serial=serial)
    if target is not None:
        run(["reverse", "--remove", GNIREHTET_SOCKET], serial=serial)
    report_error("USB_VPN_STALE_STOPPED", serial, severity="warning",
                 detail=("Its USB tunnel pointed at " + target + ", where nothing answers."
                         if target else "It had no USB tunnel at all."))
    return True


_open_failure_dumps = set()
_open_failure_lock = threading.Lock()


def _save_open_failure(serial, d, url):
    """Save the screen once per phone and link when a post will not open."""
    key = (str(serial), str(url))
    with _open_failure_lock:
        if key in _open_failure_dumps:
            return None
        _open_failure_dumps.add(key)
    return dump_debug(serial, d, "target_not_verified")


def open_post(serial, url, allow_warm=True, verify=None):
    # Never (re)launch Facebook once Abort has been pressed.
    if stop_requested():
        return False
    url = normalize_url(url)
    if not url:
        report_error("INVALID_URL", serial)
        return False
    if not wait_online(serial):
        if not stop_requested():
            report_error("DEVICE_UNAUTHORIZED", serial, url=url)
        return False
    variants = _fb_uri_variants(url)
    failures = []

    def _reached():
        # A variant only counts when Facebook is foreground AND - when a
        # verifier is given - the post view itself is confirmed. This is what
        # lets a link with several possible openers (numeric fb://post, or a raw
        # https for pfbid/share) skip past a variant that merely opened Facebook
        # on the feed and try the next one, instead of settling on the feed.
        if not poll_until(lambda: fb_foreground(serial), 6):
            return False
        if verify is None:
            return True
        try:
            return bool(verify())
        except Exception:
            return False

    if allow_warm and fb_foreground(serial):
        for uri in variants:
            for args in _post_view_attempts(uri):
                if stop_requested():
                    return False
                rc, out, err = run(args, serial=serial)
                low = (out + "\n" + err).strip().lower()
                if rc != 0 or any(k in low for k in
                                  ("error", "exception", "unable to resolve", "not found")):
                    failures.append(f"intent rc={rc}: {low[:240]}")
                    continue
                # The previous screen stays up for a moment after a warm intent
                # (~0.5 s measured on a TECNO KM4) and can pass the check as if
                # it were the new post. Let the new screen replace it first.
                if not abort_sleep(WARM_SETTLE):
                    return False
                if _reached():
                    print(f"  [{serial}] post opened warm ({uri[:60]})")
                    return True
                break
        print(f"  [{serial}] warm open didn't land on the post, cold-starting...")

    if stop_requested():
        return False
    run(["shell", "am", "force-stop", FB_PACKAGE], serial=serial)
    poll_until(lambda: not fb_foreground(serial), 2.0)
    run(["shell", "am", "broadcast",
         "-a", "android.intent.action.CLOSE_SYSTEM_DIALOGS"], serial=serial)
    for uri in variants:
        for args in _post_view_attempts(uri):
            if stop_requested():
                return False
            rc, out, err = run(args, serial=serial)
            msg = (out + "\n" + err).strip()
            low = msg.lower()
            if rc != 0 or any(k in low for k in
                               ("error", "exception", "unable to resolve", "not found")):
                failures.append(f"intent rc={rc}: {msg[:240]}")
                if not abort_sleep(0.4):
                    return False
                continue
            if _reached():
                print(f"  [{serial}] post opened in FB Lite ({uri[:70]})")
                return True
            print(f"  [{serial}] '{uri[:50]}' opened Facebook but not the post "
                  f"- trying next variant...")
            run(["shell", "am", "force-stop", FB_PACKAGE], serial=serial)
            poll_until(lambda: not fb_foreground(serial), 1.5)
            break
    if not stop_requested():
        detail = (failures[-1] if failures else
                  "Facebook did not reach a verified single-content view.")
        report_error("TARGET_OPEN_FAILED", serial, url=url,
                     detail=f"Tried {len(variants)} URL variants. {detail}")
    return False

def open_target_post(serial, d, url, attempts=3):
    """Open the post and CONFIRM the post itself is on screen.

    open_post() only proves Facebook is in the foreground — which is equally
    true when a share link quietly drops us on the news feed. That is how the
    run ends up scrolling the feed instead of doing its job. Here we verify the
    actual post view and cold-start the app when a warm intent failed to
    navigate, so we land on the link and get straight to the task.
    """
    last_error = ""
    for i in range(attempts):
        if stop_requested():
            return False
        warm = (i == 0)
        if warm and _shows_in_reel_player(url):
            # A reel or video opened warm while a post or reel is on screen is
            # unprovable. Measured on a TECNO KM4 (Facebook Lite 538): a warm
            # intent for reel B while reel A was open was "delivered to the
            # running instance"; A stayed on screen for ~0.5 s and verified as
            # the target, then B played with its rail hidden for 14+ s, so the
            # run reopened anyway. A cold start opened it in 4.6-6 s. Post links
            # keep the quick warm open (open_post waits WARM_SETTLE first).
            try:
                if on_post_view(d):
                    warm = False
            except Exception:
                pass
        try:
            # Verify each opener variant really reaches the post, so links whose
            # only opener is a raw https (pfbid, share) don't settle on the feed.
            # A blind dump is "cannot tell", not "wrong screen" - rejecting it
            # would force-stop a post that actually opened and loop forever.
            opened = open_post(
                serial, url, allow_warm=warm,
                verify=lambda: (on_post_view(d, timeout=OPEN_VERIFY_SECONDS,
                                             feed_grace=OPEN_FEED_GRACE)
                                or dump_is_blind(d)))
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            opened = False
        if stop_requested():
            return False
        if opened:
            try:
                if dismiss_chooser(serial, d):
                    print(f"    [{serial}] dismissed 'Open with' chooser -> Facebook Lite")
            except Exception:
                pass
            if on_post_view(d, timeout=10.0 if warm else 14.0):
                print(f"  [{serial}] target post is on screen.")
                return True
            if dump_is_blind(d):
                # Screen unreadable (SystemUI window). Accept it rather than
                # restarting forever - post_comment() re-checks on_post_view()
                # before it types, so a wrong screen still cannot be commented on.
                print(f"  [{serial}] screen unreadable - assuming the post "
                      f"opened; the comment step will re-check.")
                return True
        if stop_requested():
            return False
        # No post can load without internet, and every reopen force-stops
        # Facebook Lite again. Fix the one cause we can fix (a USB-internet VPN
        # with no relay behind it), then wait for the phone's connection.
        try:
            if stop_stale_usb_vpn(serial) and not abort_sleep(3.0):
                return False
        except Exception:
            pass
        if not wait_for_phone_internet(serial, d):
            if stop_requested():
                return False
            report_error("TARGET_NOT_VERIFIED", serial, url=url,
                         detail=f"The phone had no internet for {int(PHONE_OFFLINE_WAIT)}s, "
                                f"so the post could not load. {last_error}".strip())
            _save_open_failure(serial, d, url)
            return False
        print(f"  [{serial}] not on the target post - cold reopening "
              f"({i + 1}/{attempts})")
        run(["shell", "am", "force-stop", FB_PACKAGE], serial=serial)
        if not abort_sleep(1.0):
            return False
    report_error("TARGET_NOT_VERIFIED", serial, url=url,
                 detail=f"Target verification failed after {attempts} attempts. {last_error}")
    _save_open_failure(serial, d, url)
    return False

def chooser_present(serial, d):
    _, out, _ = run(["shell", "dumpsys", "window"], serial=serial)
    for line in out.splitlines():
        if "mCurrentFocus" in line and (
                "resolver" in line.lower() or "resolveractivity" in line.lower()):
            return True
    for sel in (dict(resourceId="android:id/button_always"),
                dict(resourceId="com.transsion.resolver:id/button_once"),
                dict(text="Open with"), dict(textContains="Open with")):
        if d(**sel).exists:
            return True
    return False

def dismiss_chooser(serial, d):
    handled = False
    for _ in range(3):
        if not chooser_present(serial, d):
            break
        handled = True
        for sel in (dict(text="Lite"), dict(textContains="Lite"),
                    dict(textContains="Facebook")):
            el = d(**sel)
            if el.exists:
                try:
                    el.click()
                    time.sleep(0.5)
                except Exception:
                    pass
                break
        clicked = False
        for sel in (dict(resourceId="android:id/button_always"),
                    dict(text="Always"),
                    dict(resourceId="com.transsion.resolver:id/button_once"),
                    dict(text="Just once")):
            el = d(**sel)
            if el.exists:
                try:
                    el.click()
                    time.sleep(2)
                    clicked = True
                    break
                except Exception:
                    pass
        if not clicked:
            try:
                d.press("enter")
            except Exception:
                pass
            time.sleep(1)
    return handled

def is_logged_in(d):
    xml = safe_dump(d)
    if not xml:
        return True
    markers = ("Mobile number or email", "Create new account", "Forgot password?")
    if "Log in" in xml and ("Password" in xml or any(m in xml for m in markers)):
        return False
    return True

def ask_reaction(prompt):
    while True:
        r = input(prompt).strip().lower()
        if r in REACTIONS:
            return r
        print(f"  choose one of: {', '.join(sorted(REACTIONS))}")

def parse_bounds(value):
    m = BOUNDS_RE.match(value or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2

def center(bounds):
    x1, y1, x2, y2 = bounds
    return (x1 + x2) // 2, (y1 + y2) // 2

class Snapshot:
    """One frozen read of the screen, reusable by every detector.

    The detectors (_find_post_like, on_post_view, find_reel_like_point) only
    ever read the hierarchy and the screen size, so they can all run against a
    single dump instead of each pulling their own ~140 ms one. Take a fresh
    Snapshot whenever the screen may have moved - never reuse one across a
    swipe or a tap.
    """
    __slots__ = ("serial", "_xml", "_size")
    _frozen = True          # tells safe_dump() not to re-validate this

    def __init__(self, dev):
        self.serial = getattr(dev, "serial", None)
        self._xml = safe_dump(dev)
        self._size = safe_window_size(dev)
        # Learn the live phone's density now (once per phone, cached by
        # serial), so the detectors reading this frozen copy size their
        # limits for the real screen instead of guessing from its width.
        px_per_dp(dev)

    def dump_hierarchy(self, *a, **k):
        return self._xml

    def window_size(self):
        return self._size

    def __bool__(self):
        return bool(self._xml)


def hierarchy_root(d):
    xml = safe_dump(d)
    if not xml:
        return None
    try:
        return ET.fromstring(xml)
    except Exception:
        return None

def clickable_items(d):
    root = hierarchy_root(d)
    if root is None:
        return []
    items = []
    for node in root.iter("node"):
        if node.attrib.get("clickable") != "true":
            continue
        b = parse_bounds(node.attrib.get("bounds"))
        if b:
            items.append((b, node.attrib))
    return items

def clickable_bounds(d):
    return [b for b, _ in clickable_items(d)]

def visible_text(d):
    xml = safe_dump(d)
    if not xml:
        return ""
    vals = []
    for attr in ("text", "content-desc"):
        vals.extend(html.unescape(m.group(1))
                    for m in re.finditer(attr + r'="([^"]*)"', xml))
    return "\n".join(v for v in vals if v)

def _find_reaction_by_label(d, reaction):
    labels = REACTION_LABELS.get(reaction, [reaction])
    root   = hierarchy_root(d)
    if root is None:
        return None
    for node in root.iter("node"):
        desc = (node.attrib.get("content-desc") or
                node.attrib.get("text") or "").lower().strip()
        for lbl in labels:
            if lbl in desc:
                b = parse_bounds(node.attrib.get("bounds"))
                if b:
                    return center(b)
    return None

def _wait_for_reaction_tray(d, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_requested():
            return False
        for reaction in REACTION_ORDER:
            if _find_reaction_by_label(d, reaction) is not None:
                return True
        root = hierarchy_root(d)
        if root is not None:
            px = _scaler(d)
            imgs = []
            for node in root.iter("node"):
                if node.attrib.get("class") != "android.widget.ImageView":
                    continue
                b = node.attrib.get("bounds", "")
                try:
                    parts = b.replace("][", ",").replace("[","").replace("]","").split(",")
                    if len(parts) != 4: continue
                    x1, y1, x2, y2 = map(int, parts)
                    w, h = x2 - x1, y2 - y1
                    if px(85) <= w <= px(130) and px(85) <= h <= px(130):
                        imgs.append(((x1 + x2) // 2, (y1 + y2) // 2))
                except Exception:
                    pass
            if len(imgs) >= 4:
                xs = sorted(x for x, _ in imgs)
                if xs[-1] - xs[0] > int(safe_window_size(d)[0] * 0.35):
                    return True
        time.sleep(0.25)
    return False

def find_action_row_like_point(d):
    width, height = safe_window_size(d)
    root = hierarchy_root(d)
    if root is not None:
        for node in root.iter("node"):
            if node.attrib.get("clickable") != "true":
                continue
            desc = (node.attrib.get("content-desc") or
                    node.attrib.get("text") or "").lower()
            if desc in ("like", "like button", "like reaction"):
                b = parse_bounds(node.attrib.get("bounds"))
                if b:
                    cx, cy = center(b)
                    if cy > int(height * 0.25):
                        print(f"    [react] found Like by label at ({cx},{cy})")
                        return cx, cy

    px = _scaler(d)
    rows = {}
    for b in clickable_bounds(d):
        x1, y1, x2, y2 = b
        w = x2 - x1
        h = y2 - y1
        if h < px(35) or h > px(160) or y1 < int(height * 0.20):
            continue
        if w < int(width * 0.10) or w > int(width * 0.50):
            continue
        key = (y1 // px(10), y2 // px(10))
        rows.setdefault(key, []).append(b)

    for _, row in sorted(rows.items(), key=lambda kv: min(b[1] for b in kv[1])):
        row = sorted(row, key=lambda b: b[0])
        if len(row) < 2:
            continue
        left  = row[0]
        right = row[-1]
        covers_width = left[0] <= int(width * 0.15) and right[2] >= int(width * 0.85)
        same_band = (max(b[1] for b in row) - min(b[1] for b in row)) <= px(30)
        if same_band and covers_width:
            left_w = left[2] - left[0]
            if left_w > int(width * 0.45):
                continue
            if left[1] > int(height * 0.85):
                continue
            cx, cy = center(left)
            print(f"    [react] found action row Like at ({cx},{cy})")
            return cx, cy

    candidates = []
    for b in clickable_bounds(d):
        x1, y1, x2, y2 = b
        w, h = x2 - x1, y2 - y1
        if h < px(35) or h > px(160):
            continue
        if w < int(width * 0.08) or w > int(width * 0.55):
            continue
        if y1 < int(height * 0.55) or y1 > int(height * 0.85):
            continue
        candidates.append(b)
    groups = {}
    for b in candidates:
        groups.setdefault((b[1] + b[3]) // 2 // px(50), []).append(b)
    for _, group in sorted(groups.items(), key=lambda kv: min(b[1] for b in kv[1])):
        if len(group) < 2:
            continue
        group.sort(key=lambda b: b[0])
        left_w = group[0][2] - group[0][0]
        if left_w > int(width * 0.45):
            continue
        if group[-1][2] < int(width * 0.85):
            continue
        cx, cy = center(group[0])
        print(f"    [react] found action row Like (relaxed) at ({cx},{cy})")
        return cx, cy

    fx, fy = int(width * 0.15), int(height * 0.68)
    print(f"    [react] using fallback Like position ({fx},{fy})")
    return fx, fy

def find_reaction_tray_points(d, like_point=None):
    width, height = safe_window_size(d)
    top_xf = (0.297, 0.500, 0.703)
    bot_xf = (0.196, 0.399, 0.601, 0.804)

    root = hierarchy_root(d)
    if root is not None:
        px = _scaler(d)
        icons = []
        for node in root.iter("node"):
            if node.attrib.get("class") != "android.widget.ImageView":
                continue
            b = parse_bounds(node.attrib.get("bounds"))
            if not b:
                continue
            bw, bh = b[2]-b[0], b[3]-b[1]
            if px(85) <= bw <= px(130) and px(85) <= bh <= px(130):
                cx, cy = center(b)
                if like_point is None or cy < like_point[1] + px(10):
                    icons.append((cx, cy))
        if icons:
            bands = {}
            for cx, cy in icons:
                bands.setdefault(round(cy / px(35)), []).append((cx, cy))
            rows = [sorted(v) for v in bands.values() if len(v) >= 3]
            rows.sort(key=lambda r: r[0][1])
            if len(rows) >= 2 and len(rows[0]) == 3 and len(rows[1]) == 4:
                return rows[0] + rows[1]
            four = next((r for r in rows if len(r) == 4), None)
            if four:
                by = sum(p[1] for p in four) // 4
                gap = int(height * 0.14)
                top = [((four[i][0] + four[i+1][0]) // 2, by - gap) for i in range(3)]
                return top + four

    if like_point is not None:
        ly = like_point[1]
        top_y = ly - int(height * 0.256)
        bot_y = ly - int(height * 0.116)
        if top_y < int(height * 0.06):
            top_y = ly + int(height * 0.116)
            bot_y = ly + int(height * 0.256)
        return ([(int(width * f), top_y) for f in top_xf] +
                [(int(width * f), bot_y) for f in bot_xf])

    base_y, row2_y = int(height * 0.48), int(height * 0.60)
    return ([(int(width * f), base_y) for f in top_xf] +
            [(int(width * f), row2_y) for f in bot_xf])

# Facebook throttles accounts that react/comment in bulk. The block screen is
# EXACTLY what a second run walks into — run 1 is what earns it — and the tool
# used to be blind to it, so it just retried and scrolled forever.
ACTION_BLOCK_MARKERS = (
    "temporarily blocked", "you're temporarily blocked", "youre temporarily blocked",
    "misusing this feature", "you can't use this feature", "cant use this feature",
    "action blocked", "we limit how often", "try again later",
    "you're going too fast", "slow down", "this feature is unavailable",
    # Tagalog / Filipino builds
    "pansamantalang naka-block", "hindi mo magamit", "subukan muli mamaya",
)


def is_action_blocked(d):
    """True when Facebook is showing a rate-limit / action-block screen."""
    try:
        text = visible_text(d).lower()
    except Exception:
        return False
    return any(m in text for m in ACTION_BLOCK_MARKERS)


# ── Pacing ────────────────────────────────────────────────────────────────
# Acting as fast as ADB allows is what earns the action block in the first
# place: a human cannot open a post, react and comment in under a second, and
# certainly not on twenty accounts back to back. These randomised pauses are
# the actual fix — the block detection below is only the safety net.
PACE_ACTION = (1.1, 2.8)     # after a reaction / a comment
PACE_LINK   = (2.5, 6.0)     # between two links on the same account
PACE_ACCOUNT = (6.0, 14.0)   # after finishing an account, before switching


# Speed profiles. Pacing is the ONLY thing standing between a run and
# Facebook's action block, so this is a real trade, not free speed:
#   safe   - human-ish timing; lowest block risk
#   fast   - about half the waiting; fine for aged accounts / short runs
#   turbo  - minimal waiting; expect action blocks on large fleets
SPEED_PROFILES = {
    "safe":  {"action": (1.1, 2.8), "link": (2.5, 6.0), "account": (6.0, 14.0)},
    "fast":  {"action": (0.6, 1.4), "link": (1.2, 3.0), "account": (3.0, 7.0)},
    "turbo": {"action": (0.25, 0.6), "link": (0.5, 1.2), "account": (1.5, 3.0)},
}
SPEED = "safe"


def set_speed(profile):
    """Switch pacing profile. Returns the name actually applied."""
    global PACE_ACTION, PACE_LINK, PACE_ACCOUNT, SPEED
    p = SPEED_PROFILES.get(profile)
    if not p:
        return SPEED
    PACE_ACTION, PACE_LINK, PACE_ACCOUNT = p["action"], p["link"], p["account"]
    SPEED = profile
    print(f"  [pace] speed profile: {profile}")
    return SPEED


def pace(span, why=""):
    """Randomised, abort-aware pause. False when Abort was pressed."""
    lo, hi = span
    secs = random.uniform(lo, hi)
    if why:
        print(f"    [pace] waiting {secs:.1f}s {why}")
    return abort_sleep(secs)


DEBUG_DIR = os.path.join(HERE, "debug")


def dump_debug(serial, d, tag):
    """Save a screenshot + UI dump when something repeatedly fails, so the
    actual screen can be inspected afterwards instead of guessed at."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe = re.sub(r'[<>:"/\\|?*]', '_', str(serial)).strip() or "device"
        base = os.path.join(DEBUG_DIR,
                            f"{safe}_{tag}_{time.strftime('%Y%m%d_%H%M%S')}")
        try:
            r = subprocess.run([ADB, "-s", serial, "exec-out", "screencap", "-p"],
                               capture_output=True, timeout=15)
            if r.returncode == 0 and r.stdout:
                with open(base + ".png", "wb") as fh:
                    fh.write(r.stdout)
        except Exception:
            pass
        try:
            xml = safe_dump(d)
            if xml:
                with open(base + ".xml", "w", encoding="utf-8") as fh:
                    fh.write(xml)
        except Exception:
            pass
        print(f"    [debug] screen saved -> {base}.png / .xml")
        return base
    except Exception:
        return None


def _has_post_action_row(d):
    """True when a normal post's Like/Comment/Share row is on screen.

    Geometry only: the real buttons expose neither content-desc nor text, so a
    label lookup never matches and would just cost another full UI dump.
    """
    try:
        return _find_post_like(d) is not None
    except Exception:
        return False


def _rail_is_uniform(stack, width_tol=0.30, gap_tol=0.12):
    """True when a column looks MANUFACTURED, like the reel player's rail.

    The rail is a fixed control strip: same-width buttons at IDENTICAL
    spacing. Measured on two different phones the gaps are exact - 144/144/144
    on the itel (720x1612), 126/126/126 on the TECNO (720x1600) - so the
    tolerance can be tight, and it has to be.

    At 35% this also accepted Facebook Lite's NOTIFICATIONS tab, whose
    right-edge column is four 95x95 cells with gaps of 224/178/223 (20%
    variance). That made the notifications screen pass as "the target post":
    on_post_view() returned True and the run would have commented there.
    Comment "..." menus and ordinary list rows fail the same way - their gaps
    follow the content height above them, which varies.
    """
    if len(stack) < 3:
        return False
    widths = [b[2] - b[0] for b in stack]
    if min(widths) <= 0 or (max(widths) - min(widths)) / max(widths) > width_tol:
        return False
    gaps = [stack[i + 1][1] - stack[i][1] for i in range(len(stack) - 1)]
    if not gaps or min(gaps) <= 0:
        return False
    return (max(gaps) - min(gaps)) / max(gaps) <= gap_tol


def _reel_action_stack(d):
    width, height = safe_window_size(d)
    # Sizes measured on the 720-px fleet (rail buttons ~108x128 there); px()
    # carries them to this screen - 148x176 on a 1080-px, density-440 phone.
    px = _scaler(d)
    col = []
    for b in clickable_bounds(d):
        x1, y1, x2, y2 = b
        w = x2 - x1
        h = y2 - y1
        cx = (x1 + x2) // 2
        if cx < int(width * 0.82):
            continue
        if w < px(35) or w > px(140) or h < px(40) or h > px(175):
            continue
        if y1 < int(height * 0.42):
            continue
        col.append(b)
    if len(col) < 2:
        return None
    col.sort(key=lambda b: b[1])
    stack = [col[0]]
    for b in col[1:]:
        if b[1] - stack[-1][1] >= px(60):
            stack.append(b)
    if len(stack) < 3:
        return None
    if stack[-1][1] - stack[0][1] < px(120):
        return None
    if center(stack[0])[1] > int(height * 0.88):
        return None
    # A post's comment list puts a "..." button at the right edge of every
    # comment, and three of those stacked look exactly like a rail. When a real
    # post action row is on screen, believe the ROW - unless the URL says this
    # is a reel AND the column has the manufactured look of a genuine rail.
    #
    # Both halves are needed. Without the reel override, the reel player's own
    # author row (square ~96x96 avatar + name + Follow, one band hugging the
    # left edge) trips _find_post_like and vetoes the very rail this exists to
    # find - that is what made reels unreachable. Without the uniformity test,
    # the override fires on ANY screen while a reel link is loaded, so a phone
    # sitting on the feed reports "on the post", and the tap goes into whatever
    # is under those coordinates - Marketplace, someone else's post, anywhere.
    # Uniformity is not merely for the override below - it is what makes this
    # a REEL RAIL instead of "four clickables down the right edge". Notification
    # rows, comment "..." menus and list items all make that shape; none of them
    # are evenly spaced.
    if not _rail_is_uniform(stack):
        return None
    # A reel's comment SHEET lists every comment with a small Like button down
    # the right side, and four one-line comments space those as evenly as the
    # rail. Measured on 6 real sheets: those buttons end at x=702-704 with the
    # comment box open, while the real rail runs to the screen edge (x=720 on
    # all 64 real reel-player screens, which never show a comment box). Without
    # this, a sheet passed as the reel player and "Like" or "Comment" landed on
    # somebody's comment. A sheet whose comments are switched off has no box,
    # but it also hides the player's progress bar (a SeekBar on 47 of the 64
    # real player screens; every one of the 64 reaches the edge anyway).
    if any(b[2] < width - px(8) for b in stack):
        if (_fb_comment_box(d) is not None
                or "android.widget.SeekBar" not in (safe_dump(d) or "")):
            return None
    if _has_post_action_row(d) and not _is_reel_context():
        return None
    return [center(b) for b in stack]

def find_reel_like_point(d):
    stack = _reel_action_stack(d)
    return stack[0] if stack else None

def find_reel_tray_points(d):
    """The 7-reaction tray raised by long-pressing a reel's Like button.

    HARD VERTICAL FLOOR. The tray pops up beside the action rail, in the lower
    part of the screen - never at the top. Facebook Lite's TOP NAVIGATION BAR
    is a row of full-width-spanning cells of almost exactly the tray's shape:
    measured on this reel, six cells at (0,67)-(720,173), each 120 wide and 106
    tall, which sits inside every width/height band below and spans edge to
    edge. Only `len(row) >= 7` kept them apart, and the nav has six - a
    one-element margin. Any badge or extra clickable landing in that band makes
    the reaction picker aim at the nav bar instead, and its rightmost cell is
    MARKETPLACE. That is how a react could navigate the phone clean out of the
    post.
    """
    width, height = safe_window_size(d)
    px = _scaler(d)
    floor = int(height * 0.30)
    rows = {}
    for b in clickable_bounds(d):
        x1, y1, x2, y2 = b
        w = x2 - x1
        h = y2 - y1
        if y1 < floor:                       # never the top nav bar
            continue
        if w < int(width * 0.08) or w > int(width * 0.20):
            continue
        if h < px(55) or h > px(145):
            continue
        rows.setdefault(y1 // px(14), []).append(b)
    for _, row in sorted(rows.items()):
        row = sorted(row, key=lambda b: b[0])
        if len(row) >= 7:
            cells = row[:7]
            if cells[0][0] < int(width * 0.12) and cells[-1][2] > int(width * 0.86):
                return [center(b) for b in cells]
    return None

def _is_reel_context():
    """True when this thread's link opens in the reel player.

    Reels AND videos: Facebook Lite plays every video there (see
    _target_kind). Only /reel/ and /share/r/ used to count, so a /share/v/,
    fb.watch, /watch?v= or /<page>/videos/ link that opened in the reel player
    was judged "not the target post" - the reel's author row looks like a
    post's action row - and never got its comment (19 real fleet screens show
    exactly that refusal on an open reel).
    """
    return _shows_in_reel_player(current_post_url() or "")

def _wait_reel_like(d, timeout=6.0):
    pt = find_reel_like_point(d)
    if pt is not None:
        return pt
    w, h = safe_window_size(d)
    deadline = time.time() + timeout
    revealed = False
    while time.time() < deadline:
        if not revealed:
            try:
                if _fb_comment_box(d) is not None:
                    # A comment sheet covers the rail. Tapping the middle would
                    # land on someone's comment; Back closes the sheet instead.
                    d.press("back")
                else:
                    d.click(int(w * 0.5), int(h * 0.5))
            except Exception:
                pass
            revealed = True
            time.sleep(0.8)
        else:
            time.sleep(0.4)
        pt = find_reel_like_point(d)
        if pt is not None:
            return pt
    return None

def _find_post_like(d):
    """Like button = leftmost of the post's action row.

    Width limits measured from real Facebook Lite screens: this build renders
    the row as pills of ~0.14-0.20 of the screen width ([Like 453][Comment 33]
    [Share 128] plus the reaction icons), NOT the ~0.30 wide thirds the older
    build used. The previous 0.22 floor rejected every button on this layout,
    so the row was never found and the run just scrolled forever.

    Comment rows look similar but start indented (x ~0.25 of the width), so
    anchoring the row to the left edge is what separates them.
    """
    width, height = safe_window_size(d)
    px = _scaler(d)
    rows = {}
    for b in clickable_bounds(d):
        x1, y1, x2, y2 = b
        bw, bh = x2 - x1, y2 - y1
        if bh < px(40) or bh > px(140):
            continue
        if bw < int(width * 0.12) or bw > int(width * 0.42):
            continue
        rows.setdefault(round((y1 + y2) / 2 / px(26)), []).append(b)
    for _, row in sorted(rows.items()):
        row = sorted(row, key=lambda b: b[0])
        if len(row) < 3:
            continue
        if row[0][0] > int(width * 0.12):               # must hug the left edge
            continue                                     # (comment rows indent)
        if row[-1][2] < int(width * 0.55):              # and reach across
            continue
        # Two lookalikes live outside the post body and must be excluded by
        # position, not by spacing (the older build tiles its action row flush,
        # exactly like the feed's tab bar):
        #   * the feed's tab bar   - pinned ~14% down from the top
        #   * Android's nav bar    - Overview/Home/Back, three 0.19-wide
        #                            buttons at ~98% down, present on EVERY
        #                            screen, which is what made the news feed
        #                            look like a reactable post
        cy = center(row[0])[1]
        if cy < int(height * 0.20) or cy > int(height * 0.92):
            continue
        return center(row[0])
    return None

def _scroll_to_post_like(d, max_scrolls=12):
    # A long caption, a tall photo or a stack of images pushes the like/comment
    # row well below the fold, so keep looking for it — but re-check on every
    # scroll that we are STILL on the target post. That keeps it adaptive for
    # big posts while never letting it wander into the feed and act on the
    # wrong thing. Bails out immediately on Abort.
    # This mirrors the original, proven algorithm and must not be "improved":
    #
    #  * Facebook Lite only renders the action row into the accessibility tree
    #    when it is near the MIDDLE of the screen, so the row is found by
    #    scrolling DOWN in small steps and polling after each one.
    #  * Never drag downwards from near the top of the screen to "go back up":
    #    that is Facebook's pull-to-refresh gesture. It reloads the post, the
    #    row never settles, and the run scrolls forever without reacting.
    #  * Detection is geometric only. The buttons carry no content-desc and no
    #    text in the real a11y tree, so a label lookup can never match and only
    #    costs an extra full UI dump per poll.
    width, height = safe_window_size(d)
    for _ in range(3):                      # let the post finish rendering
        if stop_requested():
            return None
        pt = _find_post_like(d)
        if pt:
            return pt
        if not abort_sleep(0.7):
            return None

    off_target = 0
    for i in range(max_scrolls):
        if stop_requested():
            return None
        # a dump can come back empty for a frame mid-scroll; only give up on
        # two consecutive misses, never on a single bad read
        # ONE dump answers both questions: is the action row already on
        # screen, and are we still on the target post? Asking them separately
        # meant two ~140 ms dumps of the very same frame. Checking for the row
        # BEFORE swiping also means we stop the moment it appears.
        snap = Snapshot(d)
        pt = _find_post_like(snap)
        if pt:
            if i:
                print(f"    [react] found action row after {i} scroll(s)")
            return pt
        if on_post_view(snap):
            off_target = 0
        else:
            off_target += 1
            if off_target >= 2:
                print("    [react] left the target post while scrolling - stopping")
                return None
        try:
            d.swipe(width // 2, int(height * 0.72),
                    width // 2, int(height * 0.46), 0.22)
        except Exception:
            pass
        for _ in range(2):                  # poll as the row settles near centre
            if not abort_sleep(0.7):
                return None
            pt = _find_post_like(d)
            if pt:
                print(f"    [react] found action row after {i + 1} scroll(s)")
                return pt
    return None

def _post_surface(d):
    if _find_post_like(d) is not None:
        return True
    try:
        if d(className="android.widget.EditText").exists:
            return True
    except Exception:
        pass
    return False

def _react_reel(d, reaction, reel_like):
    idx = REACTION_ORDER.index(reaction)
    for attempt in range(2):
        if stop_requested():
            return "stopped"
        try:
            d.long_click(reel_like[0], reel_like[1], 1.1 if attempt == 0 else 1.6)
        except Exception as e:
            return f"react-failed:longpress:{e}"
        pt, points = None, None
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if stop_requested():
                return "stopped"
            pt = _find_reaction_by_label(d, reaction)
            if pt:
                break
            points = find_reel_tray_points(d)
            if points:
                break
            if not abort_sleep(0.25):
                return "stopped"
        if pt:
            try:
                d.click(*pt); time.sleep(1); return f"reacted:{reaction}"
            except Exception as e:
                return f"react-failed:tap:{e}"
        if points and idx < len(points):
            time.sleep(0.45)
            stable = find_reel_tray_points(d) or points
            if idx < len(stable):
                points = stable
            slot = (len(points) - 1) - idx
            try:
                d.click(*points[slot]); time.sleep(1)
                return f"reacted:{reaction}"
            except Exception as e:
                return f"react-failed:tap:{e}"
        try:
            d.press("back")
        except Exception:
            pass
        time.sleep(0.6)
    if reaction == "like":
        try:
            d.click(*reel_like); time.sleep(1); return "reacted:like"
        except Exception as e:
            return f"react-failed:tap:{e}"
    return f"react-failed:no-{reaction}-tray"

def react(d, reaction):
    if stop_requested():
        return "stopped"
    # Rate-limited? Retrying just burns 200 swipes against a wall.
    if is_action_blocked(d):
        print("    [react] Facebook has action-blocked this account")
        return "action-blocked"
    # Only ever react to the post we were told to react to. If we are not on a
    # post view (e.g. the deep link dumped us on the feed), refuse and let the
    # caller re-open the target URL rather than reacting to a random post.
    if not on_post_view(d, timeout=5.0):
        if stop_requested():
            return "stopped"
        if is_action_blocked(d):
            print("    [react] Facebook has action-blocked this account")
            return "action-blocked"
        print("    [react] not on the target post - refusing to react here")
        return "react-failed:not-on-target-post"
    idx = REACTION_ORDER.index(reaction)
    # Only consider the reel player when the target actually IS a reel, or when
    # the screen has no post action row at all.
    #
    # A reel is detected as "3+ small clickables stacked on the right edge" —
    # which is also exactly what a column of comment "..." menu buttons looks
    # like. Every run adds another comment, so once a post had 3+ comments on
    # screen this fired on every attempt and long-pressed a comment's overflow
    # menu instead of Like. That is why it worked the first time and never
    # again.
    reel_like = None
    if _is_reel_context():
        reel_like = find_reel_like_point(d)
        if reel_like is None and not _post_surface(d):
            reel_like = _wait_reel_like(d, 5.0)
    elif not _has_post_action_row(d):
        reel_like = find_reel_like_point(d)
    if reel_like is not None:
        return _react_reel(d, reaction, reel_like)

    like = _scroll_to_post_like(d)
    if stop_requested():
        return "stopped"

    if like is None:
        w, h = safe_window_size(d)
        media = None
        for b in clickable_bounds(d):
            cy = center(b)[1]
            if (b[2] - b[0]) >= int(w * 0.85) and int(h * 0.28) < cy < int(h * 0.82) \
                    and (b[3] - b[1]) >= int(h * 0.12):
                if media is None or (b[3] - b[1]) > (media[3] - media[1]):
                    media = b
        rlike = None
        if media is not None:
            mx = (media[0] + media[2]) // 2
            my = media[3] - min(int((media[3] - media[1]) * 0.30), int(h * 0.14))
            try:
                d.click(mx, my); time.sleep(3.2)
            except Exception:
                pass
            rlike = find_reel_like_point(d) or _wait_reel_like(d, 3.5)
        if rlike is not None:
            time.sleep(1.0)
            try:
                d.long_click(rlike[0], rlike[1], 1.3)
            except Exception:
                return "react-failed:no-action-row"
            pts = None
            deadline = time.time() + 3.5
            while time.time() < deadline:
                pts = find_reel_tray_points(d)
                if pts:
                    break
                time.sleep(0.3)
            if pts and idx < len(pts):
                time.sleep(0.4)
                pts = find_reel_tray_points(d) or pts
                slot = (len(pts) - 1) - idx
                try:
                    d.click(*pts[slot]); time.sleep(1); return f"reacted:{reaction}"
                except Exception:
                    pass
            if reaction == "like":
                try:
                    d.click(*rlike); time.sleep(1); return "reacted:like"
                except Exception:
                    pass
        return "react-failed:no-action-row"
    x, y = like

    for attempt in range(2):
        if stop_requested():
            return "stopped"
        try:
            d.long_click(x, y, 1.5 if attempt == 0 else 1.7)
        except Exception as e:
            return f"react-failed:longpress:{e}"
        tray_found = _wait_for_reaction_tray(d, timeout=5.0)
        if tray_found:
            break
        if stop_requested():
            return "stopped"
        print(f"    [react] tray not found, re-finding Like + retrying...")
        try:
            d.press("back")
        except Exception:
            pass
        if not abort_sleep(0.8):
            return "stopped"
        relike = _scroll_to_post_like(d)
        if relike:
            like = relike
            x, y = like

    if stop_requested():
        return "stopped"
    pt = _find_reaction_by_label(d, reaction)
    if pt:
        try:
            d.click(*pt); time.sleep(0.8); return f"reacted:{reaction}"
        except Exception as e:
            return f"react-failed:tap:{e}"
    points = find_reaction_tray_points(d, like_point=like)
    if idx < len(points):
        try:
            d.click(*points[idx]); time.sleep(0.8); return f"reacted:{reaction}"
        except Exception as e:
            return f"react-failed:tap:{e}"
    if reaction == "like":
        try:
            d.click(x, y); time.sleep(1.2); return "reacted:like"
        except Exception as e:
            return f"react-failed:direct-tap{e}"
    return f"react-failed:no-{reaction}-point"

def normalize_account_name(value):
    value = html.unescape(value or "").strip()
    value = re.sub(r",?\s*\d+\+?\s+notifications?\b", "", value, flags=re.I).strip()
    value = re.sub(r"\s+", " ", value)
    return value

def is_password_prompt(d):
    text    = visible_text(d).lower()
    markers = ("password", "enter your password", "forgot password", "incorrect password")
    return (any(m in text for m in markers) and
            ("log in" in text or "login" in text or "continue" in text))

def is_suspended(d):
    text = visible_text(d).lower()
    return any(marker in text for marker in SUSPENDED_MARKERS) or is_suspended_layout(d)

def is_suspended_layout(d):
    width, height = safe_window_size(d)
    px = _scaler(d)
    items = clickable_items(d)
    if not (3 <= len(items) <= 6):
        return False
    points = [(b, center(b)) for b, _ in items]
    has_top_right = any(
        x > int(width * 0.85) and int(height * 0.04) <= y <= int(height * 0.12)
        and px(40) <= b[2]-b[0] <= px(90) and px(40) <= b[3]-b[1] <= px(90)
        for b, (x, y) in points)
    has_profile = any(
        int(width*0.40) <= x <= int(width*0.60) and
        int(height*0.10) <= y <= int(height*0.22) and
        px(70) <= b[2]-b[0] <= px(130) and px(70) <= b[3]-b[1] <= px(130)
        for b, (x, y) in points)
    has_appeal_button = any(
        int(width*0.03) <= b[0] <= int(width*0.08) and
        b[2] >= int(width*0.92) and
        int(height*0.35) <= y <= int(height*0.48) and
        px(45) <= b[3]-b[1] <= px(90)
        for b, (x, y) in points)
    return has_top_right and has_profile and has_appeal_button

def wait_for_account_block(d, timeout=6):
    """Watch for a suspended / logged-out / password-prompt screen.

    Three things mattered here, all measured on a real run where this was the
    single most expensive step (7.4s per call, twice per account switch):

      * the three checks each pulled their OWN dump, so one iteration cost
        three round trips - they now share one Snapshot;
      * it slept with time.sleep(), so Abort could not interrupt it;
      * a healthy account never returns early, so it always burned the whole
        timeout. That is inherent to waiting for a bad screen to appear, so
        callers that are about to read the UI anyway pass a short timeout.
    """
    deadline = time.time() + timeout
    pending = None
    while True:
        snap = d if getattr(d, "_frozen", False) else Snapshot(d)
        if is_suspended(snap):
            found = "suspended"
        elif is_password_prompt(snap) or not is_logged_in(snap):
            found = "not-successful"
        else:
            found = None
        if found:
            # A Facebook that is still loading briefly looks logged out, and
            # the old code believed the first reading - marking a healthy
            # account blocked. Require the same verdict twice in a row. With
            # timeout=0 there is no second look, so report it as before.
            if timeout <= 0 or pending == found:
                return found
            pending = found
        else:
            pending = None
        if time.time() >= deadline:
            return None
        if not abort_sleep(min(0.6, max(0.05, deadline - time.time()))):
            return None

def _go_to_home_tab(serial, d, attempts=3):
    """Put Facebook on the FEED, not wherever it happened to resume.

    A force-stop + MainActivity relaunch does NOT reliably land on the feed:
    on FB Lite 527 it comes back on the REELS tab, showing some unrelated
    reel. That matters because the reels player carries the same right-edge
    rail as a target reel, so on_post_view() answers True and
    classify_screen() says "post" - the tool believes it is on the target post
    while sitting on a stranger's reel. The switcher hunt then runs from that
    screen and taps the top-right nav cell, which on this build is the Creator
    dashboard, and the next attempt force-restarts back onto Reels. That is
    the reels -> dashboard -> exit -> reels cycle.

    The nav row's LEFTMOST cell is Home on every build seen here.
    """
    width, height = safe_window_size(d)
    for _ in range(attempts):
        snap = Snapshot(d)
        if find_reel_like_point(snap) is None:
            return True                       # not on a reel player: good
        top = [b for b, _ in clickable_items(snap) if b[1] < int(height * 0.12)]
        if not top:
            return False
        home = min(top, key=lambda b: b[0])
        dbg(serial, "home-tab", f"landed on a reel - tapping Home {center(home)}")
        d.click(*center(home))
        if not abort_sleep(1.5):
            return False
    return find_reel_like_point(Snapshot(d)) is None


def open_fb_home(serial, d, force=True):
    # Never relaunch Facebook after Abort — this function restarts the app and
    # taps around the home/search chrome, which is exactly the "it reopened and
    # started doing things on its own" behaviour.
    if stop_requested():
        return
    if force:
        run(["shell", "am", "force-stop", FB_PACKAGE], serial=serial)
        poll_until(lambda: not fb_foreground(serial), 1.5)
    if stop_requested():
        return
    run(["shell", "am", "start", "-n", FB_ACTIVITY], serial=serial)
    poll_until(lambda: fb_foreground(serial), 4)
    # Foreground is not the same as focused: for a few seconds after a cold
    # start nothing holds input focus and every dump comes back as SystemUI, so
    # the menu/switcher detectors below would search an empty screen.
    poll_until(lambda: fb_has_focus(serial), 8)
    if chooser_present(serial, d):
        dismiss_chooser(serial, d)
        poll_until(lambda: not chooser_present(serial, d), 2)
    # Relaunching can resume on the Reels tab; get onto the feed before any
    # menu/switcher detector runs, or they search the wrong screen.
    _go_to_home_tab(serial, d)
    width, height = safe_window_size(d)
    for _ in range(4):
        if stop_requested():
            return
        text = visible_text(d)
        if not any(marker in text for marker in ("Search...", "Search Facebook")):
            break
        d.click(int(width * 0.058), int(height * 0.074))
        if not abort_sleep(1.0):
            return

def find_gear_icon(d):
    width, height = safe_window_size(d)
    px = _scaler(d)
    candidates = []
    for b, attrs in clickable_items(d):
        cx, cy = center(b)
        if cy > int(height * 0.12) or cx < int(width * 0.80):
            continue
        w, h = b[2]-b[0], b[3]-b[1]
        if px(40) <= w <= px(100) and px(40) <= h <= px(100):
            candidates.append((cx, b))
    if not candidates:
        return None
    candidates.sort(key=lambda c: -c[0])
    return center(candidates[0][1])

def wait_for_gear_icon(d, timeout=15):
    deadline  = time.time() + timeout
    last_gear = None
    while time.time() < deadline:
        gear = find_gear_icon(d)
        if gear is not None:
            last_gear = gear
        if gear is not None and (is_suspended(d) or is_suspended_layout(d)):
            return gear
        time.sleep(1)
    return last_gear if (is_suspended(d) or is_suspended_layout(d)) else None

def open_gear_menu(serial, d):
    open_fb_home(serial, d)
    gear = wait_for_gear_icon(d)
    if gear is None:
        return False
    d.click(*gear)
    time.sleep(2)
    return True

def find_logout_row(d):
    for b, attrs in clickable_items(d):
        text = normalize_account_name(attrs.get("content-desc") or attrs.get("text"))
        low  = text.lower()
        if "log out" in low or "logout" in low:
            return center(b)
    width, height = safe_window_size(d)
    px = _scaler(d)
    sheet_rows = []
    for b, _ in clickable_items(d):
        w  = b[2]-b[0]
        h  = b[3]-b[1]
        cx, cy = center(b)
        if (b[0] <= int(width*0.08) and b[2] >= int(width*0.90) and
                cy > int(height*0.70) and w > int(width*0.75) and h >= px(50)):
            sheet_rows.append((cy, b))
    if len(sheet_rows) >= 2:
        return center(sorted(sheet_rows)[-1][1])
    return None

def confirm_logout_if_needed(d):
    text = visible_text(d).lower()
    if "log out?" in text or "are you sure you want to log out" in text:
        for b, attrs in clickable_items(d):
            label = normalize_account_name(
                attrs.get("content-desc") or attrs.get("text")).lower()
            if label in ("yes", "log out", "logout"):
                d.click(*center(b))
                time.sleep(3)
                return True
    width, height = safe_window_size(d)
    px = _scaler(d)
    buttons = []
    for b, _ in clickable_items(d):
        w  = b[2]-b[0]
        h  = b[3]-b[1]
        cx, cy = center(b)
        if (int(width*0.15) <= cx <= int(width*0.85) and
                int(height*0.52) <= cy <= int(height*0.62) and
                px(150) <= w <= px(260) and px(45) <= h <= px(90)):
            buttons.append((cx, b))
    if len(buttons) >= 2:
        d.click(*center(sorted(buttons)[0][1]))
        time.sleep(3)
        return True
    return False

def logout_current_account(serial, d):
    if not open_gear_menu(serial, d):
        return False
    found = False
    for _ in range(6):
        row = find_logout_row(d)
        if row:
            d.click(*row)
            time.sleep(2)
            found = True
            break
        d.swipe_ext("up", scale=0.6)
        time.sleep(1)
    if not found:
        return False
    confirm_logout_if_needed(d)
    time.sleep(3)
    return True

def login_screen_account_rows(d):
    width, height = safe_window_size(d)
    px = _scaler(d)
    rows = []
    for b, attrs in clickable_items(d):
        desc = normalize_account_name(attrs.get("content-desc") or attrs.get("text"))
        if not desc:
            continue
        low = desc.lower()
        if any(skip in low for skip in ACCOUNT_SKIP_TEXT):
            continue
        w, h = b[2]-b[0], b[3]-b[1]
        if w < int(width * 0.50) or h < px(60):
            continue
        rows.append({"name": desc, "bounds": b, "point": center(b)})
    deduped, seen = [], set()
    for row in sorted(rows, key=lambda r: (r["bounds"][1], r["bounds"][0])):
        key = row["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped

def return_to_login_chooser(d):
    for _ in range(4):
        if login_screen_account_rows(d):
            return True
        try:
            d.press("back")
        except Exception:
            pass
        time.sleep(1)
    return bool(login_screen_account_rows(d))

def find_switch_account_arrow(d):
    """The chevron beside the profile row ON THE MENU SCREEN.

    NEVER look for it on a post or reel. The shape test below - any 35-90px
    square on the right of the upper third - also matches the reel player's
    search / camera / profile icons, measured at (461,207) (552,207) (643,207)
    on this build. It picked the CAMERA, open_account_switcher() clicked it,
    and the phone went somewhere new; the next attempt force-restarted
    Facebook and repeated the same tap. That is the reels -> dashboard -> exit
    -> reels cycle that ran for a whole session after the first account.
    """
    width, height = safe_window_size(d)
    try:
        if on_post_view(d, timeout=0.0):
            return None
    except Exception:
        pass
    px = _scaler(d)
    arrows = []
    for b, _ in clickable_items(d):
        x1, y1, x2, y2 = b
        w  = x2-x1
        h  = y2-y1
        cx, cy = center(b)
        if cx < int(width*0.78) or cy < int(height*0.12) or cy > int(height*0.34):
            continue
        if px(35) <= w <= px(90) and px(35) <= h <= px(90):
            arrows.append((cy, cx, b))
    if not arrows:
        return None
    arrows.sort()
    # Current Facebook Lite menus show a SINGLE chevron next to the profile row.
    # Older layouts had two icons in this band and the switcher was the second
    # one, so keep that pick when there are several — but never give up just
    # because there is only one, which used to abort account switching entirely
    # (the run then processed one account and stopped).
    return center(arrows[1][2] if len(arrows) >= 2 else arrows[0][2])

_MENU_LABELS = ("menu", "see more", "more options", "more", "settings menu")


# ── Label-less builds ────────────────────────────────────────────────────────
# Some Facebook builds expose NO text and NO content-desc anywhere in the
# accessibility tree - verified on CPH2631, where even a raw `uiautomator dump`
# returns bounds only. Reacting still works there because that was always
# geometric, but every account-switching path was label-driven, so those phones
# silently did ONE account and stopped. These helpers recover the same screens
# from shape alone, and are only consulted when the label path finds nothing.

# Once a phone has shown real account NAMES, never fall back to positional
# identities on it again. Mixing the two corrupts used_accounts: the same
# account appears as "Hazel Matias" one round and "#2" the next, so the run
# re-queues switches that actually happened and can revisit or skip accounts.
_labels_seen = set()
_labels_lock = threading.Lock()


def note_labels_seen(serial):
    if serial:
        with _labels_lock:
            _labels_seen.add(serial)


def labels_known(serial):
    if not serial:
        return False
    with _labels_lock:
        return serial in _labels_seen


def screen_is_on(serial):
    """False when the display is off. A sleeping phone still answers dumps,
    and a black screen produced two plausible-looking 'account rows' - so the
    positional fallback must never run on one."""
    if not serial:
        return True
    try:
        _, out, _ = run(["shell", "dumpsys", "display"], serial=serial, timeout=15)
    except Exception:
        return True
    return "mScreenState=OFF" not in out


def dump_has_labels(d):
    """True when the app's own text is visible in the tree.

    SystemUI (status bar, nav bar) always carries labels, so its window is
    excluded - otherwise a label-less Facebook still looks labelled.
    """
    try:
        xml = safe_dump(d)
    except Exception:
        return True                     # unknown: assume the normal path
    if not xml:
        return True
    for m in re.finditer(r'<node\b[^>]*>', xml):
        tag = m.group(0)
        if "com.android.systemui" in tag:
            continue
        tm = re.search(r'\btext="([^"]*)"', tag)
        dm = re.search(r'\bcontent-desc="([^"]*)"', tag)
        if (tm and tm.group(1).strip()) or (dm and dm.group(1).strip()):
            return True
    return False


def _uniform_stack(d, min_rows=2, min_w=0.50):
    """Largest run of same-width, same-height, vertically stacked rows.

    That shape is the account list in the switcher sheet: every row shares one
    x-range, one height, and sits flush against the next.
    """
    width, height = safe_window_size(d)
    px = _scaler(d)
    rows = []
    for b, _ in clickable_items(d):
        w, h = b[2] - b[0], b[3] - b[1]
        if w < int(width * min_w) or h < px(60) or h > px(260):
            continue
        if b[1] < int(height * 0.10):
            continue
        rows.append(b)
    groups = {}
    for b in rows:
        key = (b[0] // 8, b[2] // 8, (b[3] - b[1]) // 8)
        groups.setdefault(key, []).append(b)
    if not groups:
        return []
    best = max(groups.values(), key=len)
    if len(best) < min_rows:
        return []
    best.sort(key=lambda b: b[1])
    # Rows of a real list TOUCH each other. Cards on the Menu screen are the
    # same width and height but sit apart, so without this the Menu reads as an
    # account list and the switcher reports "open" while still on the Menu.
    # The contiguous run is the answer or there is no answer - never fall back
    # to the loose group, which is what defeated this check before.
    best_run, run = [], [best[0]]
    for b in best[1:]:
        if abs(b[1] - run[-1][3]) <= max(px(12), (b[3] - b[1]) // 6):
            run.append(b)
        else:
            if len(run) > len(best_run):
                best_run = run
            run = [b]
    if len(run) > len(best_run):
        best_run = run
    return best_run if len(best_run) >= min_rows else []


def find_menu_button_geometric(d):
    """The hamburger, by shape: right-most small button on the top bar row."""
    width, height = safe_window_size(d)
    top = [b for b, _ in clickable_items(d)
           if b[1] < int(height * 0.12) and (b[2] - b[0]) < int(width * 0.25)]
    if not top:
        return None
    # Band by the TOPMOST row only. The tab strip directly underneath the app
    # bar is also small and also touches the right edge, so a plain "rightmost
    # near the top" picks Marketplace instead of the hamburger.
    top_y = min(b[1] for b in top)
    band = [b for b in top if b[1] - top_y <= _scaler(d)(40)]
    right = max(band, key=lambda b: b[2])
    if right[2] < int(width * 0.80):     # must actually hug the right edge
        return None
    return center(right)


def find_switch_account_card(d):
    """The 'Switch account' row on the Menu screen, by shape.

    The Menu is a column of full-width cards followed by a two-column grid.
    The first full-width card is the profile ("View your profile"), the second
    is "Switch account" - verified against the real screen.
    """
    width, height = safe_window_size(d)
    wide = []
    for b, _ in clickable_items(d):
        w = b[2] - b[0]
        if w < int(width * 0.85):
            continue
        if b[1] < int(height * 0.06) or b[1] > int(height * 0.55):
            continue
        wide.append(b)
    if len(wide) < 2:
        return None
    wide.sort(key=lambda b: b[1])
    # drop nested duplicates: keep one card per y-band
    band = _scaler(d)(40)
    picked = []
    for b in wide:
        if picked and b[1] - picked[-1][1] < band:
            continue
        picked.append(b)
    if len(picked) < 2:
        return None
    return center(picked[1])            # [0] is the profile card


def find_menu_button(d):
    """The hamburger / menu entry, by label. Falls back to the top-right
    corner tap that the old code hard-coded, which broke whenever the bar
    layout shifted."""
    width, height = safe_window_size(d)
    for b, attrs in clickable_items(d):
        desc = (attrs.get("content-desc") or attrs.get("text") or "").strip().lower()
        if not desc:
            continue
        if desc in _MENU_LABELS:
            cx, cy = center(b)
            if cy < int(height * 0.22):
                return cx, cy
    return find_menu_button_geometric(d)


def current_account_name(d):
    """Name of the account the switcher marks as current (its top row)."""
    rows = account_rows(d)
    return rows[0]["name"] if rows else None


def settled_account_rows(d, timeout=7.0):
    """Account rows, once the sheet has stopped animating.

    A sheet mid-slide renders a partial, still-unlabelled list; reading it the
    instant it becomes non-empty yields 2 nameless rows on a phone that really
    has 9 named ones. Wait for two consecutive identical reads instead.
    """
    deadline = time.time() + timeout
    prev = None
    while time.time() < deadline:
        if stop_requested():
            return []
        rows = account_rows(d)
        if rows:
            sig = [r["name"] for r in rows]
            if sig == prev:
                return rows
            prev = sig
        else:
            prev = None
        if not abort_sleep(0.35):
            return []
    return account_rows(d)


def _back_out_to_facebook(serial, d, presses=3):
    """Undo stray navigation with Back, without leaving Facebook.

    Stops as soon as Facebook is no longer the focused package, so Back never
    walks the phone out to the launcher - which would only add a cold start to
    the next attempt.
    """
    for _ in range(presses):
        try:
            if foreground_package(serial) != FB_PACKAGE:
                return
            d.press("back")
        except Exception:
            return
        if not abort_sleep(0.8):
            return


def open_account_switcher(serial, d):
    width, height = safe_window_size(d)
    for attempt in range(3):
        open_fb_home(serial, d, force=(attempt > 0))
        if not is_logged_in(d):
            # Not "the switcher would not open" - there is no session at all.
            # This phone is sitting on Facebook's account picker and needs
            # someone to log in; retrying cannot fix it.
            print(f"    [{serial}] LOGGED OUT - Facebook is showing its login "
                  f"screen. Log this phone in; the run cannot switch accounts.")
            return False
        # Short: the switcher is read immediately after this, and a switcher
        # that opens is itself proof the account is fine. A blocked account
        # shows its screen at once, so waiting 5s for it cost ~10s per switch
        # on every healthy account.
        if wait_for_account_block(d, timeout=1.0):
            return False
        if account_rows(d):
            return True
        # open the menu by label when we can; the hard-coded corner tap is only
        # a fallback because it breaks whenever the top bar shifts
        menu = find_menu_button(d)
        target = menu if menu else (int(width * 0.914), int(height * 0.074))
        # The FIRST tap after a cold start is swallowed giving the window input
        # focus, so a single click here is regularly lost - and because every
        # outer attempt force-restarts Facebook, the next one loses its first
        # tap too. Click, wait, click once more: far cheaper than another cold
        # start, and it is what made a healthy phone take 41s instead of 8s.
        for _try in range(2):
            d.click(*target)
            if poll_until(lambda: find_switch_account_arrow(d) is not None
                          or account_rows(d), 4):
                break
        if account_rows(d):
            return True
        arrow = find_switch_account_arrow(d)
        if arrow is None:
            # The Menu screen carries no labels on some builds, so the row
            # cannot be found by text - locate it by shape instead. (The sheet
            # it opens DOES expose names, so only getting there was broken.)
            card = find_switch_account_card(d)
            if card is not None:
                d.click(*card)
                if settled_account_rows(d, 7.0):
                    return True
            # last resort: the profile row itself opens the switcher on some builds
            for b, attrs in clickable_items(d):
                desc = (attrs.get("content-desc") or attrs.get("text") or "").lower()
                if "profile" in desc or "view your profile" in desc:
                    d.click(*center(b))
                    if poll_until(lambda: bool(account_rows(d)), 2):
                        return True
                    break
            # This attempt navigated somewhere that is not the menu. UNDO it
            # before trying again: without this the taps accumulate - menu miss,
            # then dashboard, then whatever the next wrong tap opens - and the
            # phone burrows further from the post on every retry.
            _back_out_to_facebook(serial, d)
            continue
        d.click(*arrow)
        if settled_account_rows(d, 7.0):
            return True
        d.press("back")
        time.sleep(0.6)
    return False

def switcher_is_open(d):
    """True only when the account-switcher sheet is really on screen.

    Two different sheet layouts exist in the wild and BOTH must be recognised:

      * multi-account phones show a titled sheet -> "Switch accounts" (plural;
        the menu row behind it is singular "Switch account", so the plural is
        what tells them apart)
      * a phone with a single logged-in account shows a compact, UNTITLED sheet
        -> only the account row, "Go to Accounts Center" and "Dismiss"

    Matching just the title made the compact sheet look closed, so switching
    reported "Could not open account switcher" on a phone whose switcher was
    open and working.
    """
    try:
        text = visible_text(d).lower()
    except Exception:
        return False
    if any(m in text for m in ("switch accounts", "palitan ang account",
                              "mga account")):
        return True
    if ("accounts center" in text or "account center" in text) \
            and "dismiss" in text:
        return True                     # untitled compact sheet
    # Label-less build: there is no title to match, so fall back to the sheet's
    # shape - a stack of identical, full-width rows filling the lower screen.
    if not dump_has_labels(d):
        return len(_uniform_stack(d, min_rows=2, min_w=0.60)) >= 2
    return False


def account_rows(d):
    """Rows of the "Switch accounts" sheet, top first (top = current account).

    Returns [] unless the sheet is actually open. Without that guard a post
    screen yields one row ("Write a comment…"), which made
    open_account_switcher() believe the switcher was already showing — so it
    never opened it, treated that placeholder as the current account, found no
    others and ended the whole run after a single account.

    Identified by SHAPE, not screen position. The old code skipped anything in
    the upper 55% of the screen, which silently dropped the first accounts in
    the list — on a 9-account phone it hid three of them INCLUDING the one that
    is currently logged in, so the caller thought the wrong account was active.

    The sheet's rows all share one x-range, so the largest group of same-width
    rows is the account list; that also excludes the menu behind the sheet.
    """
    # ONE dump for the whole function. switcher_is_open(), dump_has_labels()
    # and clickable_items() each used to fetch their own, so a single
    # account_rows() cost 3-4 dumps - and settled_account_rows() polls it.
    # Measured on a real run: 1771 dumps / 389 s for only 10 switches.
    d = d if getattr(d, "_frozen", False) else Snapshot(d)
    if not switcher_is_open(d):
        return []
    width, height = safe_window_size(d)
    px = _scaler(d)
    cands = []
    for b, attrs in clickable_items(d):
        desc = normalize_account_name(attrs.get("content-desc") or attrs.get("text"))
        if not desc:
            continue
        low = desc.lower()
        if any(skip in low for skip in ACCOUNT_SKIP_TEXT):
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        if w < int(width * 0.50) or h < px(60) or h > px(220):
            continue
        if b[1] < int(height * 0.12):        # keep out of the top app bar only
            continue
        cands.append({"name": desc, "bounds": b, "point": center(b)})

    # group by identical x-extent: the sheet's rows are all flush to one column
    groups = {}
    for r in cands:
        key = (r["bounds"][0] // 8, r["bounds"][2] // 8)
        groups.setdefault(key, []).append(r)
    if groups:
        best = max(groups.values(), key=len)
        if len(best) >= 2:                    # a real list, not a stray row
            cands = best

    deduped, seen = [], set()
    for row in sorted(cands, key=lambda r: (r["bounds"][1], r["bounds"][0])):
        key = row["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    if deduped:
        note_labels_seen(getattr(d, "serial", None))
        return deduped
    # Nothing named, and deliberately no positional "#1/#2" fallback. The sheet
    # ALWAYS exposes names on every phone in this fleet - it is the Menu screen
    # behind it that is label-less, and that is solved geometrically by
    # find_switch_account_card(). An index-based identity was tried and only
    # ever fired on half-rendered sheets, black screens and the Menu's own
    # cards, silently mixing "#2" with real names in used_accounts - the same
    # corruption that makes a run reuse one account. Returning nothing makes
    # the caller poll again, which is what we actually want.
    return []

def count_available_accounts(serial, d):
    try:
        if not open_account_switcher(serial, d):
            return -1
        return len(account_rows(d))
    except Exception:
        return -1

def _verify_switched(serial, d, want, previous):
    """Confirm the logged-in account actually changed to `want`.

    Re-opens the switcher and reads the row it marks as current. Returns True
    when that is `want` (or at least no longer `previous`), so a tap that was
    swallowed mid-animation is caught instead of silently reusing one account.
    """
    for attempt in range(2):
        if stop_requested():
            return False
        if not open_account_switcher(serial, d):
            continue
        cur = current_account_name(d)
        try:
            d.press("back")               # leave the sheet as we found it
        except Exception:
            pass
        abort_sleep(0.6)
        if not cur:
            continue
        if cur.lower() == (want or "").lower():
            return True
        if previous and cur.lower() != previous.lower():
            print(f"    [{serial}] now on '{cur}' (wanted '{want}')")
            return True                   # moved off the old account: accept
        if attempt == 0 and not abort_sleep(1.0):
            return False
    return False


# "immediate" re-opens the switcher after every switch purely to read one name
# (~30s each). "lazy" checks the SAME thing for free on the next switch, which
# has to open the switcher anyway. Only the final switch of a run goes
# unchecked, and that costs at most one repeated account.
VERIFY_SWITCH = "lazy"


def enumerate_accounts(serial, d, max_scrolls=20):
    """Every account the switcher lists, top first, before any work starts.

    Coverage cannot be checked against a list that is only discovered as the
    run goes along: switch_to_next_account() walks the sheet until it stops
    producing new names, so "all accounts" was an outcome, never a target. This
    names the target up front so a missed account can be reported by name.

    Same two rules the switcher walk already obeys: never swipe when
    account_rows() is empty (that means the sheet is NOT on screen and the
    finger is really on the news feed), and stop as soon as a screenful adds
    no new names.

    Returns [] when the switcher could not be opened - callers treat that as
    "roster unknown" and fall back to the old discover-as-you-go behaviour
    rather than refusing to run.
    """
    if not open_account_switcher(serial, d):
        print(f"    [{serial}] could not open the account switcher - "
              f"roster unknown, falling back to discover-as-you-go")
        return []
    # REWIND FIRST. The sheet keeps its scroll position between opens, and
    # open_account_switcher() is a no-op when it is already showing - so an
    # enumeration that only walks DOWN sees nothing above wherever it happens
    # to start. Measured on a 10-account phone: a fresh open found all 10, then
    # a second pass beginning from the bottom found only the last 7 and quietly
    # dropped the top three.
    # One dump per iteration, not two: this screenful's names become the next
    # iteration's comparison. Each account_rows() is a full UI dump (~1.5s on
    # these phones), and reading before AND after every swipe made enumeration
    # cost ~48s per phone - long enough to look like a hang before the run has
    # even started.
    before = [r["name"] for r in account_rows(d)]
    for _ in range(12):
        if not before:
            # A down-swipe can dismiss the sheet instead of scrolling it. That
            # is recoverable - reopen and stop rewinding - but never scroll on
            # blind, because what is under the finger then is the news feed.
            if not open_account_switcher(serial, d):
                return []
            break
        try:
            d.swipe_ext("down", scale=0.35)
        except Exception:
            break
        if not abort_sleep(0.45):
            break
        after = [r["name"] for r in account_rows(d)]
        if not after:
            if not open_account_switcher(serial, d):
                return []
            break
        if after == before:
            break                      # already at the top
        before = after

    roster, seen = [], set()
    stale = 0
    for _ in range(max_scrolls):
        if stop_requested():
            break
        rows = account_rows(d)
        if not rows:
            break
        names = [r["name"] for r in rows]
        fresh = [n for n in names if n.lower() not in seen]
        for n in fresh:
            seen.add(n.lower())
            roster.append(n)
        # Do NOT stop at the first screenful that adds nothing. The sheet does
        # not always settle within one swipe, so a single repeat means "not
        # scrolled yet" far more often than "end of list" - measured on a
        # 10-account phone, breaking eagerly returned only 7 and the three it
        # missed were ones the switcher then happily switched to. An
        # under-counted roster silently under-reports coverage, which defeats
        # the whole point of having one.
        stale = 0 if fresh else stale + 1
        if stale >= 3:
            break
        try:
            d.swipe_ext("up", scale=0.35)
        except Exception:
            break
        if not abort_sleep(0.45):
            break
    # CLOSE THE SHEET. Leaving it open costs the very next open_target_post()
    # its warm path - the sheet covers Facebook, the warm intent "doesn't land
    # on the post", and the run cold-starts the app instead: measured 31s to
    # reach the post with the sheet left open versus 16s with it closed, on
    # every phone and every link.
    for _ in range(3):
        if not switcher_is_open(d):
            break
        try:
            d.press("back")
        except Exception:
            break
        if not abort_sleep(0.6):
            break
    if roster:
        print(f"  [{serial}] {len(roster)} account(s) on this phone: "
              f"{', '.join(roster)}")
        dbg(serial, "roster", f"{len(roster)}: {', '.join(roster)}")
    return roster


def switch_to_next_account(serial, d, used_accounts, state=None,
                           roster_out=None):
    """Switch to the next logged-in account, with retries and verification.

    `roster_out` (a list) collects every account name this walk sees. The
    switcher is opened and scrolled here anyway, so gathering the roster on the
    way costs nothing - whereas a separate enumeration pass meant one extra
    open_account_switcher() per phone, measured at 34s of pure startup delay.
    """
    if stop_requested():
        return False, None, "stopped"
    state = state if state is not None else {}
    opened_any = False
    for retry in range(3):
        if stop_requested():
            return False, None, "stopped"
        if not open_account_switcher(serial, d):
            print(f"    [{serial}] Could not open account switcher (attempt {retry+1})")
            if not abort_sleep(2):
                return False, None, "stopped"
            continue
        opened_any = True
        rows = account_rows(d)
        if not rows:
            print(f"    [{serial}] No account rows found (attempt {retry+1})")
            # NEVER scroll here. Zero rows means the switcher is not on screen,
            # and open_account_switcher() got here through open_fb_home() - so
            # what is really under the finger is the NEWS FEED. Scrolling it is
            # exactly the "after it commented, the phone went to the feed and
            # scrolled" behaviour. Re-open the switcher instead.
            if not abort_sleep(2):
                return False, None, "stopped"
            continue
        # The freshly-opened switcher shows the CURRENT account at the top.
        current = rows[0]["name"]
        # The account that was live BEFORE this switch. The caller uses it to
        # put a real name on the work already done by the starting account.
        state["was_current"] = current
        if roster_out is not None:
            for r in rows:
                _remember_account(roster_out, r["name"])

        # Free verification of the PREVIOUS switch: if the account we selected
        # last time is not the current one, that tap was swallowed. Put it back
        # in the pool so it still gets its turn.
        pending = state.pop("pending", None)
        if pending and pending.lower() != current.lower():
            # Re-queue it ONCE only. Unbounded re-queueing removes names from
            # used_accounts as fast as they are added, so the pool never
            # empties and the device loops forever - a 7-account phone was seen
            # reaching "account 154" under parallel load, where swallowed taps
            # are common.
            requeued = state.setdefault("requeued", {})
            key = pending.lower()
            if requeued.get(key, 0) < 1:
                requeued[key] = requeued.get(key, 0) + 1
                used_accounts.discard(key)
                print(f"    [{serial}] previous switch to {pending} did not take "
                      f"(now on {current}) - re-queueing it once")
            else:
                print(f"    [{serial}] switch to {pending} failed twice - "
                      f"giving up on that account")

        used_accounts.add(current.lower())

        # The switcher is a scrollable list and account_rows() only reports the
        # rows currently on screen. With 10-20 accounts the rest sit below the
        # fold, so walk the list downwards until an unused account shows up or
        # the list stops producing new names. Without this the run stopped after
        # the handful of accounts that happened to be visible.
        seen_names = set()
        rescan = False
        for _ in range(20):
            if stop_requested():
                return False, None, "stopped"
            rows = account_rows(d)
            picked = next((r for r in rows
                           if r["name"].lower() not in used_accounts), None)

            if picked is None:
                if not rows:
                    # Same rule as above: no rows at all means we are NOT
                    # looking at the switcher (it closed, or the dump went
                    # blind). Swiping now scrolls the feed, so stop instead.
                    print(f"    [{serial}] switcher is not on screen - "
                          f"not scrolling the feed")
                    break
                # Nothing unused on screen — scroll further down the switcher.
                if roster_out is not None:
                    for r in rows:
                        _remember_account(roster_out, r["name"])
                names = {r["name"].lower() for r in rows}
                if names and names.issubset(seen_names):
                    break                       # end of the list
                seen_names |= names
                try:
                    d.swipe_ext("up", scale=0.35)
                except Exception:
                    break
                if not abort_sleep(0.6):
                    return False, None, "stopped"
                continue

            name = picked["name"]
            print(f"  [{serial}] switching account -> {name}")
            task_log(serial, "switch", f"-> {name}")
            d.click(*picked["point"])
            if not abort_sleep(3):
                return False, None, "stopped"
            if chooser_present(serial, d):
                dismiss_chooser(serial, d)
                if not abort_sleep(1):
                    return False, None, "stopped"
            # 3s shorter now that a bad verdict must be confirmed twice: the
            # block screen shows up as soon as the account loads, and a healthy
            # account used to burn the whole 6s on every single switch.
            block_status = wait_for_account_block(d, timeout=3)
            if block_status:
                print(f"  [{serial}] {name}: {block_status}")
                used_accounts.add(name.lower())
                if block_status == "suspended" and logout_current_account(serial, d):
                    rescan = True           # logged out — rescan from the top
                    break
                return False, None, block_status

            # Verify the switch REALLY happened. The tap can be swallowed while
            # the sheet animates; assuming success made the run treat several
            # "accounts" as the same logged-in user.
            if VERIFY_SWITCH == "immediate":
                if not _verify_switched(serial, d, name, current):
                    print(f"  [{serial}] switch to {name} did not take - retrying")
                    if not abort_sleep(1.5):
                        return False, None, "stopped"
                    rescan = True
                    break
            else:
                state["pending"] = name      # checked free on the next switch

            used_accounts.add(name.lower())
            return True, name, "switched"

        if rescan:
            continue
        print(f"    [{serial}] No unused accounts found, refreshing...")
        d.press("back")
        if not abort_sleep(1):
            return False, None, "stopped"
        if open_account_switcher(serial, d):
            rows2 = account_rows(d)
            if rows2 and len(rows2) > len(rows):
                continue
        return False, None, "no-available-account"
    # Falling out of the retry loop without ever seeing the switcher is a
    # FAILURE, not a finished phone. Reporting it as "no-available-account" made
    # the run print "all accounts done" after processing a single account.
    return False, None, ("no-available-account" if opened_any
                         else "switcher-unreachable")

def find_reel_comment_point(d):
    stack = _reel_action_stack(d)
    return stack[1] if stack and len(stack) >= 2 else None

def find_feed_action_row(d):
    width, height = safe_window_size(d)
    px = _scaler(d)
    rows = {}
    for b in clickable_bounds(d):
        x1, y1, x2, y2 = b
        w = x2-x1
        h = y2-y1
        if h < px(40) or h > px(140):
            continue
        # Current Lite uses narrow Like / Comment / Share pills, often only
        # 14% of the screen wide. Match the same measured band as
        # _find_post_like(), otherwise a verified photo/group/video post has
        # a reaction row but no discoverable Comment button.
        if w < int(width * 0.12) or w > int(width * 0.55):
            continue
        rows.setdefault(round((y1 + y2) / 2 / px(26)), []).append(b)
    for _, row in sorted(rows.items()):
        row = sorted(row, key=lambda b: b[0])
        if not 2 <= len(row) <= 4:
            continue
        same_band = max(b[1] for b in row) - min(b[1] for b in row) <= px(24)
        covers = row[0][0] <= int(width * 0.12) and row[-1][2] >= int(width * 0.55)
        if len(row) == 2:
            # Some group layouts have only Like and Comment when sharing is
            # disabled. Preserve their two broad, almost full-width controls;
            # two arbitrary narrow buttons are not enough evidence.
            covers = (row[0][0] <= int(width * 0.12)
                      and row[-1][2] >= int(width * 0.88)
                      and all(b[2] - b[0] >= int(width * 0.20) for b in row))
        elif any(b[2] - b[0] > int(width * 0.42) for b in row):
            continue
        cy = center(row[0])[1]
        # Exclude the top tabs, Android navigation and indented reply rows.
        if same_band and covers and int(height * 0.20) <= cy <= int(height * 0.92):
            return row
    return None

def find_feed_comment_point(d):
    row = find_feed_action_row(d)
    if not row:
        return None
    if len(row) >= 2:
        return center(row[1])
    return None

def edit_nodes(d):
    root = hierarchy_root(d)
    if root is None:
        return []
    nodes = []
    for node in root.iter("node"):
        cls  = node.attrib.get("class", "")
        desc = (node.attrib.get("content-desc") or node.attrib.get("text") or "").lower()
        is_edit    = ("EditText" in cls or "AutoCompleteTextView" in cls or
                      node.attrib.get("editable") == "true")
        is_comment = ("comment as" in desc or "write a comment" in desc or
                      "add a comment" in desc)
        if is_edit or is_comment:
            b = parse_bounds(node.attrib.get("bounds"))
            if b:
                nodes.append((b, node.attrib))
    return nodes

def _settled_edit_point(d, point, timeout=2.0):
    """Follow the composer until it stops moving, then return its centre.

    Tapping the composer raises the keyboard, which shrinks the comment sheet
    and slides the edit box hundreds of pixels UP the screen. Measured on a reel
    (720x1612): the box sat at y=1508 while the sheet was still animating and
    settled at y=950 once the keyboard was up. Returning the pre-tap coordinate
    handed type_text() a point that now lands on the KEYBOARD - x=294,y=1508 is
    the spacebar - so the "click the box then type" step was really clicking a
    key. It survived only because the box already held focus from the tap above;
    a stale point landing on ?123 or the emoji key breaks typing outright.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.35)
        edits = edit_nodes(d)
        if not edits:
            return point
        edits.sort(key=lambda e: -e[0][1])
        fresh = center(edits[0][0])
        if abs(fresh[1] - point[1]) <= 8:
            return fresh
        point = fresh
    return point


def wait_for_edit_box(d, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        edits = edit_nodes(d)
        if edits:
            edits.sort(key=lambda e: -e[0][1])
            point = center(edits[0][0])
            try:
                d.click(*point)
                time.sleep(0.5)
            except Exception:
                pass
            return _settled_edit_point(d, point)
        time.sleep(0.5)
    return None

def _tap_for_comment_box(serial, d, point, wait=8):
    d.click(*point)
    time.sleep(2)
    if chooser_present(serial, d):
        dismiss_chooser(serial, d)
        time.sleep(2)
    return wait_for_edit_box(d, timeout=wait)


def try_open_comment_from_screen(serial, d):
    edits = edit_nodes(d)
    if edits:
        edits.sort(key=lambda e: -e[0][1])
        return center(edits[0][0])
    if _is_reel_context():
        point = find_reel_comment_point(d)
        if point is not None:
            # The reel player is up, and its rail is the ONLY way into the
            # comments: the second button (Like, Comment, Share, More). The
            # post guesses below were written for posts, and on a reel they
            # can take the author row for a Comment button and open a profile.
            return _tap_for_comment_box(serial, d, point, wait=10)
    for b, attrs in clickable_items(d):
        label = (attrs.get("content-desc") or attrs.get("text") or "").lower()
        if "comment" in label or "write a" in label:
            d.click(*center(b))
            time.sleep(2)
            if chooser_present(serial, d):
                dismiss_chooser(serial, d)
                time.sleep(2)
            box = wait_for_edit_box(d)
            if box is not None:
                return box
    for finder in (find_feed_comment_point, find_reel_comment_point):
        pt = finder(d)
        if pt is not None:
            d.click(*pt)
            time.sleep(2)
            if chooser_present(serial, d):
                dismiss_chooser(serial, d)
                time.sleep(2)
            box = wait_for_edit_box(d)
            if box is not None:
                return box
    return None

def find_enter_key(d):
    root = hierarchy_root(d)
    if root is None: return None
    width, height = safe_window_size(d)
    for node in root.iter("node"):
        desc = (node.attrib.get("content-desc") or "").lower()
        text = (node.attrib.get("text") or "").lower()
        if desc in ("enter", "send", "search", "done", "go", "next") or \
           text in ("enter", "send", "search", "done", "go", "next"):
            b = node.attrib.get("bounds", "")
            try:
                parts = b.replace("][", ",").replace("[","").replace("]","").split(",")
                if len(parts) == 4:
                    x1, y1, x2, y2 = map(int, parts)
                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                    if cy > height * 0.5:
                        return (cx, cy)
            except Exception:
                pass
    return None

def _reel_surface(d):
    """The reel player, or this reel's comment sheet, is on screen."""
    try:
        return find_reel_like_point(d) is not None or _reel_sheet_open(d)
    except Exception:
        return False


def _retry_reel_comment_box(serial, d, tries=2):
    """The reel is up but its comment box has not appeared yet.

    Never swipe here. A vertical swipe in the reel player is NEXT REEL, and
    every reel passes as "the post", so the post-style scroll-for-the-box loop
    used to walk away from the target and could comment on a stranger's reel.
    A slow sheet gets time instead, then Comment is pressed again.
    """
    for attempt in range(tries):
        if not abort_sleep(1.5):
            return None
        box = try_open_comment_from_screen(serial, d)
        if box is not None:
            print(f"    [comment] reel comment box opened on retry {attempt + 1}")
            return box
        if stop_requested():
            return None
    print("    [comment] the reel's comment box never opened - not scrolling away")
    dump_debug(serial, d, "reel_comment_box_missing")
    return None


def open_comment_composer(serial, d, max_scrolls=10):
    # Long captions push the comment box below the fold, so keep scrolling for
    # it — but only while we are still on the target post, so a big post is
    # handled without ever drifting onto another one.
    width, height = safe_window_size(d)
    if stop_requested():
        return None
    box = try_open_comment_from_screen(serial, d)
    if box is not None:
        return box
    for i in range(max_scrolls):
        if stop_requested():
            return None
        if _is_reel_context() and _reel_surface(d):
            return _retry_reel_comment_box(serial, d)
        if not on_post_view(d):
            print("    [comment] left the target post while scrolling - stopping")
            return None
        try:
            d.swipe(int(width*0.5), int(height*0.72),
                    int(width*0.5), int(height*0.32), 0.3)
        except Exception:
            pass
        if not abort_sleep(1.2):
            return None
        box = try_open_comment_from_screen(serial, d)
        if box is not None:
            print(f"    [comment] found composer after {i + 1} scroll(s)")
            return box
    return None

# A label in the comment row that rules a control out as Send, in any Facebook
# Lite layout or language. Anything else - an unlabelled icon, or a Send icon
# labelled in Filipino - is judged by WHERE it sits, not by what it says.
_NOT_SEND_WORDS = ("voice", "delete", "space", "search", "done", "next",
                   "previous", "back", "home", "overview", "recent", "camera",
                   "photo", "picture", "gallery", "gif", "sticker", "emoji",
                   "avatar", "profile", "like", "react", "more", "attach",
                   "mention", "close", "cancel", "details")
_SEND_WORDS = ("send", "post", "submit", "publish", "ipadala", "i-post", "ipost",
               "magpadala", "i-send")
# What an EMPTY comment field shows. Facebook Lite's Nov-2026 layout puts the
# account's name in it ("Comment as Jie Jie") and the Taglish UI says "Sumulat
# ng comment..." - neither matched, so a comment that HAD posted was reported
# as unverified and counted as a failure.
_COMPOSER_HINTS = ("write a comment", "write a public comment", "add a comment",
                   "magsulat ng komento", "sumulat ng komento")
_COMPOSER_HINT_PREFIXES = ("comment as ", "write a ", "add a comment",
                           "sumulat ng ", "magsulat ng ", "mag-comment",
                           "mag-komento", "magkomento")


def _composer_is_placeholder(value):
    """True when the comment field shows its empty-state hint, in any layout."""
    value = " ".join(str(value or "").lower().split()).rstrip(" .…")
    return value in _COMPOSER_HINTS or value.startswith(_COMPOSER_HINT_PREFIXES)


def _composer_bounds(d):
    """Bounds of the comment draft: the lowest editable field on screen."""
    try:
        edits = [bounds for bounds, attrs in edit_nodes(d)
                 if ("EditText" in attrs.get("class", "")
                     or "AutoCompleteTextView" in attrs.get("class", "")
                     or attrs.get("editable") == "true")]
    except Exception:
        return None
    return max(edits, key=lambda bounds: bounds[1]) if edits else None


def find_send_button(d):
    """The comment's Send control, found beside the draft it belongs to.

    Measured on the fleet's own screens (FBPoison/FBVenom debug dumps; every
    phone is 720 px wide): once text is typed, Facebook Lite shows ONE
    clickable icon at the right end of the comment row - 68x64 at x=626 in the
    Nov-2026 "Comment as <name>" layout, 59x56 at x=638 in the older one - with
    a strip of quick-emoji cells (90x112) just above the row. With the keyboard
    down on a gesture-navigation phone, that row IS the bottom of the screen,
    so the old "ignore the bottom 100 px" rule (meant for the Back/Home bar)
    hid Send entirely: the comment was typed and never sent, and only phones
    with an on-screen button bar kept commenting. The row is now anchored to
    the draft field itself: anything level with any part of it, which also
    covers a draft that has wrapped onto several lines.
    """
    width, height = safe_window_size(d)
    composer = _composer_bounds(d)

    def in_row(bounds):
        x1, y1, x2, y2 = bounds
        if x1 < 0 or x2 > width or y2 > height:
            return False
        cy = (y1 + y2) // 2
        if composer is None:
            # Nothing to anchor to: stay clear of the system navigation bar.
            return y1 >= int(height * 0.25) and cy <= height - _scaler(d)(100)
        margin = max(24, int(height * 0.025))
        return composer[1] - margin <= cy <= composer[3] + margin

    def says(label, words):
        return any(word in label for word in words)

    candidates = []
    for bounds, attrs in clickable_items(d):
        cls = attrs.get("class", "")
        if "EditText" in cls or "AutoCompleteTextView" in cls or bounds == composer:
            continue                       # the draft itself, not its button
        if not in_row(bounds):
            continue
        x1, y1, x2, y2 = bounds
        cx, cy = center(bounds)
        label = (attrs.get("content-desc") or attrs.get("text") or "").strip().lower()
        if label and says(label, _NOT_SEND_WORDS):
            continue
        if label and says(label, _SEND_WORDS) and cx > width * 0.5:
            candidates.append((9, (cx, cy)))
            continue
        w, h = x2 - x1, y2 - y1
        # Icon-sized relative to the screen, so it holds at any density.
        iconish = (0.06 * width <= w <= 0.16 * width
                   and 0.06 * width <= h <= 0.16 * width
                   and 0.6 <= w / max(h, 1) <= 1.6)
        if composer is not None and iconish and x1 >= int(width * 0.78):
            candidates.append((1 + cx / width, (cx, cy)))
    if candidates:
        candidates.sort(key=lambda candidate: -candidate[0])
        return candidates[0][1]

    # UiObject instances do not support selector OR, and .info is a property,
    # not a method. Query each exact label separately so wide text buttons on
    # older Lite layouts remain usable when the icon geometry does not match.
    for text in ("Send", "Post", "Submit", "Publish"):
        try:
            send_btn = d(className="android.widget.Button", text=text)
            if send_btn.exists:
                bounds = send_btn.info["bounds"]
                rect = (bounds["left"], bounds["top"], bounds["right"], bounds["bottom"])
                if in_row(rect) and center(rect)[0] > width * 0.5:
                    return center(rect)
        except Exception:
            continue
    return None

def type_text(serial, d, point, text):
    """Put the comment into Facebook Lite's comment box - never key by key.

    Measured live on a TECNO KM4 (Facebook Lite 538, the "Comment as <name>"
    box on posts): after 2-3 injected keystrokes the box drops focus and the
    keyboard closes. The remaining keystrokes then move focus to the header's
    Back arrow, and a typed space PRESSES it - the draft is lost and the phone
    lands on the news feed (or the previous reel). `adb shell input text "x y"`
    put "tes" in the box and then went back; key events T,E,S,T put "te".
    uiautomator2's set_text wrote the whole comment, and the Send arrow
    appeared without a single keystroke; its input-method send_keys did too.
    So: write the text into the field, verify it is IN the field, and send no
    keystrokes at all - no Backspace clearing (set_text replaces the content,
    and a Backspace with nothing focused is not harmless either), and no extra
    space-then-delete afterwards.
    """
    if stop_requested():
        return False
    d.click(*point)                  # focus the box, as a person would
    if not abort_sleep(1.0):
        return False
    want = " ".join(text.strip().lower().split())[:15]

    def field_has_draft():
        for _bounds, attrs in edit_nodes(d):
            cls = attrs.get("class", "")
            if ("EditText" in cls or "AutoCompleteTextView" in cls
                    or attrs.get("editable") == "true"):
                if want and want in " ".join(attrs.get("text", "").lower().split()):
                    return True
        return False

    def _set_text():
        box = d(className="android.widget.MultiAutoCompleteTextView")
        if not box.exists:
            box = d(className="android.widget.EditText")
        box.set_text(text)
        time.sleep(0.8)
        return field_has_draft()

    def _input_method():
        # uiautomator2's own input method: committed text, not key events.
        d.send_keys(text)
        time.sleep(0.8)
        return field_has_draft()

    for fn in (_set_text, _input_method):
        try:
            if fn():
                return True
        except Exception as e:
            print(f"    [type] {fn.__name__} failed: {type(e).__name__}: {e}")
        if stop_requested():
            return False
    return False

def post_comment(serial, d, text):
    """Submit once, and count only a verified composer-clear transition.

    Hiding a sheet or navigating Back makes text disappear too. That is not
    proof of posting, and an uncertain send must never be submitted again.
    """
    def fail(code, status, detail="", exc=None):
        report_error(code, serial, detail, url=current_post_url() or "", exc=exc)
        return status

    if not text or not text.strip():
        return fail("COMMENT_EMPTY", "comment-empty")
    if stop_requested():
        return "stopped"
    if not on_post_view(d, timeout=5.0):
        if stop_requested():
            return "stopped"
        if not ensure_on_target(serial, d, where="before commenting"):
            return fail("TARGET_NOT_VERIFIED", "comment-not-on-target-post")
    if stop_requested():
        return "stopped"
    if is_action_blocked(d):
        return fail("ACTION_BLOCKED", "action-blocked")
    box = open_comment_composer(serial, d)
    if box is None:
        return "stopped" if stop_requested() else fail("COMMENT_NO_BOX", "comment-no-box")

    typed = " ".join(text.strip().lower().split())[:15]

    def composer_state():
        # Check the actual editable field, not all visible text: a caption or
        # an earlier comment can contain the same prefix as this draft.
        edits = []
        for bounds, attrs in edit_nodes(d):
            cls = attrs.get("class", "")
            if ("EditText" in cls or "AutoCompleteTextView" in cls
                    or attrs.get("editable") == "true"):
                edits.append((bounds, attrs))
        if not edits:
            return "missing"
        edits.sort(key=lambda item: -item[0][1])
        value = " ".join(edits[0][1].get("text", "").strip().lower().split())
        if typed in value:
            return "draft"
        # Android may expose the field's hint through `text` after clearing.
        # Placeholder matching avoids treating arbitrary post text as an empty
        # draft, while covering every composer seen on the fleet: English,
        # Taglish ("Sumulat ng comment...") and "Comment as <account name>".
        if not value or _composer_is_placeholder(value):
            return "empty"
        return "other"

    try:
        typed_ok = type_text(serial, d, box, text)
    except Exception as e:
        return fail("COMMENT_TYPE_FAILED", "comment-type-failed", exc=e)
    if not abort_sleep(1):
        return "stopped"
    if not typed_ok or composer_state() != "draft":
        return fail("COMMENT_NOT_TYPED", "comment-not-typed")
    # Abort between typing and sending must leave the draft UNSENT.
    if stop_requested():
        return "stopped"
    if is_action_blocked(d):
        return fail("ACTION_BLOCKED", "action-blocked")

    send = find_send_button(d)
    if send is None:
        # Only keyboard Enter/Send controls can submit. Search, Next, Done and
        # Go are not comment send buttons even though find_enter_key() also
        # serves those controls elsewhere in the application.
        root = hierarchy_root(d)
        _, height = safe_window_size(d)
        if root is not None:
            for node in root.iter("node"):
                attrs = node.attrib
                label = (attrs.get("content-desc") or attrs.get("text") or "").strip().lower()
                bounds = parse_bounds(attrs.get("bounds"))
                if label in ("enter", "send", "post", "submit", "publish") and bounds:
                    point = center(bounds)
                    if point[1] > height * 0.5:
                        send = point
                        break
    if send is None:
        # Keep the screen: this failure used to leave no evidence at all.
        dump_debug(serial, d, "comment_send_failed")
        return fail("COMMENT_SEND_FAILED", "comment-send-failed", "No usable Send or Enter control.")
    if stop_requested():
        return "stopped"

    # One submission only. A delayed network acknowledgement can arrive after
    # our observation window, so never try another control after this action.
    try:
        run(["shell", "input", "tap", str(send[0]), str(send[1])], serial=serial)
    except Exception as e:
        return fail("COMMENT_SUBMIT_UNVERIFIED", "comment-submit-unverified", exc=e)
    for _ in range(8):
        if not abort_sleep(0.5):
            return fail("COMMENT_SUBMIT_UNVERIFIED", "comment-submit-unverified",
                        "Stopped after the send action; check the post before retrying.")
        if is_action_blocked(d):
            return fail("ACTION_BLOCKED", "action-blocked")
        state = composer_state()
        if state == "empty" and on_post_view(d):
            return f"commented:{text}"
    dump_debug(serial, d, "comment_unverified")
    return fail("COMMENT_SUBMIT_UNVERIFIED", "comment-submit-unverified",
                "The editable composer did not visibly clear on the post.")

def print_unready_help(serial, state):
    report_error("DEVICE_UNAUTHORIZED" if state == "unauthorized" else "DEVICE_OFFLINE",
                 serial, detail=f"state={state}")
    if state == "unauthorized":
        print(f"  [!] {serial} UNAUTHORIZED - on the phone: keep it unlocked, "
              "Developer Options -> Revoke USB debugging authorizations, "
              "toggle USB debugging OFF/ON, replug, tap Allow (Always allow).")
    else:
        print(f"  [!] {serial} {state.upper()} - not reachable. "
              "Use a data USB cable, set USB mode to File transfer, "
              "enable USB debugging, try another port/driver.")

# ── Updated react_all_saved_accounts (calls screenshot with d) ──
# Stands in for the account already logged in when a phone starts: its work
# happens before the switcher has ever been opened, so its name is not known
# until the first switch. rename_account() swaps it for the real one.
FIRST_ACCOUNT_PLACEHOLDER = "(starting account)"


class RunResults(list):
    """The usual list of status strings, plus a per-link tally.

    Subclassing list keeps every existing caller working unchanged while
    carrying `link_tally` = {link_no: {"reacted": n, "commented": n,
    "failed": n, "url": str}} so the UI can report "link 1 -> 169 reacts"
    instead of trying to parse it back out of the status strings.
    """

    def __init__(self, *a):
        super().__init__(*a)
        self.link_tally = {}
        # Per-ACCOUNT outcomes: {(account, link_no): {"reacted":..,
        # "commented":.., "reason":..}}. link_tally only counts, so it can say
        # "6 commented" but never WHICH six - and the whole point of a coverage
        # check is naming the account that was missed.
        self.account_tally = {}

    def bump(self, link_no, url, key):
        row = self.link_tally.setdefault(
            link_no, {"reacted": 0, "commented": 0, "failed": 0, "url": url})
        row["url"] = url
        row[key] = row.get(key, 0) + 1

    def note_account(self, account, link_no, key, detail=""):
        """Record what one ACCOUNT did on one link."""
        if not account:
            return
        row = self.account_tally.setdefault(
            (account, link_no), {"reacted": "", "commented": "", "reason": ""})
        row[key] = detail or "ok"
        if key == "commented" and detail and detail != "ok":
            row["reason"] = detail

    def rename_account(self, old, new):
        """Re-key one account's rows once its real name becomes known.

        The account already logged in when a phone starts is only NAMED by the
        first switcher open, which happens AFTER its work is done. Its outcomes
        are recorded under a placeholder and moved here, so account 1 of 10 is
        not permanently reported as "(unknown)".
        """
        if not old or not new or old == new:
            return
        for (acct, link_no) in [k for k in self.account_tally if k[0] == old]:
            self.account_tally[(new, link_no)] = self.account_tally.pop(
                (acct, link_no))

    def accounts_that_commented(self):
        return {acct for (acct, _), row in self.account_tally.items()
                if row.get("commented") == "ok"}


def _tally(results, link_no, url, key):
    if isinstance(results, RunResults):
        results.bump(link_no, url, key)


def _note_account(results, account, link_no, key, detail=""):
    if isinstance(results, RunResults):
        results.note_account(account, link_no, key, detail)


def _acct_log(serial, account, link_no, action, result):
    """One parseable line per account action.

    Deliberately key=value with a fixed `[acct]` prefix so a script can grep
    the run apart without guessing at prose:

        [acct] serial=<s> link=<n> account=<name> action=comment result=ok
        [acct] serial=<s> link=<n> account=<name> action=comment result=failed:comment-no-box

    Account names can contain spaces, so `account=` is quoted and kept last but
    one; `result=` never contains a space.
    """
    if not account:
        account = "(unknown)"
    line = (f'serial={serial} link={link_no} account="{account}" '
            f'action={action} result={result}')
    print(f"    [acct] {line}")
    dbg(serial, "acct", line)


def _process_target(serial, d, tgt, account_num, link_no, n_links,
                    take_screenshots, results, account_name=""):
    """React and/or comment on ONE link with the CURRENT account.

    The post is already on screen. Returns one of:
      "ok"      - done with this link
      "abort"   - Abort was pressed, unwind everything
      "blocked" - Facebook is rate-limiting this account; its remaining links
                  are pointless, move on to the next account
    Screenshots are tagged per link so each account/link pair is its own file.
    """
    url = tgt["url"]
    set_current_post_url(url)            # this link, this thread
    if isinstance(results, RunResults):
        previous = results.account_tally.get((account_name, link_no), {})
        if previous.get("commented") == "comment-submit-unverified":
            # A coverage sweep for another link must not resend an uncertain
            # submission on this one.
            return "ok"
    # Tell the UI this phone has REACHED this account, before the react/comment
    # is attempted. An account that is reached but then fails to comment still
    # counts toward "all accounts", so the Devices tile can only read "done"
    # once every reached account actually completed.
    report_progress(serial, "account", account_num, link_no, url)
    # The post is supposed to be on screen already. Say where the phone really
    # is, and steer it back if it drifted (feed, search, someone else's post).
    if not ensure_on_target(serial, d, url,
                            where=f"account {account_num} link {link_no}"):
        if stop_requested():
            results.append("stopped")
            return "abort"
        results.append("target-not-verified")
        _tally(results, link_no, url, "failed")
        _note_account(results, account_name, link_no, "commented", "target-not-verified")
        report_error("TARGET_NOT_VERIFIED", serial, account_num=account_num,
                     link_no=link_no, url=url)
        return "ok"
    reaction = tgt.get("reaction", "like")
    comment_example = tgt.get("comment", "")
    do_react = tgt.get("do_react", True)
    do_comment = tgt.get("do_comment", False)
    tag = f"link{link_no}_" if n_links > 1 else ""
    where = f"account {account_num} link {link_no}/{n_links}"

    # ── reaction ──
    react_ok = False
    if do_react:
        last_fail, repeats = None, 0
        for attempt in range(10):
            if stop_requested():
                results.append("stopped")
                return "abort"
            react_status = react(d, reaction)
            if react_status == "stopped" or stop_requested():
                results.append("stopped")
                return "abort"
            # Rate limited: no amount of retrying helps, and every retry costs
            # ~20 swipes. Stop this account immediately.
            if react_status == "action-blocked":
                results.append("action-blocked")
                print(f"    [{serial}] {where}: ACTION BLOCKED by Facebook "
                      f"- skipping this account")
                dump_debug(serial, d, "action-blocked")
                return "blocked"
            results.append(react_status)
            print(f"    [{serial}] {where}: {react_status}")
            # react() has no serial or url, so it can only REFUSE when the
            # phone is not on the post - it cannot go back. The caller does
            # have both, so recover here instead of spending the remaining
            # attempts re-refusing on the same wrong screen. This is the react
            # side of what the comment path already did.
            if react_status == "react-failed:not-on-target-post":
                ensure_on_target(serial, d, url, where=f"{where} before reacting")
            # Identical failure three times running means the screen is not
            # what we think it is; keep the evidence and stop wasting swipes.
            if not react_status.startswith("reacted"):
                repeats = repeats + 1 if react_status == last_fail else 1
                last_fail = react_status
                if repeats >= 3:
                    print(f"    [{serial}] {where}: '{react_status}' 3x in a row "
                          f"- giving up on this link")
                    dump_debug(serial, d, f"react_stuck_{react_status[:24]}")
                    break
            if react_status.startswith("reacted"):
                react_ok = True
                _tally(results, link_no, url, "reacted")
                _note_account(results, account_name, link_no, "reacted", "ok")
                _acct_log(serial, account_name, link_no, "react", "ok")
                task_log(serial, "react", f"{react_status} on {_short_target(url)}")
                report_progress(serial, "reacted", account_num, link_no, url)
                # when we also comment, hold the shot so ONE screenshot shows
                # both the reaction and the comment
                if take_screenshots and not do_comment:
                    take_screenshot(serial, account_num, f"{tag}react", d)
                # a human does not react and start typing in the same instant
                if not pace(PACE_ACTION):
                    results.append("stopped")
                    return "abort"
                break
            print(f"    [{serial}] reaction failed, retrying...")
            if not abort_sleep(1):
                results.append("stopped")
                return "abort"
            wake_and_unlock(serial)
            for _ in range(5):
                if stop_requested():
                    results.append("stopped")
                    return "abort"
                try:
                    if open_target_post(serial, d, url):
                        break
                except Exception:
                    pass
                print(f"    [{serial}] open post failed during react retry, retrying...")
                if not abort_sleep(3):
                    results.append("stopped")
                    return "abort"
            try:
                dismiss_chooser(serial, d)
            except Exception:
                pass
            poll_until(lambda: on_post_view(d), 6)
        else:
            print(f"    [{serial}] reaction failed after 10 attempts")
            results.append("react-failed")
            _tally(results, link_no, url, "failed")

    # ── comment ──
    if do_comment:
        # The post is NOT read any more - no caption, no picture, no vision
        # call. The example comments are the subject as well as the voice, so
        # they go in as the context: same styled path, same no-foreign-entity
        # gate, just sourced from what the user typed instead of from the
        # screen. No example still means no voice, and unique_comment falls
        # straight to the offline pool.
        ctx = (comment_examples_as_context(comment_example)
               if comment_example.strip() else "")
        # Usually composed already, while the phone was opening this post.
        text = take_prefetched_comment(url, comment_example)
        if stop_requested():
            results.append("stopped")
            return "abort"
        if text is None:
            text = unique_comment(comment_example, ctx=ctx)
        if not text:
            # Every wording is already under this post. Skipping is the correct
            # outcome - posting a duplicate is worse than posting nothing.
            dbg(serial, "comment-skipped",
                f"account {account_num}: no unused wording left")
            report_error("COMMENT_WORDING_EXHAUSTED", serial, account_num=account_num,
                         link_no=link_no, url=url, severity="warning")
            results.append("comment-skipped-duplicate")
            _tally(results, link_no, url, "failed")
            return "ok"
        text = humanize_typo(text)
        print(f"    [{serial}] {where} comment > {text}")
        for _ in range(2):
            if stop_requested():
                results.append("stopped")
                return "abort"
            if is_action_blocked(d):
                results.append("action-blocked")
                report_error("ACTION_BLOCKED", serial, account_num=account_num,
                             link_no=link_no, url=url)
                print(f"    [{serial}] {where}: ACTION BLOCKED - not commenting")
                dump_debug(serial, d, "action-blocked-comment")
                return "blocked"
            comment_status = post_comment(serial, d, text)
            if comment_status == "stopped" or stop_requested():
                results.append("stopped")
                return "abort"
            results.append(comment_status)
            print(f"    [{serial}] {where}: {comment_status}")
            if comment_status == "action-blocked":
                _tally(results, link_no, url, "failed")
                _note_account(results, account_name, link_no, "commented", comment_status)
                return "blocked"
            if comment_status.startswith("commented"):
                _tally(results, link_no, url, "commented")
                # Tell the UI straight away, so "doses" counts this account now
                # instead of when the whole phone finishes.
                report_progress(serial, "commented", account_num, link_no, url)
                # Log the TEXT too: it is the only way to prove after a run that
                # no wording was posted twice across the whole fleet.
                dbg(serial, "commented",
                    f"account {account_num} link {link_no} | {text}")
                _note_account(results, account_name, link_no, "commented", "ok")
                _acct_log(serial, account_name, link_no, "comment", "ok")
                task_log(serial, "comment", f"posted on {_short_target(url)}")
                if not pace(PACE_ACTION):
                    results.append("stopped")
                    return "abort"
                if take_screenshots:
                    # drop the keyboard so the reaction AND the new comment
                    # both land in the same screenshot
                    prepare_post_view_for_shot(serial, d)
                    action = f"{tag}react_comment" if do_react else f"{tag}comment"
                    take_screenshot(serial, account_num, action, d)
                break
            if comment_status == "comment-submit-unverified":
                # A send may already be in flight. Do not send the same text
                # again or put this account/link into the coverage retry pass.
                _tally(results, link_no, url, "failed")
                _note_account(results, account_name, link_no, "commented",
                              comment_status)
                _acct_log(serial, account_name, link_no, "comment", comment_status)
                return "ok"
            print(f"    [{serial}] comment failed, retrying...")
            if not abort_sleep(1):
                results.append("stopped")
                return "abort"
            wake_and_unlock(serial)
            for _ in range(3):
                if stop_requested():
                    results.append("stopped")
                    return "abort"
                try:
                    if open_target_post(serial, d, url):
                        break
                except Exception:
                    pass
                print(f"    [{serial}] open post failed during comment retry, retrying...")
                if not abort_sleep(3):
                    results.append("stopped")
                    return "abort"
            try:
                dismiss_chooser(serial, d)
            except Exception:
                pass
            poll_until(lambda: on_post_view(d), 6)
        else:
            print(f"    [{serial}] comment could not be submitted after 2 attempts")
            results.append("comment-failed")
            _tally(results, link_no, url, "failed")
            _note_account(results, account_name, link_no, "commented",
                          comment_status or "comment-failed")
            _acct_log(serial, account_name, link_no, "comment",
                      f"failed:{comment_status or 'comment-failed'}")
            bad_log(serial, "comment", f"gave up on {_short_target(url)}: "
                                       f"{comment_status or 'comment-failed'}")
            # comment failed but the reaction landed — still capture proof
            if take_screenshots and react_ok:
                prepare_post_view_for_shot(serial, d)
                take_screenshot(serial, account_num, f"{tag}react", d)
    return "ok"


def make_target(url, reaction="like", comment="", do_react=True, do_comment=False):
    """One link and what to do on it."""
    return {"url": normalize_url(url), "reaction": reaction or "like",
            "comment": comment or "", "do_react": bool(do_react),
            "do_comment": bool(do_comment)}


# Longest ENVENOM holds the phones for share-link lookups. A lookup normally
# answers in ~1 s; one that has not by then is not worth every phone sitting
# idle (a failing one could take 20 s: two attempts of 10 s each). Facebook
# Lite opens a /share/ link by itself - measured on a TECNO KM4 (FB Lite 538),
# an unresolved /share/v/ link opened and verified in 4.6 s - so the phones get
# the cleaned share link and the lookup finishes in the background, cached for
# the next run.
RESOLVE_BUDGET = 4.0
_lookup_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="link-lookup")
_lookups = {}
_lookups_lock = threading.Lock()


def _lookup(url):
    """One shared background lookup per link (finished ones re-run from cache)."""
    key = normalize_url(url) or str(url or "")
    with _lookups_lock:
        future = _lookups.get(key)
        if future is None or future.done():
            future = _lookup_pool.submit(normalize_phone_url, key)
            _lookups[key] = future
        return future


def normalize_targets_for_phones(targets, budget=None):
    """Resolve every target once before any phone worker receives it.

    Resolution is a network fetch per share link (~0.9s each). They run
    together, and the phones wait at most `budget` seconds (RESOLVE_BUDGET) for
    them: a link still being looked up goes to the phones as its cleaned share
    link. Targets this already prepared (the GUI does it once before fan-out)
    pass straight through, so every phone keeps the SAME URL - the per-post
    wording pool that keeps comments from repeating is keyed by it.
    """
    items = [dict(t) for t in (targets or [])]
    if not items:
        return []
    budget = RESOLVE_BUDGET if budget is None else float(budget)
    deadline = time.monotonic() + budget
    futures = [None if item.get("phone_ready") else _lookup(item.get("url", ""))
               for item in items]
    resolved = []
    for item, future in zip(items, futures):
        if future is None:
            resolved.append(item.get("url", ""))
            continue
        try:
            resolved.append(future.result(timeout=max(0.0, deadline - time.monotonic())))
        except concurrent.futures.TimeoutError:
            clean = normalize_url(item.get("url", ""))
            print(f"  [link] lookup still running after {budget:.0f}s - phones open "
                  f"{clean[:80]} directly (Facebook Lite resolves it)")
            resolved.append(clean)
        except Exception:
            resolved.append(normalize_url(item.get("url", "")))
    out = []
    for item, after in zip(items, resolved):
        before = item.get("url", "")
        if item.get("phone_ready"):
            if after:
                out.append(item)
            continue
        if not after:
            print(f"  [link] rejected non-Facebook or invalid URL: {before!r}")
            continue
        kind = _target_kind(after)
        if kind == "story":
            # A story's only box is a PRIVATE reply (it goes to Messenger). The
            # composer check would take it for a comment box and send a DM.
            report_error("LINK_NOT_COMMENTABLE", url=after, severity="warning")
            continue
        if kind is None:
            report_error("LINK_NOT_SINGLE_CONTENT", url=after, severity="warning")
        item["url"] = after
        item["phone_ready"] = True
        if after != before:
            print(f"  [link] normalized for phones -> {after[:100]}")
        print(f"  [link] {kind or 'unknown'}: comment box is "
              f"{_COMMENT_PLACE.get(kind, 'found on the post view')}")
        out.append(item)
    return out


# Where the run looks for the comment box, per link kind (for the run log).
_COMMENT_PLACE = {
    "reel": "behind the reel player's Comment button (2nd on the right rail)",
    "video": "behind the reel player's Comment button (2nd on the right rail)",
    "post": "at the bottom of the post, or behind its Comment button",
    "photo": "at the bottom of the post, or behind its Comment button",
}


def react_all_saved_accounts(serial, url_or_targets, reaction="like",
                             comment_example="", do_react=True, do_comment=False,
                             take_screenshots=False):
    """Public entry point: guarantees Facebook Lite is closed on Abort,
    whichever path the inner loop exits through.

    `url_or_targets` is either a single URL (legacy call) or a list of target
    dicts from make_target() — the mass-links mode. Each saved account handles
    EVERY link before the run switches to the next account, because switching
    accounts is by far the slowest and most failure-prone step: doing it once
    per account instead of once per account-per-link cuts the switch count by
    the number of links.
    """
    if isinstance(url_or_targets, (list, tuple)):
        targets = [dict(t) for t in url_or_targets]
    else:
        targets = [make_target(url_or_targets, reaction, comment_example,
                               do_react, do_comment)]
    # GUI runs normalize this list once before fan-out; direct/CLI callers land
    # here instead. The operation is idempotent and redirect results are cached.
    targets = normalize_targets_for_phones(targets)
    targets = [t for t in targets if t.get("url")]
    if not targets:
        report_error("INVALID_URL", serial)
        return ["invalid-url"]
    try:
        results = _react_all_saved_accounts(serial, targets, take_screenshots)
    except Exception as e:
        report_error("DEVICE_WORKER_FAILED", serial, exc=e)
        if stop_requested():
            close_fb(serial)
        raise
    # Close Facebook Lite on the way out either way: on Abort, and just the same
    # once every account has been processed — the run is finished, so the app
    # should not be left sitting open on the phone.
    if stop_requested():
        print(f"    [{serial}] aborted - closing Facebook Lite.")
    elif any(str(r) == "all-accounts-done" for r in (results or [])):
        print(f"    [{serial}] all accounts done - closing Facebook Lite.")
    else:
        print(f"    [{serial}] finished - closing Facebook Lite.")
    close_fb(serial)
    return results

def _react_all_saved_accounts(serial, targets, take_screenshots=False):
    url = targets[0]["url"]              # the first link gates the account check
    set_current_post_url(url)
    n_links = len(targets)
    d = connect_resilient(serial)
    if d is None:
        if stop_requested():
            return ["stopped"]
        if device_state(serial) == "unauthorized":
            report_error("DEVICE_UNAUTHORIZED", serial)
            return ["unauthorized"]
        report_error("DEVICE_CONNECT_FAILED", serial)
        return ["u2-connect-failed"]

    wake_and_unlock(serial)
    run(["shell", "svc", "power", "stayon", "true", "usb"], serial=serial)
    # A phone box plugged in moments ago can still be showing MIUI's
    # "Use USB for" over everything.
    try:
        dismiss_system_popup(serial)
    except Exception:
        pass
    # A USB-internet VPN left by a crashed or killed session keeps the phone
    # "connected" with no way out, so no post could ever load.
    try:
        stop_stale_usb_vpn(serial)
    except Exception:
        pass

    results        = RunResults()
    used_accounts  = set()
    switch_state   = {}          # carries the lazy switch check
    account_num    = 1
    transient_errors = 0
    no_gate_accounts = 0         # accounts in a row that could not open a link
    # Name the accounts BEFORE any work, so a missed one can be reported by
    # name at the end instead of "all accounts done" hiding a silent skip.
    # An empty roster is not fatal - it just means coverage cannot be checked,
    # and the run behaves exactly as it did before.
    # The roster fills in as the switcher walks (see switch_to_next_account's
    # roster_out). enumerate_accounts() still exists for a deliberate audit,
    # but calling it here cost an extra switcher open - 34s per phone - before
    # the first post was even opened, and the walk sees the same names anyway.
    roster = []
    current_account = FIRST_ACCOUNT_PLACEHOLDER
    coverage_retried = False     # the one extra sweep over accounts that missed
    # Hard ceiling. No phone has anywhere near this many logins, so if the
    # account loop ever stops terminating on its own this stops the run instead
    # of grinding for hours. It has happened: a re-queue path that could fire
    # repeatedly kept a 7-account phone going past "account 154".
    MAX_ACCOUNTS = 60

    while True:
        if account_num > MAX_ACCOUNTS:
            print(f"[{serial}] SAFETY STOP: passed {MAX_ACCOUNTS} accounts on a "
                  f"phone that cannot have that many - ending this device.")
            results.append("safety-stop-too-many-accounts")
            break
        if stop_requested():
            results.append("stopped")
            print(f"[{serial}] stop requested - halting before account {account_num}.")
            break
        d = ensure_device(serial, d)
        if stop_requested():
            results.append("stopped")
            break

        # ── The gate: open a link so this account's state can be read ──
        #
        # This was an unbounded `while True` on targets[0], and that is how a
        # phone spent 3.5 hours reopening ONE reel 2536 times without posting a
        # single comment: link 1 stopped verifying and nothing here ever gave
        # up. The run looked like "it stops commenting at 150 or 300" because
        # 150 is one account across the whole fleet - the hang starts at the
        # first account SWITCH, when this loop runs for the second time.
        #
        # Bounded now, and when the first link will not open the next ones are
        # tried as the gate instead, so one bad link cannot take a phone - or a
        # whole run - down with it.
        GATE_ATTEMPTS = 2
        GATE_LINKS    = 3
        gate_link_no  = 0            # which link the gate actually left open
        gate_blocked  = False
        for gate_no, gate_tgt in enumerate(targets[:GATE_LINKS], start=1):
            gate_url = gate_tgt["url"]
            for gate_try in range(GATE_ATTEMPTS):
                if stop_requested():
                    results.append("stopped")
                    return results
                try:
                    wake_and_unlock(serial)
                    # Point the thread at the link we are ABOUT to open.
                    # _is_reel_context() reads this, and the reel rail is only
                    # believed when it agrees - but it was set ONCE, at the top
                    # of this function, so from the second account onwards it
                    # still named the LAST link of the previous pass and every
                    # reel opened here verified as "not on the target post".
                    # A/B on one live screen, same phone, same second: stale URL
                    # -> on_post_view False, this URL -> True.
                    set_current_post_url(gate_url)
                    print(f"[{serial}] opening post for account {account_num}: {gate_url}")
                    dbg(serial, "open-link", f"account {account_num} -> {gate_url}")
                    task_log(serial, "open-link",
                             f"account {account_num} -> {_short_target(gate_url)}")
                    # Compose the comment while the post opens (no-op when
                    # this link's comment is already pending).
                    prefetch_comment(gate_tgt)
                    # Verified open: lands on the post itself, not the feed.
                    if open_target_post(serial, d, gate_url):
                        gate_link_no = gate_no
                        break
                    if wait_for_account_block(d, timeout=0):
                        gate_blocked = True
                        break      # login/suspension screen — handled below
                except Exception as e:
                    report_error("TARGET_OPEN_FAILED", serial, account_num=account_num,
                                 link_no=gate_no, url=gate_url, exc=e)
                if stop_requested():
                    results.append("stopped")
                    return results
                print(f"    [{serial}] open post failed "
                      f"({gate_try + 1}/{GATE_ATTEMPTS}), retrying in 5s...")
                if not abort_sleep(5):
                    results.append("stopped")
                    return results
            if gate_link_no or gate_blocked:
                break
            print(f"    [{serial}] link {gate_no} will not open for account "
                  f"{account_num} - trying the next link as the gate")
        if gate_link_no or gate_blocked:
            no_gate_accounts = 0
        else:
            # Nothing opened. Fall THROUGH rather than sitting here: the
            # per-link loop below has its own cap and will either find a link
            # that works or skip them all and move on to the next account.
            no_gate_accounts += 1
            results.append("gate-open-failed")
            report_error("TARGET_OPEN_FAILED", serial, account_num=account_num,
                         detail="No candidate link could be opened for this account.")
            print(f"    [{serial}] no link opened for account {account_num} "
                  f"({no_gate_accounts} account(s) in a row)")
            if no_gate_accounts >= 3:
                print(f"[{serial}] SAFETY STOP: 3 accounts in a row could not "
                      f"open a single link - ending this device.")
                results.append("safety-stop-no-link-opens")
                break

        # A verified post on screen already proves this account is logged in
        # and not suspended, so one look is enough. Watching a healthy account
        # for 6 s - it can never return early - was 6 s of nothing after every
        # account's first link opened. Only a gate that opened NOTHING waits.
        status = wait_for_account_block(d, timeout=0 if gate_link_no else 6)
        if status == "not-successful":
            results.append(status)
            report_error("ACCOUNT_LOGIN_REQUIRED", serial, account_num=account_num)
            print(f"    [!] {serial} {status}")
            for _ in range(10):
                switched, switched_name, switch_status = switch_to_next_account(
                    serial, d, used_accounts, switch_state, roster_out=roster)
                if switched:
                    prev = switch_state.get("was_current")
                    if prev and current_account == FIRST_ACCOUNT_PLACEHOLDER:
                        results.rename_account(FIRST_ACCOUNT_PLACEHOLDER, prev)
                        _remember_account(roster, prev)
                    current_account = switched_name or current_account
                    _remember_account(roster, switched_name)
                    transient_errors = 0
                    account_num += 1
                    break
                print(f"    [{serial}] switch failed ({switch_status}), retrying...")
                time.sleep(3)
            else:
                results.append(switch_status)
                print(f"    [{serial}] {switch_status} - no more accounts")
                break
            continue
        if status == "suspended":
            results.append(status)
            report_error("ACCOUNT_SUSPENDED", serial, account_num=account_num)
            print(f"    [!]{serial} {status}")
            logged_out    = logout_current_account(serial, d)
            logout_status = ("suspended-logged-out" if logged_out
                             else "suspended-logout-failed")
            results.append(logout_status)
            print(f"    [{serial}] {logout_status}")
            if logged_out:
                rows     = login_screen_account_rows(d)
                switched = False
                switch_status = "no-available-account"
                for row in rows:
                    name = row["name"]
                    if name.lower() in used_accounts:
                        continue
                    print(f"    [{serial}] logging into > {name}")
                    d.click(*row["point"])
                    time.sleep(5)
                    if chooser_present(serial, d):
                        dismiss_chooser(serial, d)
                        time.sleep(2)
                    block_status = wait_for_account_block(d, timeout=8)
                    if block_status:
                        results.append(f"{block_status}:{name}")
                        print(f"    [{serial}] {name}: {block_status}")
                        used_accounts.add(name.lower())
                        if block_status == "suspended":
                            logged_out = logout_current_account(serial, d)
                            if not logged_out:
                                switch_status = "suspended-logout-failed"
                                break
                            continue
                        switch_status = block_status
                        continue
                    used_accounts.add(name.lower())
                    switched      = True
                    switch_status = "switched"
                    break
                if switched:
                    transient_errors = 0
                    account_num += 1
                    continue
                else:
                    results.append(switch_status)
                    print(f"    [{serial}] {switch_status}")
                    break
            else:
                break
        else:
            # ── This account works through EVERY link, one by one ──
            for link_no, tgt in enumerate(targets, start=1):
                if stop_requested():
                    results.append("stopped")
                    return results
                # Point this thread at the link we are ABOUT to open, before
                # opening it. _is_reel_context() reads this, and the reel rail
                # is only accepted when it agrees - so while it still named the
                # PREVIOUS link, every reel after link 1 verified as "not on the
                # target post" and burned 3 cold reopens x 5 retries before the
                # link was abandoned. _process_target sets it again, harmlessly.
                set_current_post_url(tgt["url"])
                # Compose this link's comment while the phone opens it; the
                # gate link already has one pending, so this reuses it.
                prefetch_comment(tgt)
                if link_no != gate_link_no:
                    # the gate left ONE link on screen (0 = none opened); every
                    # other link still has to be opened here
                    opened = False
                    for _ in range(5):
                        if stop_requested():
                            results.append("stopped")
                            return results
                        try:
                            if open_target_post(serial, d, tgt["url"]):
                                opened = True
                                break
                        except Exception as e:
                            report_error("TARGET_OPEN_FAILED", serial, account_num=account_num,
                                         link_no=link_no, url=tgt["url"], exc=e)
                        print(f"    [{serial}] link {link_no}: open failed, retrying...")
                        if not abort_sleep(3):
                            results.append("stopped")
                            return results
                    if not opened:
                        results.append(f"link{link_no}-open-failed")
                        _tally(results, link_no, tgt["url"], "failed")
                        report_error("TARGET_OPEN_FAILED", serial, account_num=account_num,
                                     link_no=link_no, url=tgt["url"])
                        print(f"    [{serial}] link {link_no}: could not open - skipping")
                        # A logged-out or checkpointed account fails EVERY link
                        # the same way, and each try costs ~75s - ten links is
                        # over an hour of a phone doing nothing before the
                        # switch it needed. Ask once, here.
                        if wait_for_account_block(d, timeout=0):
                            print(f"    [{serial}] account {account_num} is on a "
                                  f"login/checkpoint screen - skipping its "
                                  f"remaining links")
                            break
                        continue
                outcome = _process_target(serial, d, tgt, account_num, link_no,
                                          n_links, take_screenshots, results,
                                          account_name=current_account)
                if outcome == "abort":
                    return results
                if outcome == "blocked":
                    # rate-limited: its other links would fail the same way
                    print(f"    [{serial}] account {account_num} is action-blocked "
                          f"- skipping its remaining links")
                    break
                if link_no < n_links and not pace(PACE_LINK, "before the next link"):
                    results.append("stopped")
                    return results

            # ── Switch to next account ──
            # Breathe before hopping accounts; back-to-back switching is the
            # fastest way to get every account on the phone throttled.
            if not pace(PACE_ACCOUNT, "before switching account"):
                results.append("stopped")
                return results
            switched = False
            for _ in range(3):
                if stop_requested():
                    results.append("stopped")
                    return results
                switched, switched_name, switch_status = switch_to_next_account(
                    serial, d, used_accounts, switch_state, roster_out=roster)
                if switched:
                    prev = switch_state.get("was_current")
                    if prev and current_account == FIRST_ACCOUNT_PLACEHOLDER:
                        results.rename_account(FIRST_ACCOUNT_PLACEHOLDER, prev)
                        _remember_account(roster, prev)
                    current_account = switched_name or current_account
                    _remember_account(roster, switched_name)
                    break
                # "no more accounts" is a final answer, not a transient glitch —
                # retrying it just burns ~9s at the end of every device run.
                if switch_status in ("no-available-account", "stopped"):
                    # ...unless accounts on the roster never actually
                    # commented. Put those back in the pool ONCE and let the
                    # normal loop walk them again - clearing them here is
                    # enough, because the next switch then finds them unused
                    # and the run continues account by account as usual.
                    if (switch_status == "no-available-account"
                            and not coverage_retried):
                        retry = _accounts_to_retry(roster, results, targets)
                        if retry:
                            coverage_retried = True
                            print(f"    [{serial}] coverage pass: "
                                  f"{len(retry)} account(s) have not commented "
                                  f"- one more try: {', '.join(retry)}")
                            dbg(serial, "coverage-retry", ", ".join(retry))
                            for miss in retry:
                                used_accounts.discard(miss.lower())
                            continue
                    break
                print(f"    [{serial}] switch failed ({switch_status}), retrying...")
                if not abort_sleep(3):
                    results.append("stopped")
                    return results
            if not switched:
                # Every logged-in account has now been handled on this post.
                # That is a successful finish, not a failure.
                if switch_status == "no-available-account":
                    results.append("all-accounts-done")
                    print(f"    [{serial}] all accounts done "
                          f"({account_num} processed) - finishing up.")
                else:
                    results.append(switch_status)
                    report_error("ACCOUNT_SWITCH_FAILED", serial, account_num=account_num,
                                 detail=switch_status)
                    print(f"    [{serial}] {switch_status} - no more accounts")
                break
            transient_errors = 0
            account_num += 1
    _report_account_coverage(serial, roster, results, targets)
    return results


def _remember_account(roster, name):
    """Keep the roster a superset of every account actually seen.

    Enumeration runs once at the start and can still come up short - the sheet
    scrolls awkwardly, or an account renders late. The switcher discovers names
    as it walks, so folding those back in means coverage is measured against
    everything the phone really has rather than only what the first scroll
    caught.
    """
    if name and name.lower() not in {r.lower() for r in roster}:
        roster.append(name)
    return roster


def _accounts_to_retry(roster, results, targets):
    """Roster accounts that were asked to comment and have not managed it.

    An account is only "done" once its comment actually landed. used_accounts
    marks an account the moment the switcher shows it, which is BEFORE any
    comment is attempted - so without this a swallowed tap or a missing
    comment box retired the account silently and coverage was never met.
    """
    if not isinstance(results, RunResults) or not roster:
        return []
    wanted = [n for n, t in enumerate(targets or [], 1) if t.get("do_comment")]
    if not wanted:
        return []
    missed = []
    for name in roster:
        for link_no in wanted:
            row = results.account_tally.get((name, link_no))
            if not row or row.get("commented") not in ("ok", "comment-submit-unverified"):
                missed.append(name)
                break
    return missed


def _report_account_coverage(serial, roster, results, targets):
    """Say, by name, which accounts commented and which did not.

    Only meaningful when a comment was actually asked for and the roster is
    known; a react-only run has nothing to be missing. Reported per link,
    because an account can land on one link and miss another.
    """
    if not isinstance(results, RunResults) or not roster:
        return
    wanted = [(n, t) for n, t in enumerate(targets or [], 1) if t.get("do_comment")]
    if not wanted:
        return
    for link_no, tgt in wanted:
        done, missed = [], []
        for name in roster:
            row = results.account_tally.get((name, link_no))
            if row and row.get("commented") == "ok":
                done.append(name)
            else:
                reason = (row or {}).get("commented") or "never-attempted"
                missed.append(f"{name} ({reason})")
        line = (f"link {link_no}: {len(done)}/{len(roster)} accounts commented")
        print(f"  [coverage] [{serial}] {line}")
        dbg(serial, "coverage", line)
        if missed:
            miss_line = "MISSED -> " + "; ".join(missed)
            print(f"  [coverage] [{serial}] {miss_line}")
            dbg(serial, "coverage", miss_line)


def cli_main():
    # Original CLI mode (called from main.py if --cli)
    url = normalize_url(input("Post URL to react to: "))
    if not url:
        print("No URL given, aborting.")
        return
    mode = input("Action - (r)eact, (c)omment, or (b)oth / react+comment? [r]: ")
    do_react, do_comment = parse_action_mode(mode)
    default = ""
    if do_react:
        default = input(
            "Default reaction for all (like/love/care/haha/wow/sad/angry), "
            "or blank to choose per phone: ").strip().lower()
        if default and default not in REACTIONS:
            print(f"  ignoring invalid default '{default}', will ask per phone")
            default = ""
    comment_example = ""
    if do_comment:
        comment_example = input(
            "Paste an example comment (each account posts a unique variation): ").strip()
        if not comment_example:
            print("No example comment given, disabling comments.")
            do_comment = False
    if not do_react and not do_comment:
        print("Nothing to do, aborting.")
        return
    if do_comment and not start_llama():
        print("Could not reach Pollinations AI, disabling comments.")
        do_comment = False
        if not do_react:
            return
    take_screenshots = input("Take screenshots after each action? (y/n): ").strip().lower() == 'y'
    devices = ensure_online()
    if not devices:
        print("No phones detected. Check cables, USB mode (File transfer), and USB debugging.")
        return
    ready = [s for s, st in devices.items() if st == "device"]
    apk   = find_apk()
    results = {}
    for serial, st in devices.items():
        if st != "device":
            print_unready_help(serial, st)
            results[serial] = st
    reset_comment_pool()
    device_reactions = {}
    for serial in ready:
        if do_react:
            device_reactions[serial] = default if default else ask_reaction(f"Reaction for {serial}: ")
        else:
            device_reactions[serial] = ""
    for serial in ready:
        wake_and_unlock(serial)
    results_lock = threading.Lock()
    def run_device(serial):
        try:
            res = react_all_saved_accounts(
                serial, url, device_reactions.get(serial, ""),
                comment_example, do_react, do_comment, take_screenshots)
            with results_lock:
                results[serial] = res
        except Exception as e:
            with results_lock:
                results[serial] = f"thread-error:{e}"
    threads = []
    for serial in ready:
        if not has_fb(serial):
            if apk and install_fb(serial, apk):
                pass
            elif apk:
                results[serial] = "install-failed"
                continue
            else:
                results[serial] = "missing-app-no-apk"
                print(f"  [!] {serial} Facebook Lite not installed and no APK found in {HERE}")
                continue
        t = threading.Thread(target=run_device, args=(serial,), daemon=True)
        t.start()
        threads.append((serial, t))
    for serial, t in threads:
        t.join()
    stop_llama()
    print("Summary:")
    reacted  = 0
    commented = 0
    for serial, status in results.items():
        statuses  = status if isinstance(status, list) else [status]
        reacted  += sum(1 for s in statuses if str(s).startswith("reacted:"))
        commented += sum(1 for s in statuses if str(s).startswith("commented:"))
        print(f"  {serial}: {', '.join(map(str, statuses))}")
    print(f"{reacted} reaction(s), {commented} comment(s) across {len(devices)} phone(s).")
