from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QStackedWidget, QScrollArea, QButtonGroup,
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit,
    QComboBox, QFrame, QMessageBox, QAbstractItemView, QDoubleSpinBox, QProgressBar,
    QListWidget, QListWidgetItem, QTextBrowser, QSplitter, QFileDialog, QInputDialog, QSlider, QMenu
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QUrl, QRectF, QPointF, QEvent
from PyQt6.QtGui import QFont, QIcon, QColor, QPixmap, QDesktopServices, QPainter, QPen, QShortcut, QKeySequence
import pyperclip  # type: ignore[import]
import importlib.util
import json
import os
import re
import threading
import time

try:
    from ..config import load_config, save_config, get_asset_path
    from ..database import Database
    from ..engine import hardware, model_manager
    from ..engine.audio_recorder import list_microphones
    from ..engine.meeting import render_meeting_html, _srt_time, _cue_lines
    from ..engine.formatter import TextFormatter
    from .styles import main_qss, COLORS
    from .floating_pill import Switch
except (ImportError, ValueError):
    from config import load_config, save_config, get_asset_path
    from database import Database
    from engine import hardware, model_manager
    from engine.audio_recorder import list_microphones
    from engine.meeting import render_meeting_html, _srt_time, _cue_lines
    from engine.formatter import TextFormatter
    from ui.styles import main_qss, COLORS
    from ui.floating_pill import Switch


# Windows virtual-key codes -> names GlobalHotkeyManager understands
_VK_NAMES = {0x10: "shift", 0xA0: "shift", 0xA1: "shift", 0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
             0x12: "alt", 0xA4: "alt", 0xA5: "alt", 0x20: "space"}
_MOD_ORDER = ("ctrl", "shift", "alt")


def vk_name(vk: int) -> str:
    """Win32 virtual key -> hotkey token ('' if unsupported)."""
    if vk in _VK_NAMES:
        return _VK_NAMES[vk]
    if 0x70 <= vk <= 0x87:
        return f"f{vk - 0x6F}"
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    return ""


def _is_fkey(k: str) -> bool:
    return k[:1] == "f" and k[1:].isdigit()


def combo_from_keys(keys) -> str:
    """Orders tokens ctrl+shift+alt+key; '' if the combo would hijack normal typing."""
    mods = [m for m in _MOD_ORDER if m in keys]
    rest = sorted(k for k in keys if k not in _MOD_ORDER)
    if not mods and not (len(rest) == 1 and _is_fkey(rest[0])):
        return ""  # a bare letter, digit or space would fire while typing
    return "+".join(mods + rest[:1])


def pretty_hotkey(combo: str):
    return [k.upper() if _is_fkey(k) else k.capitalize() for k in combo.split("+") if k]


class _ClearButton(QPushButton):
    """Round x button, painted so the dashboard's global QPushButton padding can't distort it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(22, 22)

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        fill, ink = QColor(COLORS["text_main"]), QColor(COLORS["text_main"])  # follows the theme
        fill.setAlpha(80 if self.isDown() else 56 if self.underMouse() else 34)
        ink.setAlpha(220)
        p.setBrush(fill)
        side = min(self.width(), self.height())  # the global QSS min-height can make the widget taller
        cx, cy, d = self.width() / 2.0, self.height() / 2.0, 3.6
        p.drawEllipse(QPointF(cx, cy), side / 2.0, side / 2.0)
        p.setPen(QPen(ink, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(cx - d, cy - d), QPointF(cx + d, cy + d))
        p.drawLine(QPointF(cx - d, cy + d), QPointF(cx + d, cy - d))


class HotkeyRecorder(QFrame):
    """Rounded key box: shows the shortcut as keycaps; x clears it and records the next key press."""
    sig_recording = pyqtSignal(bool)  # True while capturing; the global hotkey must pause
    sig_changed = pyqtSignal(str)

    def __init__(self, hotkey: str, parent=None):
        super().__init__(parent)
        self._hotkey = hotkey
        self._recording = False
        self._held, self._best = set(), set()
        self.setObjectName("HotkeyBox")
        self.setFixedHeight(40)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 8, 0)
        lay.setSpacing(6)
        self._caps = QHBoxLayout()
        self._caps.setSpacing(4)
        lay.addLayout(self._caps)
        self._hint = QLabel("", self)
        self._hint.setObjectName("KeyHint")
        lay.addWidget(self._hint)
        lay.addStretch()
        self._btn = _ClearButton(self)
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn.setToolTip("Clear and record a new shortcut")
        self._btn.clicked.connect(self._on_btn)
        lay.addWidget(self._btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self._render(pretty_hotkey(hotkey))

    def hotkey(self) -> str:
        return self._hotkey

    def _render(self, caps, hint: str = "", error: bool = False):
        while self._caps.count():
            w = self._caps.takeAt(0).widget()
            if w:
                w.deleteLater()
        for c in caps:
            cap = QLabel(c, self)
            cap.setObjectName("KeyCap")
            self._caps.addWidget(cap, 0, Qt.AlignmentFlag.AlignVCenter)
        self._hint.setText(hint)
        self._hint.setProperty("error", "true" if error else "false")
        self._hint.style().unpolish(self._hint)
        self._hint.style().polish(self._hint)

    def _set_recording(self, on: bool):
        self._recording = on
        self.setProperty("recording", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        if on:
            self._held, self._best = set(), set()
            self.setFocus()
            self.grabKeyboard()
        else:
            self.releaseKeyboard()
        self.sig_recording.emit(on)

    def _on_btn(self):
        if self._recording:
            self._cancel()
        else:
            self._render([], "Press a shortcut…")
            self._set_recording(True)

    def mousePressEvent(self, a0):
        if not self._recording:
            self._on_btn()

    def _cancel(self):
        self._set_recording(False)
        self._render(pretty_hotkey(self._hotkey))

    def keyPressEvent(self, a0):
        if not self._recording:
            return super().keyPressEvent(a0)
        if a0.isAutoRepeat():
            return
        if a0.key() == Qt.Key.Key_Escape:
            return self._cancel()
        name = vk_name(a0.nativeVirtualKey())
        if name:
            self._held.add(name)
            if len(self._held) >= len(self._best):
                self._best = set(self._held)
            mods = [m for m in _MOD_ORDER if m in self._best]
            self._render(pretty_hotkey("+".join(mods + sorted(self._best - set(_MOD_ORDER)))))

    def keyReleaseEvent(self, a0):
        if not self._recording or a0.isAutoRepeat():
            return super().keyReleaseEvent(a0)
        self._held.discard(vk_name(a0.nativeVirtualKey()))
        if self._held or not self._best:
            return
        combo = combo_from_keys(self._best)  # commit once every key is up
        if not combo:
            self._best = set()
            self._render([], "Use Ctrl, Shift, Alt or an F-key", error=True)
            return
        self._hotkey = combo
        self._set_recording(False)
        self._render(pretty_hotkey(combo))
        self.sig_changed.emit(combo)

    def focusOutEvent(self, a0):
        if self._recording:
            self._cancel()
        super().focusOutEvent(a0)


def _no_wheel(cls):
    """Control that only takes the mouse wheel once focused, so scrolling a page never edits a value."""
    class Guarded(cls):
        def __init__(self, *a):
            super().__init__(*a)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        def wheelEvent(self, e):
            super().wheelEvent(e) if self.hasFocus() else e.ignore()  # ignored: the page scrolls instead
    return Guarded


NoWheelCombo, NoWheelSlider, NoWheelSpin = _no_wheel(QComboBox), _no_wheel(QSlider), _no_wheel(QDoubleSpinBox)


class Group(QFrame):
    """Inset-grouped list: rounded block of rows with hairline separators."""

    def __init__(self):
        super().__init__()
        self.setObjectName("Group")
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(0, 0, 0, 0)
        self._v.setSpacing(0)
        self._n = 0

    def _sep(self):
        if self._n:
            inset = QWidget(self)
            lay = QHBoxLayout(inset)
            lay.setContentsMargins(16, 0, 0, 0)
            line = QFrame(inset)
            line.setObjectName("Sep")
            lay.addWidget(line)
            self._v.addWidget(inset)
        self._n += 1

    def add(self, label: str = "", control: QWidget = None, sub: str = ""):
        """Row: label (and optional secondary text) left, control right. Returns the sub-label."""
        self._sep()
        row = QWidget(self)
        row.setMinimumHeight(44)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(16)
        text = QVBoxLayout()
        text.setSpacing(4)  # title and description must not touch
        if label:
            lbl = QLabel(label, row)
            lbl.setObjectName("RowLabel")
            lbl.setWordWrap(True)
            text.addWidget(lbl)
        sub_lbl = QLabel(sub, row)
        sub_lbl.setObjectName("RowSub")
        sub_lbl.setWordWrap(True)
        sub_lbl.setVisible(bool(sub))
        text.addWidget(sub_lbl)
        lay.addLayout(text, 1)
        if control is not None:
            lay.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        self._v.addWidget(row)
        return sub_lbl

    def add_widget(self, w: QWidget):
        self._sep()
        self._v.addWidget(w)

    def clear(self):
        while self._v.count():
            w = self._v.takeAt(0).widget()
            if w:
                w.hide()  # deleteLater waits for the event loop; until then the old row would paint under the new one
                w.deleteLater()
        self._n = 0


def _btn(text: str, kind: str = "", small: bool = False, slot=None) -> QPushButton:
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if kind:
        b.setObjectName({"primary": "PrimaryButton", "danger": "DangerButton"}[kind])
    if small:
        b.setProperty("small", "true")
    if slot:
        b.clicked.connect(slot)
    return b


def _logo_label(height: int, parent=None):
    """The bare logo mark (transparent PNG): no tile, no border. None if the asset is missing."""
    path = get_asset_path("logo_mark.png")
    if not os.path.exists(path):
        return None
    ratio = 2.0  # sharp on high-DPI screens
    pm = QPixmap(path).scaledToHeight(round(height * ratio), Qt.TransformationMode.SmoothTransformation)
    pm.setDevicePixelRatio(ratio)
    lbl = QLabel(parent)
    lbl.setPixmap(pm)
    return lbl


def _label(text: str, name: str, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    return lbl


def _badge(text: str, kind: str = "") -> QLabel:
    b = QLabel(text)
    b.setProperty("badge", kind or "plain")
    return b


def _panel(*widgets) -> QFrame:
    """Plain rounded block for a table or list."""
    f = QFrame()
    f.setObjectName("Group")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    for w in widgets:
        lay.addWidget(w)
    return f


def _gap_splitter(orientation) -> QSplitter:
    """Splitter whose handle is a 16 px gutter between cards instead of a hairline joining them."""
    s = QSplitter(orientation)
    s.setObjectName("Gutter")
    s.setHandleWidth(16)
    s.setChildrenCollapsible(False)
    return s


def _style_table(t: QTableWidget):
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(40)
    t.setShowGrid(False)
    t.setWordWrap(False)
    t.setTextElideMode(Qt.TextElideMode.ElideRight)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    t.horizontalHeader().setHighlightSections(False)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


# -- transcribe: cue helpers. A cue is [start_s, end_s, text, speaker, original]: `original` is what the model
# heard (None once a split or merge leaves no single original). Edits build new lists, never change one in place.

MAX_CPS, MAX_CHARS, MIN_SECONDS = 20.0, 84, 0.7  # broadcast reading limits: two lines of 42
MEDIA_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".webm", ".mp4", ".mkv", ".mov"}
SUB_EXT = {".srt", ".vtt"}
PROJECT_EXT = ".vaani.json"
QUALITY = (("fast", "Reads too fast"), ("long", "Too long for two lines"), ("short", "On screen too briefly"),
           ("overlap", "Overlaps the next line"), ("past_end", "Runs past the end"), ("order", "Ends before it starts"),
           ("empty", "No text"))
_TS = re.compile(r"^\s*(?:(\d+):)?(?:(\d+):)?(\d+(?:[.,]\d+)?)\s*$")
_CUE_TIME = re.compile(r"(\d[\d:.,]*)\s*-->\s*(\d[\d:.,]*)")
_WORD = r"[\wऀ-෿]"  # \w misses Indic vowel signs, so a whole-word match could stop inside an Odia word
_PUNCT = ".,!?;:\"'()[]“”‘’।॥…"


def parse_ts(text: str):
    """'00:01:02,500', '1:02.5' or '62.5' -> seconds; None if it isn't a time."""
    m = _TS.match(text or "")
    if not m:
        return None
    a, b, s = m.groups()
    h, mins = (int(a), int(b)) if b is not None else (0, int(a or 0))
    sec = float(s.replace(",", "."))
    if a is not None and (sec >= 60 or (b is not None and mins >= 60)):
        return None
    return h * 3600 + mins * 60 + sec


def norm_cue(c):
    """Any [start, end, text, (speaker), (original)] -> a full cue; raises on junk."""
    text = str(c[2])
    return [float(c[0]), float(c[1]), text, str(c[3] or "") if len(c) > 3 else "", c[4] if len(c) > 4 else text]


def cue_issues(cues, i: int, duration: float = 0.0):
    """Why line i is hard to read: [(kind, message)], [] when it's fine. Kinds are QUALITY's."""
    st, en, text = cues[i][:3]
    dur, out = en - st, []
    if not text.strip():
        out.append(("empty", "No text"))
    if dur <= 0:
        out.append(("order", "Ends before it starts"))
    else:
        if dur < MIN_SECONDS:
            out.append(("short", f"On screen only {dur:.1f} s"))
        if len(text) / dur > MAX_CPS:
            out.append(("fast", f"Reads fast: {len(text) / dur:.0f} characters a second"))
    if len(text) > MAX_CHARS:
        out.append(("long", f"{len(text)} characters won't fit on two lines of {MAX_CHARS // 2}"))
    if i + 1 < len(cues) and cues[i + 1][0] < en - 0.001:
        out.append(("overlap", "Overlaps the next line"))
    if duration > 0 and en > duration + 0.05:
        out.append(("past_end", "Runs past the end of the media"))
    return out


