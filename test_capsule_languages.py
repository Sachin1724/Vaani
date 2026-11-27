"""Capsule languages: Indian and world languages are listed under headings that can't be picked, the
badge opens the list in one click, and every code reaches Whisper as itself (multilingual model)."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)
from ui.floating_pill import FloatingPill
from engine.transcriber import _whisper_args, resolve_model_name

codes = [c for c, _ in FloatingPill.LANGS if c]
for want in ("ta", "te", "bn", "mr", "gu", "kn", "ml", "pa", "ur", "id", "vi", "sw"):
    assert want in codes, want
    assert _whisper_args(want) == {"language": want, "task": "transcribe"}
    assert resolve_model_name("base", want) == "base" and resolve_model_name("small-in", want) == "small"

pill = FloatingPill(current_language="ta")
combo = pill.lang_combo
assert combo.currentData() == "ta"
heads = [i for i in range(combo.count()) if combo.itemData(i) is None]
assert [combo.itemText(i) for i in heads] == ["India", "World"] and all(i in combo._disabled for i in heads)

opened = []
combo.open_menu = lambda anchor=None: opened.append(anchor)  # the real menu blocks in exec()
press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(3, 3), QPointF(3, 3), Qt.MouseButton.LeftButton,
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
release = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(3, 3), QPointF(3, 3), Qt.MouseButton.LeftButton,
                      Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
assert pill.eventFilter(pill.lang_badge, press) and not opened  # swallowed: no panel, no drag
assert pill.eventFilter(pill.lang_badge, release) and opened == [pill.lang_badge]

got = []
pill.sig_language_changed.connect(got.append)
combo.setCurrentIndex([combo.itemData(i) for i in range(combo.count())].index("ml"))
assert got == ["ml"] and pill.lang_badge.text() == "ML"
print("capsule languages ok")
