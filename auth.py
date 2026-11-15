# fbPoison/auth.py
# Access gate + denial sequence.
#
# NOTE ON "ENCRYPTION": this is a local gate, not real security. Anyone holding
# these files can read them or import core.py directly. What the layering below
# actually buys is that the password itself never appears in the source and the
# stored constant is not a recognisable digest — casual inspection gives nothing.
#
#   layer 1  SHA-256(salt || password)
#   layer 2  HMAC-SHA512(pepper, layer1)
#   layer 3  PBKDF2-HMAC-SHA256(layer2, salt, 120_000 iterations)
#   stored   XOR-masked + base64 of layer3, compared in constant time
#
# Wrong key -> INCORRECT -> binary flood -> 3D skull built out of the binary ->
# jumpscare -> verdict -> the process exits.

import base64
import hashlib
import hmac
import math
import os
import random
import sys

from PyQt6.QtWidgets import (QWidget, QLineEdit, QVBoxLayout, QHBoxLayout,
                             QLabel, QApplication)
from PyQt6.QtCore import Qt, QTimer, QEventLoop, QRectF, QPointF, QSize
from PyQt6.QtGui import (QPainter, QColor, QFont, QPen, QBrush, QPainterPath,
                         QPolygonF, QLinearGradient, QRadialGradient, QPixmap)

from gui import (C_BG, C_PANEL, C_TOXIC, C_TOXIC2, C_VENOM, C_BLOOD, C_TEXT,
                 C_MUTE, C_FAINT, C_BORDER, C_EDGE, C_INPUT, F_UI, F_MONO,
                 _qc, _lerp_hex, HazardButton, rot3, project, _smooth)

# ── layered key check ──────────────────────────────────────────────────────
_SALT   = b"fbpoison::biotoxin::dispersion::v1"
_PEPPER = b"G0Ju.VBS::engagement-toxin"
_MASK   = bytes((0x5B, 0xA7, 0x31, 0xE9, 0x0C, 0x74, 0xD2, 0x8F,
                 0x16, 0xC3, 0x4A, 0x9D, 0x2E, 0xB8, 0x60, 0xF5))
_ITERS  = 120_000

# XOR-masked + base64 digest of the authorised key. Not a recognisable hash;
# to change the key, run:  python auth.py --seal
_SEALED = "Av57aGQRqDhqb8qR9THLaDrqnA9Z1Ri4IVtTbLb6A7o="


def _mask(raw: bytes) -> str:
    out = bytes(b ^ _MASK[i % len(_MASK)] for i, b in enumerate(raw))
    return base64.b64encode(out).decode("ascii")


def _unmask(blob: str) -> bytes:
    raw = base64.b64decode(blob.encode("ascii"))
    return bytes(b ^ _MASK[i % len(_MASK)] for i, b in enumerate(raw))


def _digest(password: str) -> bytes:
    """Three chained one-way layers."""
    pw = password.encode("utf-8")
    l1 = hashlib.sha256(_SALT + pw).digest()
    l2 = hmac.new(_PEPPER, l1, hashlib.sha512).digest()
    l3 = hashlib.pbkdf2_hmac("sha256", l2, _SALT, _ITERS)
    return l3


def verify_key(password: str) -> bool:
    """Constant-time comparison against the sealed digest."""
    try:
        return hmac.compare_digest(_digest(password or ""), _unmask(_SEALED))
    except Exception:
        return False


# ── 3D skull point cloud ───────────────────────────────────────────────────
def _bone(x, y):
    """Is (x, y) inside the skull silhouette? y grows downward."""
    cran = (x * x) / (0.78 ** 2) + ((y + 0.20) ** 2) / (0.74 ** 2) <= 1.0
    jaw = False
    if 0.28 < y < 0.88:
        half = 0.54 - 0.33 * ((y - 0.28) / 0.60)
        jaw = abs(x) <= half
    if not (cran or jaw):
        return False
    # eye sockets
    for sx in (-0.335, 0.335):
        if ((x - sx) ** 2) / (0.225 ** 2) + ((y + 0.01) ** 2) / (0.205 ** 2) <= 1.0:
            return False
    # nasal cavity — inverted triangle
    if 0.11 < y < 0.35 and abs(x) <= 0.025 + 0.135 * ((y - 0.11) / 0.24):
        return False
    # teeth: vertical gaps + the gum line
    if 0.42 < y < 0.66 and abs(x) < 0.45:
        if 0.535 < y < 0.565:
            return False
        if (int((x + 1.0) / 0.058) % 2) == 0:
            return False
    return True


def build_skull(step=0.021):
    """Skull as a 3D point cloud: the silhouette above, given volume by a
    dome-shaped depth so it stays a skull from any rotation."""
    pts = []
    y = -0.96
    while y < 0.90:
        x = -0.96
        while x < 0.96:
            if _bone(x, y):
                q = 1.0 - (x / 0.88) ** 2 - ((y + 0.10) / 1.00) ** 2
                d = 0.60 * math.sqrt(q) if q > 0 else 0.06
                pts.append((x, y, d))
                if d > 0.14:
                    pts.append((x, y, -d * 0.72))      # back shell
            x += step
        y += step
    return pts


SKULL = build_skull()


def _thin(points, target):
    if len(points) <= target:
        return list(points)
    stepf = len(points) / float(target)
    return [points[int(i * stepf)] for i in range(target)]


SKULL_ANIM = _thin(SKULL, 950)           # glyph budget for the assembly


