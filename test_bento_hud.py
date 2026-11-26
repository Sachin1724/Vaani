"""Bento HUD + capsule peek shelf: tiles emit the right signals, the sheet fits its visible tiles,
and the shelf only appears over a resting capsule. Run: python test_bento_hud.py"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

app = QApplication(sys.argv)
from ui.floating_pill import FloatingPill


class Wheel:  # the bits of QWheelEvent the tiles read
    def __init__(self, dy):
        self._dy = dy

    def angleDelta(self):
        return QPoint(0, self._dy)

    def accept(self):
        pass


def test_bento_hud():
    pill = FloatingPill(current_language="en")
    got = []
    pill.sig_tone_changed.connect(lambda k: got.append(("tone", k)))
    pill.sig_paste_last.connect(lambda: got.append(("paste",)))
    pill.sig_retry_last.connect(lambda: got.append(("retry",)))
    pill.sig_language_changed.connect(lambda c: got.append(("lang", c)))
    pill.expand_to_hud()
    app.processEvents()

    h0 = pill._hud_h
    assert 250 < h0 < 400, h0

    chips = pill.tone_combo
    keys = [chips.itemData(i) for i in range(chips.count())]
    target = keys.index("developer")
    QTest.mouseClick(chips, Qt.MouseButton.LeftButton, pos=chips._chip(target).center().toPoint())
    assert ("tone", "developer") in got, got

    QTest.mouseClick(pill.tile_paste, Qt.MouseButton.LeftButton)
    QTest.mouseClick(pill.tile_retry, Qt.MouseButton.LeftButton)
    assert ("paste",) in got and ("retry",) in got, got

    before = pill.lang_combo.currentIndex()
    pill.lang_combo.wheelEvent(Wheel(-120))
    assert pill.lang_combo.currentIndex() == (before + 1) % pill.lang_combo.count()
    assert any(g[0] == "lang" for g in got), got

    got.clear()
    pill.set_tone("email")  # external sync never echoes back
    assert chips.currentData() == "email" and not got, got

    QTest.qWait(600)  # let the collapse morph land; mid-morph fits are deferred
    pill.show_download(40, "small")
    assert pill._hud_h > h0, (pill._hud_h, h0)
    pill._hide_download_row()
    assert pill._hud_h == h0


def test_peek_shelf():
    pill = FloatingPill()
    pill.show()
    app.processEvents()
    pill.underMouse = lambda: True
    pill.current_state = "idle"
    pill._maybe_peek()
    assert pill._peek.isVisible()

    pill._peek.hide_now()
    pill.current_state = "listening"
    pill._maybe_peek()
    assert not pill._peek.isVisible(), "shelf must stay hidden while dictating"

    pill.current_state = "idle"
    pill._maybe_peek()
    assert pill._peek.isVisible()
    pill.expand_to_hud()
    assert not pill._peek.isVisible(), "opening the HUD hides the shelf"


def test_theme_switch():
    import ui.floating_pill as fp
    pill = FloatingPill()
    pill.set_theme("dark")
    assert fp.BG == fp.THEMES["dark"]["BG"] and fp.THEMES["dark"]["SURFACE"] in pill.btn_transfer.styleSheet()
    pill.set_theme("light")
    assert fp.BG == fp.THEMES["light"]["BG"] and fp.THEMES["light"]["SURFACE"] in pill.btn_transfer.styleSheet()


def test_position_is_stable():
    from PyQt6.QtCore import QPoint
    pill = FloatingPill(capsule_style="standard")  # standard: a click doesn't open the HUD, so drags are testable
    pill.show()
    QTest.qWait(100)
    start = pill.pos()
    QTest.mousePress(pill, Qt.MouseButton.LeftButton, pos=QPoint(40, 10))
    QTest.mouseMove(pill, QPoint(42, 11))  # a click's jitter
    assert pill.pos() == start, (pill.pos(), start)
    QTest.mouseMove(pill, QPoint(80, 10))  # a real drag
    QTest.mouseRelease(pill, Qt.MouseButton.LeftButton, pos=QPoint(80, 10))
    assert pill.pos() != start, "a real drag still moves the capsule"

    pill.expand_to_hud()
    QTest.qWait(600)
    hud = pill.pos()
    QTest.mousePress(pill, Qt.MouseButton.LeftButton, pos=QPoint(180, 300))
    QTest.mouseMove(pill, QPoint(180, 360))
    QTest.mouseRelease(pill, Qt.MouseButton.LeftButton, pos=QPoint(180, 360))
    assert pill.pos() == hud, "the open HUD stays where it opened"
    avail = pill._home_screen().availableGeometry()
    assert pill.geometry().bottom() <= avail.bottom(), "the HUD never sits over the taskbar"


if __name__ == "__main__":
    test_position_is_stable()
    test_bento_hud()
    test_peek_shelf()
    test_theme_switch()
    print("bento hud ok")
