"""The capsule's container: files, folders, images and text dropped on the capsule are held here
(~/.vaani/tray.json) and can be dragged back out into any app or copied with one click."""
import json
import os
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QPoint, QPointF, QRectF, QUrl, QMimeData, QVariantAnimation
from PyQt6.QtGui import (QPainter, QColor, QPen, QFont, QFontMetricsF, QPixmap, QImage, QDrag, QPainterPath, QBrush,
                         QTransform)
from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QApplication, QScrollArea,
                             QFrame, QSizePolicy)

from . import floating_pill as fp  # the same module object as the capsule's, so theme tokens match
try:
    from ..config import CONFIG_DIR
except (ImportError, ValueError):
    from config import CONFIG_DIR

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_ROWS = 5  # taller lists scroll inside the tray


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _is_link(text: str) -> bool:
    return text.startswith(("http://", "https://")) and not any(c.isspace() for c in text)


class TrayStore:
    """Held items as dicts: {id, kind: file|folder|image|text|link, name, path | text}. Saved on every change."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else CONFIG_DIR / "tray.json"
        self.image_dir = self.path.parent / "tray_images"
        self.items = []
        self.listeners = []
        try:
            self.items = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.items = []
        # files moved or deleted since they were dropped are dropped from the tray
        self.items = [it for it in self.items if "path" not in it or os.path.exists(it["path"])]

    def _changed(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            print(f"[Vaani] Couldn't save the container: {e}")
        for fn in self.listeners:
            fn()

    def _new_id(self) -> int:
        return max((it["id"] for it in self.items), default=0) + 1

    def _add(self, item) -> bool:
        key = item.get("path") or item.get("text")
        if any((it.get("path") or it.get("text")) == key for it in self.items):
            return False  # already held
        item["id"] = self._new_id()
        self.items.append(item)
        return True

    def add_mime(self, mime: QMimeData) -> int:
        """Hold everything a drop carries; returns how many new items were added."""
        added = 0
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    p = os.path.normpath(url.toLocalFile())
                    if not os.path.exists(p):
                        continue
                    kind = "folder" if os.path.isdir(p) else "image" if Path(p).suffix.lower() in IMAGE_EXT else "file"
                    added += self._add({"kind": kind, "name": os.path.basename(p.rstrip("\\/")) or p, "path": p})
                elif url.scheme() in ("http", "https"):
                    added += self._add({"kind": "link", "name": url.toString(), "text": url.toString()})
        elif mime.hasImage():
            img = mime.imageData()
            if isinstance(img, QImage) and not img.isNull():
                self.image_dir.mkdir(parents=True, exist_ok=True)
                p = str(self.image_dir / time.strftime("Image %Y-%m-%d %H%M%S.png"))
                if img.save(p, "PNG"):
                    added += self._add({"kind": "image", "name": os.path.basename(p), "path": p})
        elif mime.hasText() and mime.text().strip():
            t = mime.text().strip()
            added += self._add({"kind": "link" if _is_link(t) else "text", "name": " ".join(t.split())[:120], "text": t})
        if added:
            self._changed()
        return added

    def remove(self, item_id: int):
        self.items = [it for it in self.items if it["id"] != item_id]
        self._changed()

    def clear(self):
        self.items = []
        self._changed()

    @staticmethod
    def mime(items) -> QMimeData:
        """Files go out as file URLs (Explorer, chat apps and browsers take the real file); text as text."""
        m = QMimeData()
        urls = [QUrl.fromLocalFile(it["path"]) for it in items if "path" in it]
        urls += [QUrl(it["text"]) for it in items if it["kind"] == "link"]
        texts = [it["text"] for it in items if "text" in it]
        if urls:
            m.setUrls(urls)
        if texts:
            m.setText("\n".join(texts))
        return m

    @staticmethod
    def meta(it) -> str:
        if it["kind"] == "folder":
            try:
                return f"Folder · {len(os.listdir(it['path']))} items"
            except OSError:
                return "Folder"
        if it["kind"] in ("file", "image"):
            try:
                size = _size(os.path.getsize(it["path"]))
            except OSError:
                size = ""
            where = os.path.basename(os.path.dirname(it["path"]))
            return " · ".join(x for x in ("Image" if it["kind"] == "image" else "", size, where) if x)
        if it["kind"] == "link":
            return "Link"
        return f"Text · {len(it['text'])} characters"


class _ItemIcon(QWidget):
    """36 px tile: an image's own thumbnail, a file's extension, or a folder / text / link glyph."""

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.setFixedSize(36, 36)
        self.item = item
        self.thumb = None
        if item["kind"] == "image":
            pm = QPixmap(item["path"])
            if not pm.isNull():
                self.thumb = pm.scaled(72, 72, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                       Qt.TransformationMode.SmoothTransformation)

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.PenStyle.NoPen)
        if self.thumb is not None:
            # the image as the brush of an antialiased rounded rect: a clip path would cut a hard, stepped edge
            brush = QBrush(self.thumb)
            t = QTransform()
            t.scale(0.5, 0.5)  # 72 px source drawn in the 36 px tile (sharp on 2x screens)
            t.translate(-(self.thumb.width() - 72) / 2, -(self.thumb.height() - 72) / 2)
            brush.setTransform(t)
            p.setBrush(brush)
            p.drawRoundedRect(r, 9, 9)
            return
        p.setBrush(QColor(fp.SURFACE_HI))
        p.drawRoundedRect(r, 9, 9)
        kind, c = self.item["kind"], r.center()
        p.setPen(QPen(fp._fg(), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        if kind == "file":
            ext = Path(self.item["path"]).suffix.lstrip(".").upper()[:4] or "FILE"
            p.setFont(fp.ui_font(fp.ui_font_family(), 7, QFont.Weight.Bold))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, ext)
        elif kind == "folder":
            p.drawRoundedRect(QRectF(c.x() - 9, c.y() - 5, 18, 13), 2.5, 2.5)
            p.drawLine(QPointF(c.x() - 9, c.y() - 5), QPointF(c.x() - 7, c.y() - 8))
            p.drawLine(QPointF(c.x() - 7, c.y() - 8), QPointF(c.x() - 2, c.y() - 8))
        elif kind == "link":
            p.drawRoundedRect(QRectF(c.x() - 9, c.y() - 3.5, 10, 7), 3.5, 3.5)
            p.drawRoundedRect(QRectF(c.x() - 1, c.y() - 3.5, 10, 7), 3.5, 3.5)
        else:  # text
            for dy, w in ((-5, 14), (0, 14), (5, 9)):
                p.drawLine(QPointF(c.x() - 7, c.y() + dy), QPointF(c.x() - 7 + w, c.y() + dy))


class _Row(QWidget):
    """One held item. Press and drag to drop it into another app; Copy puts it on the clipboard."""

    def __init__(self, tray, item):
        super().__init__()
        self.tray, self.item = tray, item
        self._press = None
        self.setFixedHeight(52)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip(item.get("path") or item.get("text", ""))
        self._hover = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 6, 0)
        lay.setSpacing(10)
        lay.addWidget(_ItemIcon(item, self))
        text = QVBoxLayout()
        text.setSpacing(1)
        family = fp.ui_font_family()
        name = QLabel(self)
        name.setFont(fp.ui_font(family, 9, QFont.Weight.Medium))
        cut = Qt.TextElideMode.ElideMiddle if "path" in item else Qt.TextElideMode.ElideRight  # keep a file's extension
        name.setText(QFontMetricsF(name.font()).elidedText(item["name"], cut, 176))
        meta = QLabel(TrayStore.meta(item), self)
        meta.setFont(fp.ui_font(family, 8))
        meta.setObjectName("Meta")
        for lbl in (name, meta):  # may shrink below their text: a long name never pushes the row past the tray
            lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            text.addWidget(lbl)
        lay.addLayout(text, 1)
        self.btn_copy = QPushButton("Copy", self)
        self.btn_copy.setObjectName("Chip")
        self.btn_copy.setFixedSize(58, 28)
        self.btn_copy.clicked.connect(self._copy)
        lay.addWidget(self.btn_copy)
        rm = QPushButton("✕", self)
        rm.setObjectName("Ghost")
        rm.setFixedSize(26, 26)
        rm.setToolTip("Remove from the container")
        rm.clicked.connect(lambda: tray.store.remove(item["id"]))
        lay.addWidget(rm)
        for b in (self.btn_copy, rm):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def enterEvent(self, event):
        self._hover = True
        self.update()

    def leaveEvent(self, a0):
        self._hover = False
        self.update()

    def paintEvent(self, a0):
        """The card, painted antialiased: a stylesheet border-radius is drawn with stepped corners."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(fp.SURFACE_HI if self._hover else fp.SURFACE))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)

    def _copy(self):
        QApplication.clipboard().setMimeData(TrayStore.mime([self.item]))
        self.btn_copy.setText("Copied")
        QTimer.singleShot(1400, lambda: self.btn_copy.setText("Copy"))

    def mousePressEvent(self, a0):
        if a0.button() == Qt.MouseButton.LeftButton:
            self._press = a0.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseReleaseEvent(self, a0):
        self._press = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mouseMoveEvent(self, a0):
        if self._press is None or (a0.position().toPoint() - self._press).manhattanLength() < QApplication.startDragDistance():
            return
        self._press = None
        drag = QDrag(self)
        drag.setMimeData(TrayStore.mime([self.item]))
        pm = self.grab()
        drag.setPixmap(pm.scaledToHeight(40, Qt.TransformationMode.SmoothTransformation))
        drag.setHotSpot(QPoint(20, 20))
        self.tray.dragging = True
        drag.exec(Qt.DropAction.CopyAction)  # copy: the item stays held after it lands
        self.tray.dragging = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if not self.tray.underMouse():
            self.tray.hide_timer.start()