# ── Horror audio (synthesised, no external files) ──────────────────────────
def _synth_scare_wav(seconds=2.6, rate=22050):
    """A loopable dread drone: sub-bass rumble + detuned dissonance + noise
    swells + an intermittent screech. Returned as in-memory WAV bytes."""
    import struct
    n = int(seconds * rate)
    frames = bytearray()
    for i in range(n):
        tt = i / rate
        env = 0.5 + 0.5 * math.sin(2 * math.pi * tt / seconds - math.pi / 2)
        sub = math.sin(2 * math.pi * 46 * tt) * 0.55
        # detuned pair -> slow beating, very uneasy
        dis = (math.sin(2 * math.pi * 196 * tt) +
               math.sin(2 * math.pi * 203.5 * tt)) * 0.16
        swell = (random.random() * 2 - 1) * 0.12 * env
        screech = 0.0
        if (tt % 1.3) > 1.02:
            screech = math.sin(2 * math.pi * (900 + 500 * math.sin(tt * 30)) * tt) * 0.10
        s = (sub * (0.6 + 0.4 * env) + dis + swell + screech)
        s = max(-1.0, min(1.0, s * 0.85))
        frames += struct.pack("<h", int(s * 32767))
    data = bytes(frames)
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" +
           b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) +
           b"data" + struct.pack("<I", len(data)))
    return hdr + data


# ── "Daisy Bell" (Harry Dacre, 1892 — public domain) ──────────────────────
# Synthesised, never downloaded. The horror is the HAL 9000 treatment: the same
# phrase repeating on a music box while the pitch sags, the tuning drifts apart
# and the tempo drags, as if something is dying while it sings to you.
#
# "Dai-sy,  Dai-sy,  give me your ans-wer do"   (semitones from C4, beats)
_DAISY_PHRASE = [
    (12, 1.0), (9, 1.0), (5, 1.0), (0, 1.0),
    (2, 0.5), (4, 0.5), (5, 0.5), (2, 0.5),
    (0, 2.0),
]


def _synth_daisy_wav(rate=22050, passes=2, beat=0.56):
    """A music-box Daisy Bell that decays into something wrong."""
    import struct
    events = []
    for k in range(passes):
        for semi, beats in _DAISY_PHRASE:
            # each pass drags and sinks further
            events.append((semi, beats * beat * (1.0 + 0.22 * k)))
    total = sum(d for _, d in events)
    frames = bytearray()
    elapsed = 0.0
    for semi, dur in events:
        n = int(dur * rate)
        prog = elapsed / max(0.001, total)          # 0 -> 1 across the whole song
        # the machine winding down: pitch sags, wow/flutter deepens
        sag = 1.0 - 0.20 * prog
        base = 261.63 * (2.0 ** (semi / 12.0)) * sag
        detune = 1.0 + 0.004 + 0.012 * prog          # tuning drifts apart
        for i in range(n):
            tt = i / rate
            wow = 1.0 + (0.004 + 0.012 * prog) * math.sin(2 * math.pi * 1.7 * (elapsed + tt))
            f = base * wow
            # music-box: bright partials, fast exponential decay
            env = math.exp(-tt * (4.2 - 1.8 * prog))
            s = (math.sin(2 * math.pi * f * tt) * 0.55 +
                 math.sin(2 * math.pi * f * 2 * tt) * 0.22 +
                 math.sin(2 * math.pi * f * 3.01 * tt) * 0.10)
            # detuned twin -> slow beating, sounds haunted
            s += math.sin(2 * math.pi * f * detune * tt) * 0.30
            s *= env
            # tape hiss + a low breath underneath
            s += (random.random() * 2 - 1) * (0.012 + 0.03 * prog)
            s += math.sin(2 * math.pi * 47 * (elapsed + tt)) * 0.10 * prog
            s = max(-1.0, min(1.0, s * 0.62))
            frames += struct.pack("<h", int(s * 32767))
        elapsed += dur
    data = bytes(frames)
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" +
           b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) +
           b"data" + struct.pack("<I", len(data)))
    return hdr + data


_DAISY_WAV = None


def prewarm_audio():
    """Render the song ahead of time so its phase starts without a hitch."""
    def go():
        global _DAISY_WAV, _SCARE_WAV, _DAISY_PATH, _SCARE_PATH
        try:
            if _DAISY_PATH is None:
                _DAISY_WAV = _DAISY_WAV or _synth_daisy_wav()
                _DAISY_PATH = _wav_to_temp(_DAISY_WAV, "fbp_daisy.wav")
            if _SCARE_PATH is None:
                _SCARE_WAV = _SCARE_WAV or _synth_scare_wav()
                _SCARE_PATH = _wav_to_temp(_SCARE_WAV, "fbp_dread.wav")
        except Exception as e:
            _audio_fail("prewarm", e)
        # warm the .NET speech assembly so the verdict starts talking promptly
        try:
            if sys.platform == "win32":
                import subprocess
                subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Add-Type -AssemblyName System.Speech"],
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               timeout=25)
        except Exception as e:
            _audio_fail("tts-warm", e)
    import threading
    threading.Thread(target=go, daemon=True).start()


AUDIO_DEBUG = False          # set True to see why a sound failed


def _audio_fail(where, exc):
    if AUDIO_DEBUG:
        sys.stderr.write(f"[audio] {where}: {exc!r}\n")


