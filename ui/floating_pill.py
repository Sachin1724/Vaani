import ctypes
import math
import re
import time
import glob
import os
from functools import lru_cache
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QLabel, QPushButton,
    QProgressBar, QFrame, QApplication, QMenu, QSizePolicy, QCheckBox
)
from PyQt6.QtCore import (
    Qt, QTimer, QPoint, QPointF, QRect, QRectF, QSize, QPropertyAnimation, QVariantAnimation,
    QEasingCurve, pyqtSignal
)
from PyQt6.QtGui import (
    QRegion,
    QPainter, QColor, QBrush, QPen, QFont, QFontDatabase, QFontMetricsF, QLinearGradient,
    QPainterPath, QPolygonF, QPixmap, QIcon, QPalette, QCursor, QGuiApplication
)


try:
    from ..config import get_asset_path
    from ..engine.audio_recorder import list_microphones
    from ..engine import model_manager
except (ImportError, ValueError):
    from config import get_asset_path
    from engine.audio_recorder import list_microphones
    from engine import model_manager
try:
    from .blobatar_widget import BlobatarWidget
except (ImportError, ValueError):
    try:
        from vaani.ui.blobatar_widget import BlobatarWidget
    except ImportError:
        from ui.blobatar_widget import BlobatarWidget


def _force_topmost(widget: QWidget):
    """Qt's stay-on-top hint is set once; other topmost windows (Teams, games, full-screen apps)
    can still cover us. Re-assert HWND_TOPMOST every time the capsule shows."""
    if not isinstance(widget, QMenu):
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QMenu) and w.isVisible():
                return
    HWND_TOPMOST, SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = -1, 0x0001, 0x0002, 0x0010
    try:
        # c_void_p: a bare -1 would reach a 64-bit HWND as 0xFFFFFFFF, not HWND_TOPMOST
        ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(int(widget.winId())), ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                                          SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    except Exception:
        pass

try:
    from ..engine.preview_text import merge_preview
except ImportError:
    try:
        from vaani.engine.preview_text import merge_preview
    except ImportError:
        from engine.preview_text import merge_preview

_ASSETS = get_asset_path().replace("\\", "/")

# Website palette (docs/site.css): light = cream surfaces, slate ink, steel accent; dark = the same family inverted.
# Painters read these module tokens on every paint; stylesheets that bake them in are re-applied on a switch.
THEMES = {
    "light": dict(BG="#F5EFE6", SURFACE="#E8DFCA", SURFACE_HI="#DCD1B6", SURFACE_PRESS="#D0C4A6",
                  INK="#1E2A36", SOFT="#4A5868", INK_HOVER="#2E4560", INK_PRESS="#16202A",
                  CHECK_ICON="check.svg", CHECK_ICON_ON="check_white.svg", PILL_INK="#24303D", PILL_SOFT="#5C6A79",
                  ACCENT="#7895B2", SWITCH_ON="#7895B2"),
    # dark: the Dynamic Island's black and greys (ui/island.py), so both capsule styles look alike
    "dark": dict(BG="#000000", SURFACE="#1C1D21", SURFACE_HI="#2A2C31", SURFACE_PRESS="#3A3D44",
                 INK="#FFFFFF", SOFT="#9A9FA9", INK_HOVER="#E6E6E6", INK_PRESS="#CFCFD4",
                 CHECK_ICON="check_white.svg", CHECK_ICON_ON="check.svg", PILL_INK="#FFFFFF", PILL_SOFT="#9A9FA9",
                 ACCENT="#FFFFFF", SWITCH_ON="#22C55E"),
}


THEME_KEYS = ("BG", "SURFACE", "SURFACE_HI", "SURFACE_PRESS", "INK", "SOFT", "INK_HOVER", "INK_PRESS",
              "CHECK_ICON", "CHECK_ICON_ON", "PILL_INK", "PILL_SOFT", "ACCENT", "SWITCH_ON")
THEME = "light"
# the active theme's tokens, declared here so tools can see them; use_theme() assigns them
BG = SURFACE = SURFACE_HI = SURFACE_PRESS = INK = SOFT = INK_HOVER = INK_PRESS = ""
CHECK_ICON = CHECK_ICON_ON = PILL_INK = PILL_SOFT = ACCENT = SWITCH_ON = ""
FG = FG_DIM = FG_FAINT = HAIRLINE = TINT = TINT_STRONG = FILL = ""


def use_theme(name: str):
    """Make "light" or "dark" the active colour tokens (FloatingPill.set_theme also restyles live widgets)."""
    global THEME, BG, SURFACE, SURFACE_HI, SURFACE_PRESS, INK, SOFT, INK_HOVER, INK_PRESS
    global CHECK_ICON, CHECK_ICON_ON, PILL_INK, PILL_SOFT, ACCENT, SWITCH_ON
    global FG, FG_DIM, FG_FAINT, HAIRLINE, TINT, TINT_STRONG, FILL
    THEME = name if name in THEMES else "light"
    (BG, SURFACE, SURFACE_HI, SURFACE_PRESS, INK, SOFT, INK_HOVER, INK_PRESS,
     CHECK_ICON, CHECK_ICON_ON, PILL_INK, PILL_SOFT, ACCENT, SWITCH_ON) = (THEMES[THEME][k] for k in THEME_KEYS)
    c = QColor(INK)

    def rgba(a):
        return f"rgba({c.red()}, {c.green()}, {c.blue()}, {a})"
    FG, FILL = INK, SURFACE
    FG_DIM, FG_FAINT, HAIRLINE, TINT, TINT_STRONG = rgba(0.62), rgba(0.38), rgba(0.12), rgba(0.08), rgba(0.14)


TONES = {  # alert / badge colours as "r,g,b"
    "light": {"green": "21,128,61", "red": "180,35,24", "steel": "62,90,117", "neutral": "74,88,104"},
    "dark": {"green": "34,197,94", "red": "239,68,68", "steel": "214,217,224", "neutral": "154,159,169"},
}


def _tone(name: str) -> str:
    return TONES[THEME].get(name, TONES[THEME]["neutral"])


def _accent(alpha: float = 255) -> QColor:
    c = QColor(ACCENT)
    c.setAlpha(int(alpha))
    return c


def _fg(alpha: float = 255) -> QColor:
    c = QColor(INK)
    c.setAlpha(int(alpha))
    return c


def _bgc(alpha: float = 255) -> QColor:
    c = QColor(BG)
    c.setAlpha(int(alpha))
    return c


def _mix(a: str, b: str, k: float, alpha: float = 255) -> QColor:
    ca, cb = QColor(a), QColor(b)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * k), round(ca.green() + (cb.green() - ca.green()) * k),
                  round(ca.blue() + (cb.blue() - ca.blue()) * k), int(alpha))


use_theme("light")
GREEN = "#22C55E"
RED = "#EF4444"


def screen_scale(screen) -> float:
    """Bigger screens get a proportionally bigger capsule, never a smaller one: 1920 px wide = 1x, 2560 = 1.33x."""
    return max(1.0, min(1.5, screen.geometry().width() / 1920.0)) if screen else 1.0


def get_squircle_pixmap(image_path: str, size: int = 26, radius: float = 6.0) -> QPixmap:
    """Renders a high-DPI squircle brand emblem with smooth anti-aliasing and hairline rim."""
    dest = QPixmap(size, size)
    dest.fill(Qt.GlobalColor.transparent)
    if not os.path.exists(image_path):
        return dest
    src = QPixmap(image_path).scaled(
        size * 2, size * 2,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation
    )
    painter = QPainter(dest)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, size, size, src)
    painter.setClipping(False)
    # Subtle translucent outer rim
    painter.setPen(QPen(_fg(38), 1.0))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(0.5, 0.5, size - 1, size - 1), radius, radius)
    painter.end()
    return dest


def animations_enabled():
    """Windows 'Animation effects' (Settings > Accessibility > Visual effects). Off: the shape snaps, fades stay short."""
    try:
        import ctypes
        on = ctypes.c_int(1)
        ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0)  # SPI_GETCLIENTAREAANIMATION
        return bool(on.value)
    except (AttributeError, OSError):
        return True


def _bezier(x1, y1, x2, y2) -> QEasingCurve:
    c = QEasingCurve(QEasingCurve.Type.BezierSpline)
    c.addCubicBezierSegment(QPointF(x1, y1), QPointF(x2, y2), QPointF(1, 1))
    return c


SNAP_EASE = _bezier(0.05, 0.6, 0.4, 0.95)  # sharp ease-out for size changes: most of the move lands in the first frames
BUBBLE_EASE = QEasingCurve(QEasingCurve.Type.OutBack)
BUBBLE_EASE.setOvershoot(1.25)  # gentle organic bounce on bubble wake/expand without jarring snap


@lru_cache(maxsize=1)
def ui_font_family() -> str:
    """Bundled General Sans (the website's face; Inter as fallback), so every PC looks the same. Never calls
    QFontDatabase.families(): enumerating system fonts takes seconds on machines with many fonts installed."""
    family = ""
    fonts = sorted(glob.glob(os.path.join(get_asset_path("fonts"), "*.ttf")))
    for f in sorted(fonts, key=lambda p: not os.path.basename(p).startswith("GeneralSans")):  # GeneralSans-* first
        families = QFontDatabase.applicationFontFamilies(QFontDatabase.addApplicationFont(f))
        family = family or (families[0] if families else "")
    return family or "Segoe UI"


def ui_font(family: str, size: float, weight=QFont.Weight.Normal, px: bool = False) -> QFont:
    """The UI face, unhinted and antialiased: Windows' default hinting snaps stems to the pixel grid,
    which looks jagged at 100-125 % scaling on these small translucent windows."""
    f = QFont(family)
    if px:
        f.setPixelSize(int(size))
    else:
        f.setPointSizeF(float(size))
    f.setWeight(weight)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    # greyscale, not ClearType: subpixel colour fringes show as blue/orange edges on translucent windows
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.NoSubpixelAntialias)
    return f