class TrayPanel(QWidget):
    """Grows up out of the capsule: opacity plus a short rise, 240 ms in and 180 ms out, never a bounce."""
    W = 380

    def __init__(self, pill, store: TrayStore):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        self.pill, self.store = pill, store
        self.dragging = False
        self._a = 0.0
        self._target = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(fp.SNAP_EASE)
        self._anim.valueChanged.connect(self._on_tick)
        self._anim.finished.connect(self._on_done)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(450)  # grace to cross the gap between capsule and tray
        self.hide_timer.timeout.connect(self._maybe_close)

        family = fp.ui_font_family()
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.title = QLabel("Container", self)
        self.title.setFont(fp.ui_font(family, 10, QFont.Weight.DemiBold))
        self.count = QLabel(self)
        self.count.setFont(fp.ui_font(family, 9))
        self.count.setObjectName("Meta")
        self.btn_all = QPushButton("Copy all", self)
        self.btn_all.setObjectName("Primary")
        self.btn_all.clicked.connect(self._copy_all)
        self.btn_clear = QPushButton("Clear", self)
        self.btn_clear.setObjectName("Quiet")
        self.btn_clear.clicked.connect(store.clear)
        self.btn_settings = QPushButton("Settings", self)
        self.btn_settings.setObjectName("Ghost")
        self.btn_settings.setToolTip("Open the capsule settings")
        self.btn_settings.clicked.connect(self._open_settings)
        head.addWidget(self.title)
        head.addWidget(self.count, 1)
        for b in (self.btn_all, self.btn_clear, self.btn_settings):
            b.setFixedHeight(28)
            b.setFont(fp.ui_font(family, 9, QFont.Weight.DemiBold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            head.addWidget(b)
        root.addLayout(head)
        self.list_scroll = QScrollArea(self)
        self.list_scroll.setWidgetResizable(True)
        self.list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list = QWidget()
        self.list.setObjectName("List")
        self.rows = QVBoxLayout(self.list)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(6)
        self.list_scroll.setWidget(self.list)
        root.addWidget(self.list_scroll)
        self.empty = QLabel("Nothing held. Drop files, folders, images or text on the capsule to keep them here.", self)
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setObjectName("Empty")
        self.empty.setFont(fp.ui_font(family, 9))
        root.addWidget(self.empty)
        store.listeners.append(self._on_store_changed)

    # ---------- content ----------

    def refresh(self):
        """Rebuild rows and colours from the store and the current theme."""
        while self.rows.count():
            w = self.rows.takeAt(0).widget()
            if w:
                w.deleteLater()
        for it in reversed(self.store.items):  # newest first
            self.rows.addWidget(_Row(self, it))
        n = len(self.store.items)
        self.count.setText("1 item" if n == 1 else f"{n} items")
        self.empty.setVisible(n == 0)
        self.list_scroll.setVisible(n > 0)
        self.btn_all.setEnabled(n > 0)
        self.btn_clear.setEnabled(n > 0)
        self.list_scroll.setFixedHeight(min(n, MAX_ROWS) * 58 - 6 if n else 0)
        self.setStyleSheet(f"""
            QLabel {{ color: {fp.INK}; background: transparent; }}
            QLabel#Meta, QLabel#Empty {{ color: {fp.FG_DIM}; }}
            QLabel#Empty {{ border: 1.5px dashed {fp.FG_FAINT}; border-radius: 12px; padding: 22px 16px; }}
            QWidget#List, QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}
            QPushButton {{ border: none; border-radius: 14px; padding: 0 12px; }}
            QPushButton#Primary {{ background: {fp.INK}; color: {fp.BG}; }}
            QPushButton#Primary:hover {{ background: {fp.INK_HOVER}; }}
            QPushButton#Primary:disabled {{ background: {fp.FG_FAINT}; }}
            QPushButton#Chip {{ background: {fp.BG}; color: {fp.INK}; padding: 0; }}
            QPushButton#Chip:hover {{ background: {fp.SURFACE_PRESS}; }}
            QPushButton#Quiet {{ background: {fp.SURFACE}; color: {fp.INK}; }}
            QPushButton#Quiet:hover {{ background: {fp.SURFACE_HI}; }}
            QPushButton#Quiet:disabled {{ color: {fp.FG_FAINT}; }}
            QPushButton#Ghost {{ background: transparent; color: {fp.FG_DIM}; padding: 0 6px; }}
            QPushButton#Ghost:hover {{ background: {fp.TINT}; color: {fp.INK}; }}
            QScrollBar:vertical {{ background: transparent; width: 6px; }}
            QScrollBar::handle:vertical {{ background: {fp.FG_FAINT}; border-radius: 3px; min-height: 24px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
        """)
        self.setFixedWidth(self.W)
        self.layout().activate()
        self.setFixedHeight(self.layout().sizeHint().height())
        self.place()
        self.update()

    def _on_store_changed(self):
        if self.isVisible():
            self.refresh()
            if not self.store.items:
                self.hide_timer.start()

    def _copy_all(self):
        QApplication.clipboard().setMimeData(TrayStore.mime(self.store.items))
        self.btn_all.setText("Copied")
        QTimer.singleShot(1400, lambda: self.btn_all.setText("Copy all"))

    def _open_settings(self):
        self.hide_now()
        self.pill.expand_to_hud()

    # ---------- open / close ----------

    def open(self):
        self.hide_timer.stop()
        if not self.isVisible():
            self._a = 0.0
            self.refresh()
            self.setWindowOpacity(0.0)
            self.show()
            fp._force_topmost(self)
        self._run(1.0, 240)

    def toggle(self):
        self.close_animated() if self.isVisible() and self._target > 0.5 else self.open()

    def close_animated(self):
        if self.isVisible():
            self._run(0.0, 180)

    def hide_now(self):
        self.hide_timer.stop()
        self._anim.stop()
        self._a = self._target = 0.0
        self.hide()

    def _maybe_close(self):
        if self.dragging or self.underMouse() or self.pill.underMouse():
            return
        self.close_animated()

    def _run(self, target: float, ms: int):
        self._target = target
        self._anim.stop()
        if not fp.animations_enabled():
            self._on_tick(target)
            self._on_done()
            return
        self._anim.setDuration(ms)
        self._anim.setStartValue(self._a)
        self._anim.setEndValue(float(target))
        self._anim.start()

    def _on_tick(self, v):
        self._a = float(v)
        self.setWindowOpacity(max(0.0, min(1.0, self._a)) * self.pill.opacity)
        self.place()

    def _on_done(self):
        if self._a <= 0.01:
            self.hide()

    def place(self):
        """Above the classic capsule; below the island when it sits at the top of the screen."""
        a = self.pill.tray_anchor()
        shift = 10.0 * (1.0 - self._a) if fp.animations_enabled() else 0.0  # grows out of the capsule
        x = round(a.center().x() - self.W / 2)
        screen = QApplication.screenAt(a.center()) or QApplication.primaryScreen()
        if screen:
            r = screen.availableGeometry()
            x = max(r.left() + 8, min(x, r.right() - self.W - 8))
        if self.pill.tray_below:
            self.move(x, round(a.bottom() + 10 - shift))
        else:
            self.move(x, round(a.top() - self.height() - 10 + shift))

    # ---------- events ----------

    def enterEvent(self, event):
        self.hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, a0):
        if not self.dragging:
            self.hide_timer.start()
        super().leaveEvent(a0)

    def dragEnterEvent(self, a0):
        m = a0.mimeData()
        if a0.source() is None and (m.hasUrls() or m.hasText() or m.hasImage()):  # not our own rows
            a0.acceptProposedAction()

    def dropEvent(self, a0):
        self.store.add_mime(a0.mimeData())
        a0.acceptProposedAction()

    def paintEvent(self, a0):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(fp._fg(40), 1.0))
        p.setBrush(fp._bgc(250))
        p.drawRoundedRect(r, 20, 20)