def _wav_to_temp(data, name):
    """winsound CANNOT play asynchronously from memory — SND_MEMORY|SND_ASYNC
    raises 'Cannot play asynchronously from memory', which is why the song and
    the drone were silently doing nothing. Async + looping only works from a
    file, so spill the rendered WAV to the temp dir and play that."""
    import tempfile
    path = os.path.join(tempfile.gettempdir(), name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


_DAISY_PATH = None


def start_daisy_song():
    global _DAISY_WAV, _DAISY_PATH
    try:
        if sys.platform != "win32":
            return
        import winsound
        if _DAISY_PATH is None:
            if _DAISY_WAV is None:
                _DAISY_WAV = _synth_daisy_wav()
            _DAISY_PATH = _wav_to_temp(_DAISY_WAV, "fbp_daisy.wav")
        winsound.PlaySound(_DAISY_PATH, winsound.SND_FILENAME |
                           winsound.SND_ASYNC | winsound.SND_LOOP)
    except Exception as e:
        _audio_fail("daisy", e)


_SCARE_WAV = None
_SCARE_PATH = None


def start_scare_sound():
    global _SCARE_WAV, _SCARE_PATH
    try:
        if sys.platform != "win32":
            return
        import winsound
        if _SCARE_PATH is None:
            if _SCARE_WAV is None:
                _SCARE_WAV = _synth_scare_wav()
            _SCARE_PATH = _wav_to_temp(_SCARE_WAV, "fbp_dread.wav")
        winsound.PlaySound(_SCARE_PATH, winsound.SND_FILENAME |
                           winsound.SND_ASYNC | winsound.SND_LOOP)
    except Exception as e:
        _audio_fail("dread", e)


def stop_scare_sound():
    try:
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(None, 0)
    except Exception as e:
        _audio_fail("stop", e)


def speak_verdict(seconds=10.0):
    """Free-speaking TTS looping the verdict for ~`seconds`, then returns.

    Returns a threading.Event that is set once the voice has actually finished,
    so the caller can avoid exiting mid-sentence.

    Timing notes (measured on this machine): PowerShell + System.Speech starts
    in ~0.24s, and one pass of both lines takes ~6.3s. The old loop tested its
    deadline once per PAIR, so it would start a whole 6.3s pass at t=9.9s and
    run to ~12.4s. We now test before EVERY line and stop opening new ones once
    less than a line's worth of time remains, which lands the speech close to
    the requested duration instead of overshooting by 25%.
    """
    done = None
    try:
        import threading
        done = threading.Event()
    except Exception:
        return None

    budget = max(1.0, seconds - 2.6)      # room for the final line to finish

    def go():
        try:
            if sys.platform == "win32":
                import subprocess
                ps = (
                    "Add-Type -AssemblyName System.Speech;"
                    "$v = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                    "$v.Rate = -1; $v.Volume = 100;"
                    "$lines = @('YOUR PASSWORD IS WRONG','YOU ARE NOT G0Ju.VBS');"
                    f"$end = (Get-Date).AddSeconds({budget:.1f});"
                    "$i = 0;"
                    "while ((Get-Date) -lt $end) {"
                    "  $v.Speak($lines[$i % 2]); $i++ }")
                subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               timeout=seconds + 12)
            else:
                sys.stdout.write("\a"); sys.stdout.flush()
        except Exception as e:
            _audio_fail("tts", e)
        finally:
            done.set()

    import threading
    threading.Thread(target=go, daemon=True).start()
    return done


# ── Procedural uncanny / scary face ────────────────────────────────────────
def _fc(r, g, b, a=255):
    return QColor(max(0, min(255, int(r))), max(0, min(255, int(g))),
                  max(0, min(255, int(b))), max(0, min(255, int(a))))


