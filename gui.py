# FBVenom desktop workspace — symbiote edition.
# Presentation lives here; device workers and application behavior are preserved.

import sys
import os
import math
import random
import time
import threading
import queue
import json
import copy
import subprocess
import aiconfig
import updater
from workspaces import WorkspaceStore
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QGridLayout, QLabel, QLineEdit, QPushButton, QComboBox,
    QListWidget, QListWidgetItem, QProgressBar, QFrame, QScrollArea,
    QSizePolicy, QButtonGroup, QGraphicsOpacityEffect, QGraphicsDropShadowEffect,
    QCheckBox, QDialog, QTabBar, QSpinBox, QMessageBox, QPlainTextEdit)
from PyQt6.QtCore import (Qt, QTimer, QPropertyAnimation, QEasingCurve,
    QRect, QRectF, QPointF, QSize, QVariantAnimation, QPoint)
from PyQt6.QtGui import (QPainter, QColor, QBrush, QPen, QFont, QFontDatabase,
    QPainterPath, QPolygonF, QPixmap, QIcon, QLinearGradient, QRadialGradient,
    QCursor)

# Import core functions from fb_core
import core as fbcore
from core import (
    resource_path, ensure_online, connect_resilient, count_available_accounts,
    REACTIONS, parse_action_mode, normalize_url, start_llama, stop_llama,
    reset_comment_pool, find_apk, install_fb, has_fb, wait_online, wake_and_unlock,
    react_all_saved_accounts, HERE, abort_devices
)

# Symbiote palette: ink-black surfaces, white eyes, and a crimson accent.
C_VOID    = "#030405"
C_BG      = "#080A0E"
C_PANEL   = "#101319"
C_CARD    = "#191D25"
C_BORDER  = "#2B313C"
C_EDGE    = "#536071"
C_INPUT   = "#0B0E13"
C_TEXT    = "#F7F7F8"
C_MUTE    = "#AFB6C3"
C_FAINT   = "#7D8798"
C_TOXIC   = "#ECEEF2"
C_TOXIC2  = "#FFFFFF"
C_VENOM   = "#C2C8D2"
C_HAZARD  = "#F5BE7B"
C_BLOOD   = "#FF6174"
C_CRIMSON = "#CC2945"

# legacy aliases used by the handlers below
C_ACCENT = C_TOXIC
C_GREEN  = C_TOXIC
C_RED    = C_BLOOD
C_GOLD   = C_HAZARD
C_AMBER  = C_HAZARD
C_CYAN   = C_TOXIC
C_MAG    = C_VENOM
C_LIME   = C_TOXIC

# System faces: a UI sans for headings and body, a monospace for serials,
# timers and counts. Nothing is bundled, so there is no font folder to ship.
F_DISP = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
F_BODY = F_DISP
F_UI   = F_BODY
F_MONO = "Consolas" if sys.platform == "win32" else "DejaVu Sans Mono"

# Display call sites were written against a tiny arcade face; the UI face needs
# more room, so one factor rescales every heading without touching them all.
_DISP_SCALE = 1.5

_FONTS_LOADED = False


def ensure_pixel_fonts():
    """Kept for call compatibility; the UI now uses the system faces."""
    global _FONTS_LOADED
    _FONTS_LOADED = True


def _ui_font(family, size, weight):
    f = QFont(family, int(size), weight)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


def PF(size, weight=QFont.Weight.Normal):
    """Body copy."""
    return _ui_font(F_BODY, size, weight)


def PFM(size, weight=QFont.Weight.Normal):
    """Monospace, for serials, timers and counts."""
    return _ui_font(F_MONO, size, weight)


def PFD(size, weight=QFont.Weight.DemiBold):
    """Headings, labels and buttons."""
    return _ui_font(F_DISP, max(9, round(size * _DISP_SCALE)), weight)


_INPUT_QSS = (
    f"QLineEdit{{background:{C_INPUT};color:{C_TEXT};border:1px solid {C_BORDER};"
    f"border-radius:8px;padding:10px 13px;font-size:13px;"
    f"font-family:'{F_UI}';selection-background-color:{C_TOXIC};"
    f"selection-color:{C_VOID}}}"
    f"QLineEdit:hover{{border:1px solid {C_EDGE}}}"
    f"QLineEdit:focus{{border:1px solid {C_EDGE};background:{C_CARD}}}"
)
_FIELD_LBL = (f"color:{C_MUTE};font-size:11px;font-weight:600;"
              f"letter-spacing:0.6px;font-family:'{F_DISP}';background:transparent")


def _qc(c, a=None):
    col = QColor(c)
    if a is not None:
        col.setAlpha(max(0, min(255, int(a))))
    return col

def _lerp_hex(a, b, t):
    a, b = QColor(a), QColor(b)
    return "#{:02x}{:02x}{:02x}".format(
        int(a.red()   + (b.red()   - a.red())   * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue()  + (b.blue()  - a.blue())  * t))

def pixelate(pm, factor=None):
    """Nearest-neighbour down-then-up so any image reads as chunky pixels."""
    if pm is None or pm.isNull():
        return pm
    factor = factor or 110
    small = pm.scaled(max(1, pm.width() // 1), max(1, pm.height() // 1))
    w = max(8, factor)
    h = max(8, int(factor * pm.height() / max(1, pm.width())))
    down = pm.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)
    return down.scaled(pm.width(), pm.height(),
                       Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.FastTransformation)


def _r(rect):
    """Snap any rect to whole device pixels - no half-pixel seams."""
    return QRect(int(rect.x()), int(rect.y()),
                 int(rect.width()), int(rect.height()))


def notched_path(rect, n):
    """Stepped ("notched") corners - AniVerse's PixelBorder shape.

    Each corner is cut back by one step, which is what makes a box read as
    drawn on a pixel grid rather than merely square.
    """
    r = _r(rect)
    n = int(max(0, min(n, r.width() // 2, r.height() // 2)))
    path = QPainterPath()
    if n <= 0:
        path.addRect(QRectF(r))
        return path
    l, t = r.x(), r.y()
    rt, b = r.x() + r.width(), r.y() + r.height()
    pts = [(l + n, t), (rt - n, t), (rt - n, t + n), (rt, t + n),
           (rt, b - n), (rt - n, b - n), (rt - n, b), (l + n, b),
           (l + n, b - n), (l, b - n), (l, t + n), (l + n, t + n)]
    path.moveTo(*pts[0])
    for pt in pts[1:]:
        path.lineTo(*pt)
    path.closeSubpath()
    return path


def _mix(c, other, t):
    return QColor(_lerp_hex(QColor(c).name(), other, t))


def bevel_rect(p, rect, fill, light=None, dark=None, border=None, bw=1,
               inset=True, shadow=0, bevel=False, rivets=False, radius=8):
    """A flat card: softly rounded corners, a hairline border, no ornament.

    The remaining arguments are accepted so existing call sites keep working;
    `inset=False` simply darkens the face to read as pressed.
    """
    r = QRectF(_r(rect)).adjusted(0.5, 0.5, -0.5, -0.5)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    fill_c = QColor(fill) if not isinstance(fill, QColor) else fill
    if not inset:
        fill_c = _mix(fill_c, "#000000", 0.22)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(fill_c)
    p.drawRoundedRect(r, radius, radius)
    if border is not None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(border), 1))
        p.drawRoundedRect(r, radius, radius)


def draw_label(p, rect, text, size=8, color=C_TEXT, align=None, outline=0,
               weight=QFont.Weight.DemiBold):
    """Plain display text. `outline` is ignored - kept for call compatibility."""
    if align is None:
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setFont(PFD(size, weight))
    p.setPen(QColor(color))
    p.drawText(_r(rect), align, text)


def dither_fill(p, rect, color, alpha=255, step=4):
    """Sparse checkerboard of single art-pixels - cheap pixel texture."""
    r = _r(rect)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_qc(color, alpha))
    p.save()
    p.setClipRect(r)
    y = r.y()
    row = 0
    while y < r.bottom():
        x = r.x() + (0 if row % 2 == 0 else step)
        while x < r.right():
            p.drawRect(x, y, 2, 2)
            x += step * 2
        y += step
        row += 1
    p.restore()


def scanlines(p, rect, alpha=20, step=3):
    """Faint horizontal CRT lines across a region."""
    r = _r(rect)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_qc(C_VOID, alpha))
    y = r.y()
    while y < r.bottom():
        p.drawRect(r.x(), y, r.width(), 1)
        y += step


def cyber_path(r, cut=13, tl=False, tr=True, br=False, bl=True):
    p = QPainterPath()
    x, y, w, h = r.x(), r.y(), r.width(), r.height()
    cut = min(cut, w / 2.0, h / 2.0)
    pts = []
    pts.append((x + cut, y) if tl else (x, y))
    if tr:
        pts += [(x + w - cut, y), (x + w, y + cut)]
    else:
        pts.append((x + w, y))
    if br:
        pts += [(x + w, y + h - cut), (x + w - cut, y + h)]
    else:
        pts.append((x + w, y + h))
    if bl:
        pts += [(x + cut, y + h), (x, y + h - cut)]
    else:
        pts.append((x, y + h))
    if tl:
        pts.append((x, y + cut))
    p.moveTo(*pts[0])
    for pt in pts[1:]:
        p.lineTo(*pt)
    p.closeSubpath()
    return p

def _glow_pixmap(color, size=256, stops=((0.0, 190), (0.45, 70), (1.0, 0))):
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QRadialGradient(size / 2, size / 2, size / 2)
    for pos, alpha in stops:
        g.setColorAt(pos, _qc(color, alpha))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(g)
    p.drawEllipse(0, 0, size, size)
    p.end()
    return pm


# ── 3D maths ───────────────────────────────────────────────────────────────
def rot3(p, ax, ay, az=0.0):
    """Rotate a point about X, then Y, then Z."""
    x, y, z = p
    ca, sa = math.cos(ax), math.sin(ax)
    y, z = y * ca - z * sa, y * sa + z * ca
    cb, sb = math.cos(ay), math.sin(ay)
    x, z = x * cb + z * sb, -x * sb + z * cb
    if az:
        cc, sc = math.cos(az), math.sin(az)
        x, y = x * cc - y * sc, x * sc + y * cc
    return x, y, z

def project(p, focal, cam_z=3.6):
    """Perspective-divide a rotated point. Returns (sx, sy, depth)."""
    x, y, z = p
    d = cam_z + z
    if d < 0.25:
        d = 0.25
    return x * focal / d, y * focal / d, d

def build_icosahedron():
    """12 vertices / 30 edges — the 'toxin molecule'."""
    P = (1.0 + 5.0 ** 0.5) / 2.0
    vs = []
    for a in (-1.0, 1.0):
        for b in (-P, P):
            vs += [(0.0, a, b), (a, b, 0.0), (b, 0.0, a)]
    n = (1.0 + P * P) ** 0.5
    vs = [(x / n, y / n, z / n) for x, y, z in vs]
    pairs = []
    for i in range(len(vs)):
        for j in range(i + 1, len(vs)):
            d = math.dist(vs[i], vs[j])
            pairs.append((d, i, j))
    pairs.sort()
    emin = pairs[0][0]
    edges = [(i, j) for d, i, j in pairs if d < emin * 1.05]
    return vs, edges

ICO_V, ICO_E = build_icosahedron()


def draw_symbiote(p, rect, eyes_only=False):
    """Resolution-independent symbiote art shared by the header and app icon."""
    p.save()
    p.translate(rect.x(), rect.y())
    p.scale(rect.width() / 100.0, rect.height() / 100.0)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if not eyes_only:
        head = QPainterPath()
        head.moveTo(50, 5)
        head.cubicTo(5, 3, 7, 50, 18, 71)
        head.cubicTo(27, 90, 42, 97, 50, 98)
        head.cubicTo(70, 95, 87, 76, 89, 49)
        head.cubicTo(96, 17, 76, 4, 50, 5)
        sheen = QLinearGradient(15, 5, 80, 90)
        sheen.setColorAt(0, QColor("#414A59"))
        sheen.setColorAt(0.35, QColor("#131820"))
        sheen.setColorAt(1, QColor("#030405"))
        p.setPen(QPen(QColor(C_EDGE), 0.8))
        p.setBrush(QBrush(sheen))
        p.drawPath(head)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(C_TEXT))
    eye = QPainterPath()
    eye.moveTo(15, 22)
    eye.cubicTo(19, 39, 35, 30, 45, 52)
    eye.cubicTo(35, 57, 20, 57, 17, 47)
    eye.cubicTo(12, 39, 17, 34, 15, 22)
    eye.closeSubpath()
    p.drawPath(eye)
    p.save()
    p.translate(100, 0)
    p.scale(-1, 1)
    p.drawPath(eye)
    p.restore()
    if not eyes_only:
        mouth = QPainterPath()
        mouth.moveTo(20, 61)
        mouth.cubicTo(34, 67, 67, 67, 81, 57)
        mouth.cubicTo(76, 87, 31, 92, 20, 61)
        p.setBrush(QColor(C_VOID))
        p.drawPath(mouth)
        p.save()
        p.setClipPath(mouth)
        p.setBrush(QColor(C_TEXT))
        for x in range(21, 79, 7):
            y = 62 + 4 * math.sin((x - 20) / 60 * math.pi)
            p.drawPolygon(QPolygonF([QPointF(x, y - 3), QPointF(x + 6, y - 3),
                                    QPointF(x + 3, y + 9)]))
            p.drawPolygon(QPolygonF([QPointF(x, 86), QPointF(x + 6, 86),
                                    QPointF(x + 3, 74)]))
        tongue = QPainterPath()
        tongue.moveTo(53, 80)
        tongue.cubicTo(72, 69, 68, 93, 89, 86)
        tongue.cubicTo(76, 104, 58, 85, 53, 80)
        p.setBrush(QColor(C_CRIMSON))
        p.drawPath(tongue)
        p.restore()
    p.restore()


def app_icon_pixmap(size=256, pixel=False):
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    draw_symbiote(p, QRectF(0, 0, size, size))
    p.end()
    return pm


class SymbioteBanner(QFrame):
    """An ink-black hero with organic edge detail, kept behind readable text."""
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0, QColor("#1B202A"))
        gradient.setColorAt(0.52, QColor("#101218"))
        gradient.setColorAt(1, QColor("#200D16"))
        p.setBrush(QBrush(gradient))
        p.setPen(QPen(QColor(C_BORDER), 1))
        p.drawRoundedRect(r, 16, 16)
        p.setClipRect(self.rect())
        for i in range(5):
            tendril = QPainterPath()
            x = self.width() * 0.57 + i * 20
            tendril.moveTo(x, self.height() + 8)
            tendril.cubicTo(x - 56, 61 - i * 9, x + 92, 42, x + 34, -15)
            p.setPen(QPen(_qc(C_EDGE, 38 - i * 4), 2.4 - i * 0.3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(tendril)
        p.end()


def hazard_stripes(p, rect, t=0.0, alpha=42, band=13):
    """Diagonal hazard tape — the one motif that says 'toxic' instantly."""
    p.save()
    p.setClipRect(rect)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_qc(C_HAZARD, alpha))
    off = (t * 22.0) % (band * 2)
    x = rect.x() - rect.height() - band * 2 + off
    while x < rect.x() + rect.width() + band * 2:
        poly = QPolygonF([
            QPointF(x, rect.y() + rect.height()),
            QPointF(x + band, rect.y() + rect.height()),
            QPointF(x + band + rect.height(), rect.y()),
            QPointF(x + rect.height(), rect.y()),
        ])
        p.drawPolygon(poly)
        x += band * 2
    p.restore()


# ── 3D backdrop ────────────────────────────────────────────────────────────
class RootCanvas(QWidget):
    """Perspective floor + depth-sorted spore field. Owns frameless resize."""
    EDGE = 7

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.t = 0.0
        self.setMouseTracking(True)
        self._bg = None            # cached pixel backdrop, rebuilt on resize
        self._bg_size = None
        self._embers = []
        self._ember_t = 0.0
        self._resize_edge = None
        self._start_geo = None
        self._start_mouse = None

    def advance(self, dt):
        # Static ground: nothing animates behind the UI any more.
        self.t += dt

    def _build_bg(self, w, h):
        pm = QPixmap(w, h)
        pm.fill(QColor(C_BG))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Cool reflections on the black surface; no moving artwork under fields.
        sheen = QLinearGradient(0, 0, 0, h)
        sheen.setColorAt(0.0, _qc("#FFFFFF", 10))
        sheen.setColorAt(0.45, _qc("#FFFFFF", 0))
        p.fillRect(QRect(0, 0, w, h), QBrush(sheen))
        glow = QRadialGradient(QPointF(w, h * 0.85), w * 0.5)
        glow.setColorAt(0, _qc(C_CRIMSON, 14))
        glow.setColorAt(1, _qc(C_CRIMSON, 0))
        p.fillRect(QRect(0, 0, w, h), QBrush(glow))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(C_BORDER), 1))
        p.drawRect(0, 0, w - 1, h - 1)
        p.end()
        return pm

    def paintEvent(self, _e):
        w, h = self.width(), self.height()
        if self._bg is None or self._bg_size != (w, h):
            self._bg = self._build_bg(w, h)
            self._bg_size = (w, h)
        p = QPainter(self)
        p.drawPixmap(0, 0, self._bg)
        p.end()

    # ── frameless edge resize ──
    def _edge_at(self, pos):
        x, y, w, h = pos.x(), pos.y(), self.width(), self.height()
        e = self.EDGE
        left, right = x <= e, x >= w - e
        top, bot = y <= e, y >= h - e
        if top and left:    return "tl"
        if top and right:   return "tr"
        if bot and left:    return "bl"
        if bot and right:   return "br"
        if left:            return "l"
        if right:           return "r"
        if top:             return "t"
        if bot:             return "b"
        return None

    _CURSORS = {
        "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
        "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
        "tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
        "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
    }

    def mouseMoveEvent(self, ev):
        if self._resize_edge and self._start_geo is not None:
            g = QRect(self._start_geo)
            dlt = ev.globalPosition().toPoint() - self._start_mouse
            e = self._resize_edge
            if "l" in e: g.setLeft(g.left() + dlt.x())
            if "r" in e: g.setRight(g.right() + dlt.x())
            if "t" in e: g.setTop(g.top() + dlt.y())
            if "b" in e: g.setBottom(g.bottom() + dlt.y())
            mn = self.win.minimumSize()
            if g.width() >= mn.width() and g.height() >= mn.height():
                self.win.setGeometry(g)
            return
        edge = self._edge_at(ev.position().toPoint())
        self.setCursor(QCursor(self._CURSORS.get(edge, Qt.CursorShape.ArrowCursor)))

    def mousePressEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton or self.win.isMaximized():
            return
        edge = self._edge_at(ev.position().toPoint())
        if edge:
            self._resize_edge = edge
            self._start_geo = self.win.geometry()
            self._start_mouse = ev.globalPosition().toPoint()

    def mouseReleaseEvent(self, _ev):
        self._resize_edge = None
        self._start_geo = None


