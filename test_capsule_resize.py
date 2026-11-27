"""Capsule smart resize: at any size factor, no visible part is squeezed below its own size,
and everything visible fits inside the capsule. Run: python test_capsule_resize.py"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PyQt6.QtWidgets import QApplication

app = QApplication(sys.argv)
from ui.floating_pill import FloatingPill, ui_font_family

assert ui_font_family() in ("Inter", "General Sans"), ui_font_family()
pill = FloatingPill()
for style in ("standard", "compact", "micro"):
    pill.set_capsule_style(style)
    for f in (0.5, 0.6, 0.8, 1.0, 1.3, 1.6):
        pill.set_capsule_size(f, f, False)
        lay = pill.capsule_layout
        lay.activate()
        need = lay.totalMinimumSize()
        assert pill._cap_w >= need.width() and pill._cap_h >= need.height(), (style, f, need, pill._cap_w)
        for w in (pill.indicator, pill.lang_badge, pill.visualizer, pill.btn_more):
            if w.isVisibleTo(pill.capsule_widget):
                assert w.width() >= w.minimumWidth() and w.height() <= pill._cap_h, (style, f, w)
        shots = os.environ.get("CAPSULE_SHOTS")
        if shots:
            pill.capsule_widget.grab().save(os.path.join(shots, f"{style}_{f}.png"))
pill.set_capsule_size(1.0, 1.0, False)

# resizing at rest previews the size, then settles back: never the listening look, and the rest timer runs
pill.current_state = "idle"
pill.set_capsule_size(1.2, 1.2, False)
assert not pill.visualizer.isVisibleTo(pill.capsule_widget) and pill.btn_stop.isHidden()
assert pill.hide_timer.isActive()
print("capsule resize ok")