def draw_face(p, cx, cy, w, h, dread, t=0.0, seed=7):
    """Draw an uncanny (dread≈0.3) → scary (dread≈1.0) human face.

    If uncanny1.png / uncanny2.png exist next to this file they are used
    instead; otherwise everything is drawn procedurally so the app needs no
    downloaded assets.
    """
    img = _face_image(dread)
    if img is not None:
        jit = dread * dread * 5.0
        ox = random.uniform(-jit, jit)
        oy = random.uniform(-jit, jit)
        fw = w * 2.4
        fh = fw * img.height() / max(1, img.width())
        p.drawPixmap(QRectF(cx - fw / 2 + ox, cy - fh * 0.45 + oy, fw, fh),
                     img, QRectF(0, 0, img.width(), img.height()))
        return

    rng = random.Random(int(t * 20) * 7 + seed) if dread > 0.85 else random.Random(seed)
    jit = dread * dread * 4.0
    cx += rng.uniform(-jit, jit)
    cy += rng.uniform(-jit, jit)
    p.setPen(Qt.PenStyle.NoPen)

    # neck + shoulders
    torso = QPainterPath()
    torso.moveTo(cx - w * 0.34, cy + h * 0.55)
    torso.lineTo(cx - w * 1.5, cy + h * 1.9)
    torso.lineTo(cx + w * 1.5, cy + h * 1.9)
    torso.lineTo(cx + w * 0.34, cy + h * 0.55)
    tg = QLinearGradient(0, cy + h * 0.5, 0, cy + h * 1.9)
    tg.setColorAt(0, _fc(28 - 10 * dread, 26, 30))
    tg.setColorAt(1, _fc(6, 6, 8))
    p.fillPath(torso, QBrush(tg))

    skin_hi = _fc(236 - 96 * dread, 226 - 120 * dread, 214 - 118 * dread)
    skin_mid = _fc(196 - 96 * dread, 184 - 110 * dread, 172 - 108 * dread)
    skin_lo = _fc(120 - 66 * dread, 110 - 74 * dread, 104 - 74 * dread)
    head = QPainterPath()
    head.addEllipse(QRectF(cx - w, cy - h, 2 * w, 2 * h * 1.02))
    jaw = QPainterPath()
    jaw.moveTo(cx - w * 0.92, cy + h * 0.05)
    jaw.quadTo(cx - w * 0.5, cy + h * 1.02, cx, cy + h * 1.06)
    jaw.quadTo(cx + w * 0.5, cy + h * 1.02, cx + w * 0.92, cy + h * 0.05)
    jaw.closeSubpath()
    head = head.united(jaw)
    g = QRadialGradient(cx - 0.22 * w, cy - 0.34 * h, 1.7 * w)
    g.setColorAt(0.0, skin_hi)
    g.setColorAt(0.6, skin_mid)
    g.setColorAt(1.0, skin_lo)
    p.fillPath(head, QBrush(g))
    for sx in (-1, 1):
        hol = QRadialGradient(cx + sx * 0.6 * w, cy + 0.28 * h, 0.55 * w)
        hol.setColorAt(0.0, _fc(40, 30, 34, int(120 * dread)))
        hol.setColorAt(1.0, _fc(0, 0, 0, 0))
        p.fillPath(head, QBrush(hol))

    p.save()
    p.setClipPath(head)

    hair = QPainterPath()
    hair.moveTo(cx - w * 1.05, cy - h * 0.2)
    hair.quadTo(cx - w * 1.1, cy - h * 1.2, cx, cy - h * 1.12)
    hair.quadTo(cx + w * 1.1, cy - h * 1.2, cx + w * 1.05, cy - h * 0.2)
    hair.quadTo(cx + w * 0.7, cy - h * 0.55, cx + w * 0.55, cy - h * 0.2)
    hair.quadTo(cx, cy - h * 0.62, cx - w * 0.55, cy - h * 0.2)
    hair.quadTo(cx - w * 0.7, cy - h * 0.55, cx - w * 1.05, cy - h * 0.2)
    hair.closeSubpath()
    p.fillPath(hair, _fc(14, 12, 16))

    ey = cy - 0.16 * h
    ex = 0.44 * w
    er = 0.24 * w
    for sx in (-1, 1):
        sock = QRadialGradient(cx + sx * ex, ey, er * 1.7)
        sock.setColorAt(0.0, _fc(20, 12, 16, int(180 + 60 * dread)))
        sock.setColorAt(1.0, _fc(0, 0, 0, 0))
        p.setBrush(QBrush(sock))
        p.drawEllipse(QPointF(cx + sx * ex, ey), er * 1.6, er * 1.7)

    for i, sx in enumerate((-1, 1)):
        exx = cx + sx * ex
        eyy = ey + (2.5 if sx > 0 else -1.5) * (1 - dread)
        if dread < 0.55:
            p.setBrush(_fc(226, 220, 214))
            p.drawEllipse(QPointF(exx, eyy), er, er * 0.82)
            gaze = 0.10 * er
            p.setBrush(_fc(60, 40, 30))
            p.drawEllipse(QPointF(exx + gaze, eyy), er * 0.62, er * 0.62)
            p.setBrush(_fc(8, 6, 8))
            p.drawEllipse(QPointF(exx + gaze, eyy), er * 0.24, er * 0.24)
            p.setBrush(_fc(255, 255, 255, 220))
            p.drawEllipse(QPointF(exx + gaze - er * 0.18, eyy - er * 0.2),
                          er * 0.10, er * 0.10)
        else:
            void = QRadialGradient(exx, eyy, er * 1.1)
            void.setColorAt(0.0, _fc(0, 0, 0))
            void.setColorAt(0.7, _fc(0, 0, 0))
            void.setColorAt(1.0, _fc(30, 0, 0))
            p.setBrush(QBrush(void))
            p.drawEllipse(QPointF(exx, eyy), er * 1.05, er * 1.15)
            ember = 0.5 + 0.5 * math.sin(t * 8 + i)
            gl = QRadialGradient(exx, eyy, er * 0.6)
            gl.setColorAt(0.0, _fc(255, 40, 40, int(220 * ember)))
            gl.setColorAt(1.0, _fc(120, 0, 0, 0))
            p.setBrush(QBrush(gl))
            p.drawEllipse(QPointF(exx, eyy), er * 0.6, er * 0.6)
            p.setBrush(_fc(255, 90, 70))
            p.drawEllipse(QPointF(exx, eyy), er * 0.12, er * 0.12)
            p.setPen(QPen(_fc(120, 0, 0), er * 0.16))
            drip = er * (1.2 + 2.5 * ((t * 0.6 + i) % 1.0))
            p.drawLine(QPointF(exx, eyy + er * 0.6),
                       QPointF(exx, eyy + er * 0.6 + drip))
            p.setPen(Qt.PenStyle.NoPen)

    ny = cy + 0.14 * h
    nsh = QRadialGradient(cx, ny, 0.3 * w)
    nsh.setColorAt(0.0, _fc(90 - 40 * dread, 70, 66, 120))
    nsh.setColorAt(1.0, _fc(0, 0, 0, 0))
    p.setBrush(QBrush(nsh))
    p.drawEllipse(QPointF(cx, ny), 0.16 * w, 0.26 * h)
    p.setBrush(_fc(20, 12, 14))
    for sx in (-1, 1):
        p.drawEllipse(QPointF(cx + sx * 0.07 * w, ny + 0.12 * h), 0.03 * w, 0.045 * h)

    my = cy + 0.5 * h
    if dread < 0.55:
        mw = 0.5 * w
        smile = QPainterPath()
        smile.moveTo(cx - mw, my)
        smile.quadTo(cx, my + 0.14 * h, cx + mw, my)
        p.setPen(QPen(_fc(120, 70, 70), 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(smile)
        p.setPen(Qt.PenStyle.NoPen)
    else:
        mw = 0.62 * w * (1.0 + 0.12 * math.sin(t * 5))
        mh = 0.34 * h
        mouth = QPainterPath()
        mouth.moveTo(cx - mw, my - 0.03 * h)
        mouth.quadTo(cx, my - 0.16 * h, cx + mw, my - 0.03 * h)
        mouth.quadTo(cx, my + mh, cx - mw, my - 0.03 * h)
        mouth.closeSubpath()
        p.setBrush(_fc(8, 0, 0))
        p.drawPath(mouth)
        p.save()
        p.setClipPath(mouth)
        n = 11
        for k in range(n):
            tx = cx - mw + (2 * mw) * (k + 0.5) / n
            th = 0.11 * h * (0.6 + 0.7 * ((k * 7) % 3) / 2.0)
            p.setBrush(_fc(206, 196, 176))
            p.drawPolygon(QPolygonF([QPointF(tx - mw / n, my - 0.05 * h),
                                     QPointF(tx + mw / n, my - 0.05 * h),
                                     QPointF(tx, my - 0.05 * h + th)]))
            p.drawPolygon(QPolygonF([QPointF(tx - mw / n, my + mh * 0.72),
                                     QPointF(tx + mw / n, my + mh * 0.72),
                                     QPointF(tx, my + mh * 0.72 - th)]))
        p.restore()
        p.setPen(QPen(_fc(110, 0, 0), 5))
        p.drawLine(QPointF(cx - mw * 0.5, my + mh * 0.4),
                   QPointF(cx - mw * 0.5, my + mh * 0.4 + 0.4 * h))
        p.setPen(Qt.PenStyle.NoPen)

    if dread > 0.55:
        p.setPen(QPen(_fc(60, 10, 14, 150), 1.4))
        cr = random.Random(seed)
        for _ in range(14):
            x0 = cx + cr.uniform(-w, w)
            y0 = cy + cr.uniform(-h, h)
            path = QPainterPath()
            path.moveTo(x0, y0)
            for _s in range(3):
                x0 += cr.uniform(-0.15 * w, 0.15 * w)
                y0 += cr.uniform(-0.15 * h, 0.15 * h)
                path.lineTo(x0, y0)
            p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)

    p.restore()


_FACE_CACHE = {}


def _face_image(dread):
    """Return a user-supplied face QPixmap if present, else None (procedural)."""
    key = "scary" if dread > 0.55 else "uncanny"
    if key in _FACE_CACHE:
        return _FACE_CACHE[key]
    here = os.path.dirname(os.path.abspath(__file__))
    names = (["uncanny2.png", "scary.png"] if key == "scary"
             else ["uncanny1.png", "uncanny.png"])
    pm = None
    for nm in names:
        path = os.path.join(here, nm)
        if os.path.exists(path):
            cand = QPixmap(path)
            if not cand.isNull():
                pm = cand
                break
    _FACE_CACHE[key] = pm
    return pm


# ── The denial sequence ────────────────────────────────────────────────────
class DenialSequence(QWidget):
    """Full-screen: INCORRECT -> binary flood -> 3D skull -> jumpscare ->
    verdict. run() blocks until finished, then the caller exits the program."""

    # wall-clock phase boundaries (seconds)
    P_INCORRECT   = 1.05     # red flash / glitch / shake
    P_FLOOD       = 3.35     # binary fills the screen
    P_ASSEMBLE    = 5.85     # binary slowly reorganises into a 3D skull
    P_SKULL_HOLD  = 15.85    # the finished skull holds, slowly turning (~10s)
    P_MORPH       = 18.35    # skull slowly morphs into an uncanny person
    P_UNCANNY     = 28.35    # the uncanny person holds (~10s)
    P_SCARY       = 38.35    # instant cut to a scary face + sound, holds (~10s)
    P_VERDICT     = 58.35    # sound stops, TTS speaks the verdict (~20s)
    TOTAL         = 58.35
    TTS_GRACE     = 8.0      # extra seconds allowed so the voice can finish

    def __init__(self, screen_geo, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint)
        self.setGeometry(screen_geo)
        self.t = 0.0
        self.shake = QPointF(0, 0)
        self.cols = []
        self.glyphs = []
        self._grid_built = False
        self._beeped = False
        self._sound_on = False
        self._daisy_on = False
        self._spoke = False
        self._tts_done = None
        self._loop = QEventLoop()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    def _jumpscare_beep(self):
        """A harsh burst at the jumpscare — best-effort, never fatal."""
        def boom():
            try:
                if sys.platform == "win32":
                    import winsound
                    for fr in (1400, 240, 900):
                        winsound.Beep(fr, 90)
                else:
                    sys.stdout.write("\a"); sys.stdout.flush()
            except Exception:
                pass
        try:
            import threading
            threading.Thread(target=boom, daemon=True).start()
        except Exception:
            pass

    # ── run ──
    def run(self):
        import time
        prewarm_audio()          # render song + drone while the intro plays
        self._clock = time.perf_counter()
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.timer.start(16)
        self._loop.exec()

    def _tick(self):
        # Wall-clock driven: the sequence always lasts TOTAL seconds and the
        # jumpscare fires on time even if painting drops frames. (Frame-locking
        # stretched an 8s scare to 20s on slow paints, killing the punch.)
        import time
        clk = getattr(self, "_clock", None)
        self.t = (time.perf_counter() - clk) if clk is not None else (self.t + 0.016)
        # phase-scaled screen shake
        amp = 0.0
        if self.t < self.P_INCORRECT:
            amp = 7.0
        elif self.P_FLOOD < self.t < self.P_ASSEMBLE:
            amp = 2.0                                    # subtle during assembly
        elif self.P_UNCANNY <= self.t < self.P_UNCANNY + 0.6:
            # the instant cut to the scary face — violent slam, then settle
            amp = 26.0 * max(0.0, 1.0 - (self.t - self.P_UNCANNY) / 0.6)
        elif self.P_UNCANNY + 0.6 <= self.t < self.P_SCARY:
            amp = 2.5 + 2.0 * random.random()            # unease under the sound
        self.shake = QPointF(random.uniform(-amp, amp), random.uniform(-amp, amp))

        # ── the uncanny person sings: warped "Daisy Bell" ──
        # One-shot latch (same reason as the drone below).
        if not self._daisy_on and self.P_MORPH <= self.t < self.P_UNCANNY:
            self._daisy_on = True
            start_daisy_song()

        # ── the instant scary cut: beep + looping dread sound ──
        # One-shot latch. Driving the start off `not self._sound_on` made it
        # restart every frame once the stop cleared the flag, so the drone
        # stuttered for the whole final phase instead of stopping.
        if not self._beeped and self.t >= self.P_UNCANNY:
            self._beeped = True
            self._sound_on = True
            self._jumpscare_beep()
            start_scare_sound()
        # sound stops when the scary hold ends
        if self._sound_on and self.t >= self.P_SCARY:
            self._sound_on = False
            stop_scare_sound()
        # verdict phase: free-speaking TTS, once
        if not self._spoke and self.t >= self.P_SCARY:
            self._spoke = True
            self._tts_done = speak_verdict(self.P_VERDICT - self.P_SCARY)

        if self.t >= self.TOTAL:
            # Never quit mid-sentence: the app used to exit on the clock while
            # the voice was still talking, chopping the verdict off. Hold the
            # screen until the speech finishes, with a hard cap so a wedged
            # PowerShell can never trap the user in a full-screen window.
            waiting = (self._tts_done is not None
                       and not self._tts_done.is_set()
                       and self.t < self.TOTAL + self.TTS_GRACE)
            if not waiting:
                self.timer.stop()
                stop_scare_sound()
                if self._loop.isRunning():
                    self._loop.quit()
                self.close()
                return
        self.update()

    # ── lazy build of the binary grid + its skull mapping ──
    def _build_grid(self):
        w, h = self.width(), self.height()
        cw, ch = 17, 22
        ncol = w // cw + 1
        nrow = h // ch + 1
        # falling columns of 0/1 for the flood
        for c in range(ncol):
            self.cols.append({
                "x": c * cw,
                "speed": random.uniform(520, 1150),
                "head": random.uniform(-h, 0),
                "delay": random.uniform(0.0, 1.1),
                "chars": [random.randint(0, 1) for _ in range(nrow + 2)],
                "flip": random.uniform(0, 1),
            })
        # a pool of glyphs used to assemble the skull
        cx, cy = w / 2.0, h / 2.0
        scale = min(w, h) * 0.34
        for tx, ty, tz in SKULL_ANIM:
            self.glyphs.append({
                "hx": random.uniform(0, w), "hy": random.uniform(0, h),
                "sx3": tx, "sy3": ty, "sz3": tz,
                "bit": random.randint(0, 1),
                "seed": random.uniform(0, 6.28),
            })
        self._skull_cx, self._skull_cy, self._skull_scale = cx, cy, scale
        self._grid_built = True

    # ── paint ──
    def paintEvent(self, _e):
        if not self._grid_built:
            self._build_grid()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.translate(self.shake)
        w, h = self.width(), self.height()
        p.fillRect(-40, -40, w + 80, h + 80, QColor("#04070A"))
        t = self.t

        if t < self.P_INCORRECT:
            self._paint_incorrect(p, w, h, t)
        elif t < self.P_FLOOD:
            self._paint_flood(p, w, h, t)
        elif t < self.P_ASSEMBLE:
            self._paint_skull(p, w, h, t)             # binary -> skull
        elif t < self.P_SKULL_HOLD:
            self._paint_skull_hold(p, w, h, t)        # 10s hold, slow turn
        elif t < self.P_MORPH:
            self._paint_morph(p, w, h, t)             # skull -> uncanny person
        elif t < self.P_UNCANNY:
            self._paint_face(p, w, h, t, 0.32)        # uncanny person, 10s hold
        elif t < self.P_SCARY:
            self._paint_face(p, w, h, t, 1.0)         # scary face + sound, 10s
        else:
            self._paint_verdict(p, w, h, t)           # TTS verdict, 10s
        p.end()

    # phase 0
    def _paint_incorrect(self, p, w, h, t):
        flash = max(0.0, 1.0 - t / self.P_INCORRECT)
        p.fillRect(0, 0, w, h, _qc(C_BLOOD, int(70 * flash)))
        f = QFont(F_MONO, 60, QFont.Weight.Black)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 10)
        p.setFont(f)
        for dx, col, a in ((-5, C_VENOM, 150), (5, C_BLOOD, 150), (0, "#FFFFFF", 255)):
            jitter = random.uniform(-3, 3) if random.random() < 0.5 else 0
            p.setPen(_qc(col, a))
            p.drawText(QRectF(dx + jitter, 0, w, h),
                       Qt.AlignmentFlag.AlignCenter, "INCORRECT")
        # scanline tearing
        p.setPen(Qt.PenStyle.NoPen)
        for _ in range(9):
            yy = random.uniform(0, h)
            p.fillRect(QRectF(0, yy, w, random.uniform(1, 4)),
                       _qc(C_BLOOD, 40))

    # phase 1 — binary flood, column by column
    def _paint_flood(self, p, w, h, t):
        u = (t - self.P_INCORRECT) / (self.P_FLOOD - self.P_INCORRECT)
        f = QFont(F_MONO, 15, QFont.Weight.Bold)
        p.setFont(f)
        ch = 22
        for col in self.cols:
            prog = max(0.0, (u - col["delay"] * 0.5) / 0.5)
            head = col["head"] + col["speed"] * (t - self.P_INCORRECT) * (0.5 + prog)
            fill = min(1.0, prog)
            top = h - fill * (h + ch)
            for r, bit in enumerate(col["chars"]):
                y = r * ch
                if y < top - ch:
                    # still falling (rain head)
                    ry = (head - (len(col["chars"]) - r) * ch) % (h + ch)
                    if 0 < ry < h:
                        dist = 1.0 - min(1.0, abs(ry - head) / (h * 0.4))
                        p.setPen(_qc(C_TOXIC, int(60 + 160 * dist)))
                        p.drawText(QPointF(col["x"], ry), str(bit))
                    continue
                glow = 1.0 if r % 7 == int(t * 9) % 7 else 0.0
                a = int(90 + 90 * glow)
                p.setPen(_qc(C_TOXIC2 if glow else C_TOXIC, a))
                p.drawText(QPointF(col["x"], y), str(bit))

    # ── shared: the binary skull point cloud ──
    def _skull_cloud(self, p, t, assemble=1.0, opacity=1.0):
        """assemble 0->1 flies the glyphs from their rain positions onto the
        skull surface; opacity fades the whole cloud (used by the morph)."""
        # gentle front-facing sway — never turns to profile, so it always
        # reads as a skull
        ang = 0.34 * math.sin((t - self.P_FLOOD) * 0.85)
        cx, cy, sc = self._skull_cx, self._skull_cy, self._skull_scale
        p.setFont(QFont(F_MONO, 15, QFont.Weight.Bold))
        drawn = []
        for g in self.glyphs:
            rx, ry, rz = rot3((g["sx3"], g["sy3"], g["sz3"]), 0.0, ang)
            if rz < -0.15:
                continue
            sx, sy, d = project((rx, ry, rz), sc * 3.0, cam_z=3.4)
            gx, gy = cx + sx, cy + sy
            X = g["hx"] + (gx - g["hx"]) * assemble
            Y = g["hy"] + (gy - g["hy"]) * assemble
            drawn.append((d, X, Y, g["bit"]))
        grn = QColor(C_TOXIC).name()
        for d, X, Y, bit in sorted(drawn, key=lambda q: -q[0]):
            # front of the skull (smaller d) burns brightest
            lit = max(0.0, min(1.0, (3.9 - d) / 1.4))
            if assemble > 0.6 and random.random() < 0.04:
                bit ^= 1
            red = QColor(int(150 + 105 * lit), int(16 + 44 * lit), int(20 + 40 * lit))
            col = QColor(_lerp_hex(grn, red.name(), min(1.0, assemble * 1.15)))
            col.setAlpha(int((150 + 105 * (assemble * 0.5 + lit * 0.5)) * opacity))
            p.setPen(col)
            p.drawText(QPointF(X, Y), str(bit))

    def _residual_rain(self, p, fade):
        if fade <= 0.02:
            return
        p.setFont(QFont(F_MONO, 15))
        p.setPen(_qc(C_TOXIC, int(40 * fade)))
        for col in self.cols[::2]:
            for r in range(0, len(col["chars"]), 3):
                p.drawText(QPointF(col["x"], r * 22), str(col["chars"][r]))

    # phase 2 — the falling binary slowly assembles into the skull
    def _paint_skull(self, p, w, h, t):
        u = _smooth(0.0, 1.0,
                    (t - self.P_FLOOD) / (self.P_ASSEMBLE - self.P_FLOOD))
        self._residual_rain(p, 1.0 - u)
        self._skull_cloud(p, t, assemble=u)

    # phase 3 — the finished skull holds (~10s), slowly turning
    def _paint_skull_hold(self, p, w, h, t):
        self._skull_cloud(p, t, assemble=1.0)

    # phase 4 — the skull slowly becomes an uncanny person
    def _paint_morph(self, p, w, h, t):
        m = _smooth(0.0, 1.0,
                    (t - self.P_SKULL_HOLD) / (self.P_MORPH - self.P_SKULL_HOLD))
        # bone dissolves as flesh forms over it
        self._skull_cloud(p, t, assemble=1.0, opacity=1.0 - m)
        if m > 0.02:
            p.setOpacity(m)
            self._draw_face_centred(p, w, h, t, dread=0.32 * m)
            p.setOpacity(1.0)

    # phases 5 & 6 — the uncanny person, then the scary cut
    def _paint_face(self, p, w, h, t, dread):
        creep = 0.0
        if dread >= 0.9:
            # sickly red wash, pulsing with the drone
            pulse = 26 + 16 * (0.5 + 0.5 * math.sin(t * 2.3))
            p.fillRect(0, 0, w, h, _qc(C_BLOOD, int(pulse)))
            # it leans in over the whole 10s hold
            creep = max(0.0, min(1.0,
                        (t - self.P_UNCANNY) / (self.P_SCARY - self.P_UNCANNY)))
        self._draw_face_centred(p, w, h, t, dread, creep)

    def _draw_face_centred(self, p, w, h, t, dread, creep=0.0):
        s = min(w, h)
        # the scary face is bigger from the first frame and looms closer still
        k = 0.23 + 0.11 * max(0.0, (dread - 0.5) / 0.5) + 0.10 * creep
        draw_face(p, w / 2.0, h * (0.46 + 0.04 * creep),
                  s * k, s * k * 1.26, dread, t)

    # final phase — the verdict is SPOKEN only, never written on screen.
    # Just the dim residual skull left in the dark while the voice repeats.
    def _paint_verdict(self, p, w, h, t):
        u = (t - self.P_SCARY) / (self.P_VERDICT - self.P_SCARY)
        fade = min(1.0, u / 0.18)
        cx, cy, sc = self._skull_cx, self._skull_cy, self._skull_scale
        p.setPen(Qt.PenStyle.NoPen)
        for g in self.glyphs[::3]:
            rx, ry, rz = rot3((g["sx3"], g["sy3"], g["sz3"]), 0.0, 0.0)
            if rz < -0.1:
                continue
            sx, sy, d = project((rx, ry, rz), sc * 3.0, cam_z=3.4)
            p.setBrush(_qc(C_BLOOD, int(70 * fade)))
            p.drawEllipse(QPointF(cx + sx, cy + sy), 1.6, 1.6)