# ── The rotating 3D toxin molecule ─────────────────────────────────────────
class Molecule3D(QWidget):
    def __init__(self, size=74, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.t = 0.0
        self.rate = 1.0
        self.color = C_TOXIC
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def advance(self, dt):
        self.t += dt * self.rate
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        focal = w * 1.35
        ax, ay = self.t * 0.62, self.t * 0.93

        pts = []
        for v in ICO_V:
            r = rot3(v, ax, ay)
            sx, sy, d = project(r, focal)
            pts.append((cx + sx, cy + sy, d))

        # far edges first so nearer ones overlay them
        edges = sorted(ICO_E, key=lambda e: -(pts[e[0]][2] + pts[e[1]][2]))
        for i, j in edges:
            x1, y1, d1 = pts[i]
            x2, y2, d2 = pts[j]
            dd = (d1 + d2) / 2.0
            t = max(0.0, min(1.0, (4.7 - dd) / 2.0))
            p.setPen(QPen(_qc(self.color, 40 + 165 * t), 0.7 + 1.5 * t))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        p.setPen(Qt.PenStyle.NoPen)
        for x, y, d in sorted(pts, key=lambda q: -q[2]):
            t = max(0.0, min(1.0, (4.7 - d) / 2.0))
            r = 1.3 + 2.5 * t
            p.setBrush(_qc(self.color, 30 + 60 * t))
            p.drawEllipse(QPointF(x, y), r * 2.6, r * 2.6)
            p.setBrush(_qc(C_TOXIC2, 110 + 145 * t))
            p.drawEllipse(QPointF(x, y), r, r)
        p.end()


# ── Chrome ─────────────────────────────────────────────────────────────────
class WinButton(QPushButton):
    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(40, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hot = 0.0
        self._anim = QVariantAnimation(self, duration=140,
                                       startValue=0.0, endValue=1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_hot)

    def _set_hot(self, v):
        self._hot = float(v); self.update()

    def enterEvent(self, e):
        self._anim.setDirection(QVariantAnimation.Direction.Forward)
        self._anim.start(); super().enterEvent(e)

    def leaveEvent(self, e):
        self._anim.setDirection(QVariantAnimation.Direction.Backward)
        self._anim.start(); super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        hot = self._hot
        tint = C_BLOOD if self.kind == "close" else C_TOXIC
        if hot > 0.01:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qc(tint, 30 * hot))
            p.drawRect(0, 0, w, h)
        col = QColor(_lerp_hex(C_MUTE, tint, hot))
        p.setPen(QPen(col, 2))
        cx, cy = w / 2, h / 2
        if self.kind == "min":
            p.drawLine(QPointF(cx - 5, cy + 3), QPointF(cx + 5, cy + 3))
        elif self.kind == "max":
            p.drawRect(QRectF(cx - 5, cy - 5, 10, 10))
        else:
            p.drawLine(QPointF(cx - 5, cy - 5), QPointF(cx + 5, cy + 5))
            p.drawLine(QPointF(cx + 5, cy - 5), QPointF(cx - 5, cy + 5))
        p.end()


class TitleBar(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.setFixedHeight(54)
        self.t = 0.0
        self._drag = None
        lo = QHBoxLayout(self)
        lo.setContentsMargins(23, 0, 10, 0)
        lo.setSpacing(11)

        self.mark = QLabel()
        self.mark.setFixedSize(34, 34)
        self.mark.setPixmap(app_icon_pixmap(34))
        self.mark.setAccessibleName("FBVenom icon")
        self.mark.setToolTip("FBVenom")
        lo.addWidget(self.mark)

        name = QLabel("FBVENOM")
        name.setStyleSheet(
            f"color:{C_TEXT};font-size:13px;font-weight:700;letter-spacing:2px;"
            f"font-family:'{F_DISP}';background:transparent")
        lo.addWidget(name)

        tag = QLabel("SYMBIOTE / COMMENT CONTROL")
        tag.setStyleSheet(
            f"color:{C_FAINT};font-size:10px;letter-spacing:0.8px;"
            f"font-family:'{F_DISP}';background:transparent")
        lo.addWidget(tag)
        lo.addStretch()

        self.b_min = WinButton("min");   self.b_min.clicked.connect(win.showMinimized)
        self.b_max = WinButton("max");   self.b_max.clicked.connect(self._toggle_max)
        self.b_cls = WinButton("close"); self.b_cls.clicked.connect(win.close)
        for b in (self.b_min, self.b_max, self.b_cls):
            lo.addWidget(b)

    def advance(self, dt):
        self.t += dt

    def _toggle_max(self):
        self.win.showNormal() if self.win.isMaximized() else self.win.showMaximized()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(C_PANEL))
        p.fillRect(0, h - 1, w, 1, QColor(C_BORDER))
        p.fillRect(0, h - 2, 112, 2, QColor(C_CRIMSON))
        p.end()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag = ev.globalPosition().toPoint() - self.win.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev):
        if self._drag is not None and ev.buttons() & Qt.MouseButton.LeftButton:
            if self.win.isMaximized():
                self.win.showNormal()
                self._drag = QPoint(self.win.width() // 2, 20)
            self.win.move(ev.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _ev):
        self._drag = None

    def mouseDoubleClickEvent(self, _ev):
        self._toggle_max()


# ── Panels & controls ──────────────────────────────────────────────────────
class Panel(QFrame):
    def __init__(self, title="", accent=C_TOXIC, parent=None):
        super().__init__(parent)
        self.accent = accent
        self.title = title
        self.body = QVBoxLayout(self)
        top = 56 if title else 18
        self.body.setContentsMargins(18, top, 18, 18)
        self.body.setSpacing(12)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        bevel_rect(p, QRect(0, 0, w, h), C_PANEL, border=C_BORDER, radius=10)
        if self.title:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setFont(PFD(8))
            title = p.fontMetrics().elidedText(self.title,
                Qt.TextElideMode.ElideRight, max(0, w - 54))
            draw_label(p, QRect(20, 2, w - 40, 44), title, 8, C_TEXT)
            p.setPen(QPen(_qc(C_BORDER, 190), 1))
            p.drawLine(QPointF(20, 47.5), QPointF(w - 20, 47.5))
            p.setPen(QPen(QColor(C_CRIMSON), 2))
            p.drawLine(QPointF(20, 47.5), QPointF(57, 47.5))
        p.end()


class HazardButton(QPushButton):
    """Extruded along Z — the face physically drops onto its base when pressed."""

    def __init__(self, text, accent=C_TOXIC, primary=True, depth=5, parent=None):
        super().__init__(text, parent)
        self.accent = accent
        self.setAccessibleName(text)
        self.primary = primary
        self.depth = depth
        self.setFont(PFD(7))
        self.t = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hot = 0.0
        self._press = 0.0
        self._a = QVariantAnimation(self, duration=170,
                                    startValue=0.0, endValue=1.0)
        self._a.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._a.valueChanged.connect(self._set_hot)
        self._pa = QVariantAnimation(self, duration=90,
                                     startValue=0.0, endValue=1.0)
        self._pa.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._pa.valueChanged.connect(self._set_press)

    def advance(self, dt):
        self.t += dt
        if self.isEnabled() and self.primary:
            self.update()

    def _set_hot(self, v):
        self._hot = float(v); self.update()

    def _set_press(self, v):
        self._press = float(v); self.update()

    def enterEvent(self, e):
        if self.isEnabled():
            self._a.setDirection(QVariantAnimation.Direction.Forward); self._a.start()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._a.setDirection(QVariantAnimation.Direction.Backward); self._a.start()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        self._pa.setDirection(QVariantAnimation.Direction.Forward); self._pa.start()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._pa.setDirection(QVariantAnimation.Direction.Backward); self._pa.start()
        super().mouseReleaseEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        enabled = self.isEnabled()
        hot = self._hot if enabled else 0.0
        down = self.isDown() and enabled
        w, h = self.width(), self.height()
        shift = 2 if down else 0
        face = QRect(0, shift, w, h - shift)
        if not enabled:
            fill, text, border = C_PANEL, C_FAINT, C_BORDER
        elif self.primary:
            # Blood face, bone label - AniVerse's primary/onPrimary pair.
            fill = _lerp_hex(self.accent, C_TOXIC2, hot * 0.35)
            text, border = C_VOID, None
        else:
            fill = _lerp_hex(C_CARD, self.accent, hot * 0.10)
            text = C_TEXT
            border = _lerp_hex(C_BORDER, C_EDGE, hot)
        bevel_rect(p, face, fill, border=border, bw=2, inset=not down,
                   shadow=3, bevel=True)
        p.setFont(PFD(7))
        label = p.fontMetrics().elidedText(self.text(),
            Qt.TextElideMode.ElideRight, max(0, w - 26))
        draw_label(p, face, label, 7, text,
                   Qt.AlignmentFlag.AlignCenter,
                   outline=1 if self.primary else 0)
        p.end()


class GhostButton(QPushButton):
    def __init__(self, text, accent=C_TOXIC, parent=None):
        super().__init__(text, parent)
        self.accent = accent
        self.setAccessibleName(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(32)
        self.setFont(PFD(5))
        self._hot = 0.0
        self._a = QVariantAnimation(self, duration=150,
                                    startValue=0.0, endValue=1.0)
        self._a.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._a.valueChanged.connect(self._set_hot)
        self.setMinimumWidth(self.fontMetrics().horizontalAdvance(text) + 26)

    def _set_hot(self, v):
        self._hot = float(v); self.update()

    def enterEvent(self, e):
        self._a.setDirection(QVariantAnimation.Direction.Forward); self._a.start()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._a.setDirection(QVariantAnimation.Direction.Backward); self._a.start()
        super().leaveEvent(e)

    def setText(self, t):
        super().setText(t)
        self.setMinimumWidth(self.fontMetrics().horizontalAdvance(t) + 26)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        enabled = self.isEnabled()
        hot = self._hot if enabled else 0.0
        w, h = self.width(), self.height()
        fill = _lerp_hex(C_PANEL, self.accent, hot * 0.22)
        bevel_rect(p, QRect(0, 0, w, h), fill, border=C_BORDER, bw=2,
                   shadow=2, bevel=True)
        colour = (_lerp_hex(C_MUTE, C_TOXIC2, hot) if enabled else C_FAINT)
        p.setFont(PFD(5))
        label = p.fontMetrics().elidedText(self.text(),
            Qt.TextElideMode.ElideRight, max(0, w - 10))
        draw_label(p, QRect(0, 0, w - 2, h - 2), label, 5, colour,
                   Qt.AlignmentFlag.AlignCenter, outline=0)
        p.end()


class Segmented(QWidget):
    def __init__(self, values, emoji=None, on_change=None, accent=C_TOXIC,
                 compact=False, emoji_only=False, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent")
        self.values = list(values)
        self.setAccessibleName("Choose " + ", ".join(self.values))
        self.emoji = emoji or {}
        self.emoji_only = emoji_only
        self.accent = accent
        self._value = values[0]
        self._on_change = on_change
        self._hot_i = -1
        self._pos = 0.0
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(36 if compact else 40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QVariantAnimation(self, duration=280)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_pos)

    def _set_pos(self, v):
        self._pos = float(v); self.update()

    def _seg_w(self):
        return self.width() / max(1, len(self.values))

    def _label(self, v):
        if self.emoji_only and v in self.emoji:
            return self.emoji[v]
        return (f"{self.emoji[v]} " if v in self.emoji else "") + v.title()

    def mouseMoveEvent(self, ev):
        i = int(ev.position().x() // self._seg_w())
        i = max(0, min(len(self.values) - 1, i))
        if i != self._hot_i:
            self._hot_i = i; self.update()

    def leaveEvent(self, _e):
        self._hot_i = -1; self.update()

    def mousePressEvent(self, ev):
        i = int(ev.position().x() // self._seg_w())
        if 0 <= i < len(self.values):
            self._select(self.values[i])

    def _select(self, v):
        if v == self._value:
            return
        self._value = v
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(float(self.values.index(v)))
        self._anim.start()
        if self._on_change:
            self._on_change(v)

    def value(self):
        return self._value

    def setValue(self, v):
        if v in self.values:
            self._value = v
            self._anim.stop()
            self._pos = float(self.values.index(v))
            self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        sw = self._seg_w()
        enabled = self.isEnabled()
        border = self.accent if (self.hasFocus() and enabled) else C_BORDER
        bevel_rect(p, QRect(0, 0, w, h), C_INPUT, light=C_VOID,
                   dark=_qc(C_EDGE, 60), border=border, bw=2, inset=True)
        sel = QRect(int(self._pos * sw) + 3, 3, int(sw) - 6, h - 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.fillRect(sel, _qc(self.accent, 70 if enabled else 28))
        p.fillRect(QRect(sel.x(), sel.y(), sel.width(), 2),
                   _qc(self.accent, 235 if enabled else 60))
        for i, value in enumerate(self.values):
            active = value == self._value
            if not enabled:
                color = C_FAINT
            elif active:
                color = C_TOXIC2
            elif i == self._hot_i:
                color = C_TEXT
            else:
                color = C_MUTE
            size = 5 if len(self.values) > 4 else 6
            p.setFont(PFD(size))
            label = p.fontMetrics().elidedText(self._label(value),
                        Qt.TextElideMode.ElideRight, max(0, int(sw - 10)))
            draw_label(p, QRect(int(i * sw), 0, int(sw), h - 2), label, size,
                       color, Qt.AlignmentFlag.AlignCenter, outline=0)
        p.end()

    def keyPressEvent(self, event):
        keys = (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Home, Qt.Key.Key_End)
        if event.key() in keys and self.isEnabled():
            index = self.values.index(self._value)
            if event.key() == Qt.Key.Key_Home:
                index = 0
            elif event.key() == Qt.Key.Key_End:
                index = len(self.values) - 1
            else:
                index = max(0, min(len(self.values) - 1,
                    index + (-1 if event.key() == Qt.Key.Key_Left else 1)))
            self._select(self.values[index])
            event.accept()
        else:
            super().keyPressEvent(event)


class NeonCheck(QCheckBox):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(26)
        self.setMinimumWidth(self._text_w(text))
        self._t = 0.0
        self._a = QVariantAnimation(self, duration=190,
                                    startValue=0.0, endValue=1.0)
        self._a.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._a.valueChanged.connect(self._set_t)
        self.toggled.connect(self._on_toggle)

    def _set_t(self, v):
        self._t = float(v); self.update()

    def _on_toggle(self, on):
        self._a.setDirection(QVariantAnimation.Direction.Forward if on
                             else QVariantAnimation.Direction.Backward)
        self._a.start()

    def _text_w(self, text):
        from PyQt6.QtGui import QFontMetrics
        return 27 + QFontMetrics(PF(9)).horizontalAdvance(text) + 10

    def sizeHint(self):
        return QSize(self._text_w(self.text()), 24)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        enabled = self.isEnabled()
        checked = self.isChecked()
        box = QRect(2, (self.height() - 18) // 2, 18, 18)
        border = C_TOXIC if (checked or self.hasFocus()) else C_EDGE
        bevel_rect(p, box, C_TOXIC if (checked and enabled) else C_INPUT,
                   light=_qc(C_EDGE, 90), dark=C_VOID, border=border, bw=2)
        if checked:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C_VOID if enabled else C_FAINT))
            cx, cy = box.center().x(), box.center().y()
            for dx, dy in [(-3, 0), (-2, 1), (-1, 2), (0, 1), (1, -1),
                           (2, -2), (3, -3)]:                 # chunky pixel tick
                p.drawRect(int(cx + dx) - 1, int(cy + dy) - 1, 2, 2)
        p.setPen(QColor(C_TEXT if enabled else C_FAINT))
        p.setFont(PF(9))
        p.drawText(QRect(28, 0, self.width() - 28, self.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.text())
        p.end()


class ToxicProgress(QWidget):
    """A dosing tube: liquid fill, rising bubbles, meniscus."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(20)
        self.setAccessibleName("Run progress")
        self._target = 0
        self._value = 0.0
        self.t = 0.0
        self.bubbles = [{"x": random.random(), "y": random.random(),
                         "r": random.uniform(1.0, 2.4),
                         "sp": random.uniform(0.25, 0.7)} for _ in range(16)]
        self._a = QVariantAnimation(self, duration=460)
        self._a.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._a.valueChanged.connect(self._set_v)

    def _set_v(self, v):
        self._value = float(v); self.update()

    def setRange(self, *_a): pass
    def setTextVisible(self, *_a): pass

    def setValue(self, v):
        v = max(0, min(100, int(v)))
        self._target = v
        self._a.stop()
        self._a.setStartValue(self._value)
        self._a.setEndValue(float(v))
        self._a.start()

    def value(self):
        return self._target

    def advance(self, dt):
        self.t += dt
        for b in self.bubbles:
            b["y"] -= dt * b["sp"]
            if b["y"] < 0:
                b["y"] = 1.0
                b["x"] = random.random()
        if self._value > 0:
            self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        track = QRect(0, (h - 12) // 2, w, 12)
        bevel_rect(p, track, C_INPUT, light=C_VOID, dark=_qc(C_EDGE, 60),
                   border=C_BORDER, bw=2, inset=True)
        inner = track.adjusted(2, 2, -2, -2)
        filled = int(inner.width() * self._value / 100.0)
        if filled > 0:
            p.setPen(Qt.PenStyle.NoPen)
            p.fillRect(QRect(inner.x(), inner.y(), filled, inner.height()),
                       QColor(C_TOXIC))
            gx = inner.x() + 6                       # chop the bar into cells
            while gx < inner.x() + filled:
                p.fillRect(QRect(gx, inner.y(), 1, inner.height()), QColor(C_INPUT))
                gx += 6
        p.end()


class StatTile(QFrame):
    def __init__(self, caption, color=C_TOXIC, parent=None):
        super().__init__(parent)
        self.caption = caption
        self.setAccessibleName(caption)
        self.color = color
        self.setMinimumHeight(74)
        self._text = "0"
        self._shown = 0.0
        self._numeric = True
        self._flash = 0.0
        self._count = QVariantAnimation(self, duration=560)
        self._count.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._count.valueChanged.connect(self._set_shown)
        self._fl = QVariantAnimation(self, duration=560,
                                     startValue=1.0, endValue=0.0)
        self._fl.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fl.valueChanged.connect(self._set_flash)

    def _set_shown(self, v):
        self._shown = float(v); self.update()

    def _set_flash(self, v):
        self._flash = float(v); self.update()

    def setText(self, text):
        text = str(text)
        if text == self._text:
            return
        old = self._text
        self._text = text
        try:
            target = float(text)
            self._numeric = True
            self._count.stop()
            try:
                self._count.setStartValue(float(old))
            except ValueError:
                self._count.setStartValue(0.0)
            self._count.setEndValue(target)
            self._count.start()
        except ValueError:
            self._numeric = False
        self._fl.stop(); self._fl.start()
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        bevel_rect(p, QRect(0, 0, w, h), C_CARD, border=C_BORDER, bw=2,
                   shadow=3, bevel=True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc(self.color, 150))
        p.drawEllipse(QPointF(16, 20), 3.0, 3.0)
        shown = str(int(round(self._shown))) if self._numeric else self._text
        p.setFont(PFD(14))
        shown = p.fontMetrics().elidedText(shown, Qt.TextElideMode.ElideRight,
                                           max(0, w - 34))
        draw_label(p, QRect(14, 8, w - 34, 30), shown, 14, self.color)
        p.setFont(PFD(6))
        caption = p.fontMetrics().elidedText(self.caption,
                    Qt.TextElideMode.ElideRight, max(0, w - 34))
        draw_label(p, QRect(14, 44, w - 34, 16), caption, 6, C_MUTE, outline=0)
        p.end()


class LinkTally(QWidget):
    """all devices -> link_1 -> 169 reacts · 12 comments"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.setVisible(False)

    def set_rows(self, rows):
        """rows = [(link_no, url, reacted, commented, failed), ...]"""
        self.rows = rows
        self.setVisible(bool(rows))
        self.setMinimumHeight(30 + len(rows) * 52 if rows else 0)
        self.update()

    def paintEvent(self, _e):
        if not self.rows:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w = self.width()
        draw_label(p, QRect(0, 0, w, 20), 'Results by link', 7, C_MUTE, outline=0)
        y = 30
        for no, url, reacted, commented, failed in self.rows:
            draw_label(p, QRect(0, y, 70, 18), f'Link {no}', 6, C_TEXT, outline=0)
            short = url.split('?')[0].rstrip('/').rsplit('/', 1)[-1] if url else ''
            p.setPen(QColor(C_MUTE))
            p.setFont(PF(8))
            p.drawText(QRectF(64, y, max(0, w - 64), 18),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       p.fontMetrics().elidedText(short, Qt.TextElideMode.ElideRight,
                                                max(0, int(w - 64))))
            parts = [(f'{reacted} reacts', C_TOXIC2), (f'{commented} comments', C_VENOM)]
            if failed:
                parts.append((f'{failed} failed', C_BLOOD))
            x = 0
            for label, color in parts:
                p.setPen(QColor(color))
                width = p.fontMetrics().horizontalAdvance(label)
                p.drawText(QRectF(x, y + 20, width, 18), Qt.AlignmentFlag.AlignVCenter, label)
                x += width + 12
            p.setPen(QPen(_qc(C_BORDER, 150), 1))
            p.drawLine(QPointF(0, y + 43), QPointF(w, y + 43))
            y += 52
        p.end()


class Briefing(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.setMinimumHeight(126)

    def set_rows(self, rows):
        self.rows = rows
        self.setMinimumHeight(25 + ((len(rows) + 1) // 2) * 22)
        self.setToolTip('\n'.join(f'{label}: {value}' for label, value, _color in rows))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w = self.width()
        draw_label(p, QRect(0, 0, w, 20), 'Run summary', 7, C_MUTE, outline=0)
        gap = 24
        cell_width = max(0, (w - gap) / 2)
        for i, (label, value, color) in enumerate(self.rows):
            label = {"strain": "Mode", "hosts": "Devices", "samples": "Examples",
                     "vector": "Post", "doses": "Planned actions", "evidence": "Screenshots"}.get(label, label.title())
            value = str(value).replace(" hosts", " devices")
            x = (i % 2) * (cell_width + gap)
            y = 25 + (i // 2) * 22
            label_width = min(104, cell_width * 0.48)
            p.setFont(PF(8))
            p.setPen(QColor(C_MUTE))
            p.drawText(QRectF(x, y, label_width, 18), Qt.AlignmentFlag.AlignVCenter,
                       p.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight,
                                                max(0, int(label_width - 6))))
            p.setFont(PF(8, QFont.Weight.DemiBold))
            p.setPen(QColor(color))
            text = p.fontMetrics().elidedText(str(value), Qt.TextElideMode.ElideRight,
                                               max(0, int(cell_width - label_width)))
            p.drawText(QRectF(x + label_width, y, cell_width - label_width, 18),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)
        p.end()


class DeviceCard(QFrame):
    STATUS_COLORS = {'pending': C_FAINT, 'processing': C_TOXIC,
                     'done': C_TOXIC, 'failed': C_BLOOD, 'suspended': C_HAZARD}

    def __init__(self, serial, parent=None):
        super().__init__(parent)
        self.serial = serial
        self.status = 'pending'
        self.detail = ""
        self.setFixedHeight(58)
        self.t = 0.0
        self._in = 0.0
        self._anim = QVariantAnimation(self, duration=380,
                                       startValue=0.0, endValue=1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_in)

    def _set_in(self, v):
        self._in = float(v); self.update()

    def play_in(self, delay=0):
        QTimer.singleShot(delay, self._anim.start)

    def advance(self, dt):
        if self.status == 'processing':
            self.t += dt
            self.update()

    def set_status(self, status, detail=""):
        self.status = status
        self.detail = detail or status
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        progress = max(0.0, min(1.0, self._in)) if self._anim.state() or self._in else 1.0
        p.setOpacity(progress)
        w, h = self.width(), self.height()
        color = self.STATUS_COLORS.get(self.status, C_MUTE)
        bevel_rect(p, QRect(0, 0, w, h), C_CARD, border=C_BORDER, bw=2,
                   shadow=3, bevel=True)
        pulse = (0.6 + 0.4 * math.sin(self.t * 2.8)
                 if self.status == 'processing' else 1.0)
        # chunky pixel phone as the status chip
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc(color, int(38 * pulse)))
        p.drawRoundedRect(QRectF(11, (h - 28) / 2, 24, 28), 7, 7)
        p.setBrush(_qc(color, int(220 * pulse)))
        p.drawEllipse(QPointF(23, h / 2), 4.0, 4.0)
        p.setFont(PFD(6))
        serial = p.fontMetrics().elidedText(self.serial,
            Qt.TextElideMode.ElideRight, max(0, int(w - 56)))
        draw_label(p, QRect(40, 7, w - 56, 16), serial, 6, C_TEXT, outline=0)
        p.setPen(QColor(color))
        p.setFont(PF(9))
        p.drawText(QRect(40, 28, w - 56, 18), Qt.AlignmentFlag.AlignVCenter,
                   p.fontMetrics().elidedText((self.detail or self.status),
                       Qt.TextElideMode.ElideRight, max(0, int(w - 56))))
        p.end()


class StatusChip(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(150, 32)
        self.text = "DORMANT"
        self.color = C_FAINT
        self.active = False
        self.t = 0.0

    def set_state(self, text, color, active):
        self.text, self.color, self.active = text.upper(), color, active
        self.update()

    def advance(self, dt):
        if self.active:
            self.t += dt
            self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        bevel_rect(p, QRect(0, 0, w, h), _lerp_hex(C_PANEL, self.color, 0.10),
                   border=C_BORDER, bw=2, shadow=2, bevel=True)
        alpha = 0.6 + 0.4 * math.sin(self.t * 2.6) if self.active else 1.0
        p.setPen(Qt.PenStyle.NoPen)
        p.fillRect(QRect(11, h // 2 - 4, 7, 7), _qc(self.color, int(255 * alpha)))
        label = {"DORMANT": "Ready", "SPREADING": "Running", "PURGING": "Stopping",
                 "SATURATED": "Complete", "PURGED": "Stopped"}.get(
                     self.text.upper(), self.text.title())
        p.setFont(PFD(6))
        label = p.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight,
                                           max(0, int(w - 40)))
        draw_label(p, QRect(26, 0, w - 34, h - 2), label, 6, C_TEXT, outline=0)
        p.end()


# ── Splash ─────────────────────────────────────────────────────────────────
class SplashScreen(QWidget):
    BOOT = [
        "Preparing your workspace",
        "Connecting the device bridge",
        "Getting your tools ready",
        "Preparing AI settings",
        "Ready to begin",
    ]

    def __init__(self):
        super().__init__()
        ensure_pixel_fonts()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(680, 420)
        self.setWindowTitle("FBVENOM")
        self.center()
        self._icon = app_icon_pixmap(128)
        self.t = 0.0
        self.tick_count = 0
        self.timer = QTimer()
        self.timer.timeout.connect(self.on_tick)
        self.timer.start(16)
        self.opacity_effect = QGraphicsOpacityEffect()
        self.opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self.opacity_effect)
        self.fade_anim = QPropertyAnimation(self.opacity_effect, b"opacity")
        self.fade_anim.setDuration(430)
        self.fade_anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self.fade_anim.finished.connect(self.on_fade_done)
        self.fade_anim.setStartValue(1.0)
        self.fade_anim.setEndValue(0.0)

    def on_tick(self):
        self.tick_count += 1
        self.t += 0.016
        self.update()

    def on_fade_done(self):
        self.timer.stop()

    def center(self):
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.geometry()
            self.move((geo.width()-self.width())//2, (geo.height()-self.height())//2)

    @staticmethod
    def _phase(t, start, length):
        """Eased 0..1 progress of one beat of the intro."""
        if length <= 0:
            return 1.0
        x = max(0.0, min(1.0, (t - start) / length))
        return 1.0 - (1.0 - x) ** 3          # ease-out cubic

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        t = self.t
        cx, cy = w / 2.0, h * 0.40

        # ── stage: pure black with a faint centre lift ──
        p.fillRect(QRect(0, 0, w, h), QColor(C_VOID))
        lift = QRadialGradient(QPointF(cx, cy), w * 0.55)
        lift.setColorAt(0.0, _qc("#FFFFFF", 16))
        lift.setColorAt(1.0, _qc("#FFFFFF", 0))
        p.fillRect(QRect(0, 0, w, h), QBrush(lift))

        # ── beat 1: the logo rises and settles ──
        appear = self._phase(t, 0.05, 0.60)
        if appear > 0:
            ic = self._icon
            # The sweep is painted INTO a copy of the mark with SourceAtop, so
            # the light only lands on the artwork - never on the empty space
            # around it now that the logo has no background.
            sweep = (t - 0.55) / 0.55
            if ic and not ic.isNull() and 0.0 <= sweep <= 1.0:
                lit = QPixmap(ic.size())
                lit.fill(Qt.GlobalColor.transparent)
                sp = QPainter(lit)
                sp.drawPixmap(0, 0, ic)
                sp.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_SourceAtop)
                x = -ic.width() + sweep * ic.width() * 2.4
                grad = QLinearGradient(x, 0, x + ic.width() * 0.5, 0)
                peak = int(90 * (1 - abs(sweep - 0.5) * 2))
                grad.setColorAt(0.0, _qc("#FFFFFF", 0))
                grad.setColorAt(0.5, _qc("#FFFFFF", max(0, peak)))
                grad.setColorAt(1.0, _qc("#FFFFFF", 0))
                sp.fillRect(lit.rect(), QBrush(grad))
                sp.end()
                ic = lit
            scale = 0.82 + 0.18 * appear
            p.save()
            p.setOpacity(appear)
            p.translate(cx, cy - 26 + (1.0 - appear) * 14)
            p.scale(scale, scale)
            if ic and not ic.isNull():
                p.drawPixmap(QPointF(-ic.width() / 2.0, -ic.height() / 2.0), ic)
            p.restore()

            # a grey ring breathing outward from the mark
            ring = self._phase(t, 0.10, 0.95)
            if 0 < ring < 1:
                radius = 72 + 92 * ring
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(_qc("#FFFFFF", int(70 * (1 - ring))), 1))
                p.drawEllipse(QPointF(cx, cy - 26), radius, radius)


        # ── beat 2: the wordmark fades in, letter-spacing tightening ──
        word = self._phase(t, 0.62, 0.55)
        if word > 0:
            f = QFont(F_DISP, 30, QFont.Weight.DemiBold)
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing,
                               14.0 - 8.0 * word)
            p.setFont(f)
            p.setOpacity(word)
            p.setPen(QColor(C_TEXT))
            p.drawText(QRectF(0, cy + 56, w, 46),
                       Qt.AlignmentFlag.AlignCenter, "FBVENOM")
            p.setOpacity(1.0)

        # ── beat 3: the strapline ──
        strap = self._phase(t, 0.95, 0.50)
        if strap > 0:
            p.setOpacity(strap)
            p.setPen(QColor(C_MUTE))
            p.setFont(PF(12))
            p.drawText(QRectF(0, cy + 104, w, 22), Qt.AlignmentFlag.AlignCenter,
                       "We are Venom. Your comment workspace.")
            p.setOpacity(1.0)

        # ── beat 4: a hairline loader ──
        prog = max(0.0, min(1.0, (t - 0.35) / 1.85))
        bar_w = 280
        bx, by = (w - bar_w) / 2.0, h - 74.0
        p.setPen(Qt.PenStyle.NoPen)
        p.fillRect(QRectF(bx, by, bar_w, 2), _qc("#FFFFFF", 26))
        if prog > 0:
            p.fillRect(QRectF(bx, by, bar_w * prog, 2), QColor(C_TEXT))
        idx = max(0, min(len(self.BOOT) - 1, int((t - 0.35) / 0.42)))
        p.setOpacity(self._phase(t, 0.45, 0.40))
        p.setPen(QColor(C_FAINT))
        p.setFont(PF(10))
        p.drawText(QRectF(0, by + 12, w, 18), Qt.AlignmentFlag.AlignCenter,
                   self.BOOT[idx])
        p.setOpacity(1.0)
        p.end()


# ── Main window ────────────────────────────────────────────────────────────
class PurpleComboBox(QComboBox):
    """Native combo behavior with a consistently rendered disclosure arrow."""

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(C_MUTE if self.isEnabled() else C_FAINT), 1.5,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        x, y = self.width() - 17, self.height() / 2
        arrow = QPainterPath(QPointF(x - 3, y - 1.5))
        arrow.lineTo(x, y + 1.5)
        arrow.lineTo(x + 3, y - 1.5)
        p.drawPath(arrow)
        p.end()


class AISettings(QDialog):
    """Where the AI endpoint lives.

    FBPoison hard-codes one Groq URL in core.py. This is the commenting tool,
    so the endpoint is a first-class setting: pick a provider, paste a key,
    change the model, and Test it before running anything. Saved to
    venom_ai.json beside the program.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("FBVENOM · AI settings")
        self.setMinimumSize(640, 550)
        self.resize(740, 790)
        self.setStyleSheet(
            f"QDialog{{background:{C_BG}}}"
            f"QLabel{{color:{C_MUTE};font-family:'{F_UI}';font-size:12px;"
            "background:transparent}"
            f"QFrame#settingsCard{{background:{C_PANEL};border:1px solid {C_BORDER};"
            "border-radius:8px}"
            + _INPUT_QSS +
            f"QComboBox{{background:{C_INPUT};color:{C_TEXT};border:1px solid {C_BORDER};"
            f"border-radius:8px;padding:9px 12px;font-family:'{F_UI}';font-size:13px}}"
            f"QComboBox:hover{{border-color:{C_EDGE}}}"
            f"QComboBox:focus{{border-color:{C_TOXIC}}}"
            "QComboBox::drop-down{border:0;width:29px}"
            "QComboBox::down-arrow{image:none;width:0;height:0;border:none}"
            f"QComboBox QLineEdit{{background:transparent;color:{C_TEXT};border:0;"
            "border-radius:0;padding:0}"
            f"QComboBox QAbstractItemView{{background:{C_CARD};color:{C_TEXT};"
            f"border:1px solid {C_EDGE};padding:5px;outline:none;"
            f"selection-background-color:{C_TOXIC};selection-color:{C_VOID}}}"
            "QComboBox QAbstractItemView::item{min-height:28px;padding:4px 8px}"
            "QScrollArea{background:transparent;border:0}"
            "QScrollBar:vertical{background:transparent;width:8px;margin:2px 0}"
            f"QScrollBar::handle:vertical{{background:{C_EDGE};min-height:24px;border-radius:3px}}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0}"
            "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent}"
        )
        lo = QVBoxLayout(self)
        lo.setContentsMargins(26, 24, 26, 22)
        lo.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(13)
        mark = QLabel("AI")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(44, 44)
        mark.setStyleSheet(
            f"background:{C_CARD};color:{C_TOXIC2};border:1px solid {C_EDGE};"
            "border-radius:8px;font-size:16px;font-weight:700")
        header.addWidget(mark)
        heading = QVBoxLayout()
        heading.setSpacing(3)
        title = QLabel("AI settings")
        title.setStyleSheet(f"color:{C_TEXT};font-size:23px;font-weight:700")
        heading.addWidget(title)
        subtitle = QLabel("Connect the model that writes your comments.")
        subtitle.setWordWrap(True)
        heading.addWidget(subtitle)
        header.addLayout(heading, 1)
        lo.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setStyleSheet("background:transparent")
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 5, 0)
        body.setSpacing(14)

        def section(title_text, description):
            card = QFrame()
            card.setObjectName("settingsCard")
            card_lo = QVBoxLayout(card)
            card_lo.setContentsMargins(18, 16, 18, 18)
            card_lo.setSpacing(12)
            section_title = QLabel(title_text)
            section_title.setStyleSheet(f"color:{C_TEXT};font-size:14px;font-weight:600")
            card_lo.addWidget(section_title)
            if description:
                text = QLabel(description)
                text.setWordWrap(True)
                text.setStyleSheet(f"color:{C_MUTE};font-size:11px")
                card_lo.addWidget(text)
            grid = QGridLayout()
            grid.setHorizontalSpacing(14)
            grid.setVerticalSpacing(10)
            grid.setColumnMinimumWidth(0, 96)
            grid.setColumnStretch(1, 1)
            card_lo.addLayout(grid)
            body.addWidget(card)
            return grid

        connection = section("Connection", "Use any OpenAI-compatible chat-completions endpoint.")
        self.provider = PurpleComboBox()
        self._keys = list(aiconfig.PRESETS.keys())
        for k in self._keys:
            self.provider.addItem(aiconfig.PRESETS[k]["label"])
        self.provider.currentIndexChanged.connect(self._preset_picked)
        self.provider.setAccessibleName("AI provider")
        self.provider.setMinimumHeight(42)
        connection.addWidget(QLabel("Provider"), 0, 0)
        connection.addWidget(self.provider, 0, 1)

        self.url = QLineEdit()
        self.url.setPlaceholderText("https://.../v1/chat/completions")
        self.url.setAccessibleName("API URL")
        self.url.setMinimumHeight(42)
        connection.addWidget(QLabel("API URL"), 1, 0)
        connection.addWidget(self.url, 1, 1)

        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("API key · optional for a local model")
        self.key.setAccessibleName("API key")
        self.key.setMinimumHeight(42)
        connection.addWidget(QLabel("API key"), 2, 0)
        connection.addWidget(self.key, 2, 1)

        generation = section("Comment generation", "Choose a model, tone, and the interval between AI calls.")
        self.model = PurpleComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.setMinimumHeight(42)
        self.model.setAccessibleName("Main model")
        self.model.lineEdit().setPlaceholderText("Choose or type a model")
        mrow = QHBoxLayout()
        mrow.setSpacing(8)
        mrow.addWidget(self.model, 1)
        self.models_btn = HazardButton("List models", C_VENOM, primary=False, depth=0)
        self.models_btn.setFixedSize(124, 42)
        self.models_btn.clicked.connect(self._list_models)
        mrow.addWidget(self.models_btn)
        generation.addWidget(QLabel("Model"), 0, 0)
        generation.addLayout(mrow, 0, 1)

        self.fallback = PurpleComboBox()
        self.fallback.setEditable(True)
        self.fallback.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.fallback.setMinimumHeight(42)
        self.fallback.setAccessibleName("Fallback model")
        self.fallback.lineEdit().setPlaceholderText("Used if the main model fails")
        generation.addWidget(QLabel("Fallback"), 1, 0)
        generation.addWidget(self.fallback, 1, 1)

        self.gap = QLineEdit()
        self.gap.setPlaceholderText("Seconds between calls, shared by all devices")
        self.gap.setAccessibleName("Rate limit in seconds")
        self.gap.setMinimumHeight(42)
        generation.addWidget(QLabel("Interval (sec)"), 2, 0)
        generation.addWidget(self.gap, 2, 1)

        self.tone = PurpleComboBox()
        self._tones = list(fbcore.tone_names())
        for t in self._tones:
            self.tone.addItem(fbcore.tone_label(t))
        self.tone.setAccessibleName("Comment tone")
        self.tone.setMinimumHeight(42)
        generation.addWidget(QLabel("Tone"), 3, 0)
        generation.addWidget(self.tone, 3, 1)
        body.addStretch(1)
        scroll.setWidget(content)
        lo.addWidget(scroll, 1)

        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setMinimumHeight(16)
        self.result.setStyleSheet(f"color:{C_MUTE};font-size:11px;background:transparent")
        lo.addWidget(self.result)

        row = QHBoxLayout()
        row.setSpacing(10)
        close = HazardButton("Close", C_MUTE, primary=False, depth=0)
        close.setFixedSize(92, 44)
        close.clicked.connect(self.reject)
        row.addWidget(close)
        row.addStretch()
        test = HazardButton("Test connection", C_VENOM, primary=False, depth=0)
        test.setFixedSize(154, 44)
        test.clicked.connect(self._test)
        row.addWidget(test)
        save = HazardButton("Save settings", C_TOXIC, primary=True, depth=0)
        save.setFixedSize(145, 44)
        save.clicked.connect(self._save)
        row.addWidget(save)
        lo.addLayout(row)
        self._load()

    def _load(self):
        cfg = aiconfig.load(force=True)
        prov = cfg.get("provider", "groq")
        if prov in self._keys:
            self.provider.blockSignals(True)
            self.provider.setCurrentIndex(self._keys.index(prov))
            self.provider.blockSignals(False)
        self.url.setText(str(cfg.get("url", "")))
        self.key.setText(str(cfg.get("api_key", "")))
        self.model.setCurrentText(str(cfg.get("model", "")))
        self.fallback.setCurrentText(str(cfg.get("fallback_model", "")))
        self.gap.setText(str(cfg.get("min_gap", 2.5)))
        tone = str(cfg.get("tone", "neutral"))
        if tone in self._tones:
            self.tone.setCurrentIndex(self._tones.index(tone))

    def _preset_picked(self, idx):
        p = aiconfig.PRESETS[self._keys[idx]]
        self.url.setText(p["url"])
        self.model.setCurrentText(p["model"])
        self.fallback.setCurrentText(p["fallback"])
        self.gap.setText(str(p["min_gap"]))
        hint = f"  —  keys: {p['keys_url']}" if p["keys_url"] else ""
        self._say(f"switched to {p['label']}{hint} — press LIST MODELS to load its models", C_MUTE)

    def _list_models(self):
        """Fill both dropdowns with the models the endpoint actually serves.
        Uses whatever URL/key/provider is currently in the dialog, so you can
        list before saving. Editable combos keep any custom name you typed."""
        cfg = self._collect()
        if not str(cfg.get("api_key") or "").strip() and cfg.get("provider") != "local":
            self._say("enter the API key first, then LIST MODELS", C_AMBER)
            return
        self._say("fetching model list…", C_MUTE)
        QApplication.processEvents()
        try:
            ids = aiconfig.list_models(cfg)
        except Exception as e:
            self._say(f"couldn't list models · {type(e).__name__}: {e}", C_AMBER)
            return
        if not ids:
            self._say("endpoint returned no models", C_AMBER)
            return
        cur_m, cur_f = self.model.currentText(), self.fallback.currentText()
        for combo in (self.model, self.fallback):
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(ids)
            combo.blockSignals(False)
        self.model.setCurrentText(cur_m or ids[0])
        self.fallback.setCurrentText(cur_f or ids[0])
        self._say(f"{len(ids)} models loaded — pick one from the Model list", C_TOXIC)

    def _say(self, text, col):
        self.result.setText(text)
        self.result.setStyleSheet(
            f"color:{col};font-family:'{F_MONO}';font-size:10px;"
            f"background:transparent")

    def _collect(self):
        try:
            gap = float(self.gap.text().strip() or 0)
        except ValueError:
            gap = 2.5
        return {"provider": self._keys[self.provider.currentIndex()],
                "url": self.url.text().strip(),
                "api_key": self.key.text().strip(),
                "model": self.model.currentText().strip(),
                "fallback_model": self.fallback.currentText().strip(),
                "min_gap": gap,
                "tone": self._tones[self.tone.currentIndex()]}

    def _save(self):
        cfg = aiconfig.load()
        cfg.update(self._collect())
        aiconfig.save(cfg)
        self._say(f"saved  ·  {aiconfig.describe()}", C_TOXIC)

    def _test(self):
        """Save first, then actually call the endpoint. A green result here
        means comments will really be generated during a run."""
        self._save()
        self._say("calling the endpoint…", C_MUTE)
        QApplication.processEvents()
        try:
            out = fbcore.unique_comment("grabe ang ganda nito")
            if out and out.strip().lower() != "grabe ang ganda nito":
                self._say(f"OK  ·  it replied: {out}", C_TOXIC)
            else:
                self._say("reachable, but it echoed the example back — check "
                          "the model name.", C_AMBER)
        except Exception as e:
            self._say(f"FAILED  ·  {type(e).__name__}: {e}", C_BLOOD)


class UpdateDialog(QDialog):
    """Install a published build, or go back to the one it replaced.

    Network work runs on a worker thread and reports through a polled result
    slot rather than Qt signals - the same queue-and-timer shape the main
    window already uses, so there is one concurrency pattern in this file.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("FBVENOM · Updates")
        self.setMinimumWidth(560)
        self.setStyleSheet(
            f"QDialog{{background:{C_BG}}}"
            f"QLabel{{color:{C_MUTE};font-family:'{F_UI}';font-size:12px;"
            "background:transparent}"
            + _INPUT_QSS)
        self._busy = False
        self._result = None
        self._manifest = None
        self._staged = None

        lo = QVBoxLayout(self)
        lo.setContentsMargins(24, 22, 24, 20)
        lo.setSpacing(14)

        title = QLabel("Updates")
        title.setStyleSheet(f"color:{C_TEXT};font-size:20px;font-weight:600;"
                            f"font-family:'{F_DISP}';background:transparent")
        lo.addWidget(title)

        self.installed = QLabel(f"Installed version  {updater.APP_VERSION}")
        self.installed.setStyleSheet(
            f"color:{C_TEXT};font-size:13px;background:transparent")
        lo.addWidget(self.installed)

        lo.addWidget(self._label("UPDATE ADDRESS"))
        # Pre-filled with the permanent release channel, so a fresh PC has
        # nothing to type. Still editable if the channel ever moves.
        self.url = QLineEdit(updater.load_settings().get("manifest_url", ""))
        self.url.setAccessibleName("Update address")
        lo.addWidget(self.url)

        self.status = QLabel("Check whether a newer build has been published.")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(48)
        lo.addWidget(self.status)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.check_btn = GhostButton("Check for update")
        self.check_btn.clicked.connect(self.check)
        row.addWidget(self.check_btn)
        self.install_btn = GhostButton("Install update", C_TOXIC)
        self.install_btn.clicked.connect(self.install)
        self.install_btn.setEnabled(False)
        row.addWidget(self.install_btn)
        row.addStretch()
        self.back_btn = GhostButton("Go back to previous build", C_BLOOD)
        self.back_btn.clicked.connect(self.rollback)
        self.back_btn.setEnabled(updater.can_downgrade())
        row.addWidget(self.back_btn)
        lo.addLayout(row)

        if updater.can_downgrade():
            was = updater.previous_build_version()
            self.status.setText(
                f"A previous build is kept on this PC{f' (v{was})' if was else ''}"
                " - you can go back to it if this one misbehaves.")

        self._poll = QTimer(self)
        self._poll.timeout.connect(self._drain)
        self._poll.start(120)

    def _label(self, text):
        lab = QLabel(text)
        lab.setStyleSheet(_FIELD_LBL)
        return lab

    # ── worker plumbing ────────────────────────────────────────────────
    def _run(self, fn):
        """Run one network step off the UI thread; _drain picks up the result."""
        if self._busy:
            return
        self._busy = True
        self._set_enabled(False)

        def work():
            try:
                self._result = ("ok", fn())
            except Exception as exc:                  # shown, never raised
                self._result = ("err", exc)
        threading.Thread(target=work, daemon=True).start()

    def _set_enabled(self, on):
        self.check_btn.setEnabled(on)
        self.install_btn.setEnabled(on and self._manifest is not None)
        self.back_btn.setEnabled(on and updater.can_downgrade())

    def _drain(self):
        if self._result is None:
            return
        kind, payload = self._result
        self._result = None
        self._busy = False
        if kind == "err":
            self._set_enabled(True)
            self.status.setText(str(payload))
            return
        self._finish(payload)

    def _finish(self, payload):
        step, value = payload
        if step == "check":
            manifest, available = value
            if available:
                self._manifest = manifest
                notes = f"\n{manifest['notes']}" if manifest["notes"] else ""
                self.status.setText(
                    f"Version {manifest['version']} is available "
                    f"(you have {updater.APP_VERSION}).{notes}")
            else:
                self._manifest = None
                self.status.setText(
                    f"You already have the newest build "
                    f"({manifest['version']}).")
            self._set_enabled(True)
        elif step == "download":
            self._staged = value
            self.status.setText("Download verified. Restarting into the new "
                                "build…")
            updater.install(self._staged)
            QTimer.singleShot(400, QApplication.quit)

    # ── actions ────────────────────────────────────────────────────────
    def check(self):
        url = self.url.text().strip()
        self.status.setText("Checking…")
        self._manifest = None
        self._run(lambda: ("check", updater.check(url)))

    def install(self):
        if not self._manifest:
            return
        self.status.setText("Downloading…")

        def report(done, total):
            if total:
                self.status.setText(f"Downloading… {done * 100 // total}%")
        self._run(lambda: ("download", updater.download(self._manifest, report)))

    def rollback(self):
        was = updater.previous_build_version()
        confirm = QMessageBox.question(
            self, "Go back to the previous build?",
            "This restarts FBVenom on the build that was replaced"
            + (f" (v{was})." if was else ".")
            + "\n\nThe current build is kept, so you can move forward again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            updater.downgrade()
        except Exception as exc:
            self.status.setText(str(exc))
            return
        self.status.setText("Restarting into the previous build…")
        QTimer.singleShot(400, QApplication.quit)


class WorkspaceDialog(QDialog):
    """Name a workspace before assigning its device count."""

    def __init__(self, parent, workspace=None):
        super().__init__(parent)
        self.setWindowTitle("Edit workspace" if workspace else "New workspace")
        self.setMinimumWidth(420)
        self.setStyleSheet(f"QDialog{{background:{C_PANEL};color:{C_TEXT}}}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        layout.addWidget(parent._field_label("Workspace name"))
        self.name = QLineEdit((workspace or {}).get("name", ""))
        self.name.setPlaceholderText("e.g. Morning comments")
        self.name.setAccessibleName("Workspace name")
        self.name.setStyleSheet(_INPUT_QSS)
        layout.addWidget(self.name)
        layout.addWidget(parent._field_label("Devices to assign"))
        self.count = QSpinBox()
        self.count.setRange(1, 1000000)
        self.count.setValue((workspace or {}).get("device_count", 1))
        self.count.setAccessibleName("Devices to assign")
        self.count.setStyleSheet(
            f"QSpinBox{{background:{C_INPUT};color:{C_TEXT};border:1px solid {C_BORDER};"
            "border-radius:8px;padding:10px;padding-right:32px;font-size:13px}"
            f"QSpinBox::up-button,QSpinBox::down-button{{background:{C_TOXIC2};"
            "subcontrol-origin:padding;width:25px;"
            f"border-left:1px solid {C_BORDER}}}"
            "QSpinBox::up-button{subcontrol-position:top right;border-top-right-radius:0px;}"
            "QSpinBox::down-button{subcontrol-position:bottom right;border-bottom-right-radius:0px;}"
            f"QSpinBox::up-button:hover,QSpinBox::down-button:hover{{background:{C_VENOM}}}"
            f"QSpinBox::up-button:pressed,QSpinBox::down-button:pressed{{background:{C_TOXIC}}}"
            "QSpinBox::up-arrow{image:url(:/qt-project.org/styles/commonstyle/images/arrow-up-16.png);"
            "width:12px;height:12px;}"
            "QSpinBox::down-arrow{image:url(:/qt-project.org/styles/commonstyle/images/arrow-down-16.png);"
            "width:12px;height:12px;}")
        layout.addWidget(self.count)
        note = QLabel("Connected, unmuted devices are assigned automatically. "
                      "Each device belongs to one named workspace.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{C_MUTE};font-size:12px")
        layout.addWidget(note)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color:{C_HAZARD};font-size:12px")
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        cancel = GhostButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save = GhostButton("Save workspace" if workspace else "Create workspace")
        self.save.clicked.connect(self._submit)
        buttons.addWidget(self.save)
        layout.addLayout(buttons)
        self._editing = workspace is not None
        self.name.textChanged.connect(lambda text: self.save.setEnabled(bool(text.strip())))
        self.save.setEnabled(bool(self.name.text().strip()))
        self.name.setFocus()

    def _submit(self):
        parent = self.parent()
        result = (parent.edit_workspace(self.name.text(), self.count.value())
                  if self._editing else
                  parent.create_workspace(self.name.text(), self.count.value()))
        if result:
            self.accept()
        else:
            self.error.setText(parent.workspace_notice.text())


class ReactionGUI(QMainWindow):
    def __init__(self, workspace_path=None):
        super().__init__()
        ensure_pixel_fonts()
        self.setWindowTitle("FBVenom")
        self.setWindowIcon(QIcon(app_icon_pixmap(256)))
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(1060, 740)
        self.resize(1280, 980)
        self.running        = False
        self.stop_flag      = False
        self.reaction_count = 0
        self.device_count   = 0
        self.total_devices  = 0
        self.start_time     = 0
        self.summary_mode   = False
        self.device_cards   = {}
        self.output_queue   = queue.Queue()
        self.tick           = 0
        self.active_devices = []
        self.device_comment = {}      # serial -> posts comments?
        self._device_locks = {}       # serial -> lock (ADB access)
        self._locks_guard = threading.Lock()
        self.tally_acc = {}           # link_no -> totals across devices
        self._dev_commented = {}      # serial -> {account_num} that commented
        self._dev_reacted = {}        # serial -> {account_num} that reacted
        self._dev_seen = {}           # serial -> highest account_num processed
        self._devices_full = set()    # serials whose accounts all completed
        self.device_pills = {}
        self.device_states = {}
        self.current_workspace_id = None
        self._loading_workspace = True
        self._normal_draft = None
        self._normal_selection = []
        # Session overview numbers belong to whoever produced them: one entry
        # per workspace id, plus the original Comments tab's own set.
        self._workspace_stats = {}
        self._normal_stats = None
        self.workspace_store = WorkspaceStore(
            workspace_path or os.path.join(HERE, "venom_workspaces.json"))
        # Remember how many accounts each phone has, kept beside the workspace
        # file so tests stay isolated. Lets the Devices tile know when every
        # account on a phone has finished, and survives restarts.
        self._accounts_path = os.path.join(
            os.path.dirname(str(self.workspace_store.path)), "venom_accounts.json")
        self._workspace_load_error = ""
        try:
            self.workspace_store.load()
        except (OSError, ValueError) as exc:
            self._workspace_load_error = f"Could not load workspaces: {exc}"
        self._saved_workspaces = copy.deepcopy(self.workspace_store.workspaces)
        self._workspace_save_error = ""
        self._workspace_save_timer = QTimer(self)
        self._workspace_save_timer.setSingleShot(True)
        self._workspace_save_timer.timeout.connect(self._save_workspaces)

        central = RootCanvas(self)
        self.canvas = central
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        self.titlebar = TitleBar(self)
        outer.addWidget(self.titlebar)

        root = QVBoxLayout()
        root.setContentsMargins(22, 16, 22, 18)
        root.setSpacing(12)
        outer.addLayout(root, 1)

        # Branded command header, with provider controls always in reach.
        self.hero = SymbioteBanner()
        self.hero.setMinimumHeight(104)
        hud = QHBoxLayout(self.hero)
        hud.setContentsMargins(20, 12, 20, 12)
        hud.setSpacing(16)
        self.hud_mol = QLabel()
        self.hud_mol.setFixedSize(74, 74)
        self.hud_mol.setPixmap(app_icon_pixmap(74))
        self.hud_mol.setAccessibleName("FBVenom")
        hud.addWidget(self.hud_mol, 0, Qt.AlignmentFlag.AlignTop)
        heading = QVBoxLayout()
        heading.setSpacing(5)
        eyebrow = QLabel("FBVENOM  /  SYMBIOTE CONTROL")
        eyebrow.setStyleSheet(f"color:{C_BLOOD};font-size:9px;font-weight:700;"
                              "letter-spacing:1.6px;background:transparent")
        heading.addWidget(eyebrow)
        title = QLabel("We are Venom.")
        self.workspace_title = title
        title.setStyleSheet(
            f"color:{C_TEXT};font-size:29px;font-weight:800;letter-spacing:-0.7px;font-family:'{F_DISP}';background:transparent")
        heading.addWidget(title)
        self.hud_msg = QLabel("Your links. Your devices. One symbiote.")
        self.hud_msg.setMinimumWidth(0)
        self.hud_msg.setWordWrap(True)
        self.hud_msg.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.hud_msg.setStyleSheet(
            f"color:{C_MUTE};font-size:12px;background:transparent")
        heading.addWidget(self.hud_msg)
        hud.addLayout(heading, 1)
        ai = QVBoxLayout()
        ai.setSpacing(5)
        self.ai_btn = GhostButton("AI settings")
        self.ai_btn.setFixedWidth(136)
        self.ai_btn.setToolTip("Configure your AI provider, model, and comment tone")
        self.ai_btn.clicked.connect(self.open_ai_settings)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch()
        self.update_btn = GhostButton("Updates")
        self.update_btn.setToolTip("Install a published build, or go back to "
                                   "the previous one")
        self.update_btn.clicked.connect(self.open_updates)
        buttons.addWidget(self.update_btn)
        buttons.addWidget(self.ai_btn)
        ai.addLayout(buttons)
        self.ai_status = QLabel("")
        self.ai_status.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.ai_status.setFixedWidth(285)
        self.ai_status.setWordWrap(True)
        ai.addWidget(self.ai_status)
        hud.addLayout(ai)
        root.addWidget(self.hero)

        self.feature_tabs = QTabBar()
        self.feature_tabs.addTab("Comments")
        self.feature_tabs.addTab("Comment to Comment")
        self.feature_tabs.setAccessibleName("Feature tabs")
        self.feature_tabs.setExpanding(False)
        self.feature_tabs.setDrawBase(False)
        self.feature_tabs.setStyleSheet(
            f"QTabBar::tab{{background:{C_PANEL};color:{C_MUTE};padding:10px 22px;"
            f"border:1px solid {C_BORDER};border-radius:8px;margin-right:8px;"
            f"font-family:'{F_DISP}';font-size:12px;font-weight:600}}"
            f"QTabBar::tab:selected{{background:{C_CARD};color:{C_TEXT};"
            f"border-color:{C_CRIMSON}}}"
            f"QTabBar::tab:hover{{color:{C_TEXT}}}")
        root.addWidget(self.feature_tabs)
        self.workspace_bar = QWidget()
        workspace_controls = QVBoxLayout(self.workspace_bar)
        workspace_controls.setContentsMargins(0, 0, 0, 0)
        workspace_controls.setSpacing(8)
        workspace_row = QHBoxLayout()
        workspace_row.addWidget(self._field_label("WORKSPACE"))
        self.workspace_picker = QComboBox()
        self.workspace_picker.setMinimumWidth(190)
        self.workspace_picker.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.workspace_picker.setAccessibleName("Named workspace")
        self.workspace_picker.setStyleSheet(
            f"QComboBox{{background:{C_INPUT};color:{C_TEXT};border:1px solid {C_BORDER};"
            f"border-radius:8px;padding:8px 10px;font-family:'{F_UI}';font-size:12px}}"
            f"QComboBox:hover{{border:1px solid {C_EDGE}}}"
            "QComboBox::drop-down{border:0;width:22px}"
            f"QComboBox QAbstractItemView{{background:{C_PANEL};color:{C_TEXT};"
            f"border:1px solid {C_BORDER};selection-background-color:{C_TOXIC};"
            f"selection-color:{C_VOID};outline:none}}")
        workspace_row.addWidget(self.workspace_picker, 1)
        self.new_workspace_btn = GhostButton("+ New workspace")
        self.new_workspace_btn.clicked.connect(lambda: WorkspaceDialog(self).exec())
        workspace_row.addWidget(self.new_workspace_btn)
        self.edit_workspace_btn = GhostButton("Edit")
        self.edit_workspace_btn.clicked.connect(self._edit_workspace_dialog)
        workspace_row.addWidget(self.edit_workspace_btn)
        self.delete_workspace_btn = GhostButton("Delete", C_BLOOD)
        self.delete_workspace_btn.clicked.connect(self.delete_workspace)
        workspace_row.addWidget(self.delete_workspace_btn)
        workspace_controls.addLayout(workspace_row)
        self.workspace_notice = QLabel("")
        self.workspace_notice.setWordWrap(True)
        self.workspace_notice.setStyleSheet(f"color:{C_MUTE};font-size:11px")
        workspace_controls.addWidget(self.workspace_notice)
        self.workspace_bar.hide()
        root.addWidget(self.workspace_bar)
        self.workspace_save_error = QLabel("")
        self.workspace_save_error.setWordWrap(True)
        self.workspace_save_error.setStyleSheet(f"color:{C_BLOOD};font-size:11px")
        self.workspace_save_error.setAccessibleName("Workspace save error")
        self.workspace_save_error.hide()
        root.addWidget(self.workspace_save_error)

        middle = QHBoxLayout()
        middle.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(16)

        cfg = Panel("01  /  Target links & comments", C_TOXIC)
        self.cfg_panel = cfg
        cfg.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        cfg_lo = cfg.body
        hdr = QHBoxLayout()
        hdr.setSpacing(10)
        self.links_lbl = self._field_label("1 TARGET LINK")
        hdr.addWidget(self.links_lbl)
        strain = QLabel("Comment only")
        strain.setStyleSheet(
            f"color:{C_MUTE};background:{C_CARD};border:1px solid {C_BORDER};"
            f"border-radius:6px;font-family:'{F_DISP}';padding:5px 10px;font-size:10px")
        hdr.addWidget(strain)
        hdr.addStretch()
        self.add_link_btn = GhostButton("+  Add link")
        self.add_link_btn.clicked.connect(lambda: self.add_link_row(animate=True))
        hdr.addWidget(self.add_link_btn)
        cfg_lo.addLayout(hdr)

        scroll_style = (
            "QScrollArea{border:none;background:transparent}"
            f"QScrollBar:vertical{{background:transparent;width:8px;margin:2px 0}}"
            f"QScrollBar::handle:vertical{{background:{C_EDGE};min-height:24px;border-radius:3px}}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0}"
            "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent}"
        )
        self.links_scroll = QScrollArea()
        self.links_scroll.setWidgetResizable(True)
        self.links_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.links_scroll.setStyleSheet(scroll_style)
        self.links_scroll.setMinimumHeight(125)
        self.links_host = QWidget()
        self.links_host.setStyleSheet("background:transparent")
        self.links_layout = QVBoxLayout(self.links_host)
        self.links_layout.setContentsMargins(0, 0, 4, 0)
        self.links_layout.setSpacing(10)
        self.links_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.links_scroll.setWidget(self.links_host)
        cfg_lo.addWidget(self.links_scroll, 1)
        self.link_rows = []
        self.reaction_lbl = self._field_label("")
        self.reaction_lbl.setVisible(False)
        self.comment_row = QWidget()
        self.comment_row.setVisible(False)
        self.briefing = Briefing()
        cfg_lo.addWidget(self.briefing)
        left.addWidget(cfg, 3)

        activity = Panel("03  /  Live session activity", C_VENOM)
        self.activity_panel = activity
        activity.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.link_tally = LinkTally()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(scroll_style)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        activity_content = QWidget()
        activity_content.setStyleSheet("background:transparent")
        activity_layout = QVBoxLayout(activity_content)
        activity_layout.setContentsMargins(0, 0, 0, 0)
        activity_layout.setSpacing(12)
        activity_layout.addWidget(self.link_tally)
        self.device_container = QWidget()
        self.device_container.setStyleSheet("background:transparent")
        self.dev_card_layout = QVBoxLayout(self.device_container)
        self.dev_card_layout.setContentsMargins(0, 0, 4, 0)
        self.dev_card_layout.setSpacing(8)
        self.dev_card_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        activity_layout.addWidget(self.device_container, 1)
        scroll.setWidget(activity_content)
        activity.body.addWidget(scroll, 1)
        self.activity_empty = QLabel("Device activity will appear here when a session starts.")
        self.activity_empty.setWordWrap(True)
        self.activity_empty.setStyleSheet(f"color:{C_FAINT};font-size:12px;background:transparent")
        self.dev_card_layout.addWidget(self.activity_empty)
        activity.setMinimumHeight(140)
        left.addWidget(activity, 2)
        middle.addLayout(left, 7)

        right = QVBoxLayout()
        right.setSpacing(16)
        dev = Panel("02  /  Connected devices", C_VENOM)
        dev.setMinimumWidth(300)
        dev.setMaximumHeight(260)
        self.devices_panel = dev
        dev_lo = dev.body
        dhead = QHBoxLayout()
        dhead.setSpacing(6)
        self.dev_count_lbl = QLabel("0 reachable")
        self.dev_count_lbl.setStyleSheet(f"color:{C_MUTE};font-size:11px;background:transparent")
        dhead.addWidget(self.dev_count_lbl)
        dhead.addStretch()
        self.all_btn = GhostButton("SELECT ALL", C_VENOM)
        self.all_btn.clicked.connect(self.toggle_select_all)
        dhead.addWidget(self.all_btn)
        self.refresh_btn = GhostButton("Rescan", C_VENOM)
        self.refresh_btn.clicked.connect(self.refresh_devices)
        dhead.addWidget(self.refresh_btn)
        dev_lo.addLayout(dhead)
        self.dev_list = QListWidget()
        self.dev_list.setMinimumHeight(100)
        self.dev_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self.dev_list.setStyleSheet(
            f"QListWidget{{background:{C_INPUT};border:1px solid {C_BORDER};"
            "border-radius:8px;padding:4px;outline:none}"
            "QListWidget::item{border:2px solid transparent;border-radius:8px;margin:2px}"
            f"QListWidget::item:selected{{background:{C_CARD};border:1px solid {C_EDGE}}}"
            f"QListWidget::item:hover{{background:{C_PANEL}}}"
            f"QScrollBar:vertical{{background:transparent;width:8px;margin:0}}"
            f"QScrollBar::handle:vertical{{background:{C_EDGE};min-height:24px;border-radius:3px}}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0}"
            "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent}")
        self.dev_list.setAccessibleName("Connected devices")
        self.dev_list.setMouseTracking(True)
        self.dev_list.viewport().setMouseTracking(True)
        self.dev_list.itemEntered.connect(self.on_device_hover)
        self.account_counts = self._load_account_counts()
        self.account_scan_inflight = set()
        self.dev_items = {}
        dev_lo.addWidget(self.dev_list, 1)
        device_hint = QLabel("Select devices to include. Hover to count accounts.")
        self.device_hint = device_hint
        device_hint.setWordWrap(True)
        device_hint.setStyleSheet(f"color:{C_FAINT};font-size:10px;background:transparent")
        dev_lo.addWidget(device_hint)
        right.addWidget(dev, 0)

        ops = Panel("Session overview", C_TOXIC)
        self.overview_panel = ops
        run_lo = ops.body
        srow = QHBoxLayout()
        self.status_label = QLabel("READY")
        self.status_label.setStyleSheet(
            f"color:{C_TEXT};font-size:14px;font-weight:600;font-family:'{F_DISP}';background:transparent")
        srow.addWidget(self.status_label)
        srow.addStretch()
        self.stat_time = QLabel("00:00")
        self.stat_time.setToolTip("Session elapsed time")
        self.stat_time.setStyleSheet(
            f"color:{C_TEXT};font-size:16px;font-weight:600;"
            f"font-family:'{F_MONO}';background:transparent")
        srow.addWidget(self.stat_time)
        run_lo.addLayout(srow)
        self.msg_label = QLabel("Your next session is ready to configure.")
        self.msg_label.setWordWrap(True)
        self.msg_label.setMinimumWidth(0)
        self.msg_label.setMinimumHeight(30)
        self.msg_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.msg_label.setStyleSheet(f"color:{C_MUTE};font-size:11px;background:transparent")
        run_lo.addWidget(self.msg_label)
        self.progress_bar = ToxicProgress()
        run_lo.addWidget(self.progress_bar)
        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.stat_devices = StatTile("Devices", C_TEXT)
        self.stat_reactions = StatTile("Comments", C_TOXIC2)
        tiles.addWidget(self.stat_devices)
        tiles.addWidget(self.stat_reactions)
        run_lo.addLayout(tiles)
        self.session_options_btn = GhostButton("Session options  ▸")
        self.session_options_btn.setCheckable(True)
        self.session_options_btn.setAccessibleName("Show or hide session options")
        run_lo.addWidget(self.session_options_btn)
        self.session_options = QWidget()
        self.session_options.setStyleSheet("background:transparent")
        options_lo = QVBoxLayout(self.session_options)
        options_lo.setContentsMargins(0, 0, 0, 0)
        options_lo.setSpacing(12)
        self.session_options_btn.toggled.connect(self._toggle_session_options)
        pace = QHBoxLayout()
        pace.addWidget(self._field_label("RUN SPEED"))
        pace.addStretch()
        self.dose_rate = DoseRate(on_change=self._set_speed)
        pace.addWidget(self.dose_rate)
        options_lo.addLayout(pace)
        self.screenshot_check = NeonCheck("Save screenshots after each comment")
        self.screenshot_check.setChecked(False)
        options_lo.addWidget(self.screenshot_check)
        self.open_shots_btn = GhostButton("Open screenshots folder")
        self.open_shots_btn.clicked.connect(self.open_screenshots_folder)
        options_lo.addWidget(self.open_shots_btn, 0, Qt.AlignmentFlag.AlignLeft)
        run_lo.addWidget(self.session_options)
        self.session_options.hide()
        right.addWidget(ops, 1)
        middle.addLayout(right, 4)
        self.middle_layout = middle
        # The workspace may scroll on short displays; the action bar stays fixed.
        self.workspace_scroll = QScrollArea()
        self.workspace_scroll.setWidgetResizable(True)
        self.workspace_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.workspace_scroll.setStyleSheet(scroll_style)
        workspace = QWidget()
        workspace.setStyleSheet("background:transparent")
        middle.setContentsMargins(0, 0, 3, 0)
        workspace.setLayout(middle)
        workspace.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        workspace.setMinimumHeight(middle.minimumSize().height())
        self.workspace_scroll.setWidget(workspace)
        root.addWidget(self.workspace_scroll, 1)

        # Persistent run controls stay visible at every supported window size.
        self.cmdbar = _CommandBar()
        self.cmdbar.setFixedHeight(74)
        cb = QHBoxLayout(self.cmdbar)
        cb.setContentsMargins(18, 12, 14, 12)
        cb.setSpacing(12)
        self.status_chip = StatusChip()
        cb.addWidget(self.status_chip)
        hint = QLabel("One workspace. All selected devices.")
        self.run_hint = hint
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{C_MUTE};font-size:11px;background:transparent")
        cb.addWidget(hint, 1)
        self.stop_btn = HazardButton("Stop session", C_BLOOD, primary=False, depth=0)
        self.stop_btn.setFixedSize(140, 46)
        self.stop_btn.clicked.connect(self.stop_reaction)
        self.stop_btn.setEnabled(False)
        cb.addWidget(self.stop_btn)
        self.start_btn = HazardButton("ENVENOM  →", C_TOXIC, primary=True, depth=0)
        self.start_btn.setFixedSize(190, 46)
        self.start_btn.clicked.connect(self.start_reaction)
        cb.addWidget(self.start_btn)
        root.addWidget(self.cmdbar)
        self._build_diagnostics(root)

        central.setStyleSheet(f"QWidget{{color:{C_TEXT};font-family:'{F_UI}'}}")
        self.setFont(PF(10))     # crisp, non-antialiased text on plain labels too

        # decorative injection FX, on top of everything, click-through
        self.inject_fx = InjectOverlay(central)
        self.inject_fx.setGeometry(0, 0, self.width(), self.height())
        self.layout_fx = PanelFlipFX(central)
        self.layout_fx.setGeometry(0, 0, self.width(), self.height())

        # per-row fields wire themselves up in LinkRow(on_change=...)
        self.screenshot_check.toggled.connect(lambda _b: self.update_briefing())
        self.dev_list.itemSelectionChanged.connect(self.update_briefing)

        fbcore.set_speed(self.dose_rate.profile())
        self.refresh_ai_status()

        self.add_link_row()          # always start with one target
        self.update_action_fields()
        self.refresh_devices()
        self._loading_workspace = False
        self._normal_draft = self._draft_values()
        self.feature_tabs.currentChanged.connect(self._feature_changed)
        self.workspace_picker.currentIndexChanged.connect(self._workspace_picked)
        self._refresh_workspace_picker()

        self.timer = QTimer()
        self.timer.timeout.connect(self.process_queue)
        self.timer.start(100)
        self.anim_timer = QTimer()
        self.anim_timer.timeout.connect(self.animate_status)
        self.anim_timer.start(33)

    def _toggle_session_options(self, expanded):
        self.session_options.setVisible(expanded)
        self.session_options_btn.setText("Session options  ▾" if expanded else "Session options  ▸")
        self.update_briefing()

    def _build_diagnostics(self, root):
        self._diagnostic_events = []
        self._diagnostic_errors = 0
        self._diagnostic_warnings = 0
        self.diagnostics = QFrame()
        self.diagnostics.setStyleSheet(
            f"QFrame{{background:{C_PANEL};border:1px solid {C_BORDER};border-radius:9px}}")
        layout = QVBoxLayout(self.diagnostics)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header.setSpacing(8)
        self.diagnostics_toggle = GhostButton("▸  Diagnostics · no errors")
        self.diagnostics_toggle.setAccessibleName("Show or hide diagnostics")
        self.diagnostics_toggle.setCheckable(True)
        self.diagnostics_toggle.clicked.connect(self._toggle_diagnostics)
        header.addWidget(self.diagnostics_toggle)
        self.diagnostics_hint = QLabel("Errors stay here after the session.")
        self.diagnostics_hint.setStyleSheet(
            f"color:{C_FAINT};font-size:10px;border:none;background:transparent")
        self.diagnostics_hint.setMinimumWidth(0)
        self.diagnostics_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header.addWidget(self.diagnostics_hint, 1)
        self.copy_diagnostics_btn = GhostButton("Copy log")
        self.copy_diagnostics_btn.setToolTip("Copy printable errors and troubleshooting steps")
        self.copy_diagnostics_btn.clicked.connect(self._copy_diagnostics)
        header.addWidget(self.copy_diagnostics_btn)
        self.clear_diagnostics_btn = GhostButton("Clear")
        self.clear_diagnostics_btn.clicked.connect(self._clear_diagnostics)
        header.addWidget(self.clear_diagnostics_btn)
        layout.addLayout(header)
        self.diagnostics_log = QPlainTextEdit()
        self.diagnostics_log.setAccessibleName("Session diagnostics log")
        self.diagnostics_log.setReadOnly(True)
        self.diagnostics_log.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.diagnostics_log.document().setMaximumBlockCount(4000)
        self.diagnostics_log.setFixedHeight(138)
        self.diagnostics_log.setStyleSheet(
            f"QPlainTextEdit{{background:{C_INPUT};color:{C_MUTE};border:1px solid {C_BORDER};"
            f"border-radius:6px;padding:8px;font:10px '{F_MONO}';"
            f"selection-background-color:{C_EDGE}}}")
        self.diagnostics_log.setPlaceholderText("No diagnostics yet. Errors include a code, context, and a suggested next step.")
        layout.addWidget(self.diagnostics_log)
        self.diagnostics_log.hide()
        root.addWidget(self.diagnostics)
        if self._workspace_load_error:
            self._append_diagnostic({"code": "WORKSPACE_LOAD_FAILED", "severity": "warning",
                                     "message": self._workspace_load_error})

    def _toggle_diagnostics(self, expanded):
        self.diagnostics_toggle.setChecked(bool(expanded))
        self.diagnostics_log.setVisible(bool(expanded))
        self._refresh_diagnostics_label()

    def _refresh_diagnostics_label(self):
        count = len(self._diagnostic_events)
        arrow = "▾" if self.diagnostics_toggle.isChecked() else "▸"
        caption = (f"{self._diagnostic_errors} error{'s' if self._diagnostic_errors != 1 else ''} · "
                   f"{self._diagnostic_warnings} warning{'s' if self._diagnostic_warnings != 1 else ''}"
                   if count else "no errors")
        self.diagnostics_toggle.setText(f"{arrow}  Diagnostics · {caption}")

    @staticmethod
    def _diagnostic_text(event):
        context = []
        for key, label in (("serial", "device"), ("account", "account"), ("link", "link")):
            value = event.get(key)
            if value is not None and value != "":
                context.append(f"{label} {value}")
        stamp = event.get("time") or time.strftime("%H:%M:%S")
        severity = str(event.get("severity", "error")).upper()
        code = str(event.get("code") or "ERROR")
        lines = [f"[{stamp}] {severity} {code}" + (" · " + " · ".join(context) if context else ""),
                 str(event.get("message") or code.replace("_", " ").capitalize())]
        if event.get("detail"):
            lines.append("Detail: " + str(event["detail"]))
        if event.get("hint"):
            lines.append("Next step: " + str(event["hint"]))
        if event.get("url"):
            lines.append("Target: " + str(event["url"]))
        return "\n".join(lines)

    def _append_diagnostic(self, event):
        event = dict(event)
        self._diagnostic_events.append(event)
        if len(self._diagnostic_events) > 500:
            self._diagnostic_events.pop(0)
        self._diagnostic_errors = sum(e.get("severity", "error") == "error"
                                      for e in self._diagnostic_events)
        self._diagnostic_warnings = len(self._diagnostic_events) - self._diagnostic_errors
        # Rebuild only at the bound so the plain-text view and Copy log agree.
        if len(self._diagnostic_events) == 500:
            self.diagnostics_log.setPlainText("\n\n".join(
                self._diagnostic_text(e) for e in self._diagnostic_events))
        else:
            self.diagnostics_log.appendPlainText(self._diagnostic_text(event) + "\n")
        self._refresh_diagnostics_label()
        self.diagnostics_hint.setText(str(event.get("message") or event.get("code", "")))
        self.diagnostics_hint.setToolTip(self.diagnostics_hint.text())
        # Leave layout control with the user; the count remains visible collapsed.

    def _copy_diagnostics(self):
        QApplication.clipboard().setText("\n\n".join(
            self._diagnostic_text(e) for e in self._diagnostic_events))

    def _clear_diagnostics(self):
        self._diagnostic_events.clear()
        self._diagnostic_errors = self._diagnostic_warnings = 0
        self.diagnostics_log.clear()
        self.diagnostics_hint.setText("Errors stay here after the session.")
        self._refresh_diagnostics_label()

    def _workspace_mode(self):
        return self.feature_tabs.currentIndex() == 1

    def _draft_values(self):
        return {"links": [{"url": row.url.text(),
                           "reaction": row.reaction.currentData(),
                           "comment": row.comment.text()} for row in self.link_rows],
                "screenshots": self.screenshot_check.isChecked(),
                "speed": self.dose_rate.profile()}

    def _session_stats(self):
        """Snapshot of the Session overview, so one workspace's device and
        comment counts never show up under another."""
        return {"devices": self.stat_devices._text,
                "comments": self.stat_reactions._text,
                "elapsed": self.stat_time.text(),
                "status": self.status_label.text(),
                "message": self.msg_label.text(),
                "progress": self.progress_bar.value(),
                "tally": copy.deepcopy(self.tally_acc),
                "summary": self.summary_mode,
                # Which phones this side mutes is its own business too: muting a
                # device under one workspace must not mute it under another.
                "device_comment": dict(self.device_comment)}

    def _apply_device_comments(self, mapping):
        """Swap in this side's Comments/Muted switches and re-sync the pills.

        Signals are blocked while the pills are set, so restoring a workspace
        does not look like the operator toggling every device.
        """
        self.device_comment = dict(mapping or {})
        for serial, pill in getattr(self, "device_pills", {}).items():
            pill.blockSignals(True)
            pill.setChecked(self.device_comments(serial))
            pill.blockSignals(False)
            pill.update()

    def _apply_session_stats(self, stats):
        """Put a saved overview back on screen; a workspace that has not run
        yet simply keeps the zeroed dashboard it was just given."""
        if stats is None:
            # Never run and never configured: every device speaks by default.
            self._apply_device_comments({})
            return
        self._apply_device_comments(stats.get("device_comment"))
        self.tally_acc = copy.deepcopy(stats.get("tally") or {})
        self.summary_mode = bool(stats.get("summary"))
        self.stat_devices.setText(stats.get("devices", "0 / 0"))
        self.stat_reactions.setText(stats.get("comments", "0"))
        self.stat_time.setText(stats.get("elapsed", "00:00"))
        self.status_label.setText(stats.get("status", "READY"))
        self.msg_label.setText(stats.get("message", ""))
        self.progress_bar.setValue(stats.get("progress", 0))
        self.refresh_link_tally()

    def _store_session_stats(self):
        """File the current numbers under whichever side produced them.

        Keyed off the workspace id rather than the tab, because on a tab switch
        the tab has already changed by the time this runs.
        """
        if self.current_workspace_id:
            self._workspace_stats[self.current_workspace_id] = self._session_stats()
        else:
            self._normal_stats = self._session_stats()

    def _capture_workspace(self):
        if self._loading_workspace:
            return
        self._store_session_stats()
        workspace = self.workspace_store.get(self.current_workspace_id)
        if workspace is not None:
            workspace.update(self._draft_values())

    def _clear_workspace_save_error(self, notice):
        if self.workspace_notice.text() == self._workspace_save_error:
            self.workspace_notice.setText(notice)
        self._workspace_save_error = ""
        self.workspace_save_error.clear()
        self.workspace_save_error.hide()

    def _save_workspaces(self):
        self._workspace_save_timer.stop()
        if self._workspace_load_error:
            self.workspace_notice.setText(self._workspace_load_error)
            return False
        self._capture_workspace()
        if self.workspace_store.workspaces == self._saved_workspaces:
            self._clear_workspace_save_error("No unsaved workspace changes.")
            return True
        try:
            self.workspace_store.save()
        except (OSError, ValueError) as exc:
            self._workspace_save_error = f"Workspaces could not be saved: {exc}"
            self.workspace_notice.setText(self._workspace_save_error)
            self.workspace_save_error.setText(self._workspace_save_error)
            self.workspace_save_error.show()
            return False
        self._saved_workspaces = copy.deepcopy(self.workspace_store.workspaces)
        self._clear_workspace_save_error("Workspace changes saved.")
        return True

    def _restore_draft(self, draft):
        self._loading_workspace = True
        try:
            for row in self.link_rows:
                self.links_layout.removeWidget(row)
                row.setParent(None)
                row.deleteLater()
            self.link_rows = []
            for values in draft.get("links", []) or [{}]:
                row = self.add_link_row()
                row.url.setText(values.get("url", ""))
                row.comment.setText(values.get("comment", ""))
                index = row.reaction.findData(values.get("reaction", "like"))
                row.reaction.setCurrentIndex(max(0, index))
            self.screenshot_check.setChecked(draft.get("screenshots", False))
            profiles = [step[2] for step in self.dose_rate.STEPS]
            self.dose_rate.set_index(profiles.index(draft.get("speed", "safe")))
        finally:
            self._loading_workspace = False
        self.summary_mode = False
        self.tally_acc = {}
        self.clear_device_cards()
        self.link_tally.set_rows([])
        self.progress_bar.setValue(0)
        self.stat_devices.setText("0 / 0")
        self.stat_reactions.setText("0")
        self.stat_time.setText("00:00")
        self.status_label.setText("READY")
        self.msg_label.setText("Your next session is ready to configure.")
        # ...then put this workspace's own numbers back, if it has run before.
        self._apply_session_stats(
            self._workspace_stats.get(self.current_workspace_id)
            if self._workspace_mode() else self._normal_stats)
        self.update_briefing()

    def _refresh_workspace_picker(self):
        self.workspace_picker.blockSignals(True)
        self.workspace_picker.clear()
        for workspace in self.workspace_store.workspaces:
            count = workspace["device_count"]
            self.workspace_picker.addItem(
                f"{workspace['name']} · {count} device{'s' if count != 1 else ''}",
                workspace["id"])
        if not self.workspace_store.workspaces:
            self.workspace_picker.addItem("Create your first workspace", None)
        index = self.workspace_picker.findData(self.current_workspace_id)
        self.workspace_picker.setCurrentIndex(max(0, index))
        self.workspace_picker.blockSignals(False)

    def _feature_changed(self, index):
        if self.running:
            self.feature_tabs.blockSignals(True)
            self.feature_tabs.setCurrentIndex(1 if self.current_workspace_id else 0)
            self.feature_tabs.blockSignals(False)
            return
        if index == 1:
            self._normal_draft = self._draft_values()
            self._normal_stats = self._session_stats()
            self._normal_selection = [item.data(Qt.ItemDataRole.UserRole)
                                      for item in self.dev_list.selectedItems()]
            workspace = self.workspace_store.get(getattr(self, "_last_workspace_id", None))
            if workspace is None and self.workspace_store.workspaces:
                workspace = self.workspace_store.workspaces[0]
            self.current_workspace_id = workspace["id"] if workspace else None
            self._restore_draft(workspace or {})
        else:
            self._capture_workspace()
            self._last_workspace_id = self.current_workspace_id
            if self.workspace_store.workspaces:
                self._save_workspaces()
            self.current_workspace_id = None
            self._restore_draft(self._normal_draft or {})
        self.workspace_bar.setVisible(index == 1)
        self.all_btn.setVisible(index == 0)
        self.dev_list.setSelectionMode(QListWidget.SelectionMode.NoSelection if index == 1
                                       else QListWidget.SelectionMode.MultiSelection)
        if index == 0:
            for serial, item in self.dev_items.items():
                item.setSelected(serial in self._normal_selection)
        self._refresh_workspace_picker()
        self._refresh_workspace_assignments()
        self._set_session_controls()
        self.update_briefing()

    def _workspace_picked(self, index):
        workspace_id = self.workspace_picker.itemData(index)
        if workspace_id:
            self.select_workspace(workspace_id)

    def select_workspace(self, workspace_id):
        if self.running:
            return False
        workspace = self.workspace_store.get(workspace_id)
        if workspace is None:
            return False
        if not self._workspace_mode():
            self.feature_tabs.setCurrentIndex(1)
        self._capture_workspace()
        self.current_workspace_id = workspace_id
        self._restore_draft(workspace)
        self._refresh_workspace_picker()
        self._refresh_workspace_assignments()
        self._set_session_controls()
        self._save_workspaces()
        return True

    def create_workspace(self, name, device_count):
        if self.running or self._workspace_load_error:
            return None
        self._capture_workspace()
        previous = copy.deepcopy(self.workspace_store.workspaces)
        try:
            workspace = self.workspace_store.create(name, device_count)
        except ValueError as exc:
            self.workspace_notice.setText(str(exc))
            return None
        self._refresh_workspace_assignments()
        if not self._save_workspaces():
            self.workspace_store.workspaces = previous
            self._refresh_workspace_picker()
            self._refresh_workspace_assignments()
            self._workspace_save_timer.stop()
            return None
        self.select_workspace(workspace["id"])
        return workspace

    def edit_workspace(self, name, device_count):
        if self.running or not self.current_workspace_id or self._workspace_load_error:
            return False
        self._capture_workspace()
        previous = copy.deepcopy(self.workspace_store.workspaces)
        try:
            self.workspace_store.update(self.current_workspace_id, name, device_count)
        except ValueError as exc:
            self.workspace_notice.setText(str(exc))
            return False
        self._refresh_workspace_picker()
        self._refresh_workspace_assignments()
        if not self._save_workspaces():
            self.workspace_store.workspaces = previous
            self._refresh_workspace_picker()
            self._refresh_workspace_assignments()
            self._workspace_save_timer.stop()
            return False
        return True

    def _edit_workspace_dialog(self):
        workspace = self.workspace_store.get(self.current_workspace_id)
        if workspace:
            WorkspaceDialog(self, workspace).exec()

    def delete_workspace(self):
        if self.running or not self.current_workspace_id or self._workspace_load_error:
            return False
        self.workspace_store.remove(self.current_workspace_id)
        self._workspace_stats.pop(self.current_workspace_id, None)
        self.current_workspace_id = None
        if self.workspace_store.workspaces:
            self.select_workspace(self.workspace_store.workspaces[0]["id"])
        else:
            self._restore_draft({})
            self._refresh_workspace_picker()
            self._refresh_workspace_assignments()
            self._set_session_controls()
            self._save_workspaces()
        return True

    def _refresh_workspace_assignments(self):
        if self.running:
            return
        available = [serial for serial, state in self.device_states.items()
                     if state == "device" and self.device_comments(serial)]
        assignments = self.workspace_store.allocate(available)
        workspace = self.workspace_store.get(self.current_workspace_id)
        if self._workspace_mode():
            assigned = assignments.get(self.current_workspace_id, [])
            signals_blocked = self.dev_list.blockSignals(True)
            selection_mode = self.dev_list.selectionMode()
            try:
                # Qt ignores item.setSelected() in NoSelection mode. Apply the
                # assignment with selection enabled, then lock user changes.
                self.dev_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
                self.dev_list.clearSelection()
                for serial, item in self.dev_items.items():
                    item.setSelected(serial in assigned)
            finally:
                self.dev_list.setSelectionMode(selection_mode)
                self.dev_list.blockSignals(signals_blocked)
            self.workspace_title.setText("Comment to Comment")
            self.device_hint.setText("Highlighted devices are assigned to this workspace. "
                                     "Edit the workspace to change its device count.")
            if workspace:
                count = workspace["device_count"]
                self.workspace_notice.setText(
                    f"{len(assigned)} / {count} devices assigned · "
                    + (", ".join(assigned) if assigned else "No devices available")
                    + (" · Connect more devices or lower a workspace's count."
                       if len(assigned) < count else ""))
                self.run_hint.setText(f"{workspace['name']} · {count} devices · One session at a time")
                self.hud_msg.setText(f"Configure links and comments for {workspace['name']}.")
            else:
                self.workspace_notice.setText("Name your first workspace and choose how many devices to assign.")
                self.run_hint.setText("Create a workspace to get started.")
                self.hud_msg.setText("Keep separate links, comments, and device counts in named workspaces.")
        else:
            self.workspace_title.setText("We are Venom.")
            self.device_hint.setText("Select devices to include. Hover to count accounts.")
            self.run_hint.setText("One workspace. All selected devices.")
            self.hud_msg.setText("Posts, reels, live video. Bring your links and choose your devices.")
        if self._workspace_load_error:
            self.workspace_notice.setText(self._workspace_load_error)
        elif self._workspace_save_error:
            self.workspace_notice.setText(self._workspace_save_error)
        if (not self._workspace_load_error and not self._loading_workspace
                and self.workspace_store.workspaces):
            self._workspace_save_timer.start(400)

    def _set_session_controls(self):
        configured = not self._workspace_mode() or self.current_workspace_id is not None
        idle = not self.running
        self.feature_tabs.setEnabled(idle)
        self.workspace_bar.setEnabled(idle)
        self.workspace_picker.setEnabled(bool(self.workspace_store.workspaces))
        self.new_workspace_btn.setEnabled(not self._workspace_load_error)
        self.edit_workspace_btn.setEnabled(bool(self.current_workspace_id))
        self.delete_workspace_btn.setEnabled(bool(self.current_workspace_id))
        self.cfg_panel.setEnabled(idle and configured)
        self.devices_panel.setEnabled(idle)
        self.dose_rate.setEnabled(idle and configured)
        self.screenshot_check.setEnabled(idle and configured)
        self.ai_btn.setEnabled(idle)
        self.start_btn.setEnabled(idle and configured)
        self.stop_btn.setEnabled(self.running and not self.stop_flag)

    def closeEvent(self, event):
        self._capture_workspace()
        if (self.workspace_store.workspaces != self._saved_workspaces
                and not self._save_workspaces()):
            self.workspace_save_error.setText(
                self._workspace_save_error +
                " Your unsaved draft is still open. Fix the save issue and try closing again.")
            self.workspace_save_error.show()
            event.ignore()
            return
        super().closeEvent(event)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        fx = getattr(self, "inject_fx", None)
        if fx is not None and self.centralWidget() is not None:
            fx.setGeometry(0, 0, self.centralWidget().width(),
                           self.centralWidget().height())

    # ── splash ──
    def show_splash(self):
        self.splash = SplashScreen()
        self.splash.show()
        QTimer.singleShot(2600, self.close_splash)

    def close_splash(self):
        self.splash.fade_anim.finished.connect(self._on_splash_faded)
        self.splash.fade_anim.start()

    def _on_splash_faded(self):
        sp, self.splash = self.splash, None
        if sp is not None:
            sp.close()
            sp.deleteLater()
        self.showNormal()
        self.refresh_devices()
        self._fade_in()
        self.update_timer()

    def _fade_in(self):
        eff = QGraphicsOpacityEffect(self)
        self.centralWidget().setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(340)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: self.centralWidget().setGraphicsEffect(None))
        anim.start()
        self._fade_anim = anim

    def animate_status(self):
        self.tick += 1
        dt = 0.033
        self.canvas.advance(dt)
        self.inject_fx.advance(dt)
        self.layout_fx.advance(dt)
        self.titlebar.advance(dt)
        self.progress_bar.advance(dt)
        self.status_chip.advance(dt)
        self.start_btn.advance(dt)
        self.cmdbar.advance(dt)
        for c in self.device_cards.values():
            c.advance(dt)
        if self.running and self.stop_flag:
            self.status_chip.set_state("PURGING", C_HAZARD, True)
            self.hud_mol.rate = 3.0
            self.hud_mol.color = C_HAZARD
        elif self.summary_mode:
            aborted = self.stop_flag
            self.status_chip.set_state("PURGED" if aborted else "SATURATED",
                                       C_HAZARD if aborted else C_TOXIC, False)
            self.hud_mol.rate = 0.35
            self.hud_mol.color = C_HAZARD if aborted else C_TOXIC
        elif self.running:
            self.status_chip.set_state("SPREADING", C_TOXIC, True)
            self.hud_mol.rate = 2.2
            self.hud_mol.color = C_TOXIC
        else:
            self.status_chip.set_state("DORMANT", C_FAINT, False)
            self.hud_mol.rate = 0.55
            self.hud_mol.color = C_TOXIC

    def update_timer(self):
        if self.running and self.start_time > 0:
            elapsed = int(time.time() - self.start_time)
            self.stat_time.setText(f"{elapsed//60:02d}:{elapsed%60:02d}")
        QTimer.singleShot(1000, self.update_timer)

    def _field_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(_FIELD_LBL)
        return lbl

    def toggle_select_all(self):
        rows = [self.dev_list.item(k) for k in range(self.dev_list.count())]
        rows = [it for it in rows if it and it.data(Qt.ItemDataRole.UserRole)]
        if not rows:
            return
        select = not all(it.isSelected() for it in rows)
        for it in rows:
            it.setSelected(select)
        self.all_btn.setText("CLEAR ALL" if select else "SELECT ALL")

    def _phone_pix(self):
        pix = QPixmap(22, 26)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(QColor(C_EDGE), 1))
        p.setBrush(QColor(C_CARD))
        p.drawRoundedRect(QRectF(3.5, 1.5, 15, 23), 3, 3)
        p.setPen(QPen(_qc(C_MUTE, 160), 1))
        p.drawLine(QPointF(8, 21), QPointF(14, 21))
        p.end()
        return pix

    def refresh_devices(self):
        if self.running:
            return
        self.dev_list.clear()
        self.dev_items = {}
        self.device_pills = {}
        self.device_states = {}
        try:
            devs = ensure_online()
            self.device_states = dict(devs)
            for serial, state in devs.items():
                w  = QWidget()
                w.setStyleSheet("background:transparent")
                lo = QHBoxLayout(w)
                lo.setContentsMargins(7, 4, 7, 4)
                lo.setSpacing(9)
                icon_lbl = QLabel()
                icon_lbl.setPixmap(self._phone_pix())
                icon_lbl.setStyleSheet("background:transparent")
                lo.addWidget(icon_lbl)
                vl = QVBoxLayout()
                vl.setContentsMargins(0, 0, 0, 0)
                vl.setSpacing(1)
                s_lbl = QLabel(serial)
                s_lbl.setStyleSheet(
                    f"color:{C_TEXT};font-size:11px;font-weight:600;"
                    f"font-family:'{F_MONO}';background:transparent")
                vl.addWidget(s_lbl)
                ok = state == "device"
                st_lbl = QLabel("READY" if ok else state.upper())
                st_lbl.setStyleSheet(
                    f"color:{C_TOXIC if ok else C_HAZARD};font-size:9px;"
                    f"font-weight:700;letter-spacing:1px;"
                    f"font-family:'{F_MONO}';background:transparent")
                vl.addWidget(st_lbl)
                lo.addLayout(vl)
                lo.addStretch()
                # per-device: does this phone comment, or only react?
                pill = CommentPill(
                    on=self.device_comment.get(serial, True),
                    on_toggle=lambda checked, s=serial: self._set_device_comment(s, checked))
                pill.setToolTip("Click to enable comments or mute this device for the session")
                self.device_pills[serial] = pill
                lo.addWidget(pill)
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, serial)
                item.setSizeHint(w.sizeHint())
                cached = self.account_counts.get(serial)
                item.setToolTip(f"{cached} accounts available" if cached is not None
                                else "Hover to count accounts")
                self.dev_list.addItem(item)
                self.dev_list.setItemWidget(item, w)
                item.setSelected(True)
                self.dev_items[serial] = item
            self.dev_count_lbl.setText(f"{len(devs)} reachable")
            if hasattr(self, "all_btn"):
                self.all_btn.setText("CLEAR ALL" if devs else "SELECT ALL")
        except Exception as e:
            self.dev_count_lbl.setText("error")
            item = QListWidgetItem(f"Error: {e}")
            self.dev_list.addItem(item)
        self._refresh_workspace_assignments()
        self.update_briefing()

    def on_device_hover(self, item):
        serial = item.data(Qt.ItemDataRole.UserRole)
        if not serial:
            return
        cached = self.account_counts.get(serial)
        if cached is not None:
            item.setToolTip(f"{cached} accounts available")
            return
        if serial in self.account_scan_inflight:
            return
        if getattr(self, "running", False):
            item.setToolTip("Counting paused during a run")
            return
        self.account_scan_inflight.add(serial)
        item.setToolTip("Counting accounts…")
        threading.Thread(target=self.scan_one, args=(serial,), daemon=True).start()

    def device_lock(self, serial):
        """One lock per phone. The hover account-count probe and the run both
        drive the same device over ADB, and the probe force-stops Facebook and
        walks to the menu — landing that mid-run yanked the post away and the
        run just re-opened and scrolled. Only one may hold a phone at a time."""
        with self._locks_guard:
            return self._device_locks.setdefault(serial, threading.Lock())

    def scan_one(self, serial):
        n = -1
        lock = self.device_lock(serial)
        if not lock.acquire(timeout=0.1):
            self.account_scan_inflight.discard(serial)
            return                      # phone is busy with a run: skip quietly
        try:
            if not getattr(self, "running", False):
                d = connect_resilient(serial)
                if d is not None:
                    n = count_available_accounts(serial, d)
        except Exception:
            n = -1
        finally:
            lock.release()
            self.account_scan_inflight.discard(serial)
        self.send_cmd({"action": "device_accounts", "serial": serial, "count": n})

    def get_selected_devices(self):
        if self._workspace_mode():
            workspace = self.workspace_store.get(self.current_workspace_id)
            return [serial for serial in workspace["devices"]
                    if self.device_states.get(serial) == "device" and self.device_comments(serial)] if workspace else []
        result = []
        for item in self.dev_list.selectedItems():
            serial = item.data(Qt.ItemDataRole.UserRole)
            if serial:
                result.append(serial)
        return result

    def send_cmd(self, cmd):
        self.output_queue.put(cmd)

    def _load_account_counts(self):
        """Remembered per-phone account counts, so the Devices tile knows the
        full set and the numbers survive a restart. Never fatal: a missing or
        corrupt file simply starts empty."""
        try:
            with open(self._accounts_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        counts = {}
        for serial, value in data.items():
            if (isinstance(serial, str) and isinstance(value, int)
                    and not isinstance(value, bool) and value >= 0):
                counts[serial] = value
        return counts

    def _save_account_counts(self):
        """Write remembered counts atomically. A failure here must never take a
        run down, so it is swallowed rather than raised."""
        data = {s: n for s, n in self.account_counts.items()
                if isinstance(n, int) and not isinstance(n, bool) and n >= 0}
        try:
            directory = os.path.dirname(self._accounts_path) or "."
            os.makedirs(directory, exist_ok=True)
            tmp = self._accounts_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._accounts_path)
        except OSError:
            pass

    def process_queue(self):
        while not self.output_queue.empty():
            try:
                msg = self.output_queue.get_nowait()
                if isinstance(msg, dict):
                    self.handle_cmd(msg)
            except queue.Empty:
                break

    def handle_cmd(self, cmd):
        a = cmd.get("action")
        if a == "diagnostic":
            self._append_diagnostic(cmd.get("event") or {})
        elif a == "status":
            text = str(cmd.get("text", "")).upper()
            self.status_label.setText({"SPREADING…": "RUNNING", "PURGING…": "STOPPING"}.get(text, text))
            self.status_label.setStyleSheet(
                f"color:{cmd.get('color', C_TEXT)};font-size:15px;"
                f"font-weight:800;letter-spacing:1.8px;font-family:'{F_UI}';"
                f"background:transparent")
        elif a == "msg":
            self.msg_label.setText(cmd.get("text"))
            self.msg_label.setStyleSheet(
                f"color:{cmd.get('color', C_MUTE)};font-size:10px;"
                f"font-family:'{F_MONO}';background:transparent")
            self.hud_msg.setText(str(cmd.get("text", "")).lower())
            self.hud_msg.setStyleSheet(
                f"color:{cmd.get('color', C_MUTE)};font-size:11px;"
                f"font-family:'{F_MONO}';background:transparent")
        elif a == "progress":
            self.progress_bar.setValue(cmd.get("pct", 0))
        elif a == "stats":
            self.stat_devices.setText(
                f"{cmd.get('devices',0)} / {cmd.get('total',0)}")
            self.stat_reactions.setText(f"{cmd.get('reactions',0)}")
            self.refresh_link_tally()
        elif a == "toast":
            self._append_diagnostic({"code": "SESSION_NOTICE", "severity": "warning",
                                     "message": str(cmd.get("text", ""))})
            self.msg_label.setText(cmd.get("text"))
            self.msg_label.setStyleSheet(
                f"color:{C_HAZARD};font-size:10px;font-family:'{F_MONO}';"
                f"background:transparent")
            self.hud_msg.setText(str(cmd.get("text", "")).lower())
            self.hud_msg.setStyleSheet(
                f"color:{C_HAZARD};font-size:11px;font-family:'{F_MONO}';"
                f"background:transparent")
            QTimer.singleShot(2200, lambda: (
                self.msg_label.setText("Ready"),
                self.msg_label.setStyleSheet(
                    f"color:{C_MUTE};font-size:10px;font-family:'{F_MONO}';"
                    f"background:transparent"),
                self.hud_msg.setText("Your links. Your devices. One symbiote."),
                self.hud_msg.setStyleSheet(
                    f"color:{C_MUTE};font-size:11px;font-family:'{F_MONO}';"
                    f"background:transparent")))
        elif a == "device_status":
            s  = cmd.get("serial")
            st = cmd.get("status")
            if s in self.device_cards:
                self.device_cards[s].set_status(st, cmd.get("detail", ""))
        elif a == "device_progress":
            s = cmd.get("serial")
            card = self.device_cards.get(s)
            if card is not None and card.status not in ("done", "failed", "suspended"):
                acted = cmd.get("acted", 0)
                expected = cmd.get("expected", 0)
                label = cmd.get("label", "commented")
                card.set_status("processing",
                                f"{acted} / {expected} {label}" if expected
                                else f"{acted} {label}")
        elif a == "remember_accounts":
            s = cmd.get("serial")
            n = cmd.get("count")
            if (s and isinstance(n, int) and not isinstance(n, bool)
                    and n > 0 and self.account_counts.get(s, 0) < n):
                self.account_counts[s] = n
                self._save_account_counts()
        elif a == "device_accounts":
            s = cmd.get("serial")
            n = cmd.get("count", -1)
            if n is not None and n >= 0:
                self.account_counts[s] = n
                self._save_account_counts()
                tip = f"{n} accounts available"
            else:
                tip = "Couldn't count accounts (hover again to retry)"
            item = self.dev_items.get(s)
            if item is not None:
                item.setToolTip(tip)
        elif a == "devices_init":
            self.clear_device_cards()
            for i, s in enumerate(cmd.get("devices", [])):
                self.add_device_card(s, delay=i * 55)
        elif a == "reset_dash":
            self.clear_device_cards()
            self.status_label.setText("READY")
            self.status_label.setStyleSheet(
                f"color:{C_TEXT};font-size:15px;font-weight:800;"
                f"letter-spacing:1.8px;font-family:'{F_UI}';background:transparent")
            self.msg_label.setText("Priming…")
            self.msg_label.setStyleSheet(
                f"color:{C_MUTE};font-size:10px;font-family:'{F_MONO}';"
                f"background:transparent")
            self.progress_bar.setValue(0)
            self.stat_devices.setText("0 / 0")
            self.stat_reactions.setText("0")
            self.link_tally.set_rows([])
        elif a == "show_summary":
            self.show_summary(cmd.get("results", {}), cmd.get("devices", []))
        elif a == "finished":
            self.reaction_finished()

    def open_updates(self):
        """Updates are a restart, so never mid-run."""
        if self.running:
            self.send_cmd({"action": "toast",
                           "text": "Finish the session before updating"})
            return
        UpdateDialog(self).exec()

    def open_ai_settings(self):
        """Open the endpoint editor, then refresh the little status caption."""
        dlg = AISettings(self)
        dlg.exec()
        self.refresh_ai_status()

    def refresh_ai_status(self):
        """Say plainly whether comments will actually be AI-generated.

        Without a key the run still works - it falls back to the example text -
        but every account posts the same words, so this must be visible rather
        than a silent degradation.
        """
        try:
            ready = aiconfig.is_ready()
            desc = aiconfig.describe()
        except Exception as e:
            ready, desc = False, f"config error: {e}"
        self.ai_status.setText(desc if ready else f"NO KEY — {desc}")
        self.ai_status.setToolTip(self.ai_status.text())
        self.ai_status.setStyleSheet(
            f"color:{C_TOXIC2 if ready else C_AMBER};font-size:10px;"
            f"font-family:'{F_MONO}';background:transparent")

    def _set_speed(self, profile):
        """Dose rate changed: retune the pacing in core and warn about turbo."""
        fbcore.set_speed(profile)
        note = {"safe": ("human timing — lowest block risk", C_MUTE),
                "fast": ("~2x quicker — fine for aged accounts", C_VENOM),
                "turbo": ("minimal waiting — expect action blocks", C_BLOOD)}
        text, col = note.get(profile, note["safe"])
        self.msg_label.setText(f"dose rate: {profile.upper()} — {text}")
        self.msg_label.setStyleSheet(
            f"color:{col};font-size:10px;font-family:'{F_MONO}';"
            f"background:transparent")
        self.update_briefing()

    def refresh_link_tally(self):
        """all devices -> link_N -> X reacts"""
        acc = getattr(self, "tally_acc", {})
        rows = [(no, acc[no].get("url", ""), acc[no].get("reacted", 0),
                 acc[no].get("commented", 0), acc[no].get("failed", 0))
                for no in sorted(acc)]
        self.link_tally.set_rows(rows)

    def add_device_card(self, serial, delay=0):
        card = DeviceCard(serial)
        self.device_cards[serial] = card
        self.dev_card_layout.addWidget(card)
        card.play_in(delay)

    def clear_device_cards(self):
        while self.dev_card_layout.count():
            item = self.dev_card_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.device_cards = {}

    def show_summary(self, results, devices):
        self.summary_mode = True
        self.clear_device_cards()
        aborted = self.stop_flag
        self.status_label.setText("STOPPED" if aborted else "COMPLETE")
        self.status_label.setStyleSheet(
            f"color:{C_HAZARD if aborted else C_TOXIC};font-size:15px;"
            f"font-weight:800;letter-spacing:1.8px;font-family:'{F_UI}';"
            f"background:transparent")
        # Name the coverage here rather than holding the Devices tile back: it
        # reads "every phone finished, N of them commented on every account".
        full_n, dev_n = len(self._devices_full), len(devices)
        shortfall = ("" if full_n >= dev_n else
                     f" {full_n}/{dev_n} phones commented on every account.")
        self.msg_label.setText(("Session stopped — Facebook Lite closed."
                                if aborted else
                                "Session complete — Facebook Lite closed.")
                               + shortfall)
        self.msg_label.setStyleSheet(
            f"color:{C_MUTE};font-size:10px;font-family:'{F_MONO}';"
            f"background:transparent")
        self.progress_bar.setValue(100)
        total_reacted = sum(
            1 for s in results.values()
            for st in (s if isinstance(s, list) else [s])
            if str(st).startswith("reacted:") or str(st).startswith("commented:"))
        for i, serial in enumerate(devices):
            st = results.get(serial, [])
            if isinstance(st, list):
                if any(("reacted" in str(s) or "commented" in str(s)) for s in st):
                    status = "done"
                elif any("suspend" in str(s).lower() for s in st):
                    status = "suspended"
                else:
                    status = "failed"
            else:
                status = "failed"
            detail = ", ".join(str(s) for s in (st if isinstance(st, list) else [st]))
            card = DeviceCard(serial)
            card.set_status(status, detail)
            self.device_cards[serial] = card
            self.dev_card_layout.addWidget(card)
            card.play_in(i * 45)
        self.stat_devices.setText(f"{len(devices)} / {len(devices)}")
        self.stat_reactions.setText(f"{total_reacted}")
        self.refresh_link_tally()

    def _set_device_comment(self, serial, enabled):
        self.device_comment[serial] = bool(enabled)
        # Keep the pill in step even when the switch is set in code, so the
        # panel can never disagree with the map behind it.
        pill = getattr(self, "device_pills", {}).get(serial)
        if pill is not None and pill.isChecked() != bool(enabled):
            pill.blockSignals(True)
            pill.setChecked(bool(enabled))
            pill.blockSignals(False)
            pill.update()
        self._refresh_workspace_assignments()
        self.update_briefing()

    def device_comments(self, serial):
        """Does this phone post comments? Defaults to yes."""
        return self.device_comment.get(serial, True)

    # ── mass links ──
    def add_link_row(self, animate=False):
        row = LinkRow(len(self.link_rows) + 1,
                      on_remove=self.remove_link_row,
                      on_change=self.update_briefing)
        self.link_rows.append(row)
        self.links_layout.addWidget(row)
        do_react, do_comment = parse_action_mode(self.action_mode())
        row.set_columns(do_react, do_comment)
        if animate:
            row.play_in(0)
        else:
            row._in = 1.0
        self._relayout_for_links(animate=animate)
        self.update_briefing()
        return row

    def remove_link_row(self, row):
        if len(self.link_rows) <= 1:
            self.send_cmd({"action": "toast", "text": "At least one link is required"})
            return
        self.link_rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        for i, r in enumerate(self.link_rows, start=1):
            r.set_index(i)
        self._relayout_for_links(animate=True)
        self.update_briefing()

    def _relayout_for_links(self, animate=False):
        """Keep the workspace stable while link cards scroll independently."""
        n = len(self.link_rows)
        self.links_lbl.setText(f"{n} TARGET LINK{'S' if n != 1 else ''}")
        self._mass_mode = n >= 2
        if animate and self.link_rows:
            QTimer.singleShot(0, lambda: self.links_scroll.ensureWidgetVisible(self.link_rows[-1])
                              if self.link_rows else None)

    def link_targets(self):
        do_react, do_comment = parse_action_mode(self.action_mode())
        out = []
        for r in self.link_rows:
            t = r.target()
            if not t["url"]:
                continue
            out.append(fbcore.make_target(t["url"], t["reaction"], t["comment"],
                                          do_react, do_comment))
        return out

    def action_mode(self):
        """Always "Comment". FBVenom has no other mode - see the strain label."""
        return "Comment"

    def update_action_fields(self):
        mode = self.action_mode()
        do_react, do_comment = parse_action_mode(mode)
        for r in getattr(self, "link_rows", []):
            r.set_columns(do_react, do_comment)
        self.update_briefing()

    def update_briefing(self):
        if not hasattr(self, "briefing") or not hasattr(self, "link_rows"):
            return
        mode = self.action_mode()
        do_react, do_comment = parse_action_mode(mode)
        tg = self.link_targets()
        n_sel = len(self.get_selected_devices())
        n_links = len(tg)
        rows = [("links", f"{n_links} ready" if n_links else "none set",
                 C_TOXIC if n_links else C_HAZARD),
                ("strain", mode.lower(), C_TOXIC)]
        if n_links == 1:
            short = tg[0]["url"].split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            rows.append(("vector", short, C_TEXT))
            if do_react:
                rows.append(("toxin", tg[0]["reaction"].upper(), C_VENOM))
        elif n_links > 1:
            mix = ", ".join(sorted({t["reaction"] for t in tg}))
            if do_react:
                rows.append(("toxins", mix.upper(), C_VENOM))
        if do_comment:
            missing = sum(1 for t in tg if not t["comment"])
            rows.append(("samples",
                         "all set" if not missing else f"{missing} missing",
                         C_TOXIC if not missing else C_HAZARD))
        rows.append(("hosts", f"{n_sel} selected",
                     C_TOXIC if n_sel else C_HAZARD))
        if do_comment and n_sel:
            sel = self.get_selected_devices()
            talk = sum(1 for s in sel if self.device_comments(s))
            rows.append(("commenting",
                         f"{talk} of {n_sel} hosts" if talk
                         else "none - nothing will be posted",
                         C_VENOM if talk else C_HAZARD))
        if n_links and n_sel:
            rows.append(("doses", f"{n_links} x accounts x {n_sel}", C_MUTE))
        rows.append(("evidence", "on" if self.screenshot_check.isChecked() else "off",
                     C_TOXIC if self.screenshot_check.isChecked() else C_FAINT))
        self.briefing.set_rows(rows)
        if hasattr(self, "workspace_scroll"):
            self.workspace_scroll.widget().setMinimumHeight(self.middle_layout.minimumSize().height())
        if not self._loading_workspace and self.current_workspace_id:
            self._capture_workspace()
            self._workspace_save_timer.start(400)

    def start_reaction(self):
        if self.running:
            return
        if self._workspace_mode():
            workspace = self.workspace_store.get(self.current_workspace_id)
            if workspace is None:
                self.send_cmd({"action": "toast", "text": "Create a named workspace first"})
                return
            # Rescan at launch so a disconnected phone cannot silently reduce the count.
            self.refresh_devices()
            assigned = len(self.get_selected_devices())
            requested = workspace["device_count"]
            if assigned < requested:
                self.send_cmd({"action": "toast", "text":
                    f"{workspace['name']} needs {requested} devices; {assigned} assigned. "
                    "Connect more devices or edit the workspace count."})
                return
            self._save_workspaces()
        mode = self.action_mode()
        do_react, do_comment = parse_action_mode(mode)
        # normalise back into the rows so the user sees what will be used
        for number, row in enumerate(self.link_rows, 1):
            raw = row.url.text().strip()
            if raw:
                fixed = normalize_url(raw)
                if fixed:
                    row.url.setText(fixed)
                else:
                    event = fbcore.report_error("INVALID_URL", link_no=number,
                                                url=raw, severity="warning")
                    self.send_cmd({"action": "diagnostic", "event": event})
        targets = self.link_targets()
        if not targets:
            self.send_cmd({"action": "toast", "text": "Enter at least one post URL"})
            return
        if do_react and any(t["reaction"] not in REACTIONS for t in targets):
            self.send_cmd({"action": "toast", "text": "Select a valid reaction"})
            return
        if do_comment:
            # A row with a URL but no example used to abort the WHOLE run -
            # every link, every phone - behind one small toast, which reads as
            # "I pressed start and nothing commented". Drop just that row and
            # run the rest; only refuse when nothing usable is left.
            missing = [i + 1 for i, t in enumerate(targets) if not t["comment"]]
            usable = [t for t in targets if t["comment"]]
            if not usable:
                self.send_cmd({"action": "toast",
                               "text": "Every link needs an example comment"})
                return
            if missing:
                skipped = ", ".join(str(i) for i in missing)
                self.send_cmd({"action": "toast",
                               "text": f"Skipping link(s) {skipped} - no example "
                                       f"comment. Running the other "
                                       f"{len(usable)}."})
                print(f"  [run] skipping link(s) {skipped}: no example comment")
            targets = usable
        reaction = targets[0]["reaction"]
        comment_example = targets[0]["comment"]
        selected = self.get_selected_devices()
        if not selected:
            self.send_cmd({"action": "toast", "text": "Select at least one host"})
            return

        self.reaction_count = 0
        self.device_count   = 0
        self.total_devices  = len(selected)
        self.active_devices = list(selected)
        self.tally_acc = {}                    # fresh per-link counts per run
        self._dev_commented = {}               # per-phone live account coverage
        self._dev_reacted = {}
        self._dev_seen = {}
        self._devices_full = set()
        self.summary_mode   = False
        self.running        = True
        self.stop_flag      = False
        self.start_time     = time.time()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._set_session_controls()

        # The old "toxin surge" splash is not fired any more: it read as a white
        # smear over the dark UI. Starting a session is announced by the status
        # line and the device cards instead.

        self.send_cmd({"action": "reset_dash",    "devices": selected})
        self.send_cmd({"action": "status",        "text": "Spreading…", "color": C_TOXIC})
        self.send_cmd({"action": "msg",           "text": "Dispersing payload…", "color": C_MUTE})
        self.send_cmd({"action": "devices_init",  "devices": selected})

        take_screenshots = self.screenshot_check.isChecked()

        t = threading.Thread(
            target=self.run_reactions,
            args=(targets, reaction, selected, comment_example, do_react,
                  do_comment, take_screenshots),
            daemon=True)
        t.start()

    def run_reactions(self, targets, reaction, devices,
                      comment_example="", do_react=True, do_comment=False,
                      take_screenshots=False):
        # Keep console output printable; structured errors also reach the UI
        # through the queue, never by touching a Qt widget from a worker.
        old_error_hook = getattr(fbcore, "ERROR_HOOK", None)

        def session_error_hook(event):
            self.send_cmd({"action": "diagnostic", "event": dict(event)})
            if callable(old_error_hook):
                try:
                    old_error_hook(event)
                except Exception:
                    pass

        fbcore.ERROR_HOOK = session_error_hook
        # Must set the flag on the CORE module itself. Rebinding a name imported
        # into this module would only change gui.STOP_REQUESTED, leaving
        # core.stop_requested() permanently False — i.e. Abort would be ignored
        # by every check inside core, and the workers would keep going.
        fbcore.STOP_REQUESTED = lambda: self.stop_flag
        try:
            # Resolve opaque share/watch links once in this background thread,
            # before the device workers fan out. Every phone therefore opens
            # the same canonical target and the GUI never blocks on redirects.
            targets = fbcore.normalize_targets_for_phones(targets)
            if not targets:
                self.send_cmd({"action": "msg",
                               "text": "No valid Facebook post URLs",
                               "color": C_BLOOD})
                return
            if do_comment:
                # The endpoint check costs ~3s and its ANSWER IS THROWN AWAY -
                # it only warms the connection. Blocking the fleet on it meant
                # three seconds of nothing after the button was pressed, and
                # the first comment is not composed until a phone has woken,
                # opened Facebook and reached the post - many seconds later.
                # So probe in the background and let the phones go NOW. A dead
                # endpoint is no longer silent either: the first real call
                # prints the provider's own reason and switches the AI off for
                # the run, so nothing is lost by not waiting here.
                self.send_cmd({"action": "msg",
                               "text": "Binding language endpoint…",
                               "color": C_MUTE})

                def _probe_ai():
                    failure = None
                    try:
                        ok = start_llama()
                    except Exception as exc:
                        ok, failure = False, exc
                    if not ok:
                        event = fbcore.report_error("AI_UNAVAILABLE", exc=failure,
                                                    severity="warning")
                        # The warm-up can outlive a short session; retain its
                        # diagnostic even after the session hook is restored.
                        if fbcore.ERROR_HOOK is not session_error_hook:
                            session_error_hook(event)
                        self.send_cmd({
                            "action": "msg",
                            "text": "AI endpoint unreachable — comments will "
                                    "fall back to your example wordings",
                            "color": C_BLOOD})

                threading.Thread(target=_probe_ai, daemon=True).start()
            apk          = find_apk()
            reset_comment_pool()
            results      = {}
            results_lock = threading.Lock()
            completed    = [0]
            total        = len(devices)

            def mark_done(serial=None, fully=False):
                with results_lock:
                    completed[0] += 1
                    done_n  = completed[0]
                    if fully and serial is not None:
                        self._devices_full.add(serial)
                    react_n = self.reaction_count
                self.device_count = done_n
                # The Devices tile counts phones that have FINISHED, so it
                # always walks up to N/N. Counting only phones where every
                # account commented left it parked below the total forever
                # whenever a single account was skipped. Per-phone coverage
                # lives on the device cards, and any shortfall is named in the
                # summary line instead.
                self.send_cmd({"action": "progress",
                               "pct": int(done_n / total * 100)})
                self.send_cmd({"action": "stats",
                               "devices":   done_n,
                               "total":     total,
                               "reactions": react_n})

            def on_dose(info):
                """One account just reacted/commented - count it immediately.

                Updates three live views the instant a dose lands: the Comments
                tile, the per-link Session activity tally, and the per-phone
                account coverage on its device card. mark_done() only fires once
                a whole phone is finished, so without this they moved in chunks.
                """
                info = info or {}
                serial  = info.get("serial")
                kind    = info.get("kind")
                acct    = info.get("account")
                link_no = info.get("link")
                url     = info.get("url", "")
                is_dose = kind in ("reacted", "commented")
                with results_lock:
                    if is_dose:
                        self.reaction_count += 1
                    react_n = self.reaction_count
                    done_n  = completed[0]
                    # live per-link tally (Session activity) - doses only
                    if is_dose and link_no is not None:
                        row = self.tally_acc.setdefault(
                            link_no, {"reacted": 0, "commented": 0,
                                      "failed": 0, "url": url})
                        if url:
                            row["url"] = url
                        row[kind] = row.get(kind, 0) + 1
                    # live per-phone account coverage. kind == "account" marks a
                    # phone REACHING an account at all, so an account that is
                    # reached but never comments still counts against "all
                    # accounts commented" instead of being invisible.
                    if serial is not None and isinstance(acct, int) and acct > 0:
                        self._dev_seen[serial] = max(
                            self._dev_seen.get(serial, 0), acct)
                        if kind == "commented":
                            self._dev_commented.setdefault(serial, set()).add(acct)
                        elif kind == "reacted":
                            self._dev_reacted.setdefault(serial, set()).add(acct)
                    dev_do_comment = do_comment and self.device_comments(serial)
                    acted = (len(self._dev_commented.get(serial, ())) if dev_do_comment
                             else len(self._dev_reacted.get(serial, ())))
                    # Denominator = the most accounts we know this phone has:
                    # the remembered count, or how many we have reached so far.
                    expected = max(self.account_counts.get(serial) or 0,
                                   self._dev_seen.get(serial, 0))
                self.send_cmd({"action": "stats",
                               "devices":   done_n,
                               "total":     total,
                               "reactions": react_n})
                if serial is not None:
                    self.send_cmd({"action": "device_progress",
                                   "serial": serial, "acted": acted,
                                   "expected": expected,
                                   "label": "commented" if dev_do_comment
                                            else "reacted"})
            fbcore.PROGRESS_HOOK = on_dose

            def pre_wake(serial):
                try:
                    wake_and_unlock(serial)
                except Exception:
                    pass
            wakers = [threading.Thread(target=pre_wake, args=(s,), daemon=True)
                      for s in devices]
            for w in wakers:
                w.start()
            for w in wakers:
                w.join()

            def worker(serial):
                self.send_cmd({"action": "device_status",
                               "serial": serial, "status": "processing"})

                try:
                    if not self.stop_flag:
                        wait_online(serial)
                    if not self.stop_flag and not has_fb(serial):
                        if apk and install_fb(serial, apk):
                            pass
                        elif apk:
                            fbcore.report_error("FACEBOOK_INSTALL_FAILED", serial=serial)
                            with results_lock:
                                results[serial] = "install-failed"
                            self.send_cmd({"action": "device_status",
                                           "serial": serial, "status": "failed"})
                            mark_done(serial, fully=False)
                            return
                        else:
                            fbcore.report_error("FACEBOOK_NOT_INSTALLED", serial=serial)
                            with results_lock:
                                results[serial] = "missing-app-no-apk"
                            self.send_cmd({"action": "device_status",
                                           "serial": serial, "status": "failed"})
                            mark_done(serial, fully=False)
                            return
                    # Per-device comment switch: a phone set to REACT ONLY gets
                    # its own copy of the targets with commenting turned off.
                    dev_targets = targets
                    if do_comment and not self.device_comments(serial):
                        dev_targets = [dict(t, do_comment=False) for t in targets]
                        if not do_react:
                            # comment-only run + this device may not comment
                            with results_lock:
                                results[serial] = "skipped-react-only-device"
                            self.send_cmd({"action": "device_status",
                                           "serial": serial, "status": "pending"})
                            mark_done(serial, fully=False)
                            return
                    # mass links: one call, every link handled per account
                    res = react_all_saved_accounts(
                        serial, dev_targets, reaction, comment_example,
                        do_react, do_comment, take_screenshots)
                except Exception as e:
                    fbcore.report_error("DEVICE_WORKER_FAILED", serial=serial, exc=e)
                    res = [f"device-error:{e}"]

                lst = res if isinstance(res, list) else [res]
                rc  = sum(1 for s in lst if str(s).startswith("reacted:"))
                cc  = sum(1 for s in lst if str(s).startswith("commented:"))
                with results_lock:
                    results[serial]      = res
                    # NOT counted here any more: on_dose() already counted each
                    # reaction/comment the moment it landed, so adding the
                    # per-device totals again would double every dose.
                    _ = rc + cc
                    # on_dose already tallied every landed reaction/comment per
                    # link; only failures are not reported live, so roll in just
                    # those (keeping each link's URL) to finish the tally.
                    for no, row in getattr(res, "link_tally", {}).items():
                        acc = self.tally_acc.setdefault(
                            no, {"reacted": 0, "commented": 0, "failed": 0,
                                 "url": row.get("url", "")})
                        acc["url"] = acc["url"] or row.get("url", "")
                        acc["failed"] += row.get("failed", 0)
                    # How many accounts this phone actually worked through, and
                    # how many did the action this run required.
                    seen = self._dev_seen.get(serial, 0)
                    # Coverage is judged against the accounts this run actually
                    # walked. Using the REMEMBERED count here meant a stale-high
                    # number (a phone that lost an account) could never be
                    # "fully commented" again.
                    expected_total = seen
                    dev_do_comment = do_comment and self.device_comments(serial)
                    acted = (len(self._dev_commented.get(serial, ())) if dev_do_comment
                             else len(self._dev_reacted.get(serial, ())))
                    did_action = any(("reacted" in str(s) or "commented" in str(s))
                                     for s in lst)
                    # "Done" for the tile only when EVERY account on the phone
                    # completed the required action; a partial phone stays out.
                    fully = bool(did_action and expected_total > 0
                                 and acted >= expected_total)
                    label = "commented" if dev_do_comment else "reacted"
                # Learn and remember this phone's account count so it survives a
                # restart and the tile knows the full set next time.
                if seen > 0:
                    self.send_cmd({"action": "remember_accounts",
                                   "serial": serial, "count": seen})
                if did_action:
                    self.send_cmd({"action": "device_status",
                                   "serial": serial, "status": "done",
                                   "detail": f"{acted} / {expected_total or acted} {label}"
                                             + ("" if fully else " · partial")})
                elif "suspended" in str(res):
                    self.send_cmd({"action": "device_status",
                                   "serial": serial, "status": "suspended"})
                else:
                    self.send_cmd({"action": "device_status",
                                   "serial": serial, "status": "failed"})
                mark_done(serial, fully=fully)

            _worker_body = worker
            def worker(serial):
                # Hold the phone for the whole run: a hover-triggered account
                # probe force-stops Facebook and walks to the menu, and landing
                # that mid-run yanked the post away, so the run just re-opened
                # and scrolled instead of reaching the link.
                lock = self.device_lock(serial)
                lock.acquire()
                try:
                    _worker_body(serial)
                finally:
                    lock.release()

            workers = [threading.Thread(target=worker, args=(s,), daemon=True)
                       for s in devices]
            for wk in workers:
                wk.start()
            for wk in workers:
                wk.join()

            self.send_cmd({"action": "progress", "pct": 100})
            self.send_cmd({"action": "stats",
                           "devices":   total,
                           "total":     total,
                           "reactions": self.reaction_count})
            self.send_cmd({"action": "show_summary",
                           "results": results, "devices": devices})
        except Exception as e:
            event = fbcore.report_error("SESSION_FAILED", exc=e)
            self.send_cmd({"action": "msg", "text": event["message"], "color": C_BLOOD})
        finally:
            fbcore.PROGRESS_HOOK = None   # never let it leak into the next run
            try:
                stop_llama()
            finally:
                if fbcore.ERROR_HOOK is session_error_hook:
                    fbcore.ERROR_HOOK = old_error_hook
                self.send_cmd({"action": "finished"})

    def open_screenshots_folder(self):
        folder = os.path.join(HERE, "screenshots")
        try:
            os.makedirs(folder, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            self.send_cmd({"action": "toast",
                           "text": f"Couldn't open evidence vault: {e}"})

    def reaction_finished(self):
        self.running = False
        self.summary_mode = True
        # Keep this run's numbers with the workspace that produced them, so
        # switching away and back shows them again.
        self._store_session_stats()
        self._set_session_controls()

    def stop_reaction(self):
        # Abort is immediate: raise the flag (every wait in core polls it within
        # ~0.1s) and force-stop Facebook Lite on every device right now, without
        # waiting for the workers to unwind.
        self.stop_flag = True
        self.stop_btn.setEnabled(False)
        self.send_cmd({"action": "status", "text": "Purging…", "color": C_HAZARD})
        self.send_cmd({"action": "msg",
                       "text": "Purge — halting and closing Facebook Lite…",
                       "color": C_HAZARD})
        devices = list(getattr(self, "active_devices", [])) or self.get_selected_devices()

        def kill():
            try:
                abort_devices(devices)
            except Exception as e:
                self.send_cmd({"action": "msg",
                               "text": f"Purge: couldn't close Facebook Lite: {e}",
                               "color": C_BLOOD})
            else:
                self.send_cmd({"action": "msg",
                               "text": "Purged — Facebook Lite closed.",
                               "color": C_HAZARD})
        threading.Thread(target=kill, daemon=True).start()


class DoseRate(QWidget):
    """Segmented control for the pacing profile: SAFE / FAST / TURBO.

    Faster profiles wait less between actions, links and accounts. That is the
    whole speed-up, and it is also the whole risk: the waiting is what keeps
    Facebook from action-blocking the accounts.
    """

    STEPS = [("SAFE", C_TOXIC, "safe"),
             ("FAST", C_VENOM, "fast"),
             ("TURBO", C_BLOOD, "turbo")]

    def __init__(self, on_change=None, parent=None):
        super().__init__(parent)
        self.setFixedSize(204, 34)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Run speed")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._i = 0
        self._on_change = on_change
        self._glide = 0.0
        self._a = QVariantAnimation(self, duration=260,
                                    startValue=0.0, endValue=0.0)
        self._a.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._a.valueChanged.connect(self._set_glide)
        self.setToolTip("dose rate — how hard the run is paced\n"
                        "SAFE: human timing, lowest block risk\n"
                        "FAST: ~2x quicker, fine for aged accounts\n"
                        "TURBO: minimal waiting, expect action blocks")

    def _set_glide(self, v):
        self._glide = float(v); self.update()

    def profile(self):
        return self.STEPS[self._i][2]

    def set_index(self, i):
        i = max(0, min(len(self.STEPS) - 1, int(i)))
        if i == self._i:
            return
        self._a.stop()
        self._a.setStartValue(self._glide)
        self._a.setEndValue(float(i))
        self._a.start()
        self._i = i
        if self._on_change:
            self._on_change(self.profile())

    def mousePressEvent(self, e):
        seg = self.width() / len(self.STEPS)
        self.set_index(int(e.position().x() // seg))
        super().mousePressEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        seg = w / len(self.STEPS)
        enabled = self.isEnabled()
        accent = self.STEPS[self._i][1]
        bevel_rect(p, QRect(0, 0, w, h), C_INPUT, border=C_BORDER, bw=2,
                   shadow=2, bevel=False)
        p.setPen(Qt.PenStyle.NoPen)
        cell = QRect(int(self._glide * seg) + 3, 3, int(seg) - 6, h - 7)
        p.fillRect(cell, _qc(accent, 60 if enabled else 22))
        p.fillRect(QRect(cell.x(), cell.y(), cell.width(), 2),
                   _qc(accent, 235 if enabled else 60))
        for i, (label, color, _key) in enumerate(self.STEPS):
            active = i == self._i
            draw_label(p, QRect(int(i * seg), 0, int(seg), h - 3), label.title(), 6,
                       (color if active else C_MUTE) if enabled else C_FAINT,
                       Qt.AlignmentFlag.AlignCenter, outline=0)
        p.end()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and self.isEnabled():
            self.set_index(self._i + (-1 if event.key() == Qt.Key.Key_Left else 1))
            event.accept()
        elif event.key() == Qt.Key.Key_Home and self.isEnabled():
            self.set_index(0)
            event.accept()
        elif event.key() == Qt.Key.Key_End and self.isEnabled():
            self.set_index(len(self.STEPS) - 1)
            event.accept()
        else:
            super().keyPressEvent(event)


class CommentPill(QPushButton):
    """Per-device switch: does THIS phone post comments?

    FBVenom only comments, so switching this OFF means the phone sits the run
    out entirely - it does not fall back to reacting the way FBPoison does.
    Hence "MUTED" rather than "REACT ONLY"."""

    def __init__(self, on=True, on_toggle=None, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(on)
        self.setFixedSize(104, 28)
        self.setAccessibleName("Device comments")
        self.setToolTip("Enable comments for this device, or mute it for the run.")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hot = 0.0
        self._a = QVariantAnimation(self, duration=150,
                                   startValue=0.0, endValue=1.0)
        self._a.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._a.valueChanged.connect(self._set_hot)
        if on_toggle:
            self.toggled.connect(on_toggle)
        self.toggled.connect(lambda _b: self.update())

    def _set_hot(self, v):
        self._hot = float(v); self.update()

    def enterEvent(self, e):
        self._a.setDirection(QVariantAnimation.Direction.Forward); self._a.start()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._a.setDirection(QVariantAnimation.Direction.Backward); self._a.start()
        super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        on, enabled = self.isChecked(), self.isEnabled()
        w, h = self.width(), self.height()
        accent = C_TOXIC if on else C_MUTE
        fill = _qc(accent, (36 + int(16 * self._hot)) if enabled else 14)
        border = _qc(accent, 210 if (self.hasFocus() and enabled) else 90)
        bevel_rect(p, QRect(0, 0, w, h), fill, light=_qc(C_EDGE, 50),
                   dark=C_VOID, border=border, bw=2)
        col = QColor(accent if enabled else C_FAINT)
        p.setPen(Qt.PenStyle.NoPen)
        p.fillRect(QRect(10, h // 2 - 5, 12, 8), col)     # chunky speech bubble
        p.fillRect(QRect(12, h // 2 + 3, 3, 3), col)      # tail
        draw_label(p, QRect(26, 0, w - 32, h - 2),
                   'Comments' if on else 'Muted', 6,
                   (C_TOXIC2 if on else C_MUTE) if enabled else C_FAINT,
                   outline=0)
        p.end()


class LinkRow(QFrame):
    """One target: <link> <reaction> <example comment>."""

    REACTION_EMOJI = {"like": "\U0001F44D", "love": "❤️", "care": "\U0001F970",
                      "haha": "\U0001F606", "wow": "\U0001F62E",
                      "sad": "\U0001F622", "angry": "\U0001F621"}

    def __init__(self, index, on_remove=None, on_change=None, parent=None):
        super().__init__(parent)
        self.index = index
        self._on_remove = on_remove
        self.setFixedHeight(112)
        self._in = 0.0
        self._anim = QVariantAnimation(self, duration=280,
                                       startValue=0.0, endValue=1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_in)

        layout = QGridLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(10)
        layout.setColumnStretch(2, 1)

        self.num = QLabel(str(index))
        self.num.setFixedSize(28, 28)
        self.num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.num.setStyleSheet(
            f'color:{C_VOID};background:{C_TOXIC};border:1px solid {C_EDGE};'
            f'border-radius:8px;font:700 12px "{F_UI}";')
        layout.addWidget(self.num, 0, 0, 2, 1, Qt.AlignmentFlag.AlignTop)

        url_label = QLabel('Post URL')
        self._comment_label = QLabel('Example')
        for label in (url_label, self._comment_label):
            label.setStyleSheet(f'color:{C_MUTE};font:11px "{F_UI}";background:transparent;')
            label.setFixedWidth(56)
        layout.addWidget(url_label, 0, 1)
        layout.addWidget(self._comment_label, 1, 1)

        self.url = QLineEdit()
        self.url.setPlaceholderText('Paste a post, reel, video, live, or share link…')
        self.url.setAccessibleName(f'Post {index} URL')
        self.url.setToolTip('Facebook posts, reels, live/video, photos, group posts and share links.\nYou can paste a message containing the link; it is cleaned before the run.')
        self.url.setMinimumWidth(0)
        self.url.setFixedHeight(38)
        self.url.setStyleSheet(_INPUT_QSS.replace('padding:10px 13px', 'padding:7px 10px'))
        layout.addWidget(self.url, 0, 2)

        self.reaction = QComboBox()
        for reaction in ('like', 'love', 'care', 'haha', 'wow', 'sad', 'angry'):
            self.reaction.addItem(f'{self.REACTION_EMOJI[reaction]}  {reaction.title()}', reaction)
        self.reaction.setFixedWidth(108)
        self.reaction.setFixedHeight(38)
        self.reaction.setAccessibleName(f'Post {index} reaction')
        self.reaction.setStyleSheet(
            f'QComboBox{{background:{C_INPUT};color:{C_TEXT};border:1px solid {C_BORDER};'
            f'border-radius:8px;padding:6px 8px;font:11px "{F_UI}"}}'
            f'QComboBox:hover,QComboBox:focus{{border:1px solid {C_EDGE}}}'
            f'QComboBox::drop-down{{border:none;width:16px}}'
            f'QComboBox QAbstractItemView{{background:{C_CARD};color:{C_TEXT};'
            f'border:1px solid {C_BORDER};selection-background-color:{C_TOXIC};'
            f'selection-color:{C_VOID};outline:none}}')
        layout.addWidget(self.reaction, 1, 3)

        self.comment = QLineEdit()
        self.comment.setPlaceholderText('Write an example comment…')
        self.comment.setAccessibleName(f'Post {index} example comment')
        self.comment.setToolTip(
            'One example, or several separated by |\n'
            'e.g.  salamat sa bagong fire trucks | ang laking tulong nito sa amin\n'
            'Accounts take turns through the list, and each one is reworded, so '
            'more examples means the post reads less like one comment repeated.')
        self.comment.setMinimumWidth(0)
        self.comment.setFixedHeight(38)
        self.comment.setStyleSheet(_INPUT_QSS.replace('padding:10px 13px', 'padding:7px 10px'))
        layout.addWidget(self.comment, 1, 2)

        self.kill = GhostButton('×', C_BLOOD)
        self.kill.setFixedSize(30, 30)
        self.kill.setToolTip('Remove this post')
        self.kill.setAccessibleName(f'Remove post {index}')
        self.kill.clicked.connect(lambda: self._on_remove and self._on_remove(self))
        layout.addWidget(self.kill, 0, 3, Qt.AlignmentFlag.AlignRight)

        if on_change:
            self.url.textChanged.connect(lambda _t: on_change())
            self.comment.textChanged.connect(lambda _t: on_change())
            self.reaction.currentIndexChanged.connect(lambda _i: on_change())

    def _set_in(self, v):
        self._in = float(v); self.update()

    def play_in(self, delay=0):
        QTimer.singleShot(delay, self._anim.start)

    def set_index(self, i):
        self.index = i
        self.num.setText(str(i))
        self.url.setAccessibleName(f'Post {i} URL')
        self.comment.setAccessibleName(f'Post {i} example comment')
        self.reaction.setAccessibleName(f'Post {i} reaction')
        self.kill.setAccessibleName(f'Remove post {i}')

    def set_columns(self, show_reaction, show_comment):
        self.reaction.setVisible(show_reaction)
        self.comment.setVisible(show_comment)
        self._comment_label.setVisible(show_comment)

    def target(self):
        return {"url": self.url.text().strip(),
                "reaction": self.reaction.currentData(),
                "comment": self.comment.text().strip()}

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        progress = max(0.0, min(1.0, self._in)) if self._anim.state() or self._in else 1.0
        p.setOpacity(progress)
        focused = self.url.hasFocus() or self.comment.hasFocus() or self.reaction.hasFocus()
        bevel_rect(p, QRect(0, 0, self.width(), self.height()), C_CARD,
                   light=_qc(C_EDGE, 60), dark=C_VOID,
                   border=C_TOXIC if focused else C_BORDER, bw=2)
        p.end()


class PanelFlipFX(QWidget):
    """3D fold: the panel is grabbed and swung about its Y axis with real
    perspective (quadToQuad) while the layout changes underneath it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()
        self.shot = None
        self.rect_ = QRectF()
        self.t = 0.0
        self.dur = 0.55
        self.active = False

    def play(self, widget):
        try:
            self.shot = widget.grab()
            tl = widget.mapTo(self.parentWidget(), QPoint(0, 0))
            self.rect_ = QRectF(tl.x(), tl.y(), widget.width(), widget.height())
        except Exception:
            return
        self.setGeometry(self.parentWidget().rect())
        self.t = 0.0
        self.active = True
        self.show()
        self.raise_()
        self.update()

    def advance(self, dt):
        if not self.active:
            return
        self.t += dt
        if self.t >= self.dur:
            self.active = False
            self.hide()
            return
        self.update()

    def paintEvent(self, _e):
        if not self.active or self.shot is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        u = self.t / self.dur
        # swing out and back: a half turn about Y with perspective foreshortening
        ang = math.sin(u * math.pi) * 0.55
        r = self.rect_
        cx, cy = r.center().x(), r.center().y()
        hw, hh = r.width() / 2.0, r.height() / 2.0
        focal = 900.0
        pts = []
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            x, y, z = sx * hw, sy * hh, 0.0
            xr = x * math.cos(ang) + z * math.sin(ang)
            zr = -x * math.sin(ang) + z * math.cos(ang)
            k = focal / (focal + zr)
            pts.append(QPointF(cx + xr * k, cy + y * k))
        src = QPolygonF([QPointF(0, 0), QPointF(self.shot.width(), 0),
                         QPointF(self.shot.width(), self.shot.height()),
                         QPointF(0, self.shot.height())])
        dst = QPolygonF(pts)
        from PyQt6.QtGui import QTransform
        tf = QTransform()
        if QTransform.quadToQuad(src, dst, tf):
            p.setOpacity(1.0 - u * 0.35)
            p.setTransform(tf)
            p.drawPixmap(0, 0, self.shot)
        p.end()


def _smooth(a, b, x):
    """Smoothstep between a and b."""
    if b <= a:
        return 0.0 if x < a else 1.0
    t = max(0.0, min(1.0, (x - a) / (b - a)))
    return t * t * (3.0 - 2.0 * t)


class InjectOverlay(QWidget):
    """The toxin surge played when INJECT is pressed.

    Genuinely 3D: the fluid is a world-space heightfield (summed travelling
    sine waves) drawn as depth-sorted strips through the same perspective
    projection as the rest of the app, lit by a fake normal-dot-light term.
    Droplets are ballistic in 3D and composited additively so overlapping
    blobs merge like metaballs. Purely decorative — it never blocks the run.
    """

    DURATION = 2.15

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.hide()
        self.t = 0.0
        self.active = False
        self.origin = QPointF(0, 0)
        self.drops = []
        self.rings = []
        self.bubbles = []
        # pre-rendered droplet blob: building a QRadialGradient per droplet per
        # frame was the hot spot, and blitting composites identically under
        # CompositionMode_Plus.
        self._blob = _glow_pixmap(C_TOXIC2, 96,
                                  stops=((0.0, 235), (0.40, 120), (1.0, 0)))

    # ── control ──
    def fire(self, origin: QPointF):
        self.origin = QPointF(origin)
        self.t = 0.0
        self.active = True
        self.drops = []
        self.rings = []
        self.bubbles = [{"x": random.uniform(-4.4, 4.4),
                         "z": random.uniform(1.2, 8.0),
                         "p": random.uniform(0, 6.28),
                         "r": random.uniform(1.6, 4.2)} for _ in range(46)]
        # ballistic droplets thrown out of the needle — spawned at the world
        # position that projects back onto the INJECT button, so the spray
        # actually erupts from the button rather than from screen centre
        W, H = max(1, self.width()), max(1, self.height())
        ocx, ocy = W / 2.0, H * 0.52
        ofocal = H * 0.62
        for _ in range(46):
            ang = random.uniform(-math.pi, math.pi)
            spd = random.uniform(1.6, 4.6)
            z0 = random.uniform(1.6, 4.6)
            # aim the burst back across the window instead of straight up, so
            # the spray travels the full width rather than staying in the corner
            ox_w = (self.origin.x() - ocx) * z0 / ofocal
            self.drops.append({
                "x": ox_w + random.uniform(-0.16, 0.16),
                "y": (self.origin.y() - ocy) * z0 / ofocal + random.uniform(-0.10, 0.06),
                "z": z0,
                "vx": -abs(math.cos(ang)) * spd * random.uniform(0.8, 2.1)
                      * (1.0 if ox_w > 0 else -1.0),
                "vy": -random.uniform(2.4, 5.2),
                "vz": math.sin(ang) * spd * 0.30,
                "r": random.uniform(0.045, 0.135),
                "t0": 0.0,
                "trail": [],
                "dead": False,
            })
        # splash-back: the rising pool throws droplets up across the whole
        # width, so the effect reads as the entire UI being flooded
        for i in range(40):
            x0 = -5.0 + 10.0 * (i + random.uniform(0.1, 0.9)) / 40.0
            self.drops.append({
                "x": x0,
                "y": 1.2,
                "z": random.uniform(1.3, 7.5),
                "vx": random.uniform(-0.5, 0.5),
                "vy": -random.uniform(1.6, 3.6),
                "vz": random.uniform(-0.2, 0.2),
                "r": random.uniform(0.035, 0.105),
                "t0": 0.22 + random.uniform(0.0, 0.34),
                "trail": [],
                "dead": False,
            })
        self.show()
        self._sync_to_parent()   # full-size + on top before the first frame
        self.raise_()
        self.update()

    def _sync_to_parent(self):
        """Keep the overlay exactly covering its parent, always on top.

        QMainWindow.resizeEvent fires BEFORE the central widget is re-laid out,
        so sizing from it there leaves a stale (smaller) rect after a
        maximise/resize — which clips the effect to part of the window. The
        parent's rect is the authority, so re-assert it every frame.
        """
        par = self.parentWidget()
        if par is None:
            return
        if self.geometry() != par.rect():
            self.setGeometry(par.rect())
        kids = [c for c in par.children() if c.isWidgetType()]
        if kids and kids[-1] is not self:
            self.raise_()

    def advance(self, dt):
        if not self.active:
            return
        self._sync_to_parent()
        self.t += dt
        u = self.t / self.DURATION
        if u >= 1.0:
            self.active = False
            self.hide()
            return
        lvl = self._level(u)
        for d in self.drops:
            if d["dead"] or self.t < d.get("t0", 0.0):
                continue
            d["vy"] += 7.4 * dt                      # gravity
            d["x"] += d["vx"] * dt
            d["y"] += d["vy"] * dt
            d["z"] += d["vz"] * dt
            d["trail"].append((d["x"], d["y"], d["z"]))
            if len(d["trail"]) > 7:
                d["trail"].pop(0)
            if d["y"] >= lvl and d["vy"] > 0:        # hit the fluid
                d["dead"] = True
                self.rings.append({"x": d["x"], "z": max(0.9, d["z"]),
                                   "age": 0.0, "r0": d["r"] * 1.4})
        for r in self.rings:
            r["age"] += dt
        self.rings = [r for r in self.rings if r["age"] < 0.85]
        self.update()

    # ── shape of the pour ──
    def _level(self, u):
        """World-space Y of the fluid surface (smaller = higher on screen)."""
        rise = _smooth(0.10, 0.52, u)
        fall = _smooth(0.74, 1.0, u)
        k = max(0.0, rise - fall)
        return 2.55 - 3.05 * k

    def _wave(self, x, z, u):
        t = self.t
        return (0.085 * math.sin(x * 1.5 + t * 4.6)
                + 0.055 * math.sin(z * 1.9 - t * 3.4)
                + 0.035 * math.sin((x + z) * 2.7 + t * 6.1))

    def paintEvent(self, _e):
        if not self.active:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        u = self.t / self.DURATION
        cx, cy = w / 2.0, h * 0.52
        focal = h * 0.62
        lvl = self._level(u)

        # ── 0. impact flash ──
        flash = max(0.0, 1.0 - abs(u - 0.13) / 0.13)
        if flash > 0.01:
            p.fillRect(0, 0, w, h, _qc(C_TOXIC, 42 * flash))

        # ── 0b. shockwave sweeping the whole window from the needle ──
        sw = _smooth(0.06, 0.62, u)
        if 0.0 < sw < 1.0:
            reach = math.hypot(w, h) * 1.15
            rr = reach * sw
            a = int(170 * (1.0 - sw) ** 1.4)
            if a > 2:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(_qc(C_TOXIC2, a), 3.0 * (1.0 - sw) + 0.8))
                p.drawEllipse(self.origin, rr, rr * 0.72)
                p.setPen(QPen(_qc(C_TOXIC, int(a * 0.5)), 1.4))
                p.drawEllipse(self.origin, rr * 0.82, rr * 0.82 * 0.72)

        # ── 1. charge ring collapsing into the needle ──
        if u < 0.20:
            k = 1.0 - u / 0.20
            rr = 26 + 210 * k
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_qc(C_TOXIC2, int(220 * (1 - k))), 2.4))
            p.drawEllipse(self.origin, rr, rr * 0.42)

        # ── 2. surge column bursting from the needle ──
        col = _smooth(0.05, 0.30, u) * (1.0 - _smooth(0.42, 0.72, u))
        if col > 0.01:
            ox, oy = self.origin.x(), self.origin.y()
            cw = 26 + 54 * col
            top = oy - (h * 0.78) * col
            grad = QLinearGradient(0, top, 0, oy)
            grad.setColorAt(0.0, _qc(C_TOXIC2, 0))
            grad.setColorAt(0.35, _qc(C_TOXIC2, int(150 * col)))
            grad.setColorAt(1.0, _qc(C_TOXIC, int(215 * col)))
            path = QPainterPath()
            path.moveTo(ox - cw * 0.5, oy)
            steps = 16
            for i in range(steps + 1):
                f = i / steps
                yy = oy + (top - oy) * f
                bulge = math.sin(f * math.pi) * 16 * col
                wob = math.sin(f * 9.0 - self.t * 15.0) * 5.0 * col
                path.lineTo(ox - cw * 0.5 - bulge + wob, yy)
            for i in range(steps, -1, -1):
                f = i / steps
                yy = oy + (top - oy) * f
                bulge = math.sin(f * math.pi) * 16 * col
                wob = math.sin(f * 9.0 - self.t * 15.0) * 5.0 * col
                path.lineTo(ox + cw * 0.5 + bulge + wob, yy)
            path.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.fillPath(path, QBrush(grad))

        # ── 3. the 3D fluid surface ──
        if lvl < 2.5:
            GX, GZ = 24, 13
            zs = [0.95 + (9.6 - 0.95) * (i / GZ) ** 1.35 for i in range(GZ + 1)]
            rows = []
            for z in zs:
                pts = []
                for i in range(GX + 1):
                    x = -5.2 + 10.4 * i / GX
                    y = lvl + self._wave(x, z, u)
                    sx, sy, d = project((x, y, z), focal)
                    pts.append((cx + sx, cy + sy, y))
                rows.append((z, pts))

            # body of the pool: everything below the near surface edge, so it
            # reads as a filling volume of toxin instead of a floating ribbon
            _zn0, near0 = rows[0]
            body = QPainterPath()
            body.moveTo(near0[0][0], near0[0][1])
            for x_, y_, _wy in near0[1:]:
                body.lineTo(x_, y_)
            body.lineTo(w + 40, h + 40)
            body.lineTo(-40, h + 40)
            body.closeSubpath()
            bg = QLinearGradient(0, near0[0][1] - 30, 0, h)
            bg.setColorAt(0.0, _qc(C_TOXIC, 205))
            bg.setColorAt(0.35, _qc("#7A2CD4", 195))
            bg.setColorAt(1.0, _qc("#160726", 225))
            p.setPen(Qt.PenStyle.NoPen)
            p.fillPath(body, QBrush(bg))

            for k in range(len(rows) - 1, 0, -1):     # far -> near
                zf, far = rows[k]
                zn, near = rows[k - 1]
                depth_t = max(0.0, min(1.0, 1.0 - (zf - 0.95) / 8.8))
                poly = QPolygonF()
                for x_, y_, _wy in far:
                    poly.append(QPointF(x_, y_))
                for x_, y_, _wy in reversed(near):
                    poly.append(QPointF(x_, y_))
                # fake lighting from the surface slope
                slope = (near[GX // 2][2] - far[GX // 2][2])
                lit = max(0.0, min(1.0, 0.5 + slope * 2.6))
                base = QColor(_lerp_hex("#3A1668", C_TOXIC, 0.25 + 0.55 * depth_t))
                col_ = QColor(_lerp_hex(base.name(), C_TOXIC2, lit * 0.45))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_qc(col_.name(), int(150 + 90 * depth_t)))
                p.drawPolygon(poly)

            # crest highlight along the nearest edge
            zn, near = rows[0]
            crest = QPainterPath()
            crest.moveTo(near[0][0], near[0][1])
            for x_, y_, _wy in near[1:]:
                crest.lineTo(x_, y_)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_qc("#EEE0FF", 120), 1.3))
            p.drawPath(crest)

            # bubbles suspended in the toxin
            p.setPen(Qt.PenStyle.NoPen)
            for b in self.bubbles:
                by = lvl + 0.30 + 0.16 * math.sin(self.t * 2.4 + b["p"])
                sx, sy, d = project((b["x"], by, b["z"]), focal)
                rr = max(1.0, b["r"] / d * 2.4)
                p.setBrush(_qc("#E4D2FF", 70))
                p.drawEllipse(QPointF(cx + sx, cy + sy), rr, rr * 0.7)

            # impact ripples riding the surface
            p.setBrush(Qt.BrushStyle.NoBrush)
            for r in self.rings[-14:]:
                a = 1.0 - r["age"] / 0.85
                rad = r["r0"] + r["age"] * 0.85     # world units, not screen
                sx, sy, d = project((r["x"], lvl, r["z"]), focal)
                rw = max(2.0, rad * focal / d)
                p.setPen(QPen(_qc("#EEE0FF", int(95 * a)), 1.2))
                p.drawEllipse(QPointF(cx + sx, cy + sy), rw, rw * 0.28)

        # ── 4. metaball droplets (additive so they merge) ──
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        p.setPen(Qt.PenStyle.NoPen)
        for d in self.drops:
            if d["dead"] or self.t < d.get("t0", 0.0):
                continue
            for ti, (tx, ty, tz) in enumerate(d["trail"][::2]):
                sx, sy, dd = project((tx, ty, tz), focal)
                rr = max(0.8, d["r"] * focal / dd) * (0.30 + 0.55 * ti / 4.0)
                p.setBrush(_qc(C_TOXIC, 26))
                p.drawEllipse(QPointF(cx + sx, cy + sy), rr, rr)
            sx, sy, dd = project((d["x"], d["y"], d["z"]), focal)
            rr = max(1.1, d["r"] * focal / dd) * 2.3
            p.drawPixmap(QRectF(cx + sx - rr, cy + sy - rr, rr * 2, rr * 2),
                         self._blob, QRectF(0, 0, 96, 96))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # ── 5. drain drips on the way out ──
        drain = _smooth(0.80, 1.0, u)
        if drain > 0.01:
            p.setPen(Qt.PenStyle.NoPen)
            for i in range(16):
                fx = (i + 0.5) / 16.0
                ph = math.sin(i * 12.9898) * 43758.5453
                ph -= math.floor(ph)
                dl = (drain - ph * 0.35)
                if dl <= 0:
                    continue
                dy = h * (0.55 + dl * 0.9)
                ww = 3.0 + 4.0 * (1.0 - drain)
                p.setBrush(_qc(C_TOXIC, int(160 * (1.0 - drain))))
                p.drawEllipse(QPointF(w * fx, dy), ww, ww * 2.6)
        p.end()


class _CommandBar(QFrame):
    """Bottom bar with live hazard tape."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.t = 0.0
        self.setFixedHeight(66)

    def advance(self, dt):
        self.t += dt
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(C_PANEL))
        p.setPen(QPen(QColor(C_BORDER), 1))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 14, 14)
        p.end()