class StateIndicator(QWidget):
    """Monochrome vector indicator: breathing idle dot, pulsing rings, spinner arc, or animated checkmark."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self.state = "idle"
        self.count = 0  # items held in the container: shown instead of the idle dot
        self.phase = 0.0
        self._check_p = 1.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.start(16)

    def set_state(self, state: str):
        if state == "done" and self.state != "done":
            self._check_p = 0.0
        self.state = state
        self.update()

    def _tick(self):
        self.phase += 0.09
        if self.state == "done" and self._check_p < 1.0:
            self._check_p = min(1.0, self._check_p + 0.075)
        self.update()

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # drawn on a 20 px design, scaled to the indicator's size, so a bigger capsule gets a bigger dot
        k = min(self.width(), self.height()) / 20.0
        p.translate(self.width() / 2.0, self.height() / 2.0)
        p.scale(k, k)
        c = QPointF(0.0, 0.0)

        if self.state == "listening":
            pulse = 0.5 + 0.5 * math.sin(self.phase * 1.6)
            ring = 5.0 + 3.5 * pulse
            p.setPen(QPen(QColor(239, 68, 68, int(115 * (1 - pulse) + 25)), 1.3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, ring, ring)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(239, 68, 68))
            p.drawEllipse(c, 3.6, 3.6)
        elif self.state == "processing":
            angle = int(math.degrees(self.phase * 2.4)) % 360
            p.setPen(QPen(_fg(220), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(QRectF(c.x() - 7, c.y() - 7, 14, 14), -angle * 16, 100 * 16 * 2)
        elif self.state == "done":
            # Animated stroke draw
            p.setPen(QPen(_fg(), 2.0, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            pt1 = QPointF(c.x() - 5, c.y() + 0.5)
            pt2 = QPointF(c.x() - 1.5, c.y() + 4.0)
            pt3 = QPointF(c.x() + 5.5, c.y() - 4.0)

            path = QPainterPath()
            path.moveTo(pt1)
            prog = self._check_p
            if prog <= 0.40:
                seg_p = prog / 0.40
                cur = pt1 + (pt2 - pt1) * seg_p
                path.lineTo(cur)
            else:
                path.lineTo(pt2)
                seg_p = (prog - 0.40) / 0.60
                cur = pt2 + (pt3 - pt2) * seg_p
                path.lineTo(cur)
            p.drawPath(path)
        elif self.count > 0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_fg())
            p.drawEllipse(c, 8.0, 8.0)
            p.setPen(QColor(BG))
            p.setFont(ui_font(ui_font_family(), 7, QFont.Weight.Bold))
            p.drawText(QRectF(-10, -10, 20, 20), Qt.AlignmentFlag.AlignCenter, str(self.count) if self.count < 100 else "99")
        else:
            # Idle: gentle Apple breathing luminescence
            b = 0.5 + 0.5 * math.sin(self.phase * 0.4)
            alpha = int(75 + 45 * b)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_fg(alpha))
            p.drawEllipse(c, 3.2, 3.2)



class WaveformBars(QWidget):
    """
    Apple Dynamic Island / Siri style fluid audio visualizer.
    Features living traveling sine waves with formant weighting across 7 bars,
    responsive to both resting ambient silence and active voice dynamics.
    """
    def __init__(self, parent=None, num_bars=7):
        super().__init__(parent)
        self.num_bars = num_bars
        self.target_level = 0.0
        self._current_level = 0.0
        self.phase = 0.0
        self.setFixedSize(48, 22)
        self.color = None  # None follows the theme ink; the black island sets white

        # Formant center weights (highest in middle, tapered at sides)
        self.bar_weights = [0.35, 0.65, 0.95, 1.0, 0.95, 0.65, 0.35]
        self.bar_heights = [4.5] * num_bars

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.start(16)  # 60 FPS fluid rendering

    LEVEL_SLEW = 3.0  # max level change per second (0..1 scale)

    def _scale(self) -> float:
        """1 at the default 22 px; larger capsules get proportionally thicker, taller bars."""
        return max(1.0, self.height() / 22.0)

    def set_bar_count(self, count: int):
        self.num_bars = count
        if count == 7:
            self.bar_weights = [0.35, 0.65, 0.95, 1.0, 0.95, 0.65, 0.35]
        elif count == 3:
            self.bar_weights = [0.7, 1.0, 0.7]
        else:
            self.bar_weights = [0.45, 0.85, 1.0, 0.85, 0.45]
        self.bar_heights = [4.5] * count
        self.update()

    def set_level(self, level: float):
        # Audio level registration with high sensitivity
        self.target_level = max(0.0, min(1.0, level))

    def reset(self):
        self.target_level = 0.0
        self._current_level = 0.0

    def _tick(self):
        if not self.isVisible():
            return

        # Advance traveling phase continuously for living wave
        self.phase += 0.16

        # Smooth input loudness the same at any frame rate: move 15% toward the target per 60 Hz frame,
        # never faster than LEVEL_SLEW per second, so a cough or a dropped buffer can't make the bars jump.
        now = time.monotonic()
        dt = min(max(now - getattr(self, "_last_t", now - 1 / 60), 0.001), 0.2)
        self._last_t = now
        k = math.pow(0.85, dt * 60.0)
        step = (self._current_level * k + self.target_level * (1.0 - k)) - self._current_level
        cap = dt * self.LEVEL_SLEW
        self._current_level += max(-cap, min(cap, step))

        lvl = self._current_level
        sc = self._scale()
        max_h = float(self.height() - 2)  # up to 20px
        min_h = 4.2 * sc

        for i in range(self.num_bars):
            # Traveling harmonic waves across the bars
            wave1 = math.sin(self.phase * 3.0 - i * 0.90)
            wave2 = math.cos(self.phase * 1.8 + i * 0.60)
            combined_wave = 0.5 + 0.5 * (0.65 * wave1 + 0.35 * wave2)

            # Resting / ambient wave: always gently alive and wavy when listening
            resting_h = min_h + (5.5 * sc * self.bar_weights[i] * combined_wave)

            # Active voice speech modulation:
            # Power expansion ensures quiet speech lifts middle bars dramatically
            speech_boost = math.pow(lvl, 0.35) if lvl > 0.005 else 0.0
            speech_wave = 0.50 + 0.50 * math.sin(self.phase * 4.2 - i * 1.15)
            voice_h = (max_h - min_h) * self.bar_weights[i] * speech_boost * speech_wave

            target = resting_h + voice_h
            target = max(min_h, min(max_h, target))

            cur = self.bar_heights[i]
            rate = 18.0 if target > cur else 9.0
            k_bar = 1.0 - math.exp(-dt * rate)
            self.bar_heights[i] = cur + (target - cur) * k_bar

        self.update()

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)

        alpha = int(210 + 45 * min(1.0, self._current_level * 2.5))
        c = QColor(self.color or INK)
        c.setAlpha(alpha)
        p.setBrush(c)

        sc = self._scale()
        bar_w = (2.6 if self.num_bars >= 7 else 3.2) * sc
        gap = (2.8 if self.num_bars >= 7 else 3.4) * sc
        total = self.num_bars * bar_w + (self.num_bars - 1) * gap
        x0 = (self.width() - total) / 2.0

        for i, h in enumerate(self.bar_heights):
            h = max(3.5, min(float(self.height() - 1), h))
            y = (self.height() - h) / 2.0
            x = x0 + i * (bar_w + gap)
            p.drawRoundedRect(QRectF(x, y, bar_w, h), bar_w / 2.0, bar_w / 2.0)


class MoreButton(QPushButton):
    """Round button drawing three dots (no glyph dependency)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(26, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hovered = self.underMouse()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_fg(40 if hovered else 18))
        p.drawEllipse(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
        p.setBrush(_fg(235 if hovered else 190))
        cy = self.height() / 2.0
        for dx in (-5, 0, 5):
            p.drawEllipse(QRectF(self.width() / 2.0 + dx - 1.4, cy - 1.4, 2.8, 2.8))


class StopButton(QPushButton):
    """Round stop button shown on the capsule while dictating so hands-free/toggle mode can be stopped."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(22, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Stop dictation")

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hovered = self.underMouse()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(239, 68, 68, 235 if hovered else 195))
        p.drawEllipse(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
        p.setBrush(QColor(255, 255, 255, 245))
        cx, cy = self.width() / 2.0, self.height() / 2.0
        p.drawRoundedRect(QRectF(cx - 3.6, cy - 3.6, 7.2, 7.2), 1.6, 1.6)



class Switch(QCheckBox):
    """iOS-style switch drawn in place of the checkbox; same API as QCheckBox (isChecked, toggled...)."""
    _TW, _TH = 38, 22

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pos = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)  # no overshoot
        self._anim.valueChanged.connect(self._on_tick)
        self.toggled.connect(self._animate)

    def _on_tick(self, v):
        self._pos = float(v)
        self.update()

    def _animate(self, on: bool):
        self._anim.stop()
        if not self.isVisible():  # initial state while building the UI: no slide
            self._pos = 1.0 if on else 0.0
            return
        self._anim.setStartValue(self._pos)  # from where the knob is now, so rapid taps reverse smoothly
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def sizeHint(self):
        fm = self.fontMetrics()
        w = self._TW + (10 + fm.horizontalAdvance(self.text()) if self.text() else 0)
        return QSize(w + 2, max(self._TH + 4, fm.height() + 4))

    def minimumSizeHint(self):
        return self.sizeHint()

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, a0):
        if self._anim.state() != QVariantAnimation.State.Running:
            self._pos = 1.0 if self.isChecked() else 0.0  # setChecked under blockSignals
        t = self._pos
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        tw, th = self._TW, self._TH
        y = (self.height() - th) / 2.0
        off, on = QColor(120, 120, 128, 82), QColor(SWITCH_ON)
        track = QColor(
            round(off.red() + (on.red() - off.red()) * t), round(off.green() + (on.green() - off.green()) * t),
            round(off.blue() + (on.blue() - off.blue()) * t), round(off.alpha() + (255 - off.alpha()) * t))
        if not self.isEnabled():
            track.setAlpha(track.alpha() // 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, y, tw, th), th / 2.0, th / 2.0)
        d = th - 4.0
        x = 2.0 + (tw - d - 4.0) * t
        p.setBrush(QColor(0, 0, 0, 60))
        p.drawEllipse(QRectF(x, y + 2.8, d, d))
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QRectF(x, y + 2.0, d, d))
        if self.text():
            p.setFont(self.font())
            p.setPen(self.palette().color(QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled,
                                          QPalette.ColorRole.WindowText))
            p.drawText(QRectF(tw + 10, 0, self.width() - tw - 10, self.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())


_ASSETS = get_asset_path().replace("\\", "/")


class AnimatedText(QWidget):
    """Single-line text that animates its changes.
    - Replacing text (state change): the old line lifts and fades out while the new one rises in.
    - Growing text (live preview): the existing words stay put and only the new words fade in;
      when the line overflows it scrolls left under a soft fade, like a caption."""

    def __init__(self, text: str, font: QFont, parent=None):
        super().__init__(parent)
        self._font = font
        self._text = text
        self._prev = ""
        self._mode = "swap"
        self._p = 1.0
        self._x0 = 0.0
        self._x_cur = 0.0
        self._ease = QEasingCurve(QEasingCurve.Type.OutCubic)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(240)
        self._anim.valueChanged.connect(self._on_tick)

    def text(self) -> str:
        return self._text

    def setText(self, text: str):
        if text == self._text:
            return
        self._x0 = self._x_cur
        # Speech continuation: keep the text in place and extend it, instead of flipping the
        # whole label, while a partial transcript grows.
        # Grow mode paints _prev followed by _text[len(_prev):], so it only renders the right
        # string when _text genuinely starts with _prev. A looser test (matching prefixes, or
        # merely similar lengths) sends real label changes down this path and paints a hybrid
        # of the two: "Refining" -> "Pasted 4 words" came out as "Refining words".
        is_grow = bool(self._text and text.startswith(self._text))
        self._mode = "grow" if is_grow else "swap"
        self._prev = self._text
        self._text = text
        self._p = 0.0
        self._anim.stop()
        self._anim.setDuration(150 if self._mode == "grow" else 220)
        self._anim.start()

    def _on_tick(self, v):
        self._p = float(v)
        self.update()

    def _target_x(self, fm: QFontMetricsF, text: str) -> float:
        tw = fm.horizontalAdvance(text)
        return 0.0 if tw <= self.width() else self.width() - tw

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setFont(self._font)
        p.setPen(_fg())
        fm = QFontMetricsF(self._font)
        base_y = (self.height() + fm.ascent() - fm.descent()) / 2.0
        e = self._ease.valueForProgress(self._p)

        if self._mode == "swap":
            w = self.width()
            if self._prev and e < 1.0:
                p.setOpacity(1.0 - e)
                p.drawText(QPointF(0, base_y - 7 * e), fm.elidedText(self._prev, Qt.TextElideMode.ElideRight, w))
            p.setOpacity(e)
            p.drawText(QPointF(0, base_y + 7 * (1.0 - e)), fm.elidedText(self._text, Qt.TextElideMode.ElideRight, w))
            self._x_cur = 0.0
            return

        tx = self._target_x(fm, self._text)
        x = self._x0 + (tx - self._x0) * e
        self._x_cur = x
        prefix, tail = self._prev, self._text[len(self._prev):]
        p.drawText(QPointF(x, base_y), prefix)
        p.setOpacity(e)
        p.drawText(QPointF(x + fm.horizontalAdvance(prefix), base_y), tail)
        if x < -0.5:  # overflowing: fade the left edge into the capsule
            p.setOpacity(1.0)
            g = QLinearGradient(0, 0, 26, 0)
            g.setColorAt(0.0, QColor(0, 0, 0, 255))
            g.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(QRectF(0, 0, 26, self.height()), QBrush(g))


class ValueRow(QWidget):
    """iOS-style row: title on the left, current value and a pop-up chevron on the right.
    Tapping opens a rounded, translucent menu with the current choice checked.
    Provides smooth animated glass hover highlights and value transitions."""
    currentIndexChanged = pyqtSignal(int)

    def __init__(self, title: str, font_family: str, parent=None):
        super().__init__(parent)
        self._title = title
        self._family = font_family
        self._items = []
        self._idx = -1
        self._disabled = set()
        self._pressed = False
        self._hover_alpha = 0.0
        self._val_alpha = 1.0
        self.setFixedHeight(44)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(150)
        self._hover_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hover_anim.valueChanged.connect(self._on_hover_tick)

        self._val_anim = QVariantAnimation(self)
        self._val_anim.setDuration(130)
        self._val_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._val_anim.valueChanged.connect(self._on_val_tick)

    def _on_hover_tick(self, val):
        self._hover_alpha = float(val)
        self.update()

    def _on_val_tick(self, val):
        self._val_alpha = float(val)
        self.update()

    def clear(self):
        self._items, self._idx = [], -1
        self._disabled = set()
        self.update()

    def setItemEnabled(self, i: int, enabled: bool):
        if enabled:
            self._disabled.discard(i)
        else:
            self._disabled.add(i)

    def addItem(self, text, data=None):
        self._items.append((text, data))
        if self._idx < 0:
            self._idx = 0
        self.update()

    def count(self):
        return len(self._items)

    def currentIndex(self):
        return self._idx

    def setCurrentIndex(self, i: int):
        if 0 <= i < len(self._items) and i != self._idx:
            self._idx = i
            self._val_anim.stop()
            self._val_anim.setStartValue(0.2)
            self._val_anim.setEndValue(1.0)
            self._val_anim.start()
            self.currentIndexChanged.emit(i)

    def currentData(self):
        return self._items[self._idx][1] if self._idx >= 0 else None

    def currentText(self):
        return self._items[self._idx][0] if self._idx >= 0 else ""

    def itemText(self, i: int) -> str:
        return self._items[i][0] if 0 <= i < len(self._items) else ""

    def itemData(self, i: int):
        return self._items[i][1] if 0 <= i < len(self._items) else None

    def enterEvent(self, event):
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover_alpha)
        self._hover_anim.setEndValue(1.0)
        self._hover_anim.start()

    def leaveEvent(self, a0):
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover_alpha)
        self._hover_anim.setEndValue(0.0)
        self._hover_anim.start()

    def mousePressEvent(self, a0):
        if a0.button() == Qt.MouseButton.LeftButton:
            self._pressed = True  # feedback on press, not release
            self.update()

    def mouseReleaseEvent(self, a0):
        self._pressed = False
        self.update()
        if a0.button() != Qt.MouseButton.LeftButton or not self._items:
            return

        pill = self.window()
        has_topmost_timer = hasattr(pill, "_topmost_timer") and isinstance(pill, FloatingPill)
        if has_topmost_timer:
            pill._topmost_timer.stop()

        menu = QMenu(self)
        menu.setWindowFlags(
            Qt.WindowType.Popup |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.NoDropShadowWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        menu.setStyleSheet(f"""
            QMenu {{ background: {BG}; border: 1px solid {HAIRLINE};
                     border-radius: 10px; padding: 5px; color: {INK}; }}
            QMenu::item {{ padding: 6px 22px 6px 30px; border-radius: 6px; margin: 0; }}
            QMenu::item:selected {{ background: {INK}; color: {BG}; }}
            QMenu::item:disabled {{ color: {FG_FAINT}; background: transparent; }}
            QMenu::indicator {{ width: 14px; height: 14px; left: 9px; }}
            QMenu::indicator:checked {{ image: url({_ASSETS}/{CHECK_ICON}); }}
            QMenu::indicator:checked:selected {{ image: url({_ASSETS}/{CHECK_ICON_ON}); }}
        """)
        menu.setFont(ui_font(self._family, 10))
        for i, (text, _) in enumerate(self._items):
            act = menu.addAction(text)
            act.setCheckable(True)
            act.setChecked(i == self._idx)
            act.setEnabled(i not in self._disabled)
            act.triggered.connect(lambda _=False, idx=i: QTimer.singleShot(15, lambda: self.setCurrentIndex(idx)))

        # Ensure the menu stays strictly in front of FloatingPill on Windows
        menu.aboutToShow.connect(lambda: (_force_topmost(menu), menu.raise_()))
        QTimer.singleShot(0, lambda: (_force_topmost(menu), menu.raise_()))

        try:
            pos = self.mapToGlobal(QPoint(0, self.height() + 2))
            scr = QGuiApplication.screenAt(pos) or QApplication.primaryScreen()
            need = menu.sizeHint().height()
            if scr and pos.y() + need > scr.availableGeometry().bottom():
                pos = self.mapToGlobal(QPoint(0, -need - 2))  # no room below: open above the tile
            menu.exec(pos)
        finally:
            if has_topmost_timer and pill.isVisible():
                pill._topmost_timer.start(2000)

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Smooth animated glass highlight on hover
        if self._hover_alpha > 0.01 or self._pressed:
            p.fillRect(self.rect(), QColor(255, 255, 255, int(12 * self._hover_alpha) + (14 if self._pressed else 0)))

        f = ui_font(self._family, 10)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRectF(14, 0, 96, self.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._title)

        cx, cy = self.width() - 18, self.height() / 2.0
        pen_alpha = int(120 + 85 * self._hover_alpha)
        pen = QPen(QColor(255, 255, 255, pen_alpha), 1.5,
                   Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPolyline(QPolygonF([QPointF(cx - 3, cy - 2.5), QPointF(cx, cy - 5.5), QPointF(cx + 3, cy - 2.5)]))
        p.drawPolyline(QPolygonF([QPointF(cx - 3, cy + 2.5), QPointF(cx, cy + 5.5), QPointF(cx + 3, cy + 2.5)]))

        fm = QFontMetricsF(f)
        vr = QRectF(112, 0, self.width() - 112 - 34, self.height())
        val_pen_alpha = int((150 + 50 * self._hover_alpha) * self._val_alpha)
        p.setPen(QColor(255, 255, 255, val_pen_alpha))
        p.drawText(vr, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                   fm.elidedText(self.currentText(), Qt.TextElideMode.ElideRight, vr.width()))


def _tile_bg(p: QPainter, r: QRectF, hover: float = 0.0, pressed: bool = False):
    lo, hi = QColor(SURFACE), QColor(SURFACE_PRESS if pressed else SURFACE_HI)
    k = 1.0 if pressed else hover
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(round(lo.red() + (hi.red() - lo.red()) * k), round(lo.green() + (hi.green() - lo.green()) * k),
                      round(lo.blue() + (hi.blue() - lo.blue()) * k)))
    p.drawRoundedRect(r, 12.0, 12.0)


def _tile_title(p: QPainter, family: str, title: str, width: int):
    p.setFont(ui_font(family, 7, QFont.Weight.DemiBold))
    p.setPen(_fg(130))
    p.drawText(QRectF(12, 8, width - 24, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title.upper())


class PickerTile(ValueRow):
    """Bento tile over ValueRow's items and pop-up: small title on top, value below.
    The mouse wheel cycles the value in place, skipping disabled items."""

    def __init__(self, title: str, font_family: str, parent=None):
        super().__init__(title, font_family, parent)
        self.setFixedHeight(60)
        self.level = -1.0         # >= 0 draws live input bars (mic tile)
        self.short_value = False  # show only the part before " · " (model names)
        self.wheel_cycles = True  # the mouse wheel changes the value (off for the microphone)

    def wheelEvent(self, a0):
        a0.accept()
        if not self._items or not self.wheel_cycles:
            return
        step = -1 if a0.angleDelta().y() > 0 else 1
        i = self._idx
        for _ in range(len(self._items)):
            i = (i + step) % len(self._items)
            if i not in self._disabled:
                break
        self.setCurrentIndex(i)

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        _tile_bg(p, QRectF(self.rect()), self._hover_alpha, self._pressed)
        _tile_title(p, self._family, self._title, w)
        if self.level >= 0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_fg(210))
            for i, k in enumerate((0.45, 0.8, 1.0, 0.8, 0.45)):
                bh = 2.0 + 10.0 * self.level * k
                p.drawRoundedRect(QRectF(w - 38 + i * 5, 16 - bh / 2, 3, bh), 1.5, 1.5)

        f = ui_font(self._family, 10, QFont.Weight.Medium)
        p.setFont(f)
        text = self.currentText().split(" · ")[0] if self.short_value else self.currentText()
        vr = QRectF(12, h - 32, w - 40, 22)
        p.setPen(_fg(int(255 * self._val_alpha)))
        p.drawText(vr, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetricsF(f).elidedText(text, Qt.TextElideMode.ElideRight, vr.width()))

        cx, cy = w - 18, vr.center().y()
        p.setPen(QPen(_fg(int(120 + 85 * self._hover_alpha)), 1.5,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPolyline(QPolygonF([QPointF(cx - 3, cy - 2.5), QPointF(cx, cy - 5.5), QPointF(cx + 3, cy - 2.5)]))
        p.drawPolyline(QPolygonF([QPointF(cx - 3, cy + 2.5), QPointF(cx, cy + 5.5), QPointF(cx + 3, cy + 2.5)]))


class ChipTile(ValueRow):
    """Full-width bento tile: every choice is a chip, one tap picks it, the ink fill slides across."""

    def __init__(self, title: str, font_family: str, parent=None):
        super().__init__(title, font_family, parent)
        self.setFixedHeight(64)
        self.setMouseTracking(True)
        self._fill_i = -1.0  # fill position in chip units, animated between choices
        self._hover_i = self._press_i = -1
        self._slide = QVariantAnimation(self)
        self._slide.setDuration(160)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide.valueChanged.connect(self._on_slide)

    def _on_slide(self, v):
        self._fill_i = float(v)
        self.update()

    def _chip(self, i: float) -> QRectF:
        cw = (self.width() - 16.0) / max(1, len(self._items))
        return QRectF(8.0 + i * cw + 2.0, 28.0, cw - 4.0, 28.0)

    def _chip_at(self, pos) -> int:
        return next((i for i in range(len(self._items)) if self._chip(i).contains(QPointF(pos))), -1)

    def setCurrentIndex(self, i: int):
        super().setCurrentIndex(i)
        self._slide.stop()
        if self._fill_i < 0 or not self.isVisible() or not animations_enabled():
            self._fill_i = float(self._idx)
            self.update()
        elif abs(self._fill_i - self._idx) > 0.001:
            self._slide.setStartValue(self._fill_i)
            self._slide.setEndValue(float(self._idx))
            self._slide.start()

    def mouseMoveEvent(self, a0):
        i = self._chip_at(a0.position())
        if i != self._hover_i:
            self._hover_i = i
            self.update()

    def mousePressEvent(self, a0):
        if a0.button() == Qt.MouseButton.LeftButton:
            self._press_i = self._chip_at(a0.position())  # feedback on press
            self.update()

    def mouseReleaseEvent(self, a0):
        i, self._press_i = self._press_i, -1
        if a0.button() == Qt.MouseButton.LeftButton and i >= 0 and i == self._chip_at(a0.position()):
            self.setCurrentIndex(i)
        self.update()

    def leaveEvent(self, a0):
        self._hover_i = -1
        super().leaveEvent(a0)

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        _tile_bg(p, QRectF(self.rect()))
        _tile_title(p, self._family, self._title, self.width())
        if not self._items:
            return
        r = self._chip(self._fill_i if self._fill_i >= 0 else self._idx)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(INK))
        p.drawRoundedRect(r, 8, 8)
        p.setFont(ui_font(self._family, 9, QFont.Weight.Medium))
        for i, (text, _) in enumerate(self._items):
            cr = self._chip(i)
            if i != self._idx and i in (self._hover_i, self._press_i):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_fg(30 if i == self._press_i else 16))
                p.drawRoundedRect(cr, 8, 8)
            p.setPen(QColor(BG) if i == self._idx else _fg(165))
            p.drawText(cr, Qt.AlignmentFlag.AlignCenter, text)


class ActionTile(ValueRow):
    """Small bento button (reuses ValueRow's hover/press feedback): darkens on press, fires on release inside."""
    clicked = pyqtSignal()

    def __init__(self, title: str, hint: str, font_family: str, parent=None):
        super().__init__(title, font_family, parent)
        self._hint = hint
        self.setFixedHeight(60)

    def mouseReleaseEvent(self, a0):
        was, self._pressed = self._pressed, False
        self.update()
        if was and a0.button() == Qt.MouseButton.LeftButton and self.rect().contains(a0.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        _tile_bg(p, QRectF(self.rect()), self._hover_alpha, self._pressed)
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        p.setFont(ui_font(self._family, 10, QFont.Weight.Medium))
        p.setPen(_fg())
        p.drawText(QRectF(12, 10, w - 24, 18), left, self._title)
        p.setFont(ui_font(self._family, 8))
        p.setPen(_fg(140))
        p.drawText(QRectF(12, h - 26, w - 24, 16), left, self._hint)


class AnimatedVectorIcon(QWidget):
    """28 px squircle tinted with the alert tone (14 %), a 1.5 px round-cap glyph in the tone on top.
    Mic modes: 'connected' (soft acoustic ring), 'switched' (turning arrows), 'unplugged' (warning).
    Device modes: 'charging', 'battery' (level), 'buds' / 'audio', 'usb'."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(28, 28)
        self.mode = "connected"
        self.color = None  # "r,g,b"; None picks the mode's own tone
        self.level = 0     # battery mode: percent
        self.phase = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.start(16)

    def set_mode(self, mode: str, color: str | None = None, level: int = 0):
        self.mode, self.color, self.level = mode, color, level
        self.update()

    def _tick(self):
        if not self.isVisible() or self.mode not in ("connected", "switched"):  # the rest are still glyphs
            return
        self.phase += 0.09
        self.update()

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        tone = self.color or _tone({"connected": "green", "switched": "green", "unplugged": "red"}.get(self.mode, "neutral"))
        col = QColor(*(int(v) for v in tone.split(",")))
        tint = QColor(col)
        tint.setAlpha(36)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(tint)
        p.drawRoundedRect(r, 9, 9)
        pen = QPen(col, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        x, y, m = r.center().x(), r.center().y(), self.mode

        if m == "connected":
            pulse = 0.5 + 0.5 * math.sin(self.phase * 2.0)
            halo = QColor(col)
            halo.setAlpha(int(70 * (1.0 - pulse)))
            p.setPen(QPen(halo, 1.0))
            p.drawEllipse(QPointF(x, y), 8.0 + 2.5 * pulse, 8.0 + 2.5 * pulse)
            p.setPen(pen)
            p.drawRoundedRect(QRectF(x - 2.75, y - 7.5, 5.5, 9), 2.75, 2.75)
            p.drawArc(QRectF(x - 5.5, y - 5, 11, 9.5), 180 * 16, 180 * 16)
            p.drawLine(QPointF(x, y + 4.5), QPointF(x, y + 7))
            p.drawLine(QPointF(x - 3, y + 7), QPointF(x + 3, y + 7))
        elif m == "switched":
            p.save()
            p.translate(x, y)
            p.rotate(math.degrees(self.phase * 2.2) % 360)
            rr = 6.0
            for start in (30, 210):
                p.drawArc(QRectF(-rr, -rr, 2 * rr, 2 * rr), start * 16, 120 * 16)
                end = math.radians(start + 120)
                ex, ey = rr * math.cos(end), -rr * math.sin(end)
                dx, dy = -math.sin(end), -math.cos(end)  # direction of travel at the arc's end
                nx, ny = math.cos(end), -math.sin(end)   # outward normal
                p.drawPolyline(QPolygonF([QPointF(ex - 2.6 * dx + 2 * nx, ey - 2.6 * dy + 2 * ny), QPointF(ex, ey),
                                          QPointF(ex - 2.6 * dx - 2 * nx, ey - 2.6 * dy - 2 * ny)]))
            p.restore()
        elif m == "unplugged":
            tri = QPainterPath()
            tri.moveTo(x, y - 7)
            tri.lineTo(x + 7.5, y + 6)
            tri.lineTo(x - 7.5, y + 6)
            tri.closeSubpath()
            p.drawPath(tri)
            p.drawLine(QPointF(x, y - 2.5), QPointF(x, y + 1.2))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawEllipse(QPointF(x, y + 3.7), 1.0, 1.0)
        elif m == "charging":
            p.setBrush(col)
            p.drawPolygon(QPolygonF([QPointF(x + 1.8, y - 8), QPointF(x - 4.6, y + 1), QPointF(x - 0.4, y + 1),
                                     QPointF(x - 1.8, y + 8), QPointF(x + 4.6, y - 1), QPointF(x + 0.4, y - 1)]))
        elif m == "battery":
            p.drawRoundedRect(QRectF(x - 9, y - 4.5, 15.5, 9), 2.6, 2.6)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(x + 7.2, y - 2, 2, 4), 1.0, 1.0)
            p.drawRoundedRect(QRectF(x - 7, y - 2.5, max(1.5, 11.5 * self.level / 100.0), 5), 1.3, 1.3)
        elif m in ("buds", "audio"):
            p.drawArc(QRectF(x - 6.5, y - 7.5, 13, 13), 0, 180 * 16)
            p.drawLine(QPointF(x - 6.5, y - 1), QPointF(x - 6.5, y + 1))
            p.drawLine(QPointF(x + 6.5, y - 1), QPointF(x + 6.5, y + 1))
            p.setBrush(col)
            p.drawRoundedRect(QRectF(x - 8.5, y, 4.5, 7.5), 2.2, 2.2)
            p.drawRoundedRect(QRectF(x + 4, y, 4.5, 7.5), 2.2, 2.2)
        else:  # usb: a flash drive, tilted
            p.save()
            p.translate(x, y)
            p.rotate(-45)
            p.drawRoundedRect(QRectF(-8, -4, 10.5, 8), 2.6, 2.6)
            p.drawRoundedRect(QRectF(2.5, -2.8, 5, 5.6), 1.2, 1.2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawRect(QRectF(4.2, -1.6, 1.4, 1.1))
            p.drawRect(QRectF(4.2, 0.5, 1.4, 1.1))
            p.drawEllipse(QPointF(-4.5, 0), 1.2, 1.2)
            p.restore()


class MicNotificationPill(QWidget):
    """
    Temporary 'pseudo-capsule' banner popping up smoothly directly above the main pill/line
    with Dynamic Island slide-and-spring physics (zero emojis, animated vector icons).
    """
    sig_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.w, self.h = 372, 50  # two lines: title over detail
        self.setFixedSize(self.w, self.h)
        self.font_family = ui_font_family()
        self._target_pos = QPoint()

        lay = QHBoxLayout(self)
        lay.setContentsMargins(11, 0, 12, 0)
        lay.setSpacing(10)
        center = Qt.AlignmentFlag.AlignVCenter

        self.animated_icon = AnimatedVectorIcon(self)
        lay.addWidget(self.animated_icon, 0, center)

        lines = QVBoxLayout()
        lines.setContentsMargins(0, 0, 0, 0)
        lines.setSpacing(2)
        lines.addStretch(1)  # title and detail sit together as one block, centred on the icon
        self.title_label = QLabel(self)
        self.title_label.setFont(ui_font(self.font_family, 13, QFont.Weight.DemiBold, px=True))
        self.sub_label = QLabel(self)
        self.sub_label.setFont(ui_font(self.font_family, 12, QFont.Weight.Normal, px=True))
        for lbl in (self.title_label, self.sub_label):
            lbl.setTextFormat(Qt.TextFormat.PlainText)  # plain text: no rich-text rasterising
            lines.addWidget(lbl)
        lines.addStretch(1)
        lay.addLayout(lines, 1)

        # fixed-height pill, centred: a bare QLabel stretched to the row height and looked misaligned
        self.badge = QLabel("ACTIVE", self)
        self.badge.setFont(ui_font(self.font_family, 10, QFont.Weight.DemiBold, px=True))
        self.badge.setFixedHeight(22)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.badge, 0, center)

        self.dismiss_timer = QTimer(self)
        self.dismiss_timer.setSingleShot(True)
        self.dismiss_timer.timeout.connect(self.hide_animated)

        self.fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self.fade_anim.setDuration(220)
        self.fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.fade_anim.finished.connect(self._on_hidden)

        self.slide_anim = QVariantAnimation(self)
        self.slide_anim.setDuration(280)
        self.slide_anim.setEasingCurve(QEasingCurve.Type.OutCubic)  # critically damped: no bounce on a passive banner
        self.slide_anim.valueChanged.connect(self._apply_slide_pos)

    def _apply_slide_pos(self, pt):
        self.move(pt)

    def show_event(self, mic_name: str, status: str = "connected", anchor_rect: QRect = QRect()):
        mode, title, badge, tone = {
            "switched": ("switched", "Switched microphone", "ACTIVE", "green"),
            "connected": ("connected", "Microphone connected", "ACTIVE", "green"),
        }.get(status, ("unplugged", "Microphone unplugged", "OFFLINE", "red"))
        self.animated_icon.set_mode(mode)
        self._set_content(title, mic_name.removeprefix("● ").strip(), badge, _tone(tone))
        self._slide_in(anchor_rect)

    def _set_content(self, title: str, sub: str, badge: str, rgb: str):
        """Two plain-text lines, colours from the current theme (read per show), each elided to its own room."""
        self.title_label.setStyleSheet(f"color: {PILL_INK}; background: transparent;")
        self.sub_label.setStyleSheet(f"color: {PILL_SOFT}; background: transparent;")
        self.badge.setText(badge)
        self.badge.setStyleSheet(f"color: rgb({rgb}); background: rgba({rgb},0.16); border-radius: 11px; padding: 0 9px;")
        self.badge.adjustSize()
        room = self.w - 23 - self.animated_icon.width() - 10 - self.badge.sizeHint().width() - 10
        for lbl, text in ((self.title_label, title), (self.sub_label, sub)):
            lbl.setText(lbl.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, room))

    def _slide_in(self, anchor_rect: QRect):
        if not anchor_rect.isNull():
            target_x = anchor_rect.center().x() - self.w // 2
            target_y = anchor_rect.top() - self.h - 10
        else:
            screen = QApplication.primaryScreen()
            if screen:
                r = screen.availableGeometry()
                target_x = r.x() + (r.width() - self.w) // 2
                target_y = r.y() + r.height() - self.h - 90
            else:
                target_x, target_y = 100, 100

        self._target_pos = QPoint(target_x, target_y)
        start_pos = QPoint(target_x, target_y + 14)  # slides up from behind the capsule
        self.setGeometry(start_pos.x(), start_pos.y(), self.w, self.h)

        self.setWindowOpacity(0.0)
        self.show()
        _force_topmost(self)

        self.slide_anim.stop()
        self.slide_anim.setStartValue(start_pos)
        self.slide_anim.setEndValue(self._target_pos)
        self.slide_anim.start()

        self.fade_anim.stop()
        self.fade_anim.setStartValue(0.0)
        self.fade_anim.setEndValue(1.0)
        self.fade_anim.start()
        self.dismiss_timer.start(3400)

    def show_device(self, alert: dict, anchor_rect: QRect = QRect()):
        """A power or peripheral alert (engine/device_monitor.py): glyph, bold title, dim detail, badge."""
        rgb = _tone(alert.get("tone", "neutral"))
        self.animated_icon.set_mode(alert["kind"], rgb, alert.get("level", 0))
        self._set_content(alert["title"], alert["sub"], alert["badge"], rgb)
        self._slide_in(anchor_rect)

    def hide_animated(self):
        self.fade_anim.stop()
        self.fade_anim.setStartValue(self.windowOpacity())
        self.fade_anim.setEndValue(0.0)
        self.fade_anim.start()

    def _on_hidden(self):
        if self.windowOpacity() <= 0.05:
            self.hide()


    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        r = rect.height() / 2.0
        p.setBrush(QBrush(_bgc(248)))
        p.setPen(QPen(_fg(30), 1.0))
        p.drawRoundedRect(rect, r, r)

    def mousePressEvent(self, a0):
        self.sig_clicked.emit()
        self.hide_animated()


class MeetingBar(QWidget):
    """Bar above the capsule for meetings: offers to take notes, then shows live captions."""
    sig_start = pyqtSignal()
    sig_stop = pyqtSignal()
    sig_dismiss = pyqtSignal()

    W = 460

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(self.W)
        font = ui_font_family()
        self._app = ""
        self._started = 0.0
        self._lines: list = []

        frame = self._frame = QFrame(self)
        frame.setObjectName("meetingFrame")
        self.restyle()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(14, 10, 10, 10)
        lay.setSpacing(6)

        row = QHBoxLayout()
        self.title = QLabel(frame)
        self.title.setFont(ui_font(font, 9, QFont.Weight.DemiBold))
        row.addWidget(self.title, 1)
        self.btn = QPushButton(frame)
        self.btn.setFont(ui_font(font, 8))
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.clicked.connect(self._on_btn)
        row.addWidget(self.btn)
        self.close_btn = QPushButton("✕", frame)
        self.close_btn.setObjectName("ghost")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self._on_close)
        row.addWidget(self.close_btn)
        lay.addLayout(row)

        self.captions = QLabel(frame)
        self.captions.setFont(ui_font(font, 9))
        self.captions.setWordWrap(True)
        self.captions.setTextFormat(Qt.TextFormat.RichText)
        self.captions.hide()
        lay.addWidget(self.captions)

        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)

    @property
    def recording(self) -> bool:
        return self._started > 0

    def restyle(self):
        self._frame.setStyleSheet(
            f"#meetingFrame {{ background: {BG}; border: 1px solid {HAIRLINE};"
            " border-radius: 16px; }"
            f"QLabel {{ color: {INK}; background: transparent; }}"
            f"QPushButton {{ color: {BG}; background: {INK};"
            " border: none; border-radius: 11px; padding: 4px 12px; font-weight: 600; }"
            f"QPushButton#ghost {{ color: {FG_DIM}; background: transparent; padding: 4px 6px; }}"
        )

    def show_prompt(self, app: str, anchor_rect: QRect = QRect()):
        self._app, self._started, self._lines = app, 0.0, []
        self.title.setText(f"Meeting detected · {app}")
        self.btn.setText("Start notes")
        self.close_btn.show()
        self.captions.hide()
        self._place(anchor_rect)

    def show_recording(self, anchor_rect: QRect = QRect()):
        self._started = time.time()
        self.btn.setText("Stop")
        self.close_btn.hide()
        self.captions.setText("<span style='color:rgba(30,42,54,0.5)'>Listening…</span>")
        self.captions.show()
        self._tick()
        self._clock.start(1000)
        self._place(anchor_rect)

    def add_line(self, who: str, text: str):
        self._lines = (self._lines + [(who, text)])[-3:]
        esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        def _chip(w: str) -> str:
            w_low = w.lower()
            if "you" in w_low:
                col, bg = "#0369a1", "rgba(3,105,161,0.12)"
            elif "1" in w_low:
                col, bg = "#047857", "rgba(4,120,87,0.12)"
            elif "2" in w_low:
                col, bg = "#b45309", "rgba(180,83,9,0.12)"
            elif "3" in w_low:
                col, bg = "#7e22ce", "rgba(126,34,206,0.12)"
            else:
                col, bg = "#be123c", "rgba(190,18,60,0.12)"
            return f"<span style='color:{col}; background:{bg}; font-weight:700; padding:1px 6px; border-radius:4px;'>{esc(w)}</span>"

        self.captions.setText("<br>".join(
            f"{_chip(w)} {esc(t)}" for w, t in self._lines))
        self.adjustSize()

    def finish(self):
        self._started = 0.0
        self._clock.stop()
        self.hide()

    def _tick(self):
        secs = int(time.time() - self._started)
        self.title.setText(f"● Taking notes · {self._app} · {secs // 60:02d}:{secs % 60:02d}")

    def _on_btn(self):
        (self.sig_stop if self.recording else self.sig_start).emit()

    def _on_close(self):
        self.hide()
        self.sig_dismiss.emit()

    def _place(self, anchor_rect: QRect):
        self.adjustSize()
        screen = QApplication.primaryScreen()
        if not anchor_rect.isNull():
            x, bottom = anchor_rect.center().x() - self.W // 2, anchor_rect.top() - 10
        elif screen:
            r = screen.availableGeometry()
            x, bottom = r.x() + (r.width() - self.W) // 2, r.y() + r.height() - 90
        else:
            x, bottom = 100, 400
        self.move(x, bottom - self.height())
        self.show()
        _force_topmost(self)
        self.raise_()

    def resizeEvent(self, a0):
        # Grow upward as captions wrap, so the bar never covers the capsule below it
        if self.isVisible() and a0.oldSize().height() > 0:
            self.move(self.x(), self.y() - (a0.size().height() - a0.oldSize().height()))
        super().resizeEvent(a0)


class FloatingCaptionBubble(QWidget):
    """
    Floating translucent live speech preview bubble for Option A (Wispr Flow) and Option C (Micro).
    Hovers gracefully right above the capsule to show live transcription without stretching the capsule.
    Features 60 FPS continuous width easing and artistic per-word smoothstep alpha fade-in.
    """
    def __init__(self, parent_pill: QWidget):
        super().__init__()
        self.pill = parent_pill
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._text = ""
        self._shown = 0.0   # characters typed in so far
        self._ended = False  # faded out: the next text starts a new line
        self._font = ui_font(ui_font_family(), 10, QFont.Weight.DemiBold)
        self._font.setPointSizeF(10.5)
        self._fm = QFontMetricsF(self._font)
        self._cached_layout = None  # (key_n, width, lead, visible_body, x_lead)
        self.setWindowOpacity(0.0)

        self._cur_w = 116.0
        self._target_w = 116.0
        self._cur_h = 34.0
        self._target_h = 34.0
        self._scroll_y = 0.0
        self._target_scroll_y = 0.0

        self.fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self.fade_anim.setDuration(220)
        self.fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # 60 FPS timer: smooth eased text reveal + damped bubble width/height & teleprompter scroll
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)
        self._anim_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._anim_timer.timeout.connect(self._on_anim_tick)

    def _wrap_lines(self, text: str, max_w: float) -> list[str]:
        if not text:
            return []
        lines: list[str] = []
        cur = ""
        for word in text.split(" "):
            cand = f"{cur} {word}".strip() if cur else word
            if cur and self._fm.horizontalAdvance(cand) > max_w:
                lines.append(cur)
                cur = word
            else:
                cur = cand
        if cur:
            lines.append(cur)
        return lines

    def set_text(self, text: str, merge: bool = True):
        """Dictation passes the latest window of recognised speech, merged into one steadily growing line;
        a notice (merge=False) replaces the line and types in from the start."""
        text = text.strip()
        if not text:
            self.fade_out()
            return
        if self._ended or not merge:  # a new dictation or a notice never builds on faded-out text
            self._text, self._shown, self._ended = "", 0.0, False
            self._cached_layout = None
        merged = merge_preview(self._text, text) if merge else text
        # Cap stored tail on very long paragraphs so layout measurement stays fast
        if len(merged) > 900:
            trim = merged[-900:]
            if " " in trim:
                trim = trim.split(" ", 1)[1]
            if self._text and merged.startswith(self._text):
                self._shown = max(0.0, self._shown - (len(merged) - len(trim)))
            else:
                self._shown = min(self._shown, float(len(trim)))
            merged = trim
        if merged == self._text:
            return
        self._text = merged
        self._cached_layout = None
        single_w = self._fm.horizontalAdvance(merged) + 34.0
        if single_w <= 480.0 or not merge:
            self._target_w = float(max(116, min(480, int(single_w))))
            self._target_h = 34.0
            self._target_scroll_y = 0.0
        else:
            self._target_w = 480.0
            lines = self._wrap_lines(merged, 480.0 - 28.0)
            line_h = self._fm.lineSpacing() + 3.0
            vis_lines = min(3.6, float(max(2, len(lines))))
            self._target_h = min(116.0, max(54.0, 20.0 + vis_lines * line_h))
            inner_h = self._target_h - 18.0
            self._target_scroll_y = max(0.0, len(lines) * line_h - inner_h)
        if not self._anim_timer.isActive():
            self._anim_timer.start()
        if not self.isVisible() or self.windowOpacity() < 0.1:
            self.setFixedSize(round(self._cur_w), round(self._cur_h))
            self._reposition()
            self.show()
            _force_topmost(self)
            self.fade_anim.stop()
            self.fade_anim.setStartValue(self.windowOpacity())
            self.fade_anim.setEndValue(1.0)
            self.fade_anim.start()
        self.update()

    def _on_anim_tick(self):
        n = len(self._text)
        if self._shown < n:  # eased catch-up so long paragraphs stay crisp without falling behind
            self._shown = min(float(n), self._shown + max(1.1, (n - self._shown) * 0.22))
        dw = self._target_w - self._cur_w
        dh = self._target_h - self._cur_h
        ds = self._target_scroll_y - self._scroll_y
        self._cur_w = self._target_w if abs(dw) <= 0.6 else self._cur_w + dw * 0.24
        self._cur_h = self._target_h if abs(dh) <= 0.6 else self._cur_h + dh * 0.22
        self._scroll_y = self._target_scroll_y if abs(ds) <= 0.4 else self._scroll_y + ds * 0.20
        new_w, new_h = round(self._cur_w), round(self._cur_h)
        if new_w != self.width() or new_h != self.height():
            self.setFixedSize(new_w, new_h)
            self._reposition()
        self.update()
        if self._shown >= n and abs(dw) <= 0.6 and abs(dh) <= 0.6 and abs(ds) <= 0.4:
            self._anim_timer.stop()

    def _reposition(self):
        if not self.pill or not self.pill.isVisible():
            return
        px = self.pill.x() + (self.pill.width() - self.width()) // 2
        if getattr(self.pill, "caption_below", False):  # the island sits at the top edge: caption goes underneath
            py = self.pill.y() + self.pill.body_bottom() + 8
        else:
            py = self.pill.y() + self.pill.body_top() - self.height() - 8
        self.move(px, py)

    def _reset(self):
        self._text, self._shown, self._ended = "", 0.0, False
        self._cached_layout = None
        self._cur_w = self._target_w = 116.0
        self._cur_h = self._target_h = 34.0
        self._scroll_y = self._target_scroll_y = 0.0

    def fade_out(self):
        self._ended = True
        if not self.isVisible() or self.windowOpacity() <= 0.0:
            self._reset()
            return
        self.fade_anim.stop()
        self.fade_anim.setStartValue(self.windowOpacity())
        self.fade_anim.setEndValue(0.0)
        try:
            self.fade_anim.finished.disconnect(self._on_faded)
        except Exception:
            pass
        self.fade_anim.finished.connect(self._on_faded)
        self.fade_anim.start()

    def _on_faded(self):
        try:
            self.fade_anim.finished.disconnect(self._on_faded)
        except Exception:
            pass
        if self.windowOpacity() <= 0.05:
            self.hide()
            self._anim_timer.stop()
            self._reset()

    def paintEvent(self, a0):
        if not self._text:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, 11.0, 11.0)

        # Velvety deep OLED glass
        p.fillPath(path, _bgc(246))

        # Specular hairline gradient rim
        bubble_rim = QLinearGradient(0, rect.top(), 0, rect.bottom())
        bubble_rim.setColorAt(0.0, _fg(65))
        bubble_rim.setColorAt(1.0, _fg(20))
        p.strokePath(path, QPen(QBrush(bubble_rim), 1.0))

        p.setFont(self._font)
        fm = self._fm
        n = int(self._shown)
        w_cur = self.width()

        # Teleprompter multi-line mode for longer paragraphs
        if self.height() > 40:
            clip_r = rect.adjusted(10.0, 8.0, -10.0, -8.0)
            p.save()
            p.setClipRect(clip_r)
            typed_all = self._text[:n]
            lines = self._wrap_lines(typed_all, w_cur - 28.0)
            line_h = fm.lineSpacing() + 3.0
            mid_y = clip_r.center().y()
            frac = self._shown - n if n < len(self._text) else 1.0
            for idx, line_txt in enumerate(lines):
                y_top = clip_r.top() + idx * line_h - self._scroll_y
                y_base = y_top + fm.ascent()
                if y_top + line_h < clip_r.top() - 4 or y_top > clip_r.bottom() + 4:
                    continue
                # Central highlight: lines near center stay full-bright, upper lines dim softly
                dist = abs((y_top + line_h * 0.5) - mid_y) / max(1.0, clip_r.height() * 0.5)
                alpha = int(max(125.0, 252.0 - 110.0 * min(1.0, dist)))
                is_last = idx == len(lines) - 1
                if is_last and frac < 1.0 and line_txt:
                    ease_a = 1.0 - (1.0 - frac) ** 3
                    dy = 4.0 * (1.0 - ease_a)
                    head = line_txt[:-1]
                    p.setPen(_fg(alpha))
                    p.drawText(QPointF(14.0, y_base), head)
                    p.setPen(_fg(int(45 + (alpha - 45) * ease_a)))
                    p.drawText(QPointF(14.0 + fm.horizontalAdvance(head), y_base + dy), line_txt[-1])
                else:
                    p.setPen(_fg(alpha))
                    p.drawText(QPointF(14.0, y_base), line_txt)
            p.restore()
            # Top & bottom teleprompter edge fades
            if self._scroll_y > 1.0:
                top_g = QLinearGradient(0, rect.top(), 0, rect.top() + 20.0)
                top_g.setColorAt(0.0, _bgc(246))
                top_g.setColorAt(1.0, _bgc(0))
                p.fillRect(QRectF(rect.left() + 2, rect.top() + 1, rect.width() - 4, 20.0), QBrush(top_g))
            bot_g = QLinearGradient(0, rect.bottom() - 14.0, 0, rect.bottom())
            bot_g.setColorAt(0.0, _bgc(0))
            bot_g.setColorAt(1.0, _bgc(215))
            p.fillRect(QRectF(rect.left() + 2, rect.bottom() - 14.0, rect.width() - 4, 13.0), QBrush(bot_g))
            return

        cached = self._cached_layout
        if cached and cached[0] == n and cached[1] == w_cur:
            lead, typed = cached[2], cached[3]
        else:
            typed = self._text[:n]
            lead = ""
            avail = w_cur - 28.0
            while fm.horizontalAdvance(lead + typed) > avail and " " in typed:
                typed, lead = typed.split(" ", 1)[1], "… "
            self._cached_layout = (n, w_cur, lead, typed)
        base_y = (self.height() + fm.ascent() - fm.descent()) / 2.0
        x = 14.0
        if lead:
            p.setPen(_fg(130))
            p.drawText(QPointF(x, base_y), lead)
            x += fm.horizontalAdvance(lead)
        if typed:
            frac = self._shown - n if n < len(self._text) else 1.0
            body = typed[:-1] if frac < 1.0 else typed
            p.setPen(_fg(252))
            p.drawText(QPointF(x, base_y), body)
            if frac < 1.0:
                ease_a = 1.0 - (1.0 - frac) ** 3
                p.setPen(_fg(int(45 + 207 * ease_a)))
                p.drawText(QPointF(x + fm.horizontalAdvance(body), base_y), typed[-1])


class _Veil(QWidget):
    """Paints the capsule body colour over the content at pill._veil_a, so swapping content never pops."""

    def __init__(self, pill):
        super().__init__(pill)
        self.pill = pill
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, a0):
        if self.pill._drop_hover:
            self._paint_drop_hint()
            return
        a = self.pill._veil_a
        if a <= 0.01:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.pill._body_rect()
        path = QPainterPath()
        rad = min(r.height() / 2.0, 22.0 + 6.0 * self.pill._t)
        path.addRoundedRect(r, rad, rad)
        p.fillPath(path, _bgc(int(255 * a)))

    def _paint_drop_hint(self):
        """Dragging onto the capsule: dashed ink rim, a tray-down glyph and "Drop to hold"."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.pill._body_rect().adjusted(1, 1, -1, -1)
        rad = r.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(r, rad, rad)
        p.fillPath(path, _bgc(255))
        pen = QPen(_fg(), 1.5, Qt.PenStyle.DashLine, Qt.PenCapStyle.RoundCap)
        pen.setDashPattern([3.0, 2.5])
        p.strokePath(path, pen)
        f = ui_font(ui_font_family(), 9 if r.height() >= 30 else 7, QFont.Weight.DemiBold)
        label = "Drop to hold" if r.width() >= 120 else "Drop"
        tw = QFontMetricsF(f).horizontalAdvance(label)
        x0, cy = r.center().x() - (tw + 16) / 2.0, r.center().y()
        p.setPen(QPen(_fg(), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawLine(QPointF(x0 + 5, cy - 5), QPointF(x0 + 5, cy + 2))
        p.drawPolyline(QPolygonF([QPointF(x0 + 2, cy - 1), QPointF(x0 + 5, cy + 2), QPointF(x0 + 8, cy - 1)]))
        p.drawLine(QPointF(x0 + 1, cy + 5), QPointF(x0 + 9, cy + 5))
        p.setFont(f)
        p.drawText(QRectF(x0 + 16, r.top(), tw + 2, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)


class PeekShelf(QWidget):
    """Concept 2 Peep-In hover shelf above the idle capsule: mic, language, model + Expand pill.
    Pops in with an OutBack spring; mouse wheel over left/mid/right zones cycles Mic/Lang/Model, click opens HUD."""
    W, H = 368, 54

    def __init__(self, pill):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(self.W, self.H)
        self.setMouseTracking(True)
        self._pill = pill
        self._a = 0.0
        self._hover_i = -1
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(BUBBLE_EASE)
        self._anim.valueChanged.connect(self._on_tick)
        self._anim.finished.connect(self._on_done)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(260)
        self.hide_timer.timeout.connect(self.retract)

    def _combos(self):
        return (self._pill.mic_combo, self._pill.lang_combo, self._pill.model_combo)

    def _texts(self):
        p = self._pill
        dev_raw = p.hud_device.text() if hasattr(p, "hud_device") else ""
        dev = re.sub(r"<[^>]+>", " ", dev_raw).replace("●", "").strip()
        dev = re.sub(r"\s+", " ", dev)
        model = p.model_combo.currentText().split(" · ")[0]
        if dev and dev.lower() not in model.lower():
            model = f"{model} {dev.split(' · ')[0]}"
        return (p.current_mic_name, p._lang_short(p.current_language), model)

    def _tile(self, i: int) -> QRectF:
        w = (self.W - 96.0) / 3.0
        return QRectF(10.0 + i * w, 6.0, w, self.H - 12.0)

    def _expand_rect(self) -> QRectF:
        return QRectF(self.W - 82.0, 12.0, 70.0, 30.0)

    def _tile_at(self, pos) -> int:
        pt = QPointF(pos)
        if self._expand_rect().contains(pt):
            return 3
        return next((i for i in range(3) if self._tile(i).contains(pt)), -1)

    def peek(self):
        self.hide_timer.stop()
        if not self.isVisible():
            self._a = 0.0
            self.setWindowOpacity(0.0)
            self.place()
            self.show()
            _force_topmost(self)
        self._anim.setEasingCurve(BUBBLE_EASE)
        self._anim.setDuration(220)
        self._run(1.0)

    def retract(self):
        if self.isVisible():
            self._anim.setEasingCurve(SNAP_EASE)
            self._anim.setDuration(150)
            self._run(0.0)

    def hide_now(self):
        self.hide_timer.stop()
        self._anim.stop()
        self._a = 0.0
        self.hide()

    def _run(self, target: float):
        self._anim.stop()
        if not animations_enabled():
            self._on_tick(target)
            self._on_done()
            return
        self._anim.setStartValue(self._a)
        self._anim.setEndValue(float(target))
        self._anim.start()

    def _on_tick(self, v):
        self._a = float(v)
        self.setWindowOpacity(max(0.0, min(1.0, self._a)) * self._pill.opacity)
        self.place()
        self.update()

    def _on_done(self):
        if self._a <= 0.01:
            self.hide()

    def place(self):
        body = self._pill._body_rect()
        g = self._pill.mapToGlobal(QPoint(int(body.center().x()), int(body.top())))
        rise = 6.0 * (1.0 - self._a) if animations_enabled() else 0.0
        self.move(round(g.x() - self.W / 2), round(g.y() - self.H - 8 + rise))

    def enterEvent(self, event):
        self.hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, a0):
        self._hover_i = -1
        self.update()
        self.hide_timer.start()
        super().leaveEvent(a0)

    def mouseMoveEvent(self, a0):
        i = self._tile_at(a0.position())
        if i != self._hover_i:
            self._hover_i = i
            self.update()

    def mouseReleaseEvent(self, a0):
        if a0.button() == Qt.MouseButton.LeftButton:
            self.hide_now()
            self._pill.expand_to_hud()

    def wheelEvent(self, a0):
        i = self._tile_at(a0.position())
        if 0 <= i < 3:
            self._combos()[i].wheelEvent(a0)
            self.update()

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        s = 0.94 + 0.06 * self._a  # bouncy OutBack scale from the capsule
        p.translate(self.W / 2, self.H)
        p.scale(s, s)
        p.translate(-self.W / 2, -self.H)

        bg = QRectF(0.5, 0.5, self.W - 1.0, self.H - 1.0)
        p.setPen(QPen(_fg(42), 1.0))
        p.setBrush(_bgc(246))
        p.drawRoundedRect(bg, 18.0, 18.0)

        mic_s, lang_s, mod_s = self._texts()
        f_top = ui_font(self._pill.font_family, 9, QFont.Weight.DemiBold)
        f_sub = ui_font(self._pill.font_family, 8, QFont.Weight.Normal)
        fm_top = QFontMetricsF(f_top)
        summary = f"{mic_s}  ·  {lang_s}  ·  {mod_s}"
        p.setFont(f_top)
        p.setPen(_fg(242))
        p.drawText(QRectF(16, 8, self.W - 108, 20), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   fm_top.elidedText(summary, Qt.TextElideMode.ElideRight, self.W - 108))

        hints = ("Scroll mic to switch", "Scroll language to switch", "Scroll model to switch")
        sub = hints[self._hover_i] if 0 <= self._hover_i < 3 else "Scroll any label to switch · Click Expand"
        p.setFont(f_sub)
        p.setPen(QColor(SOFT))
        p.drawText(QRectF(16, 28, self.W - 108, 18), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, sub)

        exp = self._expand_rect()
        p.setPen(QPen(_fg(36 if self._hover_i == 3 else 18), 1.0))
        p.setBrush(_fg(40 if self._hover_i == 3 else 22))
        p.drawRoundedRect(exp, 10.0, 10.0)
        p.setFont(f_top)
        p.setPen(_fg(240))
        p.drawText(exp, Qt.AlignmentFlag.AlignCenter, "Expand")


class FloatingPill(QWidget):
    """
    Black-and-white capsule HUD.
    Capsule (360x44) morphs into a grouped settings panel (360x312), anchored at its
    bottom-centre. One progress value drives geometry, corner radius and a content crossfade.
    When idle, it gracefully minimizes into a sleek rounded line (64x6) at the bottom.
    """
    sig_mic_changed = pyqtSignal(object)
    sig_language_changed = pyqtSignal(str)
    sig_tone_changed = pyqtSignal(str)
    sig_model_changed = pyqtSignal(str)
    sig_open_dashboard = pyqtSignal()
    sig_update_requested = pyqtSignal()
    sig_hide_idle_changed = pyqtSignal(bool)
    sig_paste_last = pyqtSignal()
    sig_retry_last = pyqtSignal()
    sig_stop_dictation = pyqtSignal()

    LANGS = [
        ("or-auto", "Auto: Odia + English"), ("auto", "Auto Detect"), ("en-in", "English (India) / Hinglish"), ("or", "Odia / Odia-English"), ("or-mix", "Odia + English words"),
        ("or-en", "Odia → English"), ("hi", "Hindi"), ("en", "English (US/UK)"), ("translate", "Any language → English"),
        ("es", "Spanish"), ("fr", "French"), ("de", "German"), ("zh", "Chinese"), ("ja", "Japanese"),
        ("pt", "Portuguese"), ("it", "Italian"), ("ar", "Arabic"), ("ru", "Russian"),
        ("ko", "Korean"), ("nl", "Dutch"), ("tr", "Turkish"),
    ]
    TONES = [
        ("auto", "Auto"), ("smart_clean", "Smart Clean"), ("developer", "Developer"), ("email", "Email"),
        ("bullets", "Bullet Points"), ("verbatim", "Verbatim"),
    ]
    TONE_CHIPS = {"smart_clean": "Clean", "developer": "Code", "bullets": "Bullets", "verbatim": "Raw"}  # short chip labels

    def __init__(self, current_mic_name: str = "Microphone", current_language: str = "en", current_model: str = "base", hide_when_idle: bool = False, capsule_style: str = "compact", follow_mouse_screen: bool = True):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.font_family = ui_font_family()
        self._styles = {}  # widget -> stylesheet builder, re-run on a theme switch
        self._drag_pos = QPoint()
        self.is_expanded_hud = False
        self.current_state = "idle"
        self.current_mic_name = current_mic_name
        self.current_language = current_language
        self.current_model = current_model
        self.hide_when_idle = hide_when_idle
        self.capsule_style = capsule_style
        self.follow_mouse_screen = follow_mouse_screen
        self.current_screen = None
        # monitor unplugged / display sleep deletes the QScreen; drop the stale ref
        QGuiApplication.instance().screenRemoved.connect(
            lambda s: setattr(self, "current_screen", None) if s is self.current_screen else None)
        self._is_transitioning_screen = False
        self._recent_mics = []

        if capsule_style == "micro":
            self._cap_w, self._cap_h = 88, 24
            self._snooze_w, self._snooze_h = 40, 8
        elif capsule_style == "standard":
            self._cap_w, self._cap_h = 360, 44
            self._snooze_w, self._snooze_h = 76, 8
        else: # "compact" / Wispr Flow
            self._cap_w, self._cap_h = 148, 32
            self._snooze_w, self._snooze_h = 56, 10  # empty resting bar, Wispr-slim

        self._hud_w, self._hud_h = 360, 400
        self._line_w, self._line_h = 76, 8
        self._t = 0.0            # 0 = capsule, 1 = HUD
        self._line_t = 0.0       # 0 = capsule, 1 = minimal line
        self._snooze_t = 0.0     # 0 = active capsule, 1 = snoozed resting pill
        self._is_snoozed = False
        self._mode = "capsule"   # "capsule", "line", "hud"
        self.opacity = 1.0  # resting opacity; Settings > Capsule Opacity
        self._size = (1.0, 1.0, False)  # width factor, height factor, grow with the screen; Settings > Capsule
        self._is_line_hovered = False
        self._dl = -1            # model download percent, -1 when not downloading
        self._anchor = QPoint()  # bottom-centre of the window during a morph
        # During a morph the window keeps one size and only the painted body moves (float, sub-pixel):
        # resizing a translucent top-level window every frame is what made the motion stutter on Windows.
        self._staged = False
        self._shape = QRectF()
        self._stage_cx = 0.0
        self._veil_a = 0.0

        self.caption_bubble = FloatingCaptionBubble(self)
        self._peek = PeekShelf(self)
        self._peek_timer = QTimer(self)
        self._peek_timer.setSingleShot(True)
        self._peek_timer.setInterval(350)  # a passing cursor never pops the shelf
        self._peek_timer.timeout.connect(self._maybe_peek)
        self.tray_enabled = True
        self._drop_hover = False
        self._tray = None  # TrayPanel, made on first use (it imports this module)
        self._tray_timer = QTimer(self)
        self._tray_timer.setSingleShot(True)
        self._tray_timer.setInterval(450)  # hover intent: a cursor passing over the capsule never opens it
        self._tray_timer.timeout.connect(self._maybe_open_tray)
        self.setAcceptDrops(True)

        self.morph_anim = QVariantAnimation(self)
        self.morph_anim.setDuration(360)
        self.morph_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.morph_anim.valueChanged.connect(self._apply_t)
        self.morph_anim.finished.connect(self._on_morph_finished)

        self.line_anim = QVariantAnimation(self)
        self.line_anim.setDuration(380)
        self.line_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.line_anim.valueChanged.connect(self._apply_line_t)
        self.line_anim.finished.connect(self._on_line_anim_finished)

        self.snooze_anim = QVariantAnimation(self)
        self.snooze_anim.setDuration(280)
        self.snooze_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.snooze_anim.valueChanged.connect(self._apply_snooze_t)
        self.snooze_anim.finished.connect(self._on_snooze_anim_finished)

        self.fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self.fade_anim.setDuration(380)
        self.fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.fade_anim.finished.connect(self._on_fade_finished)
        self.fade_anim.valueChanged.connect(self._on_fade_tick)

        # Isolated screen transition animation to prevent cross-talk with state fades
        self.screen_fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self.screen_fade_anim.setDuration(200)
        self.screen_fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self._fade_out_and_hide)

        # Multi-screen mouse follow timer
        self._screen_track_timer = QTimer(self)
        self._screen_track_timer.setInterval(380)
        self._screen_track_timer.timeout.connect(self._check_screen_follow)
        self._screen_track_timer.start()

        # Level smoothing timer for HUD live input bar (60 FPS liquid spring decay)
        self._target_level = 0.0
        self._smoothed_level = 0.0
        self._level_timer = QTimer(self)
        self._level_timer.timeout.connect(self._tick_level)
        self._level_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._level_timer.start(16)

        # Periodic topmost elevation keeper (keeps capsule strictly above active windows)
        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self.force_topmost)
        self._topmost_timer.start(2000)

        # Companion pseudo-capsule for mic connect/disconnect notifications
        self.mic_pill = MicNotificationPill()
        self.mic_pill.sig_clicked.connect(self.expand_to_hud)

        self._build_capsule()
        self._build_hud()
        self._veil = _Veil(self)
        self.set_capsule_style(self.capsule_style)
        self._style(self, lambda: f"QLabel {{ background: transparent; border: none; color: {FG}; }}")
        self._apply_t(0.0)
        self.position_bottom_center()

    def force_topmost(self):
        """Force the capsule window to stay at the very top of the Windows z-order above all active apps."""
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QMenu) and w.isVisible():
                return
        if self.isVisible():
            _force_topmost(self)
            self.raise_()
            if hasattr(self, "caption_bubble") and self.caption_bubble.isVisible():
                _force_topmost(self.caption_bubble)
                self.caption_bubble.raise_()

    def showEvent(self, a0):
        super().showEvent(a0)
        self.force_topmost()

    def moveEvent(self, a0):
        super().moveEvent(a0)
        if hasattr(self, "caption_bubble"):
            self.caption_bubble._reposition()
        if hasattr(self, "_peek") and self._peek.isVisible():
            self._peek.place()
        if getattr(self, "_tray", None) is not None and self._tray.isVisible():
            self._tray.place()

    def hideEvent(self, a0):
        super().hideEvent(a0)
        self._peek.hide_now()
        if self._tray:
            self._tray.hide_now()

    # ---------- container (drop tray) ----------

    @property
    def tray(self):
        if self._tray is None:
            try:
                from .drop_tray import TrayPanel, TrayStore
            except ImportError:
                from drop_tray import TrayPanel, TrayStore
            self._tray = TrayPanel(self, TrayStore())
            self._tray.store.listeners.append(self._on_tray_changed)
            self._on_tray_changed()
        return self._tray

    tray_below = False

    def tray_anchor(self) -> QRect:
        """The visible body in screen coordinates; the tray opens above it."""
        b = self._body_rect()
        return QRect(self.mapToGlobal(b.topLeft().toPoint()), b.size().toSize())

    def set_tray_store(self, store):
        """Swap the container's store (tests use a temporary file)."""
        if self._tray:
            self._tray.hide_now()
            self._tray.deleteLater()
        try:
            from .drop_tray import TrayPanel
        except ImportError:
            from drop_tray import TrayPanel
        self._tray = TrayPanel(self, store)
        store.listeners.append(self._on_tray_changed)
        self._on_tray_changed()

    def set_tray_enabled(self, on):
        self.tray_enabled = bool(on)  # config values may be any truthy type
        if not on and self._tray:
            self._tray.hide_now()
        self._on_tray_changed()

    def _held(self) -> int:
        return len(self.tray.store.items) if self.tray_enabled else 0

    def _on_tray_changed(self):
        self.indicator.count = self._held()
        self.indicator.update()

    def _resting(self) -> bool:
        return (self.isVisible() and not self.is_expanded_hud and not self._staged and self._mode == "capsule"
                and self.current_state in ("idle", "pasted") and self._line_t < 0.05 and self._snooze_t < 0.05
                and not self._is_snoozed)

    def _maybe_open_tray(self):
        if self.underMouse() and self._resting() and self._held():
            self._peek.hide_now()
            self.tray.open()

    def _accepts(self, mime) -> bool:
        return (self.tray_enabled and not self.is_expanded_hud and self.current_state in ("idle", "pasted")
                and (mime.hasUrls() or mime.hasText() or mime.hasImage()))

    def dragEnterEvent(self, a0):
        if a0.source() is not None or not self._accepts(a0.mimeData()):  # our own tray rows are not re-dropped
            a0.ignore()
            return
        a0.acceptProposedAction()
        if self._mode == "line" or self._line_t > 0.05:
            self.expand_from_line()
        if self._is_snoozed or self._snooze_t > 0.05:
            self.expand_from_snooze()
        self._peek.hide_now()
        self._drop_hover = True
        self._veil.setGeometry(self.rect())
        self._veil.raise_()
        self._veil.update()

    def dragLeaveEvent(self, a0):
        self._drop_hover = False
        self._veil.update()

    def dropEvent(self, a0):
        self._drop_hover = False
        self._veil.update()
        added = self.tray.store.add_mime(a0.mimeData())
        a0.acceptProposedAction()
        n = len(self.tray.store.items)
        if added:
            self.show_message(f"Holding {n} item" + ("" if n == 1 else "s"), duration_ms=1500)
        self.tray.open()

    def show_device_event(self, alert: dict):
        """Power / peripheral alert above the capsule. The caller holds alerts back while dictating."""
        if hasattr(self, "mic_pill"):
            self.mic_pill.show_device(alert, anchor_rect=self.geometry())

    def _maybe_peek(self):
        """Glance shelf over a resting or snoozed capsule: never while dictating, morphing or with the sheet open."""
        if (self.underMouse() and self.isVisible() and not self.is_expanded_hud and not self._staged
                and self._mode == "capsule" and self.current_state in ("idle", "pasted")
                and self._line_t < 0.05):
            self._peek.peek()

    def _tick_level(self):
        if not self.is_expanded_hud or not hasattr(self, "mic_combo"):
            return
        tgt = self._target_level
        cur = self._smoothed_level
        if tgt > cur:
            self._smoothed_level = cur + (tgt - cur) * 0.60
        else:
            self._smoothed_level = cur + (tgt - cur) * 0.18
        if abs(self.mic_combo.level - self._smoothed_level) > 0.005:
            self.mic_combo.level = self._smoothed_level
            self.mic_combo.update()

    # ---------- construction ----------

    def _font(self, size: int, weight=QFont.Weight.Normal) -> QFont:
        return ui_font(self.font_family, size, weight)

    def _build_capsule(self):
        self.capsule_widget = QWidget(self)
        self.capsule_widget.setFixedSize(self._cap_w, self._cap_h)
        self.capsule_layout = QHBoxLayout(self.capsule_widget)
        self.capsule_layout.setContentsMargins(12, 0, 12, 0)
        self.capsule_layout.setSpacing(8)

        self.indicator = StateIndicator(self.capsule_widget)
        self.blobatar = BlobatarWidget(size=24, parent=self.capsule_widget)
        self.blobatar.hide()
        self._active_dialect = None
        self.text_label = AnimatedText(self.current_mic_name, self._font(10, QFont.Weight.Medium), self.capsule_widget)
        self.text_label.setFixedHeight(24)

        self.lang_badge = QLabel(self._lang_short(self.current_language), self.capsule_widget)
        self.lang_badge.setFont(self._font(8, QFont.Weight.DemiBold))
        self.lang_badge.setFixedHeight(20)
        self.lang_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._style(self.lang_badge,
                    lambda: f"QLabel {{ color: {FG_DIM}; background: {FILL}; border-radius: 10px; padding: 0px 5px; }}")

        self.visualizer = WaveformBars(self.capsule_widget, num_bars=7)
        # hidden while writing/pasted, but its slot stays: the spinner and badge don't jump sideways
        sp = self.visualizer.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self.visualizer.setSizePolicy(sp)
        self.btn_stop = StopButton(self.capsule_widget)
        self.btn_stop.clicked.connect(self.sig_stop_dictation.emit)
        self.btn_stop.hide()
        self.btn_more = MoreButton(self.capsule_widget)
        self.btn_more.clicked.connect(self.toggle_morph_hud)

    def is_odia_active(self) -> bool:
        return self.current_language in ("or", "odia", "or-en", "or-mix", "or-auto") or bool(getattr(self, "_active_dialect", None))

    def _update_indicator_mode(self):
        use_blob = self.is_odia_active()
        if use_blob and self.current_state in ("listening", "processing", "refining"):
            self.indicator.hide()
            self.blobatar.show()
        else:
            self.blobatar.hide()
            if self.capsule_style != "micro" or self.current_state != "idle":
                self.indicator.show()

    def set_detected_dialect(self, dialect: str):
        self._active_dialect = dialect
        if dialect:
            self._update_badge_text(dialect.upper())
        self._update_indicator_mode()

    def _update_badge_text(self, text: str):
        self.lang_badge.setText(text)
        fm = self.lang_badge.fontMetrics()
        tw = fm.horizontalAdvance(text)
        bh = getattr(self, "_badge_h", 20)
        bw = max(round(bh * 1.6), tw + 14 * bh // 20)
        self.lang_badge.setFixedSize(bw, bh)

    def set_capsule_style(self, style: str):
        if style not in ("standard", "compact", "micro"):
            style = "compact"
        self.capsule_style = style
        # back to unscaled parts; _fit_capsule rescales them when a size factor is set
        self._badge_h = 20
        self.indicator.setFixedSize(20, 20)
        self.btn_more.setFixedSize(26, 26)
        self.text_label._font = self._font(10, QFont.Weight.Medium)
        self.text_label.setMinimumWidth(0)
        self.text_label.setMaximumWidth(16777215)  # QWIDGETSIZE_MAX: compact / micro had fixed it at 0
        self.lang_badge.setFont(self._font(8, QFont.Weight.DemiBold))
        self._style(self.lang_badge,
                    lambda: f"QLabel {{ color: {FG_DIM}; background: {FILL}; border-radius: 10px; padding: 0px 5px; }}")

        # Clear existing layout items completely so no phantom hidden widgets remain
        while self.capsule_layout.count():
            item = self.capsule_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(self.capsule_widget)

        if style == "standard":
            self._cap_w, self._cap_h = 360, 44
            self._snooze_w, self._snooze_h = 76, 8
            self.capsule_layout.addWidget(self.indicator, 0, Qt.AlignmentFlag.AlignVCenter)
            self.capsule_layout.addWidget(self.blobatar, 0, Qt.AlignmentFlag.AlignVCenter)
            self.capsule_layout.addWidget(self.text_label, 1, Qt.AlignmentFlag.AlignVCenter)
            self.capsule_layout.addWidget(self.lang_badge, 0, Qt.AlignmentFlag.AlignVCenter)
            self.capsule_layout.addWidget(self.visualizer, 0, Qt.AlignmentFlag.AlignVCenter)
            self.capsule_layout.addWidget(self.btn_stop, 0, Qt.AlignmentFlag.AlignVCenter)
            self.capsule_layout.addWidget(self.btn_more, 0, Qt.AlignmentFlag.AlignVCenter)
            self.text_label.show()
            self._update_badge_text(self._lang_short(self.current_language))
            self.lang_badge.show()
            self.btn_more.show()
            self.indicator.show()
            self.visualizer.setFixedSize(44, 20)
            self.visualizer.set_bar_count(5)
            self.visualizer.show()
            self.capsule_widget.setFixedSize(360, 44)
            self.capsule_layout.setContentsMargins(14, 0, 10, 0)
            self.capsule_layout.setSpacing(10)
        elif style == "compact":
            self._cap_w, self._cap_h = 148, 32
            self._snooze_w, self._snooze_h = 56, 10  # empty resting bar, Wispr-slim
            self.capsule_layout.addWidget(self.indicator, 0, Qt.AlignmentFlag.AlignVCenter)
            self.capsule_layout.addWidget(self.blobatar, 0, Qt.AlignmentFlag.AlignVCenter)
            self.capsule_layout.addWidget(self.lang_badge, 0, Qt.AlignmentFlag.AlignVCenter)
            self.capsule_layout.addStretch(1)  # a wider capsule opens one gap here, not uneven gaps everywhere
            self.capsule_layout.addWidget(self.visualizer, 0, Qt.AlignmentFlag.AlignVCenter)
            self.capsule_layout.addWidget(self.btn_stop, 0, Qt.AlignmentFlag.AlignVCenter)
            self.text_label.hide()
            self.text_label.setFixedWidth(0)
            self.btn_more.hide()
            self.indicator.show()
            self._update_badge_text(self._lang_short(self.current_language))
            self.lang_badge.show()
            self.visualizer.setFixedSize(48, 22)
            self.visualizer.set_bar_count(7)
            self.visualizer.show()
            self.capsule_widget.setFixedSize(148, 32)
            self.capsule_layout.setContentsMargins(12, 0, 12, 0)
            self.capsule_layout.setSpacing(8)
        elif style == "micro":
            self._cap_w, self._cap_h = 88, 24
            self._snooze_w, self._snooze_h = 40, 8
            self.capsule_layout.addWidget(self.indicator, 0, Qt.AlignmentFlag.AlignVCenter)
            self.capsule_layout.addWidget(self.blobatar, 0, Qt.AlignmentFlag.AlignVCenter)
            self.capsule_layout.addStretch(1)
            self.capsule_layout.addWidget(self.visualizer, 0, Qt.AlignmentFlag.AlignVCenter)
            self.text_label.hide()
            self.text_label.setFixedWidth(0)
            self.lang_badge.hide()
            self.btn_more.hide()
            self.indicator.show()
            self.visualizer.setFixedSize(40, 16)
            self.visualizer.set_bar_count(5)
            self.visualizer.show()
            self.capsule_widget.setFixedSize(88, 24)
            self.capsule_layout.setContentsMargins(8, 0, 8, 0)
            self.capsule_layout.setSpacing(6)

        self.capsule_layout.activate()
        need = self.capsule_layout.totalMinimumSize().width()
        if need > self._cap_w:
            self._cap_w = need
            self.capsule_widget.setFixedSize(self._cap_w, self._cap_h)

        auto = self._size[2]
        k = screen_scale(self.current_screen or self.screen() or QApplication.primaryScreen()) if auto else 1.0
        wf, hf = self._size[0] * k, self._size[1] * k
        if (wf, hf) != (1.0, 1.0):
            self._fit_capsule(wf, hf)

        if not self.is_expanded_hud:
            self._snooze_t = 0.0
            self._is_snoozed = False
            self._apply_t(0.0)
            self.position_bottom_center()

    def _fit_capsule(self, wf: float, hf: float):
        """Scale every part by one factor, then drop parts (label, badge, bars) until the rest fits.
        Content is never squeezed below its own size: it is removed instead."""
        s = max(0.6, min(wf, hf, 1.6))
        w, h = round(self._cap_w * wf), max(18, round(self._cap_h * hf))
        lay = self.capsule_layout
        m = lay.contentsMargins()
        lay.setContentsMargins(round(m.left() * s), 0, round(m.right() * s), 0)
        lay.setSpacing(max(4, round(lay.spacing() * s)))
        ind = min(round(20 * s), h - 4)
        self.indicator.setFixedSize(ind, ind)
        bh = min(round(20 * s), h - 4)
        self.lang_badge.setFont(self._font(max(6, round(8 * s)), QFont.Weight.DemiBold))
        rad, pad = bh // 2, max(3, round(5 * s))
        self._style(self.lang_badge,
                    lambda: f"QLabel {{ color: {FG_DIM}; background: {FILL}; border-radius: {rad}px; padding: 0px {pad}px; }}")
        self._badge_h = bh
        self._update_badge_text(self.lang_badge.text())
        self.visualizer.setFixedSize(round(self.visualizer.width() * s), min(round(self.visualizer.height() * s), h - 6))
        self.text_label._font = self._font(max(7, round(10 * s)), QFont.Weight.Medium)
        if self.text_label.isVisibleTo(self.capsule_widget):
            self.text_label.setMinimumWidth(round(64 * s))  # below this a label is noise: drop it
        self.btn_more.setFixedSize(min(round(26 * s), h - 4), min(round(26 * s), h - 4))
        # degrade order: label, badge, then fewer bars
        drops = [lambda: self.text_label.setVisible(False),
                 lambda: self.lang_badge.setVisible(False),
                 lambda: self._bars(5),
                 lambda: self._bars(3)]
        while lay.totalMinimumSize().width() > w and drops:
            drops.pop(0)()
            lay.invalidate()
        need = lay.totalMinimumSize()
        self._cap_w, self._cap_h = max(w, need.width()), max(h, need.height())
        self._snooze_w, self._snooze_h = round(self._snooze_w * wf), round(self._snooze_h * hf)
        self.capsule_widget.setFixedSize(self._cap_w, self._cap_h)

    def _bars(self, n: int):
        v = self.visualizer
        if v.num_bars > n:
            v.setFixedWidth(round(v.width() * n / v.num_bars))
            v.set_bar_count(n)

    def set_capsule_size(self, wf: float, hf: float, auto: bool):
        """Width and height as fractions of the style's default, plus 'grow with the screen'."""
        self._size = (wf, hf, auto)
        if hasattr(self, "capsule_layout"):
            self.set_capsule_style(self.capsule_style)

    def _row(self, title: str, widget: QWidget, trailing: bool = False) -> QWidget:
        """iOS settings row; trailing=True pins a compact control (switch) to the right edge."""
        row = QWidget(self)
        row.setFixedHeight(44)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(10)
        lbl = QLabel(title, row)
        lbl.setFont(self._font(10))
        if trailing:
            lay.addWidget(lbl, 1, Qt.AlignmentFlag.AlignVCenter)
            lay.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        else:
            lbl.setFixedWidth(98)
            lay.addWidget(lbl, 0, Qt.AlignmentFlag.AlignVCenter)
            lay.addWidget(widget, 1, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _style(self, w: QWidget, css):
        """Apply css() now and again whenever the theme switches."""
        self._styles[w] = css
        w.setStyleSheet(css())

    def set_theme(self, name: str):
        """Switch light/dark live: swap the tokens, re-apply baked stylesheets, repaint everything."""
        use_theme(name)
        for w, css in self._styles.items():
            w.setStyleSheet(css())
        for w in QApplication.topLevelWidgets():
            if isinstance(w, MeetingBar):
                w.restyle()
        for w in QApplication.allWidgets():
            w.update()
        if self._tray is not None and self._tray.isVisible():
            self._tray.refresh()

    def _tile(self, w: QWidget) -> QWidget:
        """Paint a plain container as a bento tile (rounded fill, no border)."""
        w.setObjectName("Tile")
        w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._style(w, lambda: f"QWidget#Tile {{ background: {FILL}; border-radius: 12px; }}")
        return w

    def _build_hud(self):
        """Bento sheet: picker tiles, style chips and quick actions on a 6-column grid.
        Its height follows the visible tiles (see _fit_hud)."""
        self.hud_widget = QWidget(self)
        self.hud_widget.setObjectName("HUD")
        self.hud_widget.setFixedWidth(self._hud_w)
        self.hud_widget.setStyleSheet("QWidget#HUD { background: transparent; border: none; }")
        lay = QVBoxLayout(self.hud_widget)
        lay.setContentsMargins(16, 18, 16, 16)
        lay.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(10)
        logo_path = get_asset_path("app_icon_square.png")
        if not os.path.exists(logo_path):
            logo_path = get_asset_path("logo.jpg")
        self.hud_logo = QLabel(self)
        self.hud_logo.setFixedSize(26, 26)
        self.hud_logo.setPixmap(get_squircle_pixmap(logo_path, size=26, radius=6.5))
        header.addWidget(self.hud_logo, 0, Qt.AlignmentFlag.AlignVCenter)
        title = QLabel("Vaani", self)
        title.setFont(self._font(12, QFont.Weight.DemiBold))
        header.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)
        self.hud_version = QLabel(self)  # "v1.5.0 · up to date": set from the updater's result
        self.hud_version.setFont(self._font(8))
        self._style(self.hud_version, lambda: f"color: {FG_DIM};")
        header.addWidget(self.hud_version, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addStretch()
        self.hud_device = QLabel(self)
        self.hud_device.setFont(self._font(8, QFont.Weight.DemiBold))
        self._style(self.hud_device, lambda: f"color: {FG_DIM};")
        header.addWidget(self.hud_device, 0, Qt.AlignmentFlag.AlignVCenter)
        self.btn_minimize = QPushButton("Done", self)
        self.btn_minimize.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_minimize.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_minimize.setFont(self._font(10, QFont.Weight.DemiBold))
        self._style(self.btn_minimize, lambda: f"""
            QPushButton {{ background: transparent; color: {INK}; border: none; padding: 4px 6px; border-radius: 6px; }}
            QPushButton:hover {{ background: {TINT}; }}
            QPushButton:pressed {{ background: {TINT_STRONG}; }}
        """)
        self.btn_minimize.clicked.connect(self.collapse_to_capsule)
        header.addWidget(self.btn_minimize, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(header)
        lay.addSpacing(4)

        self.mic_combo = PickerTile("Microphone", self.font_family, self)
        self.mic_combo.level = 0.0
        self.mic_combo.wheel_cycles = False  # a stray scroll must not switch to, say, Bluetooth earbuds
        self.populate_mics()
        self.mic_combo.currentIndexChanged.connect(self._on_mic_selected)

        self.lang_combo = PickerTile("Language", self.font_family, self)
        self.populate_languages()
        self.lang_combo.currentIndexChanged.connect(self._on_language_selected)

        self.tone_combo = ChipTile("Style", self.font_family, self)
        for key, label in self.TONES:
            self.tone_combo.addItem(self.TONE_CHIPS.get(key, label), key)
        self.tone_combo.currentIndexChanged.connect(self._on_tone_selected)

        self.model_combo = PickerTile("Model", self.font_family, self)
        self.model_combo.short_value = True
        self.populate_models()
        self.model_combo.currentIndexChanged.connect(self._on_model_selected)

        self.tile_paste = ActionTile("Paste last", "Insert again", self.font_family, self)
        self.tile_paste.clicked.connect(self._on_paste_clicked)
        self.tile_retry = ActionTile("Retry", "Re-transcribe", self.font_family, self)
        self.tile_retry.clicked.connect(self._on_retry_clicked)

        grid = QGridLayout()
        grid.setSpacing(8)
        for c in range(6):
            grid.setColumnStretch(c, 1)
        grid.addWidget(self.mic_combo, 0, 0, 1, 3)
        grid.addWidget(self.lang_combo, 0, 3, 1, 3)
        grid.addWidget(self.tone_combo, 1, 0, 1, 6)
        grid.addWidget(self.model_combo, 2, 0, 1, 2)
        grid.addWidget(self.tile_paste, 2, 2, 1, 2)
        grid.addWidget(self.tile_retry, 2, 4, 1, 2)
        lay.addLayout(grid)

        # live model download banner, shown only while a download runs
        self.hud_download_row = self._tile(QWidget(self))
        self.hud_download_row.setFixedHeight(52)
        dl_lay = QHBoxLayout(self.hud_download_row)
        dl_lay.setContentsMargins(12, 0, 12, 0)
        dl_lay.setSpacing(10)
        self.hud_download_title = QLabel("Download", self.hud_download_row)
        self.hud_download_title.setFont(self._font(10))
        self.hud_download_title.setFixedWidth(72)
        dl_lay.addWidget(self.hud_download_title)
        dl_bar_container = QWidget(self.hud_download_row)
        dl_bar_lay = QVBoxLayout(dl_bar_container)
        dl_bar_lay.setContentsMargins(0, 0, 0, 0)
        dl_bar_lay.setSpacing(3)
        self.hud_download_bar = QProgressBar(dl_bar_container)
        self.hud_download_bar.setRange(0, 100)
        self.hud_download_bar.setValue(0)
        self.hud_download_bar.setTextVisible(False)
        self.hud_download_bar.setFixedHeight(5)
        self._style(self.hud_download_bar, lambda: f"""
            QProgressBar {{ background: {HAIRLINE}; border: none; border-radius: 2px; }}
            QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}
        """)
        self.hud_download_text = QLabel("Preparing...", dl_bar_container)
        self.hud_download_text.setFont(self._font(8))
        self._style(self.hud_download_text, lambda: f"color: {FG_DIM};")
        dl_bar_lay.addWidget(self.hud_download_bar)
        dl_bar_lay.addWidget(self.hud_download_text)
        dl_lay.addWidget(dl_bar_container, 1, Qt.AlignmentFlag.AlignVCenter)
        self.hud_download_row.hide()
        lay.addWidget(self.hud_download_row)

        self.btn_update = QPushButton("Update Available · Install", self)
        self.btn_update.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_update.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_update.setFixedHeight(40)
        self.btn_update.setFont(self._font(10, QFont.Weight.DemiBold))
        self._style(self.btn_update, lambda: f"""
            QPushButton {{ background: {INK}; color: {BG}; border: none; border-radius: 12px; }}
            QPushButton:hover {{ background: {INK_HOVER}; }}
            QPushButton:pressed {{ background: {INK_PRESS}; }}
            QPushButton:disabled {{ background: {FG_FAINT}; color: {BG}; }}
        """)
        self.btn_update.hide()
        self.btn_update.clicked.connect(self._on_update_clicked)
        lay.addWidget(self.btn_update)

        self.cb_hide_idle_hud = Switch("", self)
        self.cb_hide_idle_hud.setChecked(self.hide_when_idle)
        self.cb_hide_idle_hud.toggled.connect(self._on_hud_hide_idle_toggled)
        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addWidget(self._tile(self._row("Hide when idle", self.cb_hide_idle_hud, trailing=True)), 1)
        self.btn_transfer = QPushButton("Dashboard  →", self)
        self.btn_transfer.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_transfer.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_transfer.setFixedHeight(44)
        self.btn_transfer.setFont(self._font(10, QFont.Weight.DemiBold))
        self._style(self.btn_transfer, lambda: f"""
            QPushButton {{ background: {SURFACE}; color: {INK}; border: none; border-radius: 12px; }}
            QPushButton:hover {{ background: {SURFACE_HI}; }}
            QPushButton:pressed {{ background: {SURFACE_PRESS}; }}
        """)
        self.btn_transfer.clicked.connect(self._on_transfer_clicked)
        footer.addWidget(self.btn_transfer, 1)
        lay.addLayout(footer)
        self._fit_hud()

    def _fit_hud(self):
        """Size the sheet to its visible tiles; the download and update banners come and go."""
        if self._staged:  # mid-morph the stage was sized for the old height; refit once it lands
            QTimer.singleShot(420, self._fit_hud)
            return
        lay = self.hud_widget.layout()
        lay.activate()
        h = lay.sizeHint().height()
        if h != self._hud_h:
            self._hud_h = h
            self.hud_widget.setFixedSize(self._hud_w, h)
            if self.is_expanded_hud:
                self._apply_t(self._t)

    def set_version_status(self, text: str):
        """The installed version and the last update check, shown in the HUD header."""
        self.hud_version.setText(text)

    def _refresh_device_badge(self):
        if hasattr(self, "hud_device"):
            dev = model_manager.active_device.upper()
            dot = GREEN if dev in ("CUDA", "NPU") else FG_DIM
            self.hud_device.setText(f"<span style='color:{dot}'>●</span> {dev}")

    # ---------- staging: one window size per morph, the body animates inside it ----------

    def _stage(self, x: int, y: int, w: int, h: int, cx: float):
        self._staged = True
        self._stage_cx = cx
        self.setGeometry(x, y, w, h)
        self._veil.setGeometry(0, 0, w, h)
        self._veil.raise_()

    def _unstage(self):
        if not self._staged:
            return
        self._staged = False
        self._shape = QRectF()
        self._veil_a = 0.0
        self.hud_widget.clearMask()
        self.capsule_widget.clearMask()
        self._veil.update()

    def _place_body(self, w: float, h: float) -> QRectF:
        """Body rect inside the staged window: bottom-anchored, centred on the stage anchor, kept inside."""
        x = min(max(self._stage_cx - w / 2.0, 0.0), self.width() - w)
        self._shape = QRectF(x, self.height() - h, w, h)
        return self._shape

    def _body_rect(self) -> QRectF:
        r = self._shape if self._staged else QRectF(self.rect())
        return r.adjusted(0.5, 0.5, -0.5, -0.5)

    def body_top(self) -> int:
        """Top of the visible body in window coordinates (the caption bubble sits above it)."""
        return int(self._shape.top()) if self._staged else 0

    def _clip_child(self, w: QWidget):
        """Clip a child to the rounded body so wider content never shows square corners mid-morph.
        The mask is 1-bit, so the body itself is painted (anti-aliased) by paintEvent, never by a masked child."""
        r = self._shape
        rad = min(r.height() / 2.0, 22.0 + 6.0 * self._t)
        path = QPainterPath()
        path.addRoundedRect(r.translated(-w.x(), -w.y()), rad, rad)
        w.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def _set_veil(self, a: float):
        a = max(0.0, min(1.0, a))
        if abs(a - self._veil_a) > 0.004:
            self._veil_a = a
            self._veil.raise_()
            self._veil.update()

    def _bottom_stage(self, w: int, h: int):
        """Stage a w x h window bottom-centred 35 px above the taskbar of the capsule's screen."""
        screen = self.current_screen or self.screen() or QApplication.primaryScreen()
        r = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        self._stage(r.x() + (r.width() - w) // 2, r.y() + r.height() - h - 35, w, h, w / 2.0)

    # ---------- morph ----------

    def _apply_t(self, t):
        t = float(t)
        self._t = t
        if self._staged:
            body = self._place_body(max(12.0, self._cap_w + (self._hud_w - self._cap_w) * t),
                                    max(12.0, self._cap_h + (self._hud_h - self._cap_h) * t))
            self.capsule_widget.move(round(body.center().x() - self._cap_w / 2.0),
                                     round(body.center().y() - self._cap_h / 2.0))
            self.hud_widget.move(round(body.center().x() - self._hud_w / 2.0), round(body.top()))
            swap = t > 0.35
            self.hud_widget.setVisible(swap)
            self.capsule_widget.setVisible(not swap)
            self._clip_child(self.hud_widget if swap else self.capsule_widget)
            self._set_veil(1.0 - abs(t - 0.35) / 0.22)  # fade through the body colour around the swap
            self.update()
            return
        w = round(self._cap_w + (self._hud_w - self._cap_w) * t)
        h = round(self._cap_h + (self._hud_h - self._cap_h) * t)

        screen = self._home_screen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)

        if not self._anchor.isNull():
            new_x = self._anchor.x() - w // 2
            new_y = self._anchor.y() - h
            # Keep safely inside screen bounds so HUD never clips off-screen or into taskbar
            new_x = max(avail.left() + 10, min(new_x, avail.right() - w - 10))
            if new_y < avail.top() + 10:
                new_y = avail.top() + 10
            if new_y + h > avail.bottom() - 6:
                new_y = avail.bottom() - h - 6
            self.setGeometry(new_x, new_y, w, h)
        elif self.size() != self.capsule_widget.size():
            self.resize(w, h)

        # Content positioning
        self.capsule_widget.move((w - self._cap_w) // 2, (h - self._cap_h) // 2)
        self.hud_widget.move((w - self._hud_w) // 2, 0)

        # Crisp clean transition without QGraphicsOpacityEffect ghosting
        if t <= 0.35:
            self.hud_widget.setVisible(False)
            self.capsule_widget.setVisible(True)
        else:
            self.hud_widget.setVisible(True)
            self.capsule_widget.setVisible(False)

        self.update()

    def _set_content_alpha(self, w: QWidget, a: float):
        vis = a > 0.01
        if w.isVisible() != vis:
            w.setVisible(vis)

    def _home_screen(self):
        """The screen the capsule is on. Taking the cursor's screen (or a stale saved one) put the HUD
        against the wrong screen's edges: it opened low, over the taskbar, on multi-monitor setups."""
        point = self._anchor if not self._anchor.isNull() else self.geometry().center()
        return (QGuiApplication.screenAt(point) or self.current_screen or QGuiApplication.screenAt(QCursor.pos())
                or QApplication.primaryScreen())

    def _morph_to(self, target: float):
        geo = self.geometry()
        screen = self._home_screen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        bot_y = min(avail.bottom() - 15, max(avail.top() + 100, geo.bottom() + 1))
        self._anchor = QPoint(geo.center().x(), bot_y)
        sw, sh = max(self._cap_w, self._hud_w) + 28, max(self._cap_h, self._hud_h) + 28
        sx = max(avail.left() + 10, min(self._anchor.x() - sw // 2, avail.right() - sw - 10))
        sy = min(max(bot_y - sh, avail.top() + 10), avail.bottom() - sh - 6)
        self._stage(sx, sy, sw, sh, float(self._anchor.x() - sx))
        self.morph_anim.stop()
        self.morph_anim.setDuration(380 if target >= 0.5 else 280)
        self.morph_anim.setEasingCurve(BUBBLE_EASE if target >= 0.5 else SNAP_EASE)
        self.morph_anim.setStartValue(self._t)
        self.morph_anim.setEndValue(target)
        self.morph_anim.start()

    def toggle_morph_hud(self):
        self.collapse_to_capsule() if self.is_expanded_hud else self.expand_to_hud()

    def expand_to_hud(self):
        self._peek.hide_now()
        if self._tray is not None:
            self._tray.hide_now()
        self._refresh_device_badge()
        self.is_expanded_hud = True
        self.hide_timer.stop()
        self.fade_anim.stop()
        self.line_anim.stop()
        self._line_t = 0.0
        if hasattr(self, "caption_bubble"):
            self.caption_bubble.fade_out()
        self.setWindowOpacity(self.opacity)
        self.show()
        _force_topmost(self)
        self._morph_to(1.0)

    def collapse_to_capsule(self):
        self.is_expanded_hud = False
        self._morph_to(0.0)

    def _on_morph_finished(self):
        self._unstage()
        self._apply_t(self._t)
        if self._t <= 0.05:
            self.is_expanded_hud = False
            self._anchor = QPoint()
            self.hud_widget.setVisible(False)
            self.capsule_widget.setVisible(True)
            self.position_bottom_center()
            if self.hide_when_idle and self.current_state in ("idle", "pasted"):
                self.hide_timer.stop()
                self.hide_timer.start(1200)
        elif self._t >= 0.95:
            self.is_expanded_hud = True
            self.capsule_widget.setVisible(False)
            self.hud_widget.setVisible(True)
        self.update()

    # ---------- line morph ----------

    def _on_hud_hide_idle_toggled(self, checked: bool):
        self.set_hide_when_idle(checked)
        self.sig_hide_idle_changed.emit(self.hide_when_idle)

    def set_hide_when_idle(self, hide: bool):
        self.hide_when_idle = hide
        if hasattr(self, "cb_hide_idle_hud"):
            self.cb_hide_idle_hud.blockSignals(True)
            self.cb_hide_idle_hud.setChecked(self.hide_when_idle)
            self.cb_hide_idle_hud.blockSignals(False)
        if self.hide_when_idle:
            if not self.is_expanded_hud and self.current_state in ("idle", "pasted") and (self._line_t >= 0.7 or self._mode == "line"):
                self._fade_out_and_hide()
        else:
            if not self.is_expanded_hud and not self.isVisible() and self.current_state == "idle":
                self.minimize_to_line()
                self.setWindowOpacity(0.0)
                self.show()
                _force_topmost(self)
                self.fade_anim.stop()
                self.fade_anim.setDuration(340)
                self.fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
                self.fade_anim.setStartValue(0.0)
                self.fade_anim.setEndValue(self.opacity)
                self.fade_anim.start()

    def _apply_line_t(self, val):
        if self.is_expanded_hud or self.capsule_style in ("compact", "micro"):
            return
        self._line_t = float(val)
        t = self._line_t
        target_w = 76 if (self._is_line_hovered and t > 0.85) else self._line_w
        target_h = 8 if (self._is_line_hovered and t > 0.85) else self._line_h
        if self._staged:
            self._place_body(self._cap_w - (self._cap_w - target_w) * t, self._cap_h - (self._cap_h - target_h) * t)
            # content is gone by t=0.4, before the body starts brightening into the white line
            cap_a = max(0.0, min(1.0, 1.0 - t / 0.4))
            cap_a = cap_a * cap_a * (3.0 - 2.0 * cap_a)  # smoothstep
            self._set_content_alpha(self.capsule_widget, cap_a)
            self.capsule_widget.move(0, 0)
            self._clip_child(self.capsule_widget)
            self._set_veil(1.0 - cap_a if cap_a > 0.01 else 0.0)
            self.update()
            return
        w = round(self._cap_w - (self._cap_w - target_w) * t)
        h = round(self._cap_h - (self._cap_h - target_h) * t)

        screen = self.current_screen or self.screen() or QApplication.primaryScreen()
        if screen:
            r = screen.availableGeometry()
            x = r.x() + (r.width() - w) // 2
            y = r.y() + r.height() - h - 35
            self.setGeometry(x, y, w, h)

        # Smooth organic content fade: begins softly at t=0.75 and reaches full opacity at t=0.0
        cap_a = max(0.0, min(1.0, (1.0 - t) / 0.75))
        cap_a = cap_a * cap_a * (3.0 - 2.0 * cap_a)  # smoothstep
        self._set_content_alpha(self.capsule_widget, cap_a)
        self.update()

    def minimize_to_line(self):
        if self.is_expanded_hud or self.capsule_style in ("compact", "micro"):
            return
        if self.hide_when_idle:
            self._fade_out_and_hide()
            return
        self._mode = "line"
        self.current_state = "idle"
        self.indicator.set_state("idle")
        self.visualizer.setVisible(False)
        self._bottom_stage(self._cap_w, self._cap_h)
        self.line_anim.stop()
        self.line_anim.setDuration(400)
        self.line_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.line_anim.setStartValue(self._line_t)
        self.line_anim.setEndValue(1.0)
        self.line_anim.start()

    def expand_from_line(self):
        self._mode = "capsule"
        self._bottom_stage(self._cap_w, self._cap_h)
        self.line_anim.stop()
        self.line_anim.setDuration(360)
        self.line_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.line_anim.setStartValue(self._line_t)
        self.line_anim.setEndValue(0.0)
        self.line_anim.start()

    def _on_line_anim_finished(self):
        self._unstage()
        self._apply_line_t(self._line_t)
        if self._line_t <= 0.05:
            self._mode = "capsule"
        elif self._line_t >= 0.95:
            self._mode = "line"
            if self.hide_when_idle:
                self.hide()
        self.update()

    def show_mic_event(self, mic_name: str, status: str = "connected"):
        """Spawn the pseudo-capsule notification above the main pill/line."""
        if hasattr(self, "mic_pill"):
            self.mic_pill.show_event(mic_name, status, anchor_rect=self.geometry())
        self.populate_mics()

    # ---------- data ----------

    def _lang_short(self, code: str) -> str:
        if code == "auto":
            return "AUTO"
        if code == "en-in":
            return "EN-IN"
        if code in ("or", "odia"):
            return "ODIA"
        if code == "or-en":
            return "OR→EN"
        if code == "or-mix":
            return "OR+EN"
        if code == "or-auto":
            return "OR/EN"
        if code == "translate":
            return "→EN"
        return code.upper()

    def populate_mics(self, recent_mics=None):
        if recent_mics is not None:
            self._recent_mics = list(recent_mics)
        self.mic_combo.blockSignals(True)
        curr_data = self.mic_combo.currentData()
        self.mic_combo.clear()
        self.mic_combo.addItem("System Default", None)
        for dev in list_microphones():
            is_active = (dev["name"] == self.current_mic_name)
            is_recent = (dev["name"] in self._recent_mics and not is_active)
            if is_active:
                name_display = f"● {dev['name']} (Active)"
            elif is_recent:
                name_display = f"↺ {dev['name']} (Recent)"
            else:
                name_display = dev["name"]
            self.mic_combo.addItem(name_display, dev["index"])
            if is_active or (curr_data is not None and curr_data == dev["index"]):
                self.mic_combo.setCurrentIndex(self.mic_combo.count() - 1)
        self.mic_combo.blockSignals(False)

    def populate_languages(self):
        self.lang_combo.blockSignals(True)
        self.lang_combo.clear()
        for code, label in self.LANGS:
            self.lang_combo.addItem(label, code)
            if code == self.current_language:
                self.lang_combo.setCurrentIndex(self.lang_combo.count() - 1)
        self.lang_combo.blockSignals(False)

    def populate_models(self, active_model=None):
        if active_model is not None:
            self.current_model = active_model
        if not hasattr(self, "model_combo"):
            return
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        models = model_manager.CAPSULE_SPEECH_MODELS
        clean_active = self.current_model.replace(".en", "").lower()
        selected_idx = 0
        for idx, (m_id, label, size) in enumerate(models):
            active = m_id == clean_active or clean_active.startswith(m_id)
            downloaded = model_manager.is_ready(m_id)
            # picking a model never downloads it; downloads happen in the dashboard's Model Library
            self.model_combo.addItem(label if downloaded or active else f"{label}   \u2193 {size}", m_id)
            self.model_combo.setItemEnabled(idx, downloaded or active)
            if active:
                selected_idx = idx
        self.model_combo.setCurrentIndex(selected_idx)
        self.model_combo.blockSignals(False)
        self._refresh_device_badge()

    def _on_model_selected(self, index: int):
        m_id = self.model_combo.itemData(index)
        if m_id:
            self.current_model = m_id
            self.sig_model_changed.emit(m_id)

    def update_connected_mic(self, mic_name: str):
        clean = mic_name.removeprefix("● ").strip()
        self.current_mic_name = clean
        if self.current_state == "idle":
            self.text_label.setText(clean)

    def update_language(self, lang_code: str):
        self.current_language = lang_code
        # Drop any detected dialect: is_odia_active() treats a set dialect as proof of Odia,
        # so a stale one would keep the blob and the regional badge up after a switch
        # back to English.
        self._active_dialect = None
        self._update_badge_text(self._lang_short(lang_code))
        self._update_indicator_mode()

    def _on_mic_selected(self, index: int):
        self.update_connected_mic(self.mic_combo.currentText())
        self.sig_mic_changed.emit(self.mic_combo.currentData())

    def _on_language_selected(self, index: int):
        code = self.lang_combo.currentData()
        if code:
            self.update_language(code)
            self.sig_language_changed.emit(code)

    def _on_tone_selected(self, index: int):
        tone = self.tone_combo.currentData()
        if tone:
            self.sig_tone_changed.emit(tone)

    def set_tone(self, key: str):
        """Reflect a tone chosen elsewhere (tray menu, config) without emitting it back."""
        self.tone_combo.blockSignals(True)
        for i in range(self.tone_combo.count()):
            if self.tone_combo.itemData(i) == key:
                self.tone_combo.setCurrentIndex(i)
        self.tone_combo.blockSignals(False)

    def _on_paste_clicked(self):
        self.collapse_to_capsule()
        self.sig_paste_last.emit()

    def _on_retry_clicked(self):
        self.collapse_to_capsule()
        self.sig_retry_last.emit()

    def _on_transfer_clicked(self):
        self.collapse_to_capsule()
        self.sig_open_dashboard.emit()

    def _on_update_clicked(self):
        self.btn_update.setText("Downloading update...")
        self.btn_update.setEnabled(False)
        self.sig_update_requested.emit()

    def show_update_available(self, version: str = ""):
        """Reveals the install button in the settings panel."""
        label = f"Update v{version} Available · Install" if version else "Update Available · Install"
        self.btn_update.setText(label)
        self.btn_update.setEnabled(True)
        self.btn_update.show()
        self._fit_hud()
        if not self.is_expanded_hud and self.isVisible():
            self.show_message(label, duration_ms=3000)

    def position_bottom_center(self, screen=None):
        if screen is None:
            screen = self.current_screen or (QGuiApplication.screenAt(QCursor.pos()) if QGuiApplication.instance() else None) or QApplication.primaryScreen()
        if screen:
            self.current_screen = screen
            r = screen.availableGeometry()
            self._anchor = QPoint()
            self._unstage()
            if self._is_snoozed or self._snooze_t > 0.8:
                w, h = self._snooze_w, self._snooze_h
            elif self._mode == "line" or self._line_t > 0.8:
                w, h = self._line_w, self._line_h
            else:
                w, h = self._cap_w, self._cap_h
            self.setGeometry(r.x() + (r.width() - w) // 2,
                             r.y() + r.height() - h - 35,
                             w, h)
            self.capsule_widget.move(0, 0)
            if hasattr(self, "caption_bubble"):
                self.caption_bubble._reposition()

    def set_follow_mouse_screen(self, follow: bool):
        self.follow_mouse_screen = follow

    def _check_screen_follow(self):
        if not self.follow_mouse_screen or self.is_expanded_hud or self._is_transitioning_screen:
            return
        # Only switch screens when resting / idle so we don't jump mid-sentence
        if self.current_state not in ("idle", "pasted") and not self._is_snoozed and self._mode != "line":
            return
        cursor_pos = QCursor.pos()
        target_screen = QGuiApplication.screenAt(cursor_pos)
        if not target_screen:
            return
        try:
            cur_screen = self.current_screen or self.screen() or QGuiApplication.screenAt(self.geometry().center())
            moved = cur_screen is not None and target_screen.name() != cur_screen.name()
        except RuntimeError:  # a monitor was switched off and Qt deleted its QScreen: re-home on the live one
            self.current_screen = None
            moved = True
        if moved:
            self._transition_to_screen(target_screen)

    def _transition_to_screen(self, target_screen):
        if self._is_transitioning_screen:
            return
        self._is_transitioning_screen = True
        self.screen_fade_anim.stop()
        self.screen_fade_anim.setDuration(160)
        self.screen_fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.screen_fade_anim.setStartValue(self.windowOpacity())
        self.screen_fade_anim.setEndValue(0.0)

        def _on_screen_fade_out():
            try:
                self.screen_fade_anim.finished.disconnect(_on_screen_fade_out)
            except Exception:
                pass
            self.current_screen = target_screen
            if self._size[2]:
                self.set_capsule_style(self.capsule_style)  # a bigger or smaller screen changes the capsule size
            self.position_bottom_center(target_screen)
            _force_topmost(self)
            self.screen_fade_anim.setDuration(220)
            self.screen_fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self.screen_fade_anim.setStartValue(0.0)
            self.screen_fade_anim.setEndValue(self.opacity)

            def _on_screen_fade_in():
                try:
                    self.screen_fade_anim.finished.disconnect(_on_screen_fade_in)
                except Exception:
                    pass
                self._is_transitioning_screen = False

            self.screen_fade_anim.finished.connect(_on_screen_fade_in)
            self.screen_fade_anim.start()

        self.screen_fade_anim.finished.connect(_on_screen_fade_out)
        self.screen_fade_anim.start()

    def minimize_to_snooze(self):
        if self.is_expanded_hud:
            return
        if self.hide_when_idle:
            self._fade_out_and_hide()
            return
        if self.capsule_style == "standard":
            self.minimize_to_line()
            return
        self._is_snoozed = True
        self.current_state = "idle"
        self.indicator.set_state("idle")
        self.visualizer.setVisible(False)
        self._bottom_stage(self._cap_w, self._cap_h)
        self.snooze_anim.stop()
        self.snooze_anim.setDuration(220)  # resting: unhurried
        self.snooze_anim.setEasingCurve(SNAP_EASE)
        self.snooze_anim.setStartValue(self._snooze_t)
        self.snooze_anim.setEndValue(1.0)
        self.snooze_anim.start()

    def expand_from_snooze(self):
        self._is_snoozed = False
        if self.capsule_style == "standard":
            self.expand_from_line()
            return
        self.visualizer.setVisible(True)
        self._bottom_stage(self._cap_w + 20, self._cap_h + 10)
        self.snooze_anim.stop()
        self.snooze_anim.setDuration(220)  # bouncy bubble pop on wake
        self.snooze_anim.setEasingCurve(BUBBLE_EASE)
        self.snooze_anim.setStartValue(self._snooze_t)
        self.snooze_anim.setEndValue(0.0)
        self.snooze_anim.start()

    def _apply_snooze_t(self, val):
        self._snooze_t = float(val)
        # The resting pill is empty, like Wispr's: the content leaves before the body shrinks around it
        # and comes back only once there is room, so the blob, badge and bars are never squeezed.
        self.capsule_widget.setVisible(self._snooze_t < 0.3)
        if self._staged:
            body = self._place_body(max(8.0, self._cap_w - (self._cap_w - self._snooze_w) * self._snooze_t),
                                    max(6.0, self._cap_h - (self._cap_h - self._snooze_h) * self._snooze_t))
            self.capsule_widget.setFixedSize(round(body.width()), round(body.height()))
            self.capsule_widget.move(round(body.x()), round(body.y()))
            self.update()
            if hasattr(self, "caption_bubble"):
                self.caption_bubble._reposition()
            return
        target_w = round(self._cap_w - (self._cap_w - self._snooze_w) * self._snooze_t)
        target_h = round(self._cap_h - (self._cap_h - self._snooze_h) * self._snooze_t)
        self.capsule_widget.setFixedSize(target_w, target_h)
        screen = self._home_screen()
        if screen:
            r = screen.availableGeometry()
            new_x = r.x() + (r.width() - target_w) // 2
            new_y = r.y() + r.height() - target_h - 35
            self.setGeometry(new_x, new_y, target_w, target_h)
            self.capsule_widget.move(0, 0)
        else:
            self.resize(target_w, target_h)
        if hasattr(self, "caption_bubble"):
            self.caption_bubble._reposition()
        self.update()

    def _on_snooze_anim_finished(self):
        self._unstage()
        self._apply_snooze_t(self._snooze_t)
        if self._snooze_t >= 0.95:
            self._is_snoozed = True
            self.visualizer.setVisible(False)
        else:
            self._is_snoozed = False
            self.visualizer.setVisible(True)
        self.update()

    # ---------- state ----------

    def set_opacity(self, value: float):
        self.opacity = max(0.3, min(1.0, value))
        if self.isVisible() and self.windowOpacity() > 0.05 and                 self.fade_anim.state() != QPropertyAnimation.State.Running:
            self.setWindowOpacity(self.opacity)

    def set_amplitude(self, level: float):
        self.visualizer.set_level(level)
        self._target_level = max(0.0, min(1.0, level))
        if hasattr(self, "blobatar"):
            self.blobatar.set_level(level)

    def show_listening(self, preset_name: str = "Listening"):
        self._peek.hide_now()
        if self._tray is not None:
            self._tray.hide_now()
        if self.is_expanded_hud:  # dictating with the settings sheet open: fold it so the live text shows
            self.collapse_to_capsule()
        self.hide_timer.stop()
        was_hidden = not self.isVisible() or self.windowOpacity() < 0.1

        # Check multi-screen follow on hotkey activation
        if getattr(self, "follow_mouse_screen", True):
            cursor_pos = QCursor.pos()
            target_screen = QGuiApplication.screenAt(cursor_pos)
            cur_screen = self.current_screen or self.screen() or QGuiApplication.screenAt(self.geometry().center())
            if target_screen and cur_screen and target_screen.name() != cur_screen.name():
                self.current_screen = target_screen
                self.position_bottom_center(target_screen)

        if self._mode == "line" or self._line_t > 0.05:
            self.expand_from_line()
        if self._is_snoozed or self._snooze_t > 0.05:
            self.expand_from_snooze()

        self.current_state = "listening"
        self.indicator.set_state("listening")
        self._update_indicator_mode()
        if self.capsule_style not in ("compact", "micro"):
            self.text_label.setText(preset_name)
        self._update_badge_text(self._lang_short(self.current_language))
        self.visualizer.reset()
        self.visualizer.setVisible(True)
        if hasattr(self, "btn_stop"):
            self.btn_stop.setVisible(self.capsule_style != "micro")
        self.update()

        self.fade_anim.stop()
        self.show()
        _force_topmost(self)
        if was_hidden:
            self._fade_in()
        else:
            if self.windowOpacity() < self.opacity - 0.01:
                self.fade_anim.setDuration(240)
                self.fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
                self.fade_anim.setStartValue(self.windowOpacity())
                self.fade_anim.setEndValue(self.opacity)
                self.fade_anim.start()
            else:
                self.setWindowOpacity(self.opacity)

    def update_realtime_text(self, text: str):
        if not text or self.current_state != "listening" or self.is_expanded_hud:
            return
        if self.capsule_style in ("compact", "micro"):
            self.caption_bubble.set_text(text[-160:])
        else:
            self.text_label.setText(text[-160:])

    def show_processing(self):
        if hasattr(self, "caption_bubble"):
            self.caption_bubble.fade_out()
        if self.is_expanded_hud:
            return
        self.current_state = "processing"
        self.indicator.set_state("processing")
        self._update_indicator_mode()
        if self.capsule_style not in ("compact", "micro"):
            self.text_label.setText("Writing")
        self.visualizer.reset()
        self.visualizer.setVisible(False)
        if hasattr(self, "btn_stop"):
            self.btn_stop.hide()
        self.update()

    def show_refining(self):
        """MIRA is rewriting the raw transcript: fillers out, numerals and acronyms formatted."""
        if hasattr(self, "caption_bubble"):
            self.caption_bubble.fade_out()
        if self.is_expanded_hud:
            return
        self.current_state = "refining"
        self.indicator.set_state("processing")
        self._update_indicator_mode()
        if self.capsule_style not in ("compact", "micro"):
            self.text_label.setText("Refining")
        self.visualizer.reset()
        self.visualizer.setVisible(False)
        if hasattr(self, "btn_stop"):
            self.btn_stop.hide()
        self.update()

    def _hide_download_row(self):
        self.hud_download_row.hide()
        self._fit_hud()

    def show_download(self, percent: int, model: str):
        """Capsule and HUD progress fills while a model downloads."""
        if percent >= 100:
            self._dl = -1
            if hasattr(self, "hud_download_bar"):
                self.hud_download_bar.setValue(100)
                self.hud_download_text.setText("Model ready")
                QTimer.singleShot(2500, self._hide_download_row)
            self.show_message("Model ready")
            return

        self._dl = percent

        # Update HUD download row
        if hasattr(self, "hud_download_row"):
            if self.hud_download_row.isHidden():
                self.hud_download_row.show()
                self._fit_hud()
            self.hud_download_bar.setValue(percent)
            self.hud_download_text.setText(f"{model} · {percent}%")

        # Update Capsule
        was_hidden = not self.isVisible() or self.windowOpacity() < 0.1
        if self._mode == "line" or self._line_t > 0.05:
            self.expand_from_line()
        if self._is_snoozed or self._snooze_t > 0.05:
            self.expand_from_snooze()
        self.current_state = "processing"
        self.indicator.set_state("processing")
        if self.capsule_style not in ("compact", "micro"):
            self.text_label.setText(f"Downloading {model} model  {percent}%")
        elif hasattr(self, "caption_bubble"):
            self.caption_bubble.set_text(f"Downloading {model} · {percent}%", merge=False)
        self.visualizer.setVisible(False)
        if hasattr(self, "btn_stop"):
            self.btn_stop.hide()
        self.hide_timer.stop()
        self.fade_anim.stop()
        if not self.is_expanded_hud:
            self.show()
            _force_topmost(self)
            if was_hidden:
                self._fade_in()
            else:
                self.setWindowOpacity(self.opacity)
        self.update()

    def show_message(self, text: str, duration_ms: int = 2200):
        """Brief notice (errors, empty result, ready notification), then minimize to line/snooze."""
        was_hidden = not self.isVisible() or self.windowOpacity() < 0.1
        if self._mode == "line" or self._line_t > 0.05:
            self.expand_from_line()
        if self._is_snoozed or self._snooze_t > 0.05:
            self.expand_from_snooze()
        self.current_state = "idle"
        self.indicator.set_state("idle")
        if self.capsule_style not in ("compact", "micro"):
            self.text_label.setText(text)
        self.visualizer.setVisible(False)
        if hasattr(self, "btn_stop"):
            self.btn_stop.hide()
        self.update()
        if self.capsule_style in ("compact", "micro") and hasattr(self, "caption_bubble"):
            self.caption_bubble.set_text(text, merge=False)
            QTimer.singleShot(duration_ms, self.caption_bubble.fade_out)
        if not self.is_expanded_hud:
            self.fade_anim.stop()
            self.show()
            _force_topmost(self)
            if was_hidden:
                self._fade_in()
            else:
                self.setWindowOpacity(self.opacity)
            self.hide_timer.stop()
            self.hide_timer.start(duration_ms)

    def show_pasted(self, word_count: int, latency_ms: int = 0):
        if hasattr(self, "caption_bubble"):
            self.caption_bubble.fade_out()
        self.current_state = "pasted"  # even under the HUD, or the spinner is waiting when it closes
        self.indicator.set_state("done")
        self._update_indicator_mode()
        if self.is_expanded_hud:
            return
        if self.capsule_style not in ("compact", "micro"):
            self.text_label.setText(f"Pasted {word_count} words")
        self.visualizer.setVisible(False)
        if hasattr(self, "btn_stop"):
            self.btn_stop.hide()
        self.update()
        self.hide_timer.stop()
        self.hide_timer.start(1400)

    def show_mic_event(self, mic_name: str, status: str = "connected"):
        """Displays pseudo-capsule notification pill above main capsule on mic connect/disconnect."""
        if hasattr(self, "mic_pill"):
            self.mic_pill.show_event(mic_name, status, self.geometry())

    def _fade_out_and_hide(self):
        if self.is_expanded_hud or self.current_state not in ("idle", "pasted"):
            return
        self._peek.hide_now()
        if self._tray is not None:
            self._tray.hide_now()
        if self.hide_when_idle:
            self._fade_out()
            return
        if self.capsule_style in ("compact", "micro"):
            self.minimize_to_snooze()
        else:
            self.minimize_to_line()

    # ---------- show / hide: fade + a small rise in, a quicker fade + drop out ----------

    RISE_PX = 6

    def _fade_in(self):
        """Enter: 260 ms, eases out, rising RISE_PX into place. Windows 'Animation effects' off: fade only."""
        self.fade_anim.stop()
        on = animations_enabled()
        self._rise_base = None if (self._staged or not on) else self.pos()
        self.fade_anim.setDuration(260 if on else 120)
        self.fade_anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
        self.fade_anim.setStartValue(0.0)
        self.fade_anim.setEndValue(self.opacity)
        self.setWindowOpacity(0.0)
        self.fade_anim.start()

    def _fade_out(self):
        """Exit: faster than the entrance (180 ms, eases in) and drops back the way it came."""
        self.fade_anim.stop()
        on = animations_enabled()
        self._rise_base = None if (self._staged or not on) else self.pos()
        self.fade_anim.setDuration(180 if on else 120)
        self.fade_anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.InCubic))
        self.fade_anim.setStartValue(self.windowOpacity())
        self.fade_anim.setEndValue(0.0)
        self.fade_anim.start()

    def _on_fade_tick(self, v):
        base = getattr(self, "_rise_base", None)
        if base is None or self.opacity <= 0:
            return
        k = 1.0 - float(v) / self.opacity  # 1 = hidden, 0 = shown
        self.move(base.x(), base.y() + round(self.RISE_PX * k))

    def _on_fade_finished(self):
        base = getattr(self, "_rise_base", None)
        if base is not None:
            self.move(base)  # rest exactly where we started, so a hidden window reappears in place
            self._rise_base = None
        if self.windowOpacity() > 0.01:
            return
        if self.hide_when_idle:
            self.hide()
            self._mode = "capsule"
            self._line_t = 0.0
            self._snooze_t = 0.0
            self._apply_t(0.0)
            self.position_bottom_center()
            return
        if self.capsule_style in ("compact", "micro"):
            self.minimize_to_snooze()
        else:
            self.minimize_to_line()

    # ---------- painting / dragging / hovering ----------

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = self._body_rect()

        # 1. Transition between minimal line and capsule (and idle line state)
        if self._line_t > 0.0:
            lt = self._line_t
            r = min(rect.height() / 2.0, 22.0 - (22.0 - 3.5) * lt)
            line_fill_target = 220 if self._is_line_hovered else 140
            line_pen_target = 185 if self._is_line_hovered else 80

            if lt < 0.45:
                # Early stage: deep glass background with soft edge
                p.setBrush(QBrush(_bgc(int(246 * (1.0 - lt * 0.3)))))
                p.setPen(QPen(_fg(int(40 + 30 * (1.0 - lt))), 1.0))
            else:
                # Late stage: smooth organic crossfade to luminous minimal line
                prog = (lt - 0.45) / 0.55  # 0.0 -> 1.0
                prog_e = prog * prog * (3.0 - 2.0 * prog)  # smoothstep ease
                alpha = int(246 * (1.0 - prog_e) + line_fill_target * prog_e)
                pen_a = int(60 * (1.0 - prog_e) + line_pen_target * prog_e)
                p.setBrush(QBrush(_mix(BG, INK, prog_e, alpha)))
                p.setPen(QPen(_fg(pen_a), 1.0))

            p.drawRoundedRect(rect, r, r)
            return

        # 2. Active Capsule / HUD Sheet Mode
        if self._t > 0.35:
            # Full opaque rich glass for HUD
            r = min(rect.height() / 2.0, 22.0 + 6.0 * self._t)
            hud_path = QPainterPath()
            hud_path.addRoundedRect(rect, r, r)
            p.fillPath(hud_path, _bgc(252))
            p.strokePath(hud_path, QPen(_fg(45), 1.0))
        else:
            # Apple Continuous Dynamic Island Capsule (Exact r = height / 2.0)
            r = rect.height() / 2.0
            cap_path = QPainterPath()
            cap_path.addRoundedRect(rect, r, r)

            # Deep OLED Glass Fill
            p.fillPath(cap_path, _bgc(248))

            # Specular Hairline Gradient Rim: ambient top-to-bottom light
            rim_gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
            if self.current_state == "listening":
                rim_gradient.setColorAt(0.0, _fg(125))
                rim_gradient.setColorAt(0.40, _fg(80))
                rim_gradient.setColorAt(0.75, _fg(50))
                rim_gradient.setColorAt(1.0, _fg(32))
            elif self.current_state == "processing":
                rim_gradient.setColorAt(0.0, _fg(95))
                rim_gradient.setColorAt(0.50, _fg(60))
                rim_gradient.setColorAt(1.0, _fg(24))
            elif self._is_snoozed or self._snooze_t > 0.8:
                rim_gradient.setColorAt(0.0, _fg(35))
                rim_gradient.setColorAt(0.50, _fg(20))
                rim_gradient.setColorAt(1.0, _fg(12))
            else:
                # Resting velvety rim
                rim_gradient.setColorAt(0.0, _fg(65))
                rim_gradient.setColorAt(0.45, _fg(38))
                rim_gradient.setColorAt(1.0, _fg(18))

            rim_pen = QPen(QBrush(rim_gradient), 1.0)
            p.strokePath(cap_path, rim_pen)

        if self._t > 0.4:  # sheet grabber
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_fg(int(70 * min(1.0, (self._t - 0.4) / 0.4))))
            p.drawRoundedRect(QRectF(rect.center().x() - 18, rect.top() + 7, 36, 4), 2, 2)

        if self._dl >= 0 and self._t < 0.05:  # Concept 2 blue download bar, clipped to the capsule
            clip = QPainterPath()
            clip.addRoundedRect(rect, r, r)
            p.setClipPath(clip)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_accent(34))
            p.drawRect(QRectF(rect.x(), rect.y(), rect.width() * self._dl / 100.0, rect.height()))
            bar_h = 3.5
            track = QRectF(rect.x() + 14.0, rect.bottom() - bar_h - 3.0, max(12.0, rect.width() - 28.0), bar_h)
            p.setBrush(_fg(32))
            p.drawRoundedRect(track, bar_h / 2.0, bar_h / 2.0)
            p.setBrush(_accent(230))
            p.drawRoundedRect(QRectF(track.x(), track.y(), track.width() * self._dl / 100.0, bar_h), bar_h / 2.0, bar_h / 2.0)

    def enterEvent(self, event):
        self.force_topmost()
        if self._mode == "line" or self._line_t > 0.8:
            self._is_line_hovered = True
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self._apply_line_t(self._line_t)
        else:
            self._peek.hide_timer.stop()
            if self._tray is not None:
                self._tray.hide_timer.stop()
            if self._held() and self._resting():
                self._tray_timer.start()
            else:
                self._peek_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, a0):
        self._peek_timer.stop()
        self._tray_timer.stop()
        if self._tray is not None and self._tray.isVisible():
            self._tray.hide_timer.start()
        if self._peek.isVisible():
            self._peek.hide_timer.start()
        if self._mode == "line" or self._line_t > 0.8:
            self._is_line_hovered = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._apply_line_t(self._line_t)
        super().leaveEvent(a0)

    def mousePressEvent(self, a0):
        self._peek_timer.stop()
        self._peek.hide_now()
        if self._mode == "line" or self._line_t > 0.8:
            self.expand_from_line()
            a0.accept()
            return
        if (self._is_snoozed or self._snooze_t > 0.5) and a0.button() == Qt.MouseButton.LeftButton:
            self.expand_from_snooze()
            a0.accept()
            return
        if a0.button() == Qt.MouseButton.LeftButton and not self.is_expanded_hud:
            self._click_t0 = time.monotonic()
            self._click_pos = a0.globalPosition().toPoint()
        if a0.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = a0.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._press_pos = a0.globalPosition().toPoint()
            self._dragging = False
            a0.accept()

    def mouseReleaseEvent(self, a0):
        if not self.is_expanded_hud and a0.button() == Qt.MouseButton.LeftButton:
            dt = time.monotonic() - getattr(self, "_click_t0", 0.0)
            dist = (a0.globalPosition().toPoint() - getattr(self, "_click_pos", QPoint())).manhattanLength()
            if dt < 0.32 and dist < 6 and self._held() and self._resting():
                self._tray_timer.stop()
                self.tray.toggle()
                a0.accept()
                return
            if dt < 0.32 and dist < 6 and self.capsule_style in ("compact", "micro"):
                self.expand_to_hud()
                a0.accept()
                return
        super().mouseReleaseEvent(a0)

    def mouseMoveEvent(self, a0):
        # the open HUD stays where it opened; a click's small jitter must not nudge the capsule either
        if self._mode == "line" or a0.buttons() != Qt.MouseButton.LeftButton or self.is_expanded_hud or self._staged:
            return
        pos = a0.globalPosition().toPoint()
        if not getattr(self, "_dragging", False):
            if (pos - getattr(self, "_press_pos", pos)).manhattanLength() < QApplication.startDragDistance():
                return
            self._dragging = True
        self.move(pos - self._drag_pos)
        a0.accept()