# ── The password gate ──────────────────────────────────────────────────────
class PasswordGate(QWidget):
    """Locked entry screen. Correct key -> accepted; wrong key -> the denial
    sequence plays and the program exits. No registration, one key only."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("FBVENOM · Workspace access")
        self.setMinimumSize(780, 500)
        self.resize(780, 500)
        self.accepted = False
        self.t = 0.0
        self.err = 0.0
        self._drag = None
        self._brand_width = 316

        lo = QHBoxLayout(self)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(0)

        brand = QWidget()
        brand.setFixedWidth(self._brand_width)
        brand_lo = QVBoxLayout(brand)
        brand_lo.setContentsMargins(34, 36, 30, 32)
        brand_lo.setSpacing(14)

        wordmark = QLabel("FBVENOM")
        wordmark.setStyleSheet(
            f"color:{C_TEXT};font-family:'{F_UI}';font-size:21px;"
            "font-weight:800;letter-spacing:2px;background:transparent")
        brand_lo.addWidget(wordmark)
        edition = QLabel("PURPLE EDITION")
        edition.setStyleSheet(
            f"color:{C_TOXIC2};font-family:'{F_UI}';font-size:10px;"
            "font-weight:600;letter-spacing:2px;background:transparent")
        brand_lo.addWidget(edition)
        brand_lo.addStretch(1)

        welcome = QLabel("Welcome\nback.")
        welcome.setStyleSheet(
            f"color:{C_TEXT};font-family:'{F_UI}';font-size:43px;"
            "font-weight:700;background:transparent")
        brand_lo.addWidget(welcome)
        introduction = QLabel("Your tools. Your workflow.\nOne focused workspace.")
        introduction.setStyleSheet(
            f"color:{C_TOXIC2};font-family:'{F_UI}';font-size:13px;"
            "background:transparent")
        brand_lo.addWidget(introduction)
        brand_lo.addStretch(2)
        signature = QLabel("WORKSPACE ACCESS")
        signature.setStyleSheet(
            f"color:{C_TOXIC2};font-family:'{F_UI}';font-size:10px;"
            "letter-spacing:1.5px;background:transparent")
        brand_lo.addWidget(signature)
        lo.addWidget(brand)

        form = QWidget()
        form_lo = QVBoxLayout(form)
        form_lo.setContentsMargins(42, 40, 42, 36)
        form_lo.setSpacing(0)
        form_lo.addStretch(1)

        self.status = QLabel("ACCESS REQUIRED")
        self.status.setStyleSheet(
            f"color:{C_TOXIC};font-family:'{F_UI}';font-size:10px;"
            "font-weight:700;letter-spacing:1.4px;background:transparent")
        form_lo.addWidget(self.status)
        form_lo.addSpacing(12)
        title = QLabel("Open your workspace")
        title.setStyleSheet(
            f"color:{C_TEXT};font-family:'{F_UI}';font-size:23px;"
            "font-weight:700;background:transparent")
        form_lo.addWidget(title)
        form_lo.addSpacing(10)
        description = QLabel("Enter your access key to continue to FBVENOM.")
        description.setWordWrap(True)
        description.setStyleSheet(
            f"color:{C_MUTE};font-family:'{F_UI}';font-size:13px;"
            "background:transparent")
        form_lo.addWidget(description)
        form_lo.addSpacing(30)
        field_label = QLabel("Access key")
        field_label.setStyleSheet(
            f"color:{C_TEXT};font-family:'{F_UI}';font-size:12px;"
            "font-weight:600;background:transparent")
        form_lo.addWidget(field_label)
        form_lo.addSpacing(9)

        self.field = QLineEdit()
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        self.field.setPlaceholderText("Enter your access key")
        self.field.setAccessibleName("Access key")
        self.field.setMinimumHeight(52)
        self.field.setStyleSheet(
            f"QLineEdit{{background:{C_INPUT};color:{C_TEXT};border:1px solid "
            f"{C_BORDER};border-radius:10px;padding:0 15px;font-size:14px;"
            f"font-family:'{F_UI}';selection-background-color:{C_TOXIC};"
            f"selection-color:{C_BG}}}"
            f"QLineEdit:hover{{border-color:{C_EDGE}}}"
            f"QLineEdit:focus{{border:1px solid {C_TOXIC};background:{C_BG}}}")
        field_label.setBuddy(self.field)
        self.field.returnPressed.connect(self._submit)
        form_lo.addWidget(self.field)
        form_lo.addSpacing(16)

        self.enter = HazardButton("Open workspace", C_TOXIC, primary=True, depth=0)
        self.enter.setFixedHeight(52)
        self.enter.clicked.connect(self._submit)
        form_lo.addWidget(self.enter)
        form_lo.addSpacing(18)

        self.hint = QLabel("Authorized operators only · No registration")
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setStyleSheet(
            f"color:{C_MUTE};font-size:11px;"
            f"font-family:'{F_UI}';background:transparent")
        form_lo.addWidget(self.hint)
        form_lo.addStretch(1)
        lo.addWidget(form, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)
        self._loop = QEventLoop()

    def _tick(self):
        self.t += 0.033
        if self.err > 0:
            self.err = max(0.0, self.err - 0.033)
        self.update()

    def _submit(self):
        if verify_key(self.field.text()):
            self.accepted = True
            self.timer.stop()
            if self._loop.isRunning():
                self._loop.quit()
            self.close()
        else:
            self.err = 0.7
            self.field.clear()
            self.field.setEnabled(False)
            self.enter.setEnabled(False)
            geo = QApplication.primaryScreen().geometry()
            self.hide()
            DenialSequence(geo).run()      # blocks through the whole scare
            if self._loop.isRunning():
                self._loop.quit()
            self.accepted = False
            self.close()

    def run(self):
        scr = QApplication.primaryScreen().geometry()
        self.move((scr.width() - self.width()) // 2,
                  (scr.height() - self.height()) // 2)
        self.show()
        self.raise_()
        self.activateWindow()
        self.field.setFocus()
        self._loop.exec()
        return self.accepted

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev):
        if self._drag is not None and ev.buttons() & Qt.MouseButton.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _ev):
        self._drag = None

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r = QRectF(0.5, 0.5, w - 1, h - 1)
        card = QPainterPath()
        card.addRoundedRect(r, 22, 22)
        p.fillPath(card, QColor(C_PANEL))
        p.setClipPath(card)

        # A quiet violet brand panel with translucent orbital accents.
        brand = QRectF(0, 0, self._brand_width, h)
        g = QLinearGradient(0, 0, self._brand_width, h)
        g.setColorAt(0, QColor("#513A7D"))
        g.setColorAt(0.55, QColor("#382654"))
        g.setColorAt(1, QColor("#291E40"))
        p.fillRect(brand, QBrush(g))
        p.setClipRect(brand, Qt.ClipOperation.IntersectClip)
        glow = QRadialGradient(QPointF(40, h + 10), 250)
        glow.setColorAt(0, _qc(C_TOXIC, 40))
        glow.setColorAt(1, _qc(C_TOXIC, 0))
        p.fillRect(brand, QBrush(glow))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for radius in (94, 134, 174):
            p.setPen(QPen(_qc(C_TOXIC2, 27), 1))
            p.drawEllipse(QPointF(self._brand_width + 15, h - 25), radius, radius)
        dot = QPointF(self._brand_width - 83, h - 92)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc(C_TOXIC2, 90))
        p.drawEllipse(dot, 4, 4)

        p.setClipping(False)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(C_BLOOD if self.err else C_BORDER), 1))
        p.drawPath(card)
        p.end()


def require_key() -> bool:
    """Show the gate. True only if the correct key was entered."""
    return PasswordGate().run()


if __name__ == "__main__":
    if "--seal" in sys.argv:
        pw = input("new access key: ")
        print("\nreplace _SEALED in auth.py with:\n")
        print("    _SEALED = \"%s\"" % _mask(_digest(pw)))
    elif "--denial" in sys.argv:
        app = QApplication(sys.argv)
        DenialSequence(QApplication.primaryScreen().geometry()).run()
    else:
        app = QApplication(sys.argv)
        print("access granted" if require_key() else "access refused")