def cues_export(cues, fmt: str = "srt") -> str:
    """SubRip, WebVTT, plain text or JSON; empty lines are dropped and long ones balanced over two lines."""
    kept = [(c[0], c[1], " ".join(c[2].split()), c[3]) for c in cues if c[2].strip()]
    if fmt == "json":
        return json.dumps([{"start": round(a, 3), "end": round(b, 3), "speaker": s, "text": t} for a, b, t, s in kept],
                          ensure_ascii=False, indent=2) + "\n"
    if fmt == "txt":
        out, last = [], None
        for _, _, t, s in kept:  # a transcript names the speaker once per turn
            out.append(f"{s}: {t}" if s and s != last else t)
            last = s
        return "\n".join(out) + "\n"
    if fmt == "vtt":
        vt = lambda t: _srt_time(t).replace(",", ".")
        return "WEBVTT\n\n" + "\n".join(f"{vt(a)} --> {vt(b)}\n{f'<v {s}>' if s else ''}{_cue_lines(t)}\n" for a, b, t, s in kept)
    return "\n".join(f"{n}\n{_srt_time(a)} --> {_srt_time(b)}\n{_cue_lines(t)}\n" for n, (a, b, t, _) in enumerate(kept, 1))


def parse_subtitles(text: str):
    """SRT or WebVTT text -> cues; a WebVTT <v Name> tag becomes the speaker."""
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = block.strip().split("\n")
        for k, line in enumerate(lines):
            m = _CUE_TIME.search(line)
            if not m:
                continue
            st, en = parse_ts(m.group(1)), parse_ts(m.group(2))
            body = " ".join(lines[k + 1:])
            voice = re.match(r"\s*<v(?:\.[^\s>]*)?\s+([^>]+)>", body)
            body = " ".join(re.sub(r"<[^>]+>", "", body).split())
            if st is not None and en is not None:
                cues.append([st, en, body, voice.group(1).strip() if voice else "", body])
            break
    return cues


def replace_in_cues(cues, find: str, repl: str, case: bool = False, whole: bool = False):
    """-> (new cues, number of replacements)."""
    if not find:
        return cues, 0
    pat = re.escape(find)
    if whole:
        pat = f"(?<!{_WORD}){pat}(?!{_WORD})"
    rx = re.compile(pat, 0 if case else re.IGNORECASE)
    out, n = [], 0
    for c in cues:
        t2, k = rx.subn(lambda _m: repl, c[2])
        out.append(c[:2] + [t2] + c[3:])
        n += k
    return out, n


def word_swaps(old: str, new: str):
    """Words (up to 3 at a time) the user swapped between two versions of a line: [(was, now), ...]."""
    import difflib
    a = [w.strip(_PUNCT) for w in old.split()]
    b = [w.strip(_PUNCT) for w in new.split()]
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        was, now = " ".join(a[i1:i2]).strip(), " ".join(b[j1:j2]).strip()
        if op == "replace" and i2 - i1 <= 3 and j2 - j1 <= 3 and was and now and was != now:
            out.append((was, now))
    return out


def split_cue(cue, at=None):
    """One line into two: the text at its balanced middle, the time at `at` (the playhead) when it falls
    inside the line, else shared by length. None for a one-word line."""
    st, en, text, speaker = cue[:4]
    parts = _cue_lines(text, width=0).split("\n")
    if len(parts) < 2:
        return None
    cut = at if at is not None and st < at < en else st + (en - st) * len(parts[0]) / (len(parts[0]) + len(parts[1]))
    return [st, cut, parts[0], speaker, None], [cut, en, parts[1], speaker, None]


def shift_cues(cues, seconds: float, start: int = 0):
    return [list(c) if i < start else [max(0.0, c[0] + seconds), max(0.0, c[1] + seconds)] + c[2:]
            for i, c in enumerate(cues)]


