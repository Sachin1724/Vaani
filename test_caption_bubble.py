"""Caption bubble: a progress notice that updates keeps the text already typed, and the bubble
centres on the capsule's visible body, not its (wider, mid-morph) window."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PyQt6.QtCore import QRectF
from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)
from ui.floating_pill import FloatingPill

pill = FloatingPill()
pill.set_capsule_style("compact")
pill.show()
b = pill.caption_bubble

b.set_text("Downloading small · 34%", merge=False)
b._shown = float(len(b._text))  # fully typed, as after the animation
b.set_text("Downloading small · 35%", merge=False)
assert b._shown >= len("Downloading small · 3"), b._shown  # only the changed digits type in again

pill.setGeometry(100, 500, 400, 60)  # a staged window wider than the body, body on the right
pill._staged, pill._shape = True, QRectF(250, 20, 120, 40)
b.setFixedSize(100, 34)
b._reposition()
assert b.x() + b.width() / 2 == pill.x() + 310, (b.x(), pill.x())
print("caption bubble ok")