def audio_peaks(samples, buckets: int = 2000):
    """Loudest sample per bucket, scaled 0..1, for drawing a waveform."""
    import numpy as np
    a = np.abs(np.asarray(samples, dtype=np.float32))
    if not len(a):
        return []
    k = max(1, len(a) // buckets)
    a = a[: len(a) // k * k].reshape(-1, k).max(axis=1)
    return (a / (float(a.max()) or 1.0)).tolist()


class Waveform(QWidget):
    """Audio peaks with every subtitle line shaded on them; click or drag to seek."""
    sig_seek = pyqtSignal(int)  # ms

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(56)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Waveform")
        self.setToolTip("Click or drag to jump. Shaded blocks are subtitle lines.")
        self.peaks, self.cues, self.duration, self.pos, self.current = [], [], 0.0, 0.0, -1

    def set_view(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.update()

    def _x(self, sec: float) -> float:
        return sec / self.duration * self.width() if self.duration > 0 else 0.0

    def _seek(self, e):
        if self.duration > 0:
            frac = max(0.0, min(1.0, e.position().x() / max(1, self.width())))
            self.sig_seek.emit(int(frac * self.duration * 1000))

    def mousePressEvent(self, a0):
        self._seek(a0)

    def mouseMoveEvent(self, a0):
        if a0.buttons():
            self._seek(a0)

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(COLORS["bg_control"]))
        p.drawRoundedRect(QRectF(0, 0, w, h), 8, 8)
        if self.duration <= 0:
            p.setPen(QColor(COLORS["text_muted"]))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "The waveform appears once the audio is read")
            return
        for i, c in enumerate(self.cues):
            shade = QColor(COLORS["accent_primary"])
            shade.setAlpha(110 if i == self.current else 38)
            x0, x1 = self._x(c[0]), self._x(c[1])
            p.setBrush(shade)
            p.drawRect(QRectF(x0, 4, max(1.5, x1 - x0 - 1), h - 8))
        if self.peaks:
            ink = QColor(COLORS["text_main"])
            ink.setAlpha(140)
            p.setBrush(ink)
            cols, n, mid = max(1, w // 3), len(self.peaks), h / 2.0
            for k in range(cols):
                bar = max(1.0, self.peaks[min(n - 1, k * n // cols)] * (h - 16))
                p.drawRect(QRectF(k * 3, mid - bar / 2, 2, bar))
        p.setBrush(QColor(COLORS["primary"]))
        p.drawRect(QRectF(self._x(self.pos) - 1, 0, 2, h))


NAV = [("Dictate", [("History", 0), ("Meeting Notes", 1), ("Vocabulary", 2)]), ("Tools", [("Transcribe", 7)]),
       ("Library", [("Model Library", 3)]), ("Preferences", [("Settings", 4)]), ("Insights", [("Analytics", 5)])]
COL_W = 760  # content column; wider windows keep it readable instead of stretching rows


class MainWindow(QMainWindow):
    config_changed = pyqtSignal(dict)
    sig_request_check_update = pyqtSignal()
    sig_request_install_update = pyqtSignal()
    sig_model_switch_requested = pyqtSignal(str)
    sig_model_download_requested = pyqtSignal(str)
    sig_model_delete_requested = pyqtSignal(str)
    sig_hotkey_recording = pyqtSignal(bool)
    sig_capsule_opacity = pyqtSignal(float)  # live while the slider moves
    sig_capsule_size = pyqtSignal()          # width / height / auto-scale changed; read the values from config
    sig_theme = pyqtSignal(str)              # "light" / "dark": applies at once to the dashboard and the capsule
    sig_transcribe_file = pyqtSignal()       # Tools: same actions as the tray menu
    sig_start_file_transcribe = pyqtSignal(str, str)  # (path, language)
    sig_subtitle_corrected = pyqtSignal(float, float, str, str, str)  # (start_s, end_s, orig, edited, media_path)
    sig_toggle_live = pyqtSignal()
    _sig_peaks = pyqtSignal(str, object, float)  # waveform read off the UI thread: (path, peaks, seconds)

    def __init__(self, db: Database, config: dict):
        super().__init__()
        self.db = db
        self.config = config
        self._model_progress_bars = {}

        self.setWindowTitle("Vaani")
        self.setMinimumSize(760, 560)
        self.resize(1180, 760)  # the Subtitle Studio wants its inspector beside the table
        self.setStyleSheet(main_qss(self.config.get("theme", "light")))

        icon_path = get_asset_path("app_icon_square.png")
        if not os.path.exists(icon_path):
            icon_path = get_asset_path("logo.jpg")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self._init_ui()
        self.refresh_history()
        self.refresh_dictionary()
        self.refresh_stats()
        self.refresh_models()

    # -- shell: sidebar + pages ---------------------------------------------

    def _init_ui(self):
        central = QWidget(self)
        central.setObjectName("Content")
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.stack = QStackedWidget(self)
        for page in (self._create_history_tab(), self._create_meetings_tab(), self._create_dictionary_tab(),
                     self._create_models_tab(), self._create_settings_tab(), self._create_stats_tab(),
                     self._create_about_tab(), self._create_tools_tab()):
            self.stack.addWidget(page)
        outer.addWidget(self._build_sidebar())

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        # Model download progress (hidden unless a model is downloading)
        self.dl_box = QWidget(self)
        dl_lay = QHBoxLayout(self.dl_box)
        dl_lay.setContentsMargins(28, 12, 28, 0)
        dl_lay.setSpacing(12)
        self.dl_label = QLabel("", self)
        self.dl_label.setObjectName("PageSub")
        self.dl_bar = QProgressBar(self)
        self.dl_bar.setRange(0, 100)
        self.dl_bar.setTextVisible(False)
        dl_lay.addWidget(self.dl_label)
        dl_lay.addWidget(self.dl_bar, 1)
        self.dl_box.hide()
        right.addWidget(self.dl_box)
        right.addWidget(self.stack, 1)
        outer.addLayout(right, 1)
        self.select_page(0)

    def _build_sidebar(self) -> QFrame:
        bar = QFrame(self)
        bar.setObjectName("Sidebar")
        bar.setFixedWidth(200)
        v = QVBoxLayout(bar)
        v.setContentsMargins(12, 20, 12, 12)
        v.setSpacing(2)

        brand = QHBoxLayout()
        brand.setContentsMargins(6, 0, 6, 8)
        brand.setSpacing(10)
        logo = _logo_label(18, bar)
        if logo:
            brand.addWidget(logo)
        brand.addWidget(_label("Vaani", "AppName"))
        brand.addStretch()
        v.addLayout(brand)

        self._nav_group = QButtonGroup(self)
        self._nav_btns = {}

        def nav(text, idx):
            b = QPushButton(text, bar)
            b.setProperty("nav", "true")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, i=idx: self.select_page(i))
            self._nav_group.addButton(b)
            self._nav_btns[idx] = b
            v.addWidget(b)

        for caption, items in NAV:
            v.addWidget(_label(caption.upper(), "NavCaption"))
            for text, idx in items:
                nav(text, idx)
        v.addStretch()
        nav("About", 6)
        self.hotkey_badge_text = _label("", "HotkeyHint")
        self._update_hotkey_badge()
        v.addWidget(self.hotkey_badge_text)
        return bar

    def select_page(self, i: int):
        self.stack.setCurrentIndex(i)
        self._nav_btns[i].setChecked(True)
        self.stack.currentWidget().setFocus()

    def _page(self, title: str, sub: str = "", actions=(), scroll: bool = True):
        """Page = fixed title row + (scrolling) body. Returns (root, body layout, subtitle label)."""
        root = QWidget()
        root.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # takes focus on page change, so no field starts with a ring
        v = QVBoxLayout(root)
        v.setContentsMargins(28, 28, 28, 0 if scroll else 24)
        v.setSpacing(0)
        head = QHBoxLayout()
        head.setSpacing(12)
        head.setContentsMargins(0, 0, 14 if scroll else 0, 0)  # scrollbar track width, so edges line up
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(_label(title, "PageTitle"))
        sub_lbl = _label(sub, "PageSub", wrap=True)
        sub_lbl.setVisible(bool(sub))
        col.addWidget(sub_lbl)
        head.addLayout(col, 1)
        for a in actions:
            head.addWidget(a, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(head)
        v.addSpacing(16)
        if not scroll:
            return root, v, sub_lbl
        area = QScrollArea(root)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        inner = QWidget()
        inner.setMaximumWidth(COL_W)
        body = QVBoxLayout(inner)
        body.setContentsMargins(0, 0, 0, 32)
        body.setSpacing(24)
        body.setAlignment(Qt.AlignmentFlag.AlignTop)
        area.setWidget(inner)
        v.addWidget(area, 1)
        return root, body, sub_lbl

    def _section(self, body, header: str = "", footer: str = "", action: QWidget = None):
        """Header + Group + footer note. Returns (group, footer label)."""
        box = QVBoxLayout()
        box.setSpacing(6)
        if header or action:
            row = QHBoxLayout()
            row.addWidget(_label(header.upper(), "SectionHeader"))
            row.addStretch()
            if action:
                row.addWidget(action)
            box.addLayout(row)
        g = Group()
        box.addWidget(g)
        foot = _label(footer, "SectionFooter", wrap=True)
        foot.setVisible(bool(footer))
        box.addWidget(foot)
        body.addLayout(box)
        return g, foot

    # -- history ------------------------------------------------------------

    def _create_history_tab(self) -> QWidget:
        root, body, _ = self._page("History", "Everything you have dictated, newest first.", scroll=False)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.history_search = QLineEdit(self)
        self.history_search.setPlaceholderText("Search history")
        self.history_search.setClearButtonEnabled(True)
        self.history_search.textChanged.connect(self.refresh_history)
        bar.addWidget(self.history_search, 1)
        bar.addWidget(_btn("Refresh", small=True, slot=self.refresh_history))
        bar.addWidget(_btn("Clear All", "danger", small=True, slot=self._clear_history))
        body.addLayout(bar)
        body.addSpacing(12)

        self.history_table = QTableWidget(0, 5, self)
        self.history_table.setHorizontalHeaderLabels(["Time", "App", "Words", "Speed", "Text"])
        hh = self.history_table.horizontalHeader()
        for c, w in enumerate((106, 100, 68, 84)):  # fixed so the text column keeps the rest of the width
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
            hh.resizeSection(c, w)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        _style_table(self.history_table)
        self.history_table.cellDoubleClicked.connect(self._copy_history_row)
        self.history_empty = _label("", "Empty")
        self.history_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(_panel(self.history_table, self.history_empty), 1)
        body.addSpacing(8)
        body.addWidget(_label("Double-click a row to copy its text.", "SectionFooter"))
        return root

    def refresh_history(self):
        query = self.history_search.text().strip() if hasattr(self, "history_search") else ""
        rows = self.db.get_history(limit=150, query=query)
        self.history_table.setRowCount(len(rows))
        self.history_table.setVisible(bool(rows))
        self.history_empty.setVisible(not rows)
        self.history_empty.setText("No matches." if query else "Nothing dictated yet.\nHold your hotkey in any app and start talking.")

        for idx, row in enumerate(rows):
            ts = row["timestamp"][5:16].replace("T", " ")
            app = row["target_app"] or "Desktop"
            words = str(row["word_count"] or len(row["formatted_text"].split()))
            latency = f"{row['latency_ms']} ms" if row["latency_ms"] else "-"
            text = row["formatted_text"]
            for col, val in enumerate((ts, app, words, latency, text)):
                item = QTableWidgetItem(val)
                if col == 4:
                    item.setToolTip(text)
                self.history_table.setItem(idx, col, item)

    def _copy_history_row(self, row, col):
        item = self.history_table.item(row, 4)
        if item:
            pyperclip.copy(item.text())
            QMessageBox.information(self, "Copied", "Transcription copied to clipboard.")

    def _clear_history(self):
        reply = QMessageBox.question(self, "Confirm Clear", "Clear all dictation history?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.db.clear_history()
            self.refresh_history()
            self.refresh_stats()

    # -- meeting notes ------------------------------------------------------

    def _create_meetings_tab(self) -> QWidget:
        root, body, _ = self._page("Meeting Notes", "Captions and notes from calls and anything your speakers play.", scroll=False)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        for label, slot in [("Rename Speaker", self._rename_speaker), ("Copy Markdown", self._copy_meeting),
                            ("Export .md", self._export_meeting), ("Refresh", self.refresh_meetings)]:
            bar.addWidget(_btn(label, small=True, slot=slot))
        bar.addStretch(1)
        bar.addWidget(_btn("Delete", "danger", small=True, slot=self._delete_meeting))
        body.addLayout(bar)
        body.addSpacing(12)

        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.setChildrenCollapsible(False)
        self.meeting_list = QListWidget(self)
        self.meeting_list.setMinimumWidth(190)
        self.meeting_list.currentItemChanged.connect(self._show_meeting)
        self.meeting_view = QTextBrowser(self)
        self.meeting_view.setOpenExternalLinks(True)
        split.addWidget(self.meeting_list)
        split.addWidget(self.meeting_view)
        split.setSizes([220, 460])
        self.meeting_empty = _label("No meeting notes yet.\nStart one from the capsule menu or when a call uses your microphone.", "Empty")
        self.meeting_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.meeting_split = split
        body.addWidget(_panel(split, self.meeting_empty), 1)
        body.addSpacing(8)
        body.addWidget(_label("When a call is using your mic, the capsule offers to take notes.", "SectionFooter"))
        self.refresh_meetings()
        return root

    def refresh_meetings(self):
        self._meetings = {n["id"]: n for n in self.db.get_meeting_notes()}
        self.meeting_list.clear()
        for n in self._meetings.values():
            mins = int((n["duration_seconds"] or 0) // 60)
            item = QListWidgetItem(f"{n['title']}\n{n['timestamp'][:16].replace('T', ' ')} · {mins} min")
            item.setData(Qt.ItemDataRole.UserRole, n["id"])
            self.meeting_list.addItem(item)
        self.meeting_split.setVisible(bool(self.meeting_list.count()))
        self.meeting_empty.setVisible(not self.meeting_list.count())
        if self.meeting_list.count():
            self.meeting_list.setCurrentRow(0)

    def _current_meeting(self):
        item = self.meeting_list.currentItem()
        return self._meetings.get(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _show_meeting(self, *_):
        n = self._current_meeting()
        if n:
            app_name = n.get("app_name", "Meeting")
            html = render_meeting_html(n["formatted_notes"], app_name)
            self.meeting_view.setHtml(html)

    def _rename_speaker(self):
        """Allow user to rename generic 'Person 1' or 'Person 2' to the speaker's real name across notes and transcript."""
        n = self._current_meeting()
        if not n:
            return
        notes = n["formatted_notes"]
        raw = n.get("raw_transcript", "")

        # Extract speakers present in notes
        found = set(re.findall(r"###\s*([^·\n]+)\s*·", notes))
        found.update(re.findall(r"\[\d+:\d+(?:\s*-\s*\d+:\d+)?\]\s*([^:]+):", raw))
        candidates = [s.strip() for s in found if s.strip() and "summary" not in s.lower()]
        if not candidates:
            candidates = ["Person 1", "Person 2", "You"]

        spk, ok = QInputDialog.getItem(self, "Rename Speaker", "Select speaker to rename:", sorted(candidates), 0, False)
        if not ok or not spk:
            return

        new_name, ok2 = QInputDialog.getText(self, "Rename Speaker", f"Enter real name for '{spk}':")
        if not ok2 or not new_name.strip():
            return
        new_name = new_name.strip()

        # Replace in notes and raw transcript
        updated_notes = re.sub(rf"\b{re.escape(spk)}\b", new_name, notes)
        updated_raw = re.sub(rf"\b{re.escape(spk)}\b", new_name, raw)
        self.db.update_meeting_note(n["id"], updated_raw, updated_notes)
        curr_id = n["id"]
        self.refresh_meetings()
        # Restore selection
        for i in range(self.meeting_list.count()):
            if self.meeting_list.item(i).data(Qt.ItemDataRole.UserRole) == curr_id:
                self.meeting_list.setCurrentRow(i)
                break

    def _copy_meeting(self):
        n = self._current_meeting()
        if n:
            pyperclip.copy(n["formatted_notes"])

    def _export_meeting(self):
        n = self._current_meeting()
        if not n:
            return
        name = "".join(c if c.isalnum() or c in " -_" else "_" for c in n["title"]).strip() + ".md"
        path, _ = QFileDialog.getSaveFileName(self, "Export meeting notes", name, "Markdown (*.md)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(n["formatted_notes"])

    def _delete_meeting(self):
        n = self._current_meeting()
        if n and QMessageBox.question(self, "Delete notes", f"Delete \"{n['title']}\"?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                      ) == QMessageBox.StandardButton.Yes:
            self.db.delete_meeting_note(n["id"])
            self.refresh_meetings()

    # -- vocabulary ---------------------------------------------------------

    def _create_dictionary_tab(self) -> QWidget:
        root, body, _ = self._page("Vocabulary & Snippets",
                                   "Add names, custom terms, and shortcuts like \"my address\" that expand automatically.", scroll=False)

        form = Group()
        self.dict_category = self._combo([
            ("Word or phrase", "general"),
            ("Snippet (expands after formatting)", "snippet"),
        ], "general", width=260)
        self.dict_phrase = QLineEdit(self)
        self.dict_phrase.setPlaceholderText("e.g. pi torch  or  my address")
        self.dict_replace = QLineEdit(self)
        self.dict_replace.setPlaceholderText("e.g. PyTorch  or  12 MG Road, Bhubaneswar")
        for e in (self.dict_phrase, self.dict_replace):
            e.setMinimumWidth(240)
        self.dict_case = Switch("", self)
        form.add("Type", self.dict_category)
        form.add("When I say", self.dict_phrase)
        form.add("Write", self.dict_replace)
        form.add("Match case", self.dict_case)
        body.addWidget(form)
        add_row = QHBoxLayout()
        add_row.addStretch()
        add_row.addWidget(_btn("Add Entry", "primary", slot=self._add_dictionary_word))
        body.addSpacing(8)
        body.addLayout(add_row)
        body.addSpacing(16)

        self.dict_table = QTableWidget(0, 5, self)
        self.dict_table.setHorizontalHeaderLabels(["ID", "Type", "When I say", "Write", "Match case"])
        hh = self.dict_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        _style_table(self.dict_table)
        self.dict_table.hideColumn(0)  # id stays in the table for delete
        self.dict_empty = _label("No words or snippets yet.\nAdd one above and it applies to your next dictation.", "Empty")
        self.dict_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(_panel(self.dict_table, self.dict_empty), 1)
        body.addSpacing(12)
        del_row = QHBoxLayout()
        del_row.addStretch()
        del_row.addWidget(_btn("Delete Selected", "danger", small=True, slot=self._delete_dictionary_word))
        body.addLayout(del_row)
        return root

    def refresh_dictionary(self):
        rows = self.db.get_dictionary()
        self.dict_table.setRowCount(len(rows))
        self.dict_table.setVisible(bool(rows))
        self.dict_empty.setVisible(not rows)
        labels = {"snippet": "Snippet", "learned": "Learned"}
        for idx, row in enumerate(rows):
            cat = labels.get(row.get("category", "general"), "Word")
            self.dict_table.setItem(idx, 0, QTableWidgetItem(str(row["id"])))
            self.dict_table.setItem(idx, 1, QTableWidgetItem(cat))
            self.dict_table.setItem(idx, 2, QTableWidgetItem(row["phrase_match"]))
            self.dict_table.setItem(idx, 3, QTableWidgetItem(row["replace_with"]))
            self.dict_table.setItem(idx, 4, QTableWidgetItem("Yes" if row["case_sensitive"] else "No"))

    def _add_dictionary_word(self):
        phrase = self.dict_phrase.text().strip()
        replace = self.dict_replace.text().strip()
        case_sens = self.dict_case.isChecked()
        category = self.dict_category.currentData() if hasattr(self, "dict_category") else "general"
        if not phrase or not replace:
            QMessageBox.warning(self, "Missing Fields", "Please enter both spoken phrase and replacement.")
            return
        self.db.add_dictionary_entry(phrase, replace, case_sens, category=category or "general")
        self.dict_phrase.clear()
        self.dict_replace.clear()
        self.refresh_dictionary()

    def _delete_dictionary_word(self):
        row = self.dict_table.currentRow()
        if row >= 0:
            entry_id = int(self.dict_table.item(row, 0).text())
            self.db.delete_dictionary_entry(entry_id)
            self.refresh_dictionary()

    # -- model library ------------------------------------------------------

    def _create_models_tab(self) -> QWidget:
        root, body, self.lbl_model_status_info = self._page(
            "Model Library", "", scroll=True,
            actions=[_btn("Show Cache", small=True, slot=self._open_cache_folder),
                     _btn("Clean Up", small=True, slot=self._clean_temp_blobs)])
        self.btn_open_cache = None
        self.models_group, _ = self._section(
            body, "Speech and AI models",
            "Models run 100% on this PC. Picking a model never downloads it; use the Download button.")
        return root

    def _model_row(self, m: dict) -> QWidget:
        row = QWidget()
        g = QGridLayout(row)
        g.setContentsMargins(16, 12, 16, 12)
        g.setHorizontalSpacing(16)
        g.setVerticalSpacing(4)
        g.setColumnStretch(0, 1)

        title = QHBoxLayout()
        title.setSpacing(8)
        name = _label(m["name"], "RowLabel")
        name.setStyleSheet("font-weight: 600;")
        title.addWidget(name)
        if m["is_active"]:
            title.addWidget(_badge("Active", "active"))
        if m.get("recommended"):
            title.addWidget(_badge("Recommended", "accent"))
        title.addStretch()
        g.addLayout(title, 0, 0)
        g.addWidget(_label(m["description"], "RowSub", wrap=True), 1, 0)
        size = m["disk_size_str"] if m["is_downloaded"] else m["expected_size_str"]
        meta = _label(f"Speed {m['speed']}  ·  Accuracy {m['accuracy']}  ·  {size}", "RowMeta")
        g.addWidget(meta, 2, 0)

        acts = QHBoxLayout()
        acts.setSpacing(8)
        mid = m["id"]
        if m["is_active"]:
            b = _btn("In use", small=True)
            b.setEnabled(False)
            acts.addWidget(b)
        elif m["is_downloaded"]:
            if mid != "writer":  # the writing model works beside the speech model, it never replaces it
                acts.addWidget(_btn("Use", "primary", True, lambda _=False, i=mid: self._on_switch_model(i)))
            acts.addWidget(_btn("Delete", "danger", True, lambda _=False, i=mid, n=m["name"]: self._on_delete_model(i, n)))
        else:
            acts.addWidget(_btn("Download", "primary", True, lambda _=False, i=mid: self._on_download_model(i)))
        g.addLayout(acts, 0, 1, 3, 1, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

        bar = QProgressBar()  # hidden until a download starts
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        bar.hide()
        g.addWidget(bar, 3, 0, 1, 2)
        self._model_progress_bars[mid] = bar
        return row

    def refresh_models(self):
        if not hasattr(self, "models_group"):
            return
        self.models_group.clear()
        self._model_progress_bars = {}
        curr_model = self.config.get("model_size", "base").replace(".en", "").lower()
        hw = hardware.detect()
        self.lbl_model_status_info.setText(f"Active: <b>{curr_model.title()}</b>  ·  Running on <b>{hw.get('device', 'cpu').upper()}</b>")
        self.lbl_model_status_info.show()
        for m in model_manager.list_available_models(active_model=curr_model):
            self.models_group.add_widget(self._model_row(m))

    def set_download(self, percent: int, model: str):
        """Shows or hides the download bar under the header and in the model card."""
        clean_name = model.replace(".en", "").lower()
        for mid, bar in self._model_progress_bars.items():
            if mid in clean_name:
                if percent < 100:
                    bar.show()
                    bar.setValue(percent)
                else:
                    bar.setValue(100)
                    QTimer.singleShot(1500, bar.hide)

        if percent >= 100:
            self.dl_box.hide()
            self.refresh_models()
            return
        self.dl_label.setText(f"Downloading {model} model  {percent}%")
        self.dl_bar.setValue(percent)
        self.dl_box.show()

    def _open_cache_folder(self):
        cache_dir = os.path.expanduser("~/.cache/huggingface/hub")
        os.makedirs(cache_dir, exist_ok=True)
        try:
            os.startfile(cache_dir)
        except Exception:
            QMessageBox.information(self, "Cache Folder", f"Path: {cache_dir}")

    def _clean_temp_blobs(self):
        model_manager.clean_incomplete_downloads()
        self.refresh_models()
        QMessageBox.information(self, "Clean Complete", "Stale and incomplete download blobs have been removed.")

    def _on_switch_model(self, model_id: str):
        self.sig_model_switch_requested.emit(model_id)

    def _on_download_model(self, model_id: str):
        self.sig_model_download_requested.emit(model_id)

    def _on_delete_model(self, model_id: str, model_name: str):
        reply = QMessageBox.question(
            self,
            "Confirm Model Deletion",
            f"Are you sure you want to delete '{model_name}' from disk?\n\n"
            "This will free up disk space. You can re-download it anytime.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.sig_model_delete_requested.emit(model_id)

    # -- settings -----------------------------------------------------------

    @staticmethod
    def _combo(items, current, width: int = 232) -> QComboBox:
        c = NoWheelCombo()
        for label, key in items:
            c.addItem(label, key)
        for i in range(c.count()):
            if c.itemData(i) == current:
                c.setCurrentIndex(i)
                break
        c.setFixedWidth(width)
        return c

    def _switch(self, g: Group, label: str, key: str, default: bool, sub: str = "") -> Switch:
        s = Switch("", self)
        s.setChecked(self.config.get(key, default))
        g.add(label, s, sub)
        return s

    def _create_settings_tab(self) -> QWidget:
        root, body, self._settings_sub = self._page("Settings", "Every change applies and saves at once.")
        hw = hardware.detect()

        # Speech
        g, foot = self._section(body, "Speech", hw["note"])
        self.hw_note = foot
        self.model_combo = self._combo([
            (f"Automatic ({hw['model_size']})", "auto"),
            ("Small · Indian English (~245 MB)", "small-in"),
            ("Tiny · fastest (~75 MB)", "tiny"),
            ("Base · balanced (~141 MB)", "base"), ("Small · accurate (~464 MB)", "small"),
            ("Medium · best, wants a GPU (~1.46 GB)", "medium"),
            ("Parakeet V3 · fast CPU (~640 MB)", "parakeet"),
            ("Odia IndicConformer (~190 MB)", "odia")], self.config.get("model_size", "auto").replace(".en", ""))
        g.add("Speech model", self.model_combo)
        devices = [("Automatic", "auto")]
        if importlib.util.find_spec("openvino_genai"):
            devices.append(("NPU (Intel AI Boost)", "npu"))
        devices += [("GPU (NVIDIA CUDA)", "cuda"), ("CPU", "cpu")]
        self.device_combo = self._combo(devices, self.config.get("device", "auto"))
        g.add("Run on", self.device_combo)
        self.cb_keep_warm = self._switch(g, "Keep model in memory", "keep_model_in_memory", True,
                                         "Eliminates warm-up delays by keeping the speech model hot in memory.")

        self.btn_refresh_stack = _btn("Refresh", small=True, slot=self._refresh_system_stack)
        self.stack_group, _ = self._section(body, "Hardware", action=self.btn_refresh_stack)
        self._refresh_system_stack()

        # Capsule
        g, _ = self._section(body, "Capsule")
        self.theme_combo = self._combo([("Light (website cream)", "light"), ("Dark", "dark")], self.config.get("theme", "light"))
        self.theme_combo.currentIndexChanged.connect(lambda _: self._on_theme_changed(self.theme_combo.currentData()))
        g.add("Theme", self.theme_combo)
        self.capsule_style_combo = self._combo([
            ("Compact (148 px)", "compact"), ("Standard (360 px)", "standard"),
            ("Micro (88 px)", "micro"), ("Dynamic Island (top notch)", "island")], self.config.get("capsule_style", "compact"))
        g.add("Style", self.capsule_style_combo)
        self.island_pos_combo = self._combo([("Top (tucks up when idle)", "top"), ("Bottom", "bottom")],
                                            self.config.get("island_position", "top"))
        g.add("Island position", self.island_pos_combo)

        self.opacity_slider = NoWheelSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(30, 100)  # below 30% the capsule is easy to lose
        self.opacity_slider.setValue(round(float(self.config.get("capsule_opacity", 1.0)) * 100))
        self.opacity_slider.setFixedWidth(176)
        self.opacity_label = _label(f"{self.opacity_slider.value()}%", "RowValue")
        self.opacity_label.setFixedWidth(40)
        self.opacity_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        pair = QWidget()
        pl = QHBoxLayout(pair)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(8)
        pl.addWidget(self.opacity_slider)
        pl.addWidget(self.opacity_label)
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)
        self.opacity_slider.sliderReleased.connect(lambda: save_config(self.config))
        g.add("Opacity", pair)
        self.width_slider, wpair = self._pct_slider("capsule_width_pct")
        g.add("Width", wpair)
        self.height_slider, hpair = self._pct_slider("capsule_height_pct")
        g.add("Height", hpair)
        self.cb_auto_scale = self._switch(g, "Grow on big screens", "capsule_auto_scale", True,
                                          "Scales the capsule up on screens wider than 1920 px, and back down on smaller ones.")
        self.cb_auto_scale.toggled.connect(lambda _: self._on_size_changed())
        self.cb_follow_mouse = self._switch(g, "Follow the cursor", "follow_mouse_screen", True, "Moves the capsule to the screen you are working on.")
        self.cb_realtime = self._switch(g, "Live preview", "realtime_preview", True, "Shows words above the capsule while you speak.")
        self.cb_hide_idle = self._switch(g, "Hide when idle", "hide_capsule_when_idle", False, "No resting pill on the desktop.")
        self.cb_power_alerts = self._switch(g, "Power alerts", "capsule_power_alerts", True,
                                            "Charger plugged in or out, battery low, fully charged.")
        self.cb_device_alerts = self._switch(g, "Device alerts", "capsule_device_alerts", True,
                                             "Headphones, Bluetooth audio and USB drives connecting.")
        self.cb_drop_tray = self._switch(g, "Drop container", "capsule_drop_tray", True,
                                         "Drop files, images or text on the capsule to hold them.")

        # Audio
        self.mic_combo = NoWheelCombo()
        self.mic_combo.setFixedWidth(280)
        self.mic_devices = []
        g, self.active_mic_lbl = self._section(body, "Audio", "Active input: Default System Microphone")
        g.add("Microphone", self.mic_combo)
        self.refresh_devices()
        self.silence_spin = NoWheelSpin()
        self.silence_spin.setRange(0.6, 3.5)
        self.silence_spin.setSingleStep(0.1)
        self.silence_spin.setSuffix(" s")
        self.silence_spin.setValue(float(self.config.get("silence_duration", 1.1)))
        self.silence_spin.setFixedWidth(88)
        self.silence_spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        g.add("Pause before writing", self.silence_spin)
        self.cb_autowrite = self._switch(g, "Write when I pause", "auto_write_on_silence", False)
        self.cb_sound = self._switch(g, "Sound feedback", "sound_feedback", True, "Short tones on start and finish.")
        self.cb_voice_edits = self._switch(g, "Voice edits", "voice_edits", False,
                                           "Select text and say \"make this formal\". Downloads a 1.5 GB writing model once.")
        self.cb_ai_polish = self._switch(g, "AI auto-correct (beta)", "ai_polish", False,
                                         "Off by default: keeps Whisper's exact words without an AI rewrite pass.")

        # Output
        g, _ = self._section(body, "Output")
        self.hotkey_input = HotkeyRecorder(self.config.get("hotkey", "ctrl+shift"))
        self.hotkey_input.setFixedHeight(32)
        self.hotkey_input.setMinimumWidth(168)
        self.hotkey_input.sig_recording.connect(self.sig_hotkey_recording.emit)
        self.hotkey_input.sig_changed.connect(self._on_hotkey_recorded)
        g.add("Hotkey", self.hotkey_input, "Click, then press the shortcut you want.")
        self.trigger_combo = self._combo([
            ("Hold hotkey (double-tap = hands-free)", "push_to_talk"),
            ("Press to start / stop (toggle)", "toggle"),
        ], self.config.get("trigger_mode", "push_to_talk"))
        g.add("Activation mode", self.trigger_combo)
        self.inj_combo = self._combo([("Instant paste (clipboard)", "clipboard"), ("Type-in effect (word by word)", "typein"),
                                      ("Direct typing (no clipboard)", "type")], self.config.get("injection_method", "clipboard"))
        g.add("Writing method", self.inj_combo)
        self.tone_combo = self._combo([
            ("Auto (matches the app)", "auto"), ("Smart Clean", "smart_clean"), ("Developer / Code", "developer"),
            ("Email & Professional", "email"), ("Bullet Points", "bullets"), ("Raw Verbatim", "verbatim")],
            self.config.get("tone_preset", "auto"))
        g.add("Formatting", self.tone_combo)
        self.cb_restore_clip = self._switch(g, "Restore clipboard", "restore_clipboard", True, "Puts your previous clipboard back after pasting.")
        self.cb_cursor_ctx = self._switch(g, "Read text before cursor", "use_cursor_context", True,
                                          "Uses up to 200 characters before the caret for name spelling (never password fields).")
        self.cb_learn_edits = self._switch(g, "Learn from my edits", "learn_from_edits", True,
                                           "Words you fix right after pasting join your vocabulary automatically.")

        # System & updates
        try:
            from .. import __version__
        except (ImportError, ValueError):
            from __init__ import __version__
        g, _ = self._section(body, "System")
        self.cb_autostart = self._switch(g, "Open at login", "autostart", False)
        self.btn_check_update = _btn("Check for Updates", small=True, slot=self._on_check_update_clicked)
        self.btn_install_update = _btn("Install", "primary", True, lambda: self.sig_request_install_update.emit())
        self.btn_install_update.hide()
        buttons = QWidget()
        bl = QHBoxLayout(buttons)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        bl.addWidget(self.btn_install_update)
        bl.addWidget(self.btn_check_update)
        self.update_status_label = g.add(f"Version {__version__}", buttons)
        self.update_status_label.setText("")
        self.ver_label = None
        self._wire_settings()
        return root

    def _refresh_system_stack(self):
        g = self.stack_group
        g.clear()
        stack = hardware.get_system_stack(self.config)
        ok, info, off = "ok", "info", "off"  # QLabel#RowValue[state=...] in the theme stylesheet

        def value(text, state):
            lbl = _label(text, "RowValue")
            lbl.setProperty("state", state)
            return lbl

        g.add("Active engine", _badge(stack["pipeline"], "active"))
        gpu = stack["gpu"]
        g.add("GPU", value(gpu["status"], ok if gpu["is_active"] else (info if gpu["cuda_available"] else off)),
              f"{gpu['name']} · {gpu['vram_gb']} GB VRAM · {gpu['backend']}")
        npu = stack["npu"]
        g.add("NPU", value(npu["status"], ok if npu["is_active"] else (info if npu["detected"] else off)),
              f"{npu['name']} · {npu['acceleration']}")
        cpu = stack["cpu"]
        g.add("CPU", value(cpu["status"], ok if cpu["is_active"] else off),
              f"{cpu['processor']} · {cpu['cores']} cores · {cpu['ram_gb']} GB RAM")

    def _wire_settings(self):
        """Each control writes only its own key the moment it changes. There is no Save button: a Save that
        rewrote every key from this page used to undo choices made meanwhile in the capsule or the tray."""
        self._setting_combos = {
            self.model_combo: "model_size", self.device_combo: "device", self.capsule_style_combo: "capsule_style",
            self.island_pos_combo: "island_position", self.mic_combo: "input_device_index",
            self.inj_combo: "injection_method", self.trigger_combo: "trigger_mode", self.tone_combo: "tone_preset"}
        self._setting_switches = {
            self.cb_keep_warm: "keep_model_in_memory", self.cb_follow_mouse: "follow_mouse_screen",
            self.cb_realtime: "realtime_preview", self.cb_hide_idle: "hide_capsule_when_idle",
            self.cb_power_alerts: "capsule_power_alerts", self.cb_device_alerts: "capsule_device_alerts",
            self.cb_drop_tray: "capsule_drop_tray", self.cb_autowrite: "auto_write_on_silence",
            self.cb_sound: "sound_feedback", self.cb_voice_edits: "voice_edits", self.cb_ai_polish: "ai_polish",
            self.cb_restore_clip: "restore_clipboard", self.cb_cursor_ctx: "use_cursor_context",
            self.cb_learn_edits: "learn_from_edits", self.cb_autostart: "autostart"}
        for combo, key in self._setting_combos.items():
            combo.currentIndexChanged.connect(lambda _i, c=combo, k=key: self._set_setting(k, c.currentData()))
        for sw, key in self._setting_switches.items():
            sw.toggled.connect(lambda on, k=key: self._set_setting(k, on))
        self.silence_spin.valueChanged.connect(lambda v: self._set_setting("silence_duration", float(v)))
        self._settings_timer = QTimer(self)
        self._settings_timer.setSingleShot(True)
        self._settings_timer.setInterval(350)  # one save for a burst, like holding the spin box arrow
        self._settings_timer.timeout.connect(self._apply_settings)

    def _set_setting(self, key: str, value):
        if key == "device":
            if value == "npu" and not hardware.npu_name():
                QMessageBox.warning(self, "No NPU Found",
                                    "This PC has no Intel NPU that Vaani can use. It will keep running on the GPU or CPU.")
            self.config["compute_type"] = "auto" if value == "auto" else ("float16" if value == "cuda" else "int8")
        self.config[key] = value
        self._engine_touched = getattr(self, "_engine_touched", False) or key in ("model_size", "device")
        self._settings_timer.start()

    def _apply_settings(self):
        save_config(self.config)
        self.config_changed.emit(self.config)
        if self._engine_touched:
            self._engine_touched = False
            self._refresh_system_stack()
        self._settings_sub.setText("Saved. Every change applies and saves at once.")
        QTimer.singleShot(2500, lambda: self._settings_sub.setText("Every change applies and saves at once."))

    def sync_settings(self):
        """Show values changed elsewhere (capsule, tray, Model Library) without saving them back."""
        if not hasattr(self, "_setting_combos"):
            return
        for combo, key in self._setting_combos.items():
            if key == "input_device_index":
                continue  # refresh_devices rebuilds the microphone list
            want = str(self.config.get(key, "")).replace(".en", "") if key == "model_size" else self.config.get(key)
            i = combo.findData(want)
            if i >= 0 and i != combo.currentIndex():
                combo.blockSignals(True)
                combo.setCurrentIndex(i)
                combo.blockSignals(False)
        for sw, key in self._setting_switches.items():
            if key in self.config and bool(self.config[key]) != sw.isChecked():
                sw.blockSignals(True)
                sw.setChecked(bool(self.config[key]))
                sw.blockSignals(False)

    def _pct_slider(self, key: str):
        """Slider (50-200 %) with a value label; applies live like the opacity slider."""
        slider = NoWheelSlider(Qt.Orientation.Horizontal)
        slider.setRange(50, 200)
        slider.setValue(int(self.config.get(key, 100)))
        slider.setFixedWidth(176)
        label = _label(f"{slider.value()}%", "RowValue")
        label.setFixedWidth(40)
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(slider)
        lay.addWidget(label)
        slider.valueChanged.connect(lambda v: (label.setText(f"{v}%"), self._on_size_changed()))
        slider.sliderReleased.connect(lambda: save_config(self.config))
        return slider, box

    def _on_size_changed(self):
        self.config["capsule_width_pct"] = self.width_slider.value()
        self.config["capsule_height_pct"] = self.height_slider.value()
        self.config["capsule_auto_scale"] = self.cb_auto_scale.isChecked()
        self.sig_capsule_size.emit()
        if not (self.width_slider.isSliderDown() or self.height_slider.isSliderDown()):
            save_config(self.config)

    def _on_theme_changed(self, theme: str):
        self.config["theme"] = theme
        save_config(self.config)
        self.setStyleSheet(main_qss(theme))
        self.sig_theme.emit(theme)

    def _on_opacity_changed(self, value: int):
        """Applies at once, like the hotkey; no Save needed."""
        self.opacity_label.setText(f"{value}%")
        self.config["capsule_opacity"] = value / 100
        self.sig_capsule_opacity.emit(value / 100)
        if not self.opacity_slider.isSliderDown():  # keyboard / wheel changes have no release
            save_config(self.config)

    def _update_hotkey_badge(self):
        keys = " + ".join(pretty_hotkey(self.config.get("hotkey", "ctrl+shift")))
        self.hotkey_badge_text.setText(f"Hold <b>{keys}</b> to speak")

    def _on_hotkey_recorded(self, combo: str):
        """A recorded shortcut applies at once; no Save needed."""
        self.config["hotkey"] = combo
        save_config(self.config)
        self._update_hotkey_badge()
        self.config_changed.emit(self.config)

    def update_hide_capsule_idle_state(self, hide: bool):
        if hasattr(self, "cb_hide_idle"):
            self.cb_hide_idle.blockSignals(True)
            self.cb_hide_idle.setChecked(hide)
            self.cb_hide_idle.blockSignals(False)

    def refresh_devices(self, active_mic_name: str = ""):
        """Dynamically re-populates the microphone dropdown upon hotplug events."""
        if not hasattr(self, "mic_combo"):
            return
        self.mic_devices = list_microphones()
        self.mic_combo.blockSignals(True)
        self.mic_combo.clear()
        self.mic_combo.addItem("Default system microphone", None)
        curr_dev = self.config.get("input_device_index")
        for dev in self.mic_devices:
            tag = "  ● Active" if active_mic_name and dev["name"] == active_mic_name else ""
            self.mic_combo.addItem(f"{dev['name']}{tag}", dev["index"])
            if curr_dev == dev["index"]:
                self.mic_combo.setCurrentIndex(self.mic_combo.count() - 1)
        self.mic_combo.blockSignals(False)
        if hasattr(self, "active_mic_lbl") and active_mic_name:
            self.active_mic_lbl.setText(f"Active input: {active_mic_name}")
            self.active_mic_lbl.show()

    # -- analytics ----------------------------------------------------------

    def _create_stats_tab(self) -> QWidget:
        root, body, _ = self._page("Analytics", "Your dictation so far, kept on this PC.")
        grid = QGridLayout()
        grid.setSpacing(12)
        self.card_dictations = self._make_metric_card("Dictations", "0")
        self.card_words = self._make_metric_card("Words spoken", "0")
        self.card_saved = self._make_metric_card("Typing time saved", "0 hrs")
        self.card_speed = self._make_metric_card("Effective speed", "150 WPM")
        for i, c in enumerate((self.card_dictations, self.card_words, self.card_saved, self.card_speed)):
            grid.addWidget(c, i // 2, i % 2)
        body.addLayout(grid)

        hw_info = hardware.detect()
        gpu_label = hw_info.get("gpu_name") or ("NVIDIA GPU" if hw_info.get("cuda_ok") else "CPU / Local Hardware")
        g, _ = self._section(body, "Why Vaani")
        for title, detail in [
            ("No usage limits", f"Runs 100% locally on your computer with your {gpu_label}. No monthly minute quotas or tier limits."),
            ("Fully private", "Zero audio or clipboard data is sent over the internet. Everything stays on your local disk."),
            ("Low latency", "Using faster-whisper CTranslate2 float16, dictation finishes in about 100-200 milliseconds."),
            ("Free", "No subscriptions, credit card prompts, or paywalls."),
        ]:
            g.add(title, None, detail)
        return root

    def _make_metric_card(self, title: str, value: str) -> QFrame:
        card = QFrame(self)
        card.setObjectName("Group")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(4)
        lay.addWidget(_label(title, "RowSub"))
        lay.addWidget(_label(value, "MetricValue"))
        return card

    def refresh_stats(self):
        stats = self.db.get_stats()
        self.card_dictations.findChild(QLabel, "MetricValue").setText(str(stats["total_dictations"]))
        self.card_words.findChild(QLabel, "MetricValue").setText(f"{stats['total_words']:,}")
        self.card_saved.findChild(QLabel, "MetricValue").setText(f"{stats['time_saved_hours']} hrs")

    # -- updates ------------------------------------------------------------

    def _on_check_update_clicked(self):
        self.show_update_checking()
        self.sig_request_check_update.emit()

    def show_update_checking(self):
        self.update_status_label.setText("Checking for updates...")
        self.update_status_label.show()
        self.btn_check_update.setEnabled(False)

    def show_update_available(self, version: str, notes: str = ""):
        self.btn_check_update.setEnabled(True)
        self.update_status_label.setText(f"Update available: <b>v{version}</b>")
        self.update_status_label.show()
        self.btn_install_update.setText(f"Install v{version}")
        self.btn_install_update.show()

    def show_up_to_date(self, version: str = ""):
        self.btn_check_update.setEnabled(True)
        self.update_status_label.setText("Vaani is up to date.")
        self.update_status_label.show()
        self.btn_install_update.hide()

    def show_update_error(self, message: str):
        self.btn_check_update.setEnabled(True)
        self.update_status_label.setText(f"Update check: {message}")
        self.update_status_label.show()

    # -- transcribe: Subtitle Studio ----------------------------------------
    # Workspace = media (video, live caption, waveform) over the subtitle table, a settings inspector on the
    # right and an action bar along the bottom. Flow: open -> generate (lines stream in) -> review -> export.

    C_NUM, C_START, C_END, C_SPEAKER, C_CPS, C_TEXT = range(6)

    def _create_tools_tab(self) -> QWidget:
        self._media_path, self._project_path, self._duration, self._missing_media = "", "", 0.0, ""
        self._generating = False  # lines from a run that was replaced (new file, new run) are dropped
        self._cues, self._undo, self._redo = [], [], []  # history holds whole cue lists
        self._dirty, self._playing_row, self._fix, self._gen_started = False, -1, None, 0.0

        self.live_btn = _btn("Live Captions", small=True, slot=self.sig_toggle_live.emit)
        self.live_btn.setToolTip("Captions for whatever your speakers play: YouTube, a podcast, a lecture. Your microphone stays off.")
        root, body, _ = self._page("Transcribe", "Subtitles for podcasts, interviews and videos. Generate, review, export.",
                                   actions=[self.live_btn], scroll=False)

        self._tx_stack = QStackedWidget(root)
        self._tx_stack.addWidget(self._build_drop_zone())
        work = QWidget()
        wv = QVBoxLayout(work)
        wv.setContentsMargins(0, 0, 0, 0)
        wv.setSpacing(12)
        split = _gap_splitter(Qt.Orientation.Horizontal)
        split.addWidget(self._build_workspace())
        self._inspector = self._build_inspector()
        split.addWidget(self._inspector)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        wv.addLayout(self._build_media_bar())
        wv.addWidget(split, 1)
        wv.addWidget(self._build_action_bar())
        self._tx_stack.addWidget(work)
        body.addWidget(self._tx_stack, 1)
        self._tx_work = work

        for keys, slot in (("Ctrl+Space", self._toggle_media_play), ("Alt+Left", lambda: self._seek_by(-5000)),
                           ("Alt+Right", lambda: self._seek_by(5000)), ("Ctrl+[", lambda: self._set_time_from_playhead(0)),
                           ("Ctrl+]", lambda: self._set_time_from_playhead(1)), ("Ctrl+Z", self._undo_edit),
                           ("Ctrl+Y", self._redo_edit), ("Ctrl+Shift+Z", self._redo_edit), ("Ctrl+F", self._find_edit.setFocus),
                           ("Ctrl+K", self._split_cue), ("Ctrl+M", self._merge_cues), ("Ctrl+Return", self._insert_cue),
                           ("Ctrl+R", self._restore_original), ("F8", self._next_problem),
                           ("Ctrl+S", self._save_project), ("Ctrl+E", lambda: self._export_cues("srt"))):
            sc = QShortcut(QKeySequence(keys), work)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)
        sc = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self._cue_table)
        sc.setContext(Qt.ShortcutContext.WidgetShortcut)  # not while a cell editor has focus
        sc.activated.connect(self._delete_cues)
        self._sig_peaks.connect(self._on_peaks)

        self._tx_root = root
        root.setAcceptDrops(True)
        root.installEventFilter(self)
        self._render_cues()
        return root

    def _build_drop_zone(self) -> QFrame:
        zone = QFrame()
        zone.setObjectName("DropZone")
        v = QVBoxLayout(zone)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.setSpacing(6)
        for text, name in (("Drop a podcast, interview or video here", "DropTitle"),
                           ("MP3, M4A, WAV, MP4, MKV and more, in Odia, Hindi and English.\n"
                            "To keep editing, drop an .srt or .vtt file or a saved Vaani project.", "RowSub")):
            lbl = _label(text, name, wrap=True)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            v.addWidget(lbl)
        v.addSpacing(10)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(_btn("Choose File…", "primary", slot=self._choose_file))
        row.addStretch()
        v.addLayout(row)
        return zone

    def _build_workspace(self) -> QSplitter:
        vsplit = _gap_splitter(Qt.Orientation.Vertical)
        vsplit.setMinimumWidth(520)
        vsplit.addWidget(self._build_media_card())
        vsplit.addWidget(self._build_cue_editor())
        vsplit.setStretchFactor(0, 0)
        vsplit.setStretchFactor(1, 1)
        return vsplit

    def _build_media_bar(self) -> QHBoxLayout:
        """What's open, how long, what state; playback switches and the panel toggle on the right."""
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._studio_file_lbl = _label("No media", "RowLabel")
        self._media_meta = _label("", "RowMeta")
        self._studio_status = _badge("Ready", "accent")
        bar.addWidget(self._studio_file_lbl)
        bar.addWidget(self._media_meta)
        bar.addWidget(self._studio_status)
        bar.addStretch()
        self._loop_btn, self._follow = Switch(""), Switch("")
        self._follow.setChecked(True)
        for sw, text, tip in ((self._loop_btn, "Loop line", "Replay the selected line while you fix it"),
                              (self._follow, "Follow", "Keep the line being played in view")):
            lbl = _label(text, "SectionFooter")
            lbl.setBuddy(sw)
            sw.setAccessibleName(text)
            sw.setToolTip(tip)
            bar.addWidget(lbl)
            bar.addWidget(sw)
            bar.addSpacing(8)
        bar.addWidget(_btn("Open…", small=True, slot=self._choose_file))
        self._panel_btn = _btn("Settings", small=True)
        self._panel_btn.setCheckable(True)
        self._panel_btn.setChecked(True)
        self._panel_btn.setToolTip("Show or hide the settings panel")
        self._panel_btn.toggled.connect(lambda on: self._inspector.setVisible(on))
        bar.addWidget(self._panel_btn)
        return bar

    def _build_media_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Group")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)
        top = QHBoxLayout()
        top.setSpacing(16)

        self._media_player = None
        self._audio_out = None
        try:
            from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
            from PyQt6.QtMultimediaWidgets import QVideoWidget
            self._video_widget = QVideoWidget()
            self._video_widget.setStyleSheet("background: #0F141A;")
            self._media_player = QMediaPlayer(self)
            self._audio_out = QAudioOutput(self)
            self._media_player.setAudioOutput(self._audio_out)
            self._media_player.setVideoOutput(self._video_widget)
            self._media_player.positionChanged.connect(self._on_media_pos)
            self._media_player.durationChanged.connect(self._on_media_dur)
            self._media_player.playbackStateChanged.connect(self._on_media_state)
            self._media_player.hasVideoChanged.connect(self._video_widget.setVisible)  # a podcast gets no empty box
            self._video_widget.hide()
        except Exception:
            self._video_widget = _label("Video preview isn't available on this PC. You can still edit and export.", "Empty", wrap=True)
            self._video_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._video_widget.setFixedSize(288, 162)  # 16:9
        top.addWidget(self._video_widget)

        side = QVBoxLayout()
        side.setSpacing(8)
        self._caption = _label("", "CaptionPreview", wrap=True)  # the line on screen now, as the viewer reads it
        self._caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._caption.setMinimumHeight(48)
        side.addWidget(self._caption, 1)

        transport = QHBoxLayout()
        transport.setSpacing(8)
        self._studio_play_btn = _btn("Play", small=True, slot=self._toggle_media_play)
        self._studio_play_btn.setToolTip("Play or pause (Ctrl+Space)")
        self._speed = self._combo([(f"{r:g}×", r) for r in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)], 1.0, width=88)
        self._speed.setAccessibleName("Playback speed")
        self._speed.currentIndexChanged.connect(
            lambda _i: self._media_player and self._media_player.setPlaybackRate(self._speed.currentData()))
        self._studio_time_lbl = _label("00:00 / 00:00", "TimeCode")
        for w in (_btn("−5 s", small=True, slot=lambda: self._seek_by(-5000)), self._studio_play_btn,
                  _btn("+5 s", small=True, slot=lambda: self._seek_by(5000)), self._speed):
            transport.addWidget(w)
        transport.addStretch()
        transport.addWidget(self._studio_time_lbl)
        side.addLayout(transport)

        timing = QHBoxLayout()
        timing.setSpacing(8)
        for text, tip, slot in (("Set Start", "Start the selected line at the playhead (Ctrl+[)", lambda: self._set_time_from_playhead(0)),
                                ("Set End", "End the selected line at the playhead (Ctrl+])", lambda: self._set_time_from_playhead(1)),
                                ("Shift Times…", "Move the selected line and every line after it", self._shift_cues)):
            b = _btn(text, small=True, slot=slot)
            b.setToolTip(tip)
            timing.addWidget(b)
        timing.addStretch()
        side.addLayout(timing)
        top.addLayout(side, 1)
        v.addLayout(top)

        self._wave = Waveform(card)
        self._wave.sig_seek.connect(lambda ms: self._media_player and self._media_player.setPosition(ms))
        v.addWidget(self._wave)
        return card

    def _build_cue_editor(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Group")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 16, 16, 12)
        v.setSpacing(12)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self._find_edit = QLineEdit()
        self._find_edit.setPlaceholderText("Find…")
        self._find_edit.setAccessibleName("Find in subtitles")
        self._find_edit.setToolTip("Enter jumps to the next match (Ctrl+F)")
        self._find_edit.setClearButtonEnabled(True)
        self._find_edit.textChanged.connect(self._count_matches)
        self._find_edit.returnPressed.connect(self._find_next)
        self._replace_edit = QLineEdit()
        self._replace_edit.setPlaceholderText("Replace with…")
        self._replace_edit.setAccessibleName("Replace with")
        self._replace_edit.returnPressed.connect(self._replace_all)
        self._case_btn = _btn("Aa", small=True, slot=self._count_matches)
        self._case_btn.setCheckable(True)
        self._case_btn.setToolTip("Match case")
        self._case_btn.setAccessibleName("Match case")
        tools.addWidget(self._find_edit, 1)
        tools.addWidget(self._replace_edit, 1)
        tools.addWidget(self._case_btn)
        tools.addWidget(_btn("Replace All", small=True, slot=self._replace_all))
        v.addLayout(tools)

        edit = QHBoxLayout()
        edit.setSpacing(8)
        for text, tip, slot in (("Insert", "New line after the selected one (Ctrl+Enter)", self._insert_cue),
                                ("Split", "Split the selected line at the playhead, or in the middle (Ctrl+K)", self._split_cue),
                                ("Merge", "Join the selected lines, or this line and the next (Ctrl+M)", self._merge_cues),
                                ("Restore", "Put back what the model heard for the selected lines (Ctrl+R)", self._restore_original)):
            b = _btn(text, small=True, slot=slot)
            b.setToolTip(tip)
            edit.addWidget(b)
        del_btn = _btn("Delete", "danger", True, self._delete_cues)
        del_btn.setToolTip("Delete the selected lines (Delete). Undo brings them back.")
        edit.addWidget(del_btn)
        edit.addStretch()
        self._undo_btn = _btn("Undo", small=True, slot=self._undo_edit)
        self._redo_btn = _btn("Redo", small=True, slot=self._redo_edit)
        for b, keys in ((self._undo_btn, "Ctrl+Z"), (self._redo_btn, "Ctrl+Y")):
            b.setToolTip(keys)
            edit.addWidget(b)
        v.addLayout(edit)

        # Word corrector: one fixed word offers to fix the same mistake in every other line
        self._fix_bar = QFrame()
        self._fix_bar.setObjectName("FixBar")
        fb = QHBoxLayout(self._fix_bar)
        fb.setContentsMargins(12, 6, 6, 6)
        fb.setSpacing(8)
        self._fix_lbl = _label("", "RowSub", wrap=True)
        self._fix_all_btn = _btn("", small=True, slot=self._fix_everywhere)
        close = _ClearButton(self._fix_bar)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setToolTip("Dismiss")
        close.setAccessibleName("Dismiss")
        close.clicked.connect(self._fix_bar.hide)
        fb.addWidget(self._fix_lbl, 1)
        fb.addWidget(self._fix_all_btn)
        fb.addWidget(close)
        self._fix_bar.hide()
        v.addWidget(self._fix_bar)

        t = QTableWidget(0, 6)
        t.setHorizontalHeaderLabels(["#", "Start", "End", "Speaker", "CPS", "Text"])
        _style_table(t)
        t.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # keyboard editing and the Delete shortcut need focus
        t.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.SelectedClicked
                          | QAbstractItemView.EditTrigger.EditKeyPressed | QAbstractItemView.EditTrigger.AnyKeyPressed)
        t.setWordWrap(True)
        t.setTextElideMode(Qt.TextElideMode.ElideNone)
        t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        t.customContextMenuRequested.connect(self._cue_menu)
        hh = t.horizontalHeader()
        for c in range(self.C_TEXT):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.C_TEXT, QHeaderView.ResizeMode.Stretch)
        # ponytail: re-measures every row on a width change; fine for a few thousand lines
        hh.sectionResized.connect(lambda c, _o, _n: c == self.C_TEXT and t.resizeRowsToContents())
        t.horizontalHeaderItem(self.C_CPS).setToolTip(f"Characters a second. Over {MAX_CPS:.0f} is hard to read in time.")
        t.horizontalHeaderItem(self.C_NUM).setToolTip("A dot marks a line you've edited")
        t.cellClicked.connect(self._seek_to_subtitle_row)
        t.cellChanged.connect(self._on_cue_edited)
        self._cue_table = t
        self._cue_empty = _label("Press Generate Subtitles and lines appear here as they're heard.\n"
                                 "Or press Insert to write subtitles by hand.", "Empty", wrap=True)
        self._cue_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(t, 1)
        v.addWidget(self._cue_empty, 1)
        return card

    def _build_inspector(self) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setFixedWidth(300)
        # wheel and touchpad still scroll it; a bar would push the cards off the page's right edge
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        body = QVBoxLayout(inner)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(20)
        body.setAlignment(Qt.AlignmentFlag.AlignTop)
        area.setWidget(inner)

        g, _ = self._section(body, "Transcription")
        self._studio_lang = self._combo([("Auto-detect", "auto"), ("Mixed: Odia, Eng, Hindi", "or-auto"), ("Odia (ଓଡ଼ିଆ)", "or"),
                                         ("Hindi", "hi"), ("English (India)", "en-in")], "auto", width=168)
        self._studio_out_mode = self._combo([("As spoken", "keep"), ("Translate to English", "en"), ("Odia only", "or")],
                                            "keep", width=168)
        for combo, text in ((self._studio_lang, "Spoken"), (self._studio_out_mode, "Subtitles")):
            combo.setAccessibleName(text)
        g.add("Spoken", self._studio_lang)
        g.add("Subtitles", self._studio_out_mode)
        self._studio_out_mode.setToolTip("As spoken keeps each language in its own script")
        self._model_row_sub = g.add("Model", _btn("Change", small=True, slot=lambda: self.select_page(3)), "")
        fix = _btn("Fix Words", small=True, slot=self._apply_vocabulary)
        fix.setToolTip("Apply names and terms from your Vocabulary to every line")
        g.add("Vocabulary", fix)

        self._quality_group, _ = self._section(body, "Quality checks", action=_btn("Next", small=True, slot=self._next_problem))
        self._speaker_group, _ = self._section(body, "Speakers", action=_btn("Rename…", small=True, slot=self._rename_speaker))

        g, _ = self._section(body, "Project", "Saved projects keep your edits and the model's original text.")
        g.add("Save project", _btn("Save", small=True, slot=self._save_project))
        g.add("Clear all lines", _btn("Clear", "danger", True, self._clear_cues))
        return area

    def _build_action_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("ActionBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 12, 12, 12)
        h.setSpacing(12)
        self._gen_bar = QProgressBar()
        self._gen_bar.setRange(0, 100)
        self._gen_bar.setTextVisible(False)
        self._gen_bar.setFixedWidth(140)
        self._gen_bar.hide()
        self._tx_count = _label("", "RowMeta")
        self._tx_note = _label("", "RowSub")
        self._tx_note.setMinimumWidth(0)
        h.addWidget(self._gen_bar)
        h.addWidget(self._tx_count)
        h.addWidget(self._tx_note, 1)
        self._studio_start_btn = _btn("Generate Subtitles", "primary", slot=self._start_media_transcribe)
        self._export_btn = _btn("Export…", "primary")
        menu = QMenu(self._export_btn)
        for text, fmt in (("Subtitles (.srt)", "srt"), ("Web captions (.vtt)", "vtt"),
                          ("Transcript (.txt)", "txt"), ("Data with speakers (.json)", "json")):
            menu.addAction(text, lambda f=fmt: self._export_cues(f))
        self._export_btn.setMenu(menu)
        self._export_btn.setToolTip("Ctrl+E exports .srt straight away")
        h.addWidget(self._studio_start_btn)
        h.addWidget(self._export_btn)
        return bar

    def eventFilter(self, obj, ev):
        if obj is getattr(self, "_tx_root", None):
            kind = ev.type()
            if kind in (QEvent.Type.DragEnter, QEvent.Type.Drop):
                urls = ev.mimeData().urls() if ev.mimeData().hasUrls() else []
                path = urls[0].toLocalFile() if urls else ""
                if self._file_kind(path):
                    ev.acceptProposedAction()
                    if kind == QEvent.Type.Drop:
                        self._open_file(path)
                    return True
            elif kind == QEvent.Type.Resize:  # a narrow window gives the table the room; Settings brings the panel back
                wide = obj.width() >= 900
                if wide != getattr(self, "_tx_wide", None):
                    self._tx_wide = wide
                    self._panel_btn.setChecked(wide)
        return super().eventFilter(obj, ev)

    # opening: media, subtitle files, projects ------------------------------

    @staticmethod
    def _file_kind(path: str) -> str:
        low = (path or "").lower()
        if low.endswith(PROJECT_EXT):
            return "project"
        ext = os.path.splitext(low)[1]
        return "media" if ext in MEDIA_EXT else "subs" if ext in SUB_EXT else ""

    def _choose_file(self):
        media = " ".join(f"*{e}" for e in sorted(MEDIA_EXT))
        path, _ = QFileDialog.getOpenFileName(
            self, "Open", os.path.dirname(self._media_path),
            f"Media, subtitles or projects ({media} *.srt *.vtt *{PROJECT_EXT});;Audio or video ({media});;"
            f"Subtitles (*.srt *.vtt);;Vaani projects (*{PROJECT_EXT})")
        if path:
            self._open_file(path)

    def _open_file(self, path: str):
        kind = self._file_kind(path)
        if kind == "media":
            self.load_media_file(path)
        elif kind == "subs":
            self._import_subtitles(path)
        elif kind == "project":
            self._open_project(path)

    def _keep_edits(self, question: str) -> bool:
        """True when it's fine to drop the current lines: nothing unsaved, or the user says so."""
        if not self._dirty:
            return True
        return QMessageBox.question(self, "Unsaved Subtitles", question,
                                    QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                    QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Discard

    def _set_media(self, path: str):
        """Point the player and waveform at a file ('' = none) without touching the lines."""
        self._media_path, self._duration, self._missing_media = path, 0.0, ""
        name = os.path.basename(path) if path else "No media"
        self._studio_file_lbl.setText(self._studio_file_lbl.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, 320))
        self._studio_file_lbl.setToolTip(path)
        self._media_meta.setText("")
        self._wave.set_view(peaks=[], duration=0.0, pos=0.0)
        if self._media_player is not None:
            self._media_player.setSource(QUrl.fromLocalFile(path) if path else QUrl())
        if path:
            self._read_waveform(path)
        self._set_studio_badge("Ready" if path else "No media", "accent" if path else "plain")

    def _show_lines(self, cues, note: str = ""):
        self._generating = False  # whatever was running belongs to the lines being replaced
        self._studio_start_btn.setText("Generate Subtitles")
        self._gen_bar.hide()
        self._cues, self._undo, self._redo, self._dirty = cues, [], [], False
        self._fix_bar.hide()
        self._render_cues()
        self._note(note)
        self._tx_stack.setCurrentIndex(1)
        self.select_page(7)

    def load_media_file(self, path: str):
        """Open a video/audio file in the Studio, ready to play and generate subtitles."""
        if not self._keep_edits("You have subtitle edits you haven't saved or exported. Discard them and open the new file?"):
            return
        self._project_path = ""
        self._set_media(path)
        self._show_lines([])

    def _import_subtitles(self, path: str):
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as fh:
                cues = parse_subtitles(fh.read())
        except OSError as e:
            QMessageBox.warning(self, "Couldn't Open Subtitles", f"{e.strerror or e}. Check the file and try again.")
            return
        if not cues:
            QMessageBox.warning(self, "No Subtitles Found", "That file has no timed lines. Choose an .srt or .vtt subtitle file.")
            return
        if not self._keep_edits("Opening these subtitles replaces your unsaved edits. Continue?"):
            return
        stem = os.path.splitext(path)[0]
        media = next((stem + e for e in sorted(MEDIA_EXT) if os.path.exists(stem + e)), "")
        if media or not self._media_path:  # the video next to the subtitles, else keep what's open
            self._set_media(media)
        self._project_path = ""
        self._show_lines(cues, f"Opened {len(cues)} lines from {os.path.basename(path)}.")

    def _open_project(self, path: str):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            cues = [norm_cue(c) for c in data["cues"]]
            media = str(data.get("media") or "")
        except (OSError, ValueError, KeyError, TypeError, IndexError) as e:
            QMessageBox.warning(self, "Couldn't Open Project", f"This isn't a readable Vaani project ({e}).")
            return
        if not self._keep_edits("Opening this project replaces your unsaved edits. Continue?"):
            return
        self._set_media(media if media and os.path.exists(media) else "")
        self._project_path, self._missing_media = path, "" if self._media_path else media
        moved = media and not self._media_path
        self._show_lines(cues, "The media file has moved. Open it again to play along." if moved
                         else f"Opened {os.path.basename(path)}.")

    def _save_project(self):
        if not self._cues and not self._media_path:
            return
        out = self._project_path
        if not out:
            default = (os.path.splitext(self._media_path)[0] if self._media_path else "subtitles") + PROJECT_EXT
            out, _ = QFileDialog.getSaveFileName(self, "Save Project", default, f"Vaani projects (*{PROJECT_EXT})")
            if not out:
                return
            if not out.lower().endswith(PROJECT_EXT):
                out = os.path.splitext(out)[0] + PROJECT_EXT
        tmp = out + ".tmp"
        try:  # write beside it, then swap: a crash mid-write never leaves a half project
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "media": self._media_path or self._missing_media, "cues": self._cues},
                          fh, ensure_ascii=False, indent=1)
            os.replace(tmp, out)
        except OSError as e:
            QMessageBox.warning(self, "Couldn't Save Project", f"{e.strerror or e}. Choose another folder and try again.")
            return
        self._project_path, self._dirty = out, False
        self._update_cue_footer()
        self._note(f"Saved {os.path.basename(out)}.")

    def _read_waveform(self, path: str):
        def work():
            try:
                from faster_whisper.audio import decode_audio
                audio = decode_audio(path, sampling_rate=8000)  # 8 kHz is plenty for a picture
                self._sig_peaks.emit(path, audio_peaks(audio), len(audio) / 8000.0)
            except Exception as e:
                print(f"[Vaani] No waveform for {path}: {e}")
        threading.Thread(target=work, daemon=True).start()

    def _on_peaks(self, path: str, peaks, seconds: float):
        if path == self._media_path:
            if self._duration <= 0:
                self._set_duration(seconds)
            self._wave.set_view(peaks=peaks)

    def _set_duration(self, seconds: float):
        self._duration = seconds
        self._media_meta.setText(_srt_time(seconds)[:8] if seconds else "")
        self._wave.set_view(duration=seconds)
        self._render_cues()  # "runs past the end" depends on it

    # cue table ------------------------------------------------------------

    def _cue_row(self, r: int):
        st, en, text, speaker, orig = self._cues[r]
        issues = cue_issues(self._cues, r, self._duration)
        edited = orig is not None and orig != text
        mono = QFont("Consolas", 10)  # fixed-width digits so times line up
        ink = QColor(COLORS["danger"] if issues else COLORS["text_muted"])
        cells = (f"{r + 1} •" if edited else str(r + 1), _srt_time(st), _srt_time(en), speaker,
                 f"{len(text) / (en - st):.0f}" if en > st else "–", text)
        tip = "\n".join(m for _, m in issues)
        if edited:
            tip = f"Edited. The model heard: {orig}" + (f"\n{tip}" if tip else "")
        for c, val in enumerate(cells):
            it = QTableWidgetItem(val)
            if c in (self.C_NUM, self.C_CPS):
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                it.setForeground(ink)
                it.setToolTip(tip)
            elif c == self.C_SPEAKER:
                it.setForeground(QColor(COLORS["text_muted"]))  # text_secondary is rgba() in dark, which QColor can't parse
            if c in (self.C_START, self.C_END, self.C_CPS):
                it.setFont(mono)
            self._cue_table.setItem(r, c, it)

    def _render_cues(self):
        t = self._cue_table
        t.blockSignals(True)
        t.setRowCount(len(self._cues))
        for r in range(len(self._cues)):
            self._cue_row(r)
        t.blockSignals(False)
        t.resizeRowsToContents()
        self._playing_row = -1
        self._wave.set_view(cues=self._cues)
        if self._media_player is not None:
            self._mark_playing(self._cue_at(self._media_player.position() / 1000.0))
        self._update_cue_footer()

    def _update_cue_footer(self):
        n = len(self._cues)
        counts = {}
        for i in range(n):
            for kind, _ in cue_issues(self._cues, i, self._duration):
                counts[kind] = counts.get(kind, 0) + 1
        bad = sum(1 for i in range(n) if cue_issues(self._cues, i, self._duration))
        self._cue_table.setVisible(bool(n))
        self._cue_empty.setVisible(not n)
        self._tx_count.setText(f"{n} line{'s' if n != 1 else ''}" + (f" · {bad} to check" if bad else "")
                               + (" · Unsaved" if self._dirty else ""))
        self._export_btn.setEnabled(bool(n))
        self._undo_btn.setEnabled(bool(self._undo))
        self._redo_btn.setEnabled(bool(self._redo))
        if self._studio_start_btn.text() != "Generating…":
            self._studio_start_btn.setEnabled(bool(self._media_path))
            self._studio_start_btn.setText("Generate Again" if n else "Generate Subtitles")
            self._studio_start_btn.setObjectName("" if n else "PrimaryButton")  # once there are lines, Export leads
            self._export_btn.setObjectName("PrimaryButton" if n else "")
            for b in (self._studio_start_btn, self._export_btn):
                b.style().unpolish(b)
                b.style().polish(b)

        self._quality_group.clear()
        rows = [(label, counts[kind]) for kind, label in QUALITY if counts.get(kind)]
        for label, k in rows:
            self._quality_group.add(label, _label(str(k), "RowValue"))
        if not rows:
            self._quality_group.add("Every line passes" if n else "No lines yet")
        self._speaker_group.clear()
        speakers = {}
        for c in self._cues:
            if c[3]:
                speakers[c[3]] = speakers.get(c[3], 0) + 1
        for name, k in speakers.items():
            self._speaker_group.add(name, _label(f"{k} line{'s' if k != 1 else ''}", "RowValue"))
        if not speakers:
            self._speaker_group.add("No speakers yet")
        self._model_row_sub.setText(self.config.get("model_size", "") or "Default")
        self._model_row_sub.setVisible(True)

    def _note(self, text: str):
        self._tx_note.setText(text)

    def _commit(self, cues, note: str = ""):
        """Every edit goes through here, so it can be undone."""
        self._undo.append(self._cues)
        del self._undo[:-200]
        self._redo.clear()
        self._cues, self._dirty = cues, True
        self._render_cues()
        self._note(note)

    def _undo_edit(self):
        if self._undo:
            self._redo.append(self._cues)
            self._cues, self._dirty = self._undo.pop(), True
            self._render_cues()
            self._note("Undone.")

    def _redo_edit(self):
        if self._redo:
            self._undo.append(self._cues)
            self._cues, self._dirty = self._redo.pop(), True
            self._render_cues()
            self._note("Redone.")

    def _selected_rows(self):
        rows = sorted({i.row() for i in self._cue_table.selectionModel().selectedRows()})
        cur = self._cue_table.currentRow()
        return rows or ([cur] if 0 <= cur < len(self._cues) else [])

    def _select_row(self, r: int):
        if 0 <= r < self._cue_table.rowCount():
            self._cue_table.selectRow(r)
            self._cue_table.scrollToItem(self._cue_table.item(r, 0), QAbstractItemView.ScrollHint.EnsureVisible)

    def _cue_menu(self, pos):
        r = self._cue_table.rowAt(pos.y())
        if r < 0:
            return
        if r not in self._selected_rows():
            self._select_row(r)
        menu = QMenu(self)
        for text, slot in (("Play From Here", lambda: self._play_from(r)), ("Insert After", self._insert_cue),
                           ("Split", self._split_cue), ("Merge", self._merge_cues), ("Restore Original", self._restore_original),
                           ("Rename Speaker…", lambda: self._rename_speaker(self._cues[r][3])), ("Delete", self._delete_cues)):
            menu.addAction(text, slot)
        menu.exec(self._cue_table.viewport().mapToGlobal(pos))

    def _on_cue_edited(self, r: int, c: int):
        if not 0 <= r < len(self._cues) or c not in (self.C_START, self.C_END, self.C_SPEAKER, self.C_TEXT):
            return
        val = self._cue_table.item(r, c).text()
        old = self._cues[r][2]
        if c == self.C_TEXT:
            field, value = 2, " ".join(val.split())
            if value == old:
                return
        elif c == self.C_SPEAKER:
            field, value = 3, val.strip()
        else:
            field, value = c - self.C_START, parse_ts(val)
            if value is None:
                self._cue_table.blockSignals(True)
                self._cue_row(r)
                self._cue_table.blockSignals(False)
                self._note("Type a time like 00:01:02,500 or 62.5.")
                return

        def apply():  # runs after cellChanged: re-rendering inside it would delete the item whose editor is closing
            if r < len(self._cues):
                new = [list(x) for x in self._cues]
                new[r][field] = value
                self._commit(new)
        QTimer.singleShot(0, apply)
        if c == self.C_TEXT and old:
            cues = [list(x) for x in self._cues]
            cues[r][2] = value
            self._offer_fix(old, value, r, cues)
            self.sig_subtitle_corrected.emit(cues[r][0], cues[r][1], old, value, self._media_path)

    def _offer_fix(self, old: str, text: str, row: int, cues):
        swaps = word_swaps(old, text)
        if not swaps:
            return self._fix_bar.hide()
        was, now = swaps[0]
        case = was.lower() == now.lower()  # a capitalisation fix must not count lines already right
        _, n = replace_in_cues([c for i, c in enumerate(cues) if i != row], was, now, case=case, whole=True)
        if not n:
            return self._fix_bar.hide()
        self._fix = (was, now, case)
        self._fix_lbl.setText(f"“{was}” → “{now}”. {n} more {'place has' if n == 1 else 'places have'} “{was}”.")
        self._fix_all_btn.setText(f"Fix {n} More")
        self._fix_bar.show()

    def _fix_everywhere(self):
        if self._fix:
            was, now, case = self._fix
            new, n = replace_in_cues(self._cues, was, now, case=case, whole=True)
            self._fix_bar.hide()
            if n:
                self._commit(new, f"Fixed “{was}” in {n} more place{'s' if n != 1 else ''}.")

    def _apply_vocabulary(self):
        fmt = TextFormatter([e for e in self.db.get_dictionary() if e.get("category") != "snippet"])
        new = [c[:2] + [fmt._apply_dictionary(c[2])] + c[3:] for c in self._cues]
        n = sum(x[2] != y[2] for x, y in zip(new, self._cues))
        if n:
            self._commit(new, f"Fixed words in {n} line{'s' if n != 1 else ''} from your Vocabulary.")
        else:
            self._note("No Vocabulary words to fix. Add names and terms on the Vocabulary page.")

    def _restore_original(self):
        rows = [r for r in self._selected_rows() if self._cues[r][4] is not None and self._cues[r][4] != self._cues[r][2]]
        if not rows:
            return self._note("The selected lines already match what the model heard, or were split or merged.")
        new = [list(c) for c in self._cues]
        for r in rows:
            new[r][2] = new[r][4]
        self._commit(new, f"Restored {len(rows)} line{'s' if len(rows) != 1 else ''}.")

    def _rename_speaker(self, name: str = ""):
        names = sorted({c[3] for c in self._cues if c[3]})
        if not names:
            return self._note("No speakers to rename. Type a name in the Speaker column.")
        if name not in names:
            name, ok = QInputDialog.getItem(self, "Rename Speaker", "Speaker:", names, 0, False)
            if not ok:
                return
        new_name, ok = QInputDialog.getText(self, "Rename Speaker", f"New name for {name}:", text=name)
        new_name = new_name.strip()
        if ok and new_name and new_name != name:
            self._commit([c[:3] + [new_name if c[3] == name else c[3]] + c[4:] for c in self._cues],
                         f"Renamed {name} to {new_name}.")

    def _insert_cue(self):
        rows = self._selected_rows()
        r = rows[-1] + 1 if rows else len(self._cues)
        pos = self._media_player.position() / 1000.0 if self._media_player is not None else 0.0
        st = self._cues[r - 1][1] if r else pos
        nxt = self._cues[r][0] if r < len(self._cues) else st + 2.0
        new = [list(c) for c in self._cues]
        new.insert(r, [st, max(st + 0.5, min(st + 2.0, nxt)), "", self._cues[r - 1][3] if r else "", None])
        self._commit(new)
        self._select_row(r)
        self._cue_table.setCurrentCell(r, self.C_TEXT)
        self._cue_table.editItem(self._cue_table.item(r, self.C_TEXT))

    def _split_cue(self):
        rows = self._selected_rows()
        if not rows:
            return
        r = rows[0]
        at = self._media_player.position() / 1000.0 if self._media_player is not None else None
        parts = split_cue(self._cues[r], at)
        if not parts:
            return self._note("A one-word line can't be split.")
        self._commit(self._cues[:r] + list(parts) + self._cues[r + 1:], "Split into 2 lines.")
        self._select_row(r)

    def _merge_cues(self):
        rows = self._selected_rows()
        if not rows:
            return
        a, b = (rows[0], rows[-1]) if len(rows) > 1 else (rows[0], rows[0] + 1)
        if b >= len(self._cues):
            return self._note("Select a line that has another after it.")
        span = self._cues[a:b + 1]
        merged = [span[0][0], max(c[1] for c in span), " ".join(c[2] for c in span if c[2].strip()), span[0][3], None]
        self._commit(self._cues[:a] + [merged] + self._cues[b + 1:], f"Merged {len(span)} lines.")
        self._select_row(a)

    def _delete_cues(self):
        rows = set(self._selected_rows())
        if rows:
            n = len(rows)
            self._commit([c for i, c in enumerate(self._cues) if i not in rows],
                         f"Deleted {n} line{'s' if n != 1 else ''}. Ctrl+Z brings {'them' if n != 1 else 'it'} back.")

    def _clear_cues(self):
        if self._cues:
            n = len(self._cues)
            self._commit([], f"Cleared {n} line{'s' if n != 1 else ''}. Ctrl+Z brings them back.")

    def _set_time_from_playhead(self, which: int):
        rows = self._selected_rows()
        if not rows or self._media_player is None:
            return self._note("Select a line, then play to the moment it should " + ("start." if which == 0 else "end."))
        new = [list(c) for c in self._cues]
        new[rows[0]][which] = self._media_player.position() / 1000.0
        self._commit(new)
        self._select_row(rows[0])

    def _shift_cues(self):
        if not self._cues:
            return
        rows = self._selected_rows()
        start = rows[0] if rows else 0
        secs, ok = QInputDialog.getDouble(self, "Shift Times", f"Seconds to move line {start + 1} and every line after it.\n"
                                          "Negative moves them earlier.", 0.0, -36000.0, 36000.0, 2)
        if ok and secs:
            n = len(self._cues) - start
            self._commit(shift_cues(self._cues, secs, start), f"Moved {n} line{'s' if n != 1 else ''} by {secs:+.2f} s.")

    def _next_problem(self):
        bad = [i for i in range(len(self._cues)) if cue_issues(self._cues, i, self._duration)]
        if not bad:
            return self._note("Every line passes the checks.")
        cur = self._cue_table.currentRow()
        r = next((i for i in bad if i > cur), bad[0])
        self._select_row(r)
        self._note(" · ".join(m for _, m in cue_issues(self._cues, r, self._duration)))

    def _matches(self, q: str):
        if self._case_btn.isChecked():
            return [i for i, c in enumerate(self._cues) if q in c[2]]
        return [i for i, c in enumerate(self._cues) if q.lower() in c[2].lower()]

    def _count_matches(self):
        q = self._find_edit.text()
        n = len(self._matches(q)) if q else 0
        self._note((f"{n} line{'s' if n != 1 else ''} match. Enter jumps to the next." if n else "No matches.") if q else "")

    def _find_next(self):
        q = self._find_edit.text()
        m = self._matches(q) if q else []
        if not m:
            return self._note("No matches." if q else "")
        cur = self._cue_table.currentRow()
        self._select_row(next((i for i in m if i > cur), m[0]))

    def _replace_all(self):
        q = self._find_edit.text()
        if not q:
            return self._find_edit.setFocus()
        new, n = replace_in_cues(self._cues, q, self._replace_edit.text(), case=self._case_btn.isChecked())
        if n:
            self._commit(new, f"Replaced {n} match{'es' if n != 1 else ''}.")
        else:
            self._note("No matches.")

    # player ---------------------------------------------------------------

    @staticmethod
    def _fmt_mmss(ms: int) -> str:
        s = max(0, ms // 1000)
        return f"{s // 60:02d}:{s % 60:02d}"

    def _set_studio_badge(self, text: str, kind: str = "accent"):
        self._studio_status.setText(text)
        self._studio_status.setProperty("badge", kind)
        self._studio_status.style().unpolish(self._studio_status)
        self._studio_status.style().polish(self._studio_status)

    def _cue_at(self, sec: float) -> int:
        return next((i for i, c in enumerate(self._cues) if c[0] <= sec < c[1]), -1)

    def _mark_playing(self, r: int):
        """▶ in the # column of the line on screen now; the selection stays the editor's."""
        if r == self._playing_row:
            return
        t = self._cue_table
        t.blockSignals(True)
        if 0 <= self._playing_row < len(self._cues):
            self._cue_row(self._playing_row)
        if 0 <= r < len(self._cues):
            t.item(r, 0).setText("▶")
            t.item(r, 0).setForeground(QColor(COLORS["accent_text"]))
        t.blockSignals(False)
        self._playing_row = r
        self._caption.setText(self._cues[r][2] if r >= 0 else "")
        self._wave.set_view(current=r)
        if r >= 0 and self._follow.isChecked() and t.state() != QAbstractItemView.State.EditingState:
            t.scrollToItem(t.item(r, 0), QAbstractItemView.ScrollHint.EnsureVisible)

    def _seek_by(self, ms: int):
        if self._media_player is not None:
            self._media_player.setPosition(max(0, self._media_player.position() + ms))

    def _on_media_dur(self, dur_ms: int):
        if dur_ms > 0:
            self._set_duration(dur_ms / 1000.0)
        cur = self._media_player.position() if self._media_player else 0
        self._studio_time_lbl.setText(f"{self._fmt_mmss(cur)} / {self._fmt_mmss(dur_ms)}")

    def _on_media_pos(self, pos_ms: int):
        dur = self._media_player.duration() if self._media_player else 0
        self._studio_time_lbl.setText(f"{self._fmt_mmss(pos_ms)} / {self._fmt_mmss(dur)}")
        sec = pos_ms / 1000.0
        self._wave.set_view(pos=sec)
        rows = self._selected_rows()
        if self._loop_btn.isChecked() and rows and sec >= self._cues[rows[0]][1]:
            self._media_player.setPosition(int(self._cues[rows[0]][0] * 1000))
            return
        self._mark_playing(self._cue_at(sec))

    def _on_media_state(self, state):
        from PyQt6.QtMultimedia import QMediaPlayer
        self._studio_play_btn.setText("Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play")

    def _toggle_media_play(self):
        if self._media_player is None or not self._media_path:
            return
        from PyQt6.QtMultimedia import QMediaPlayer
        if self._media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._media_player.pause()
        else:
            self._media_player.play()

    def _play_from(self, row: int):
        self._seek_to_subtitle_row(row, self.C_NUM)
        if self._media_player is not None and self._media_path:
            self._media_player.play()

    def _seek_to_subtitle_row(self, row: int, col: int):
        if self._media_player is not None and 0 <= row < len(self._cues) and col in (self.C_NUM, self.C_START, self.C_END, self.C_CPS):
            self._media_player.setPosition(int(self._cues[row][0] * 1000))

    # generate (lines stream in) -> export ---------------------------------

    def _start_media_transcribe(self):
        if not self._media_path or not self._keep_edits("Generating again replaces the lines you've edited. Continue?"):
            return
        self._show_lines([])
        self._studio_start_btn.setEnabled(False)
        self._studio_start_btn.setText("Generating…")
        self._set_studio_badge("Generating", "active")
        self._gen_started = time.monotonic()
        self._generating = True
        self._gen_bar.setValue(0)
        self._gen_bar.show()
        self._note("Listening… lines appear as they're heard.")
        lang = self._studio_lang.currentData() or "auto"
        out_mode = self._studio_out_mode.currentData() or "keep"
        if out_mode == "en":
            lang = "or-en" if lang in ("or", "or-auto") else "translate"
        elif out_mode == "or":
            lang = "or"
        self.sig_start_file_transcribe.emit(self._media_path, lang)

    def add_subtitle_segment(self, start_s: float, end_s: float, speaker: str, text: str):
        """A line heard while generating: appended live, editable at once."""
        if not self._generating:
            return  # ponytail: from a run the user replaced; a job id from main.py would also catch a restart mid-run
        # ponytail: streamed lines bypass history, so undo resets; an old snapshot would drop lines heard since
        self._undo.clear()
        self._redo.clear()
        text = " ".join(text.split())
        self._cues = self._cues + [[start_s, end_s, text, speaker or "", text]]
        t, n = self._cue_table, len(self._cues)
        t.blockSignals(True)
        t.setRowCount(n)
        for r in range(max(0, n - 2), n):  # the previous line may now overlap this one
            self._cue_row(r)
        t.blockSignals(False)
        t.resizeRowToContents(n - 1)
        if self._follow.isChecked() and t.state() != QAbstractItemView.State.EditingState:
            t.scrollToBottom()
        self._wave.set_view(cues=self._cues)
        if self._duration > 0 and self._gen_started:
            done = min(1.0, end_s / self._duration)
            left = (time.monotonic() - self._gen_started) * (1 - done) / max(done, 0.01)
            self._gen_bar.setValue(int(done * 100))
            self._note(f"Generating… {done:.0%} · about {max(1, round(left / 60))} min left")
        self._update_cue_footer()

    def finish_media_transcribe(self, default_srt_path: str):
        if not self._generating:
            return
        self._generating = False
        self._gen_started = 0.0
        self._gen_bar.hide()
        self._studio_start_btn.setText("Generate Again")
        self._update_cue_footer()
        if default_srt_path:
            self._set_studio_badge("Generated", "active")
            self._studio_status.setToolTip(default_srt_path)
            self._note(f"Done. A copy is saved as {os.path.basename(default_srt_path)}; export again after you edit.")
        else:
            self._set_studio_badge("Stopped", "plain")
            self._note("Stopped before any speech was found.")

    def _export_cues(self, fmt: str):
        if not self._cues:
            return
        ext, kind = {"srt": (".srt", "SubRip subtitles"), "vtt": (".vtt", "WebVTT captions"),
                     "txt": (".txt", "Plain text"), "json": (".json", "JSON")}[fmt]
        default = (os.path.splitext(self._media_path)[0] if self._media_path else "subtitles") + ext
        out, _ = QFileDialog.getSaveFileName(self, "Export Subtitles", default, f"{kind} (*{ext})")
        if not out:
            return
        try:
            with open(out, "w", encoding="utf-8-sig" if fmt == "srt" else "utf-8") as fh:
                fh.write(cues_export(self._cues, fmt))
        except OSError as e:
            QMessageBox.warning(self, "Couldn't Export", f"{e.strerror or e}. Choose another folder and try again.")
            return
        if not self._project_path:
            self._dirty = False
        self._update_cue_footer()
        self._note(f"Exported {os.path.basename(out)}." + (" Save the project to keep these edits in it." if self._dirty else ""))

    def set_live_running(self, running: bool):
        """Mirrors the tray's "Transcribe What's Playing" check mark."""
        self.live_btn.setText("Stop Live Captions" if running else "Live Captions")

    # -- about --------------------------------------------------------------

    def _create_about_tab(self) -> QWidget:
        try:
            from .. import __version__
        except (ImportError, ValueError):
            from __init__ import __version__
        root, body, _ = self._page("About")

        head = QHBoxLayout()
        head.setSpacing(16)
        logo = _logo_label(34)
        if logo:
            head.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        name = _label(f"Vaani  <span style='font-size:13px; font-weight:400;'>v{__version__}</span>", "PageTitle")
        name.setStyleSheet("font-size: 20px;")
        col.addWidget(name)
        col.addWidget(_label("Private, unlimited voice dictation and speech-to-writing. 100% on-device AI.", "PageSub", wrap=True))
        head.addLayout(col, 1)
        body.addLayout(head)

        def link(url):
            return lambda: QDesktopServices.openUrl(QUrl(url))

        g, _ = self._section(
            body, "Developer",
            "Developed by Sachidananda as a private, offline alternative to cloud dictation tools: "
            "dictate into any app with no cloud latency and no subscription.")
        g.add("GitHub repository", _btn("Open", small=True, slot=link("https://github.com/Sachin1724/Vaani")))
        g.add("Project website", _btn("Open", small=True, slot=link("https://sachin1724.github.io/Vaani/")))
        g.add("Contact or report an issue", _btn("Email", "primary", True, link(
            "mailto:fakesachin.19@gmail.com?subject=Vaani%20Feedback%20%2F%20Issue%20Report")))

        g, _ = self._section(body, "Privacy")
        for title, detail in [
            ("On-device transcription", "Every word is processed locally via Whisper and CTranslate2. No audio leaves your device."),
            ("No telemetry", "Vaani has no cloud tracking, analytics, or external API dependencies."),
            ("Local database", "Your history and vocabulary live on this PC in %USERPROFILE%\\.vaani\\openflow.db."),
            ("You are in control", "Delete models, purge vocabulary, or clear your history at any time."),
        ]:
            g.add(title, None, detail)
        return root
