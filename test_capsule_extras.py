"""Device alerts and the capsule's drop container. Run: python test_capsule_extras.py"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PyQt6.QtCore import QMimeData, QUrl, QPointF, Qt
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

app = QApplication(sys.argv)
from engine.device_monitor import DeviceMonitor, device_name
from ui.floating_pill import FloatingPill
from ui.drop_tray import TrayStore


def test_battery_transitions():
    seq = iter([(80, True, -2), (80, True, -2), (79, False, 15000), (15, False, 900), (14, False, 800),
                (14, True, -2), (100, True, -2), (100, True, -2)])
    m = DeviceMonitor(battery_fn=lambda: next(seq), outputs_fn=set, drives_fn=dict)
    titles = [[a["title"] for a in m.poll()] for _ in range(8)]
    assert titles == [[], [], ["On battery"], ["Battery low"], [], ["Charging"], ["Fully charged"], []], titles
    m2 = DeviceMonitor(battery_fn=lambda: (79, False, 15000), outputs_fn=set, drives_fn=dict)
    m2.poll()
    assert m2.poll() == []  # no alert just for starting on battery


def test_peripherals():
    outs = iter([{"Realtek(R) Audio"}, {"Realtek(R) Audio", "CMF Buds 2 Plus"}, {"Realtek(R) Audio"}])
    drives = iter([{}, {"E:": "SanDisk"}, {}])
    m = DeviceMonitor(battery_fn=lambda: None, outputs_fn=lambda: next(outs), drives_fn=lambda: next(drives))
    assert m.poll() == []
    got = m.poll()
    assert [(a["kind"], a["title"], a["sub"]) for a in got] == [
        ("buds", "CMF Buds 2 Plus", "Connected"), ("usb", "SanDisk (E:)", "USB drive connected")], got
    assert [a["sub"] for a in m.poll()] == ["Disconnected", "USB drive removed"]
    assert device_name("Headphones (CMF Buds 2 Plus)") == "CMF Buds 2 Plus"
    from engine.device_monitor import _left
    assert _left(4294967295) == "", "Windows' 'unknown' (0xFFFFFFFF s) right after unplugging is not a time left"
    assert _left(15000) == " · about 4 h 10 min left"


def _drop(widget, mime):
    pos = QPointF(widget.width() / 2, widget.height() / 2)
    enter = QDragEnterEvent(pos.toPoint(), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, enter)
    drop = QDropEvent(pos, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, drop)
    return enter.isAccepted()


def test_drop_tray():
    tmp = Path(tempfile.mkdtemp())
    doc = tmp / "notes.txt"
    doc.write_text("hello", encoding="utf-8")
    store = TrayStore(tmp / "tray.json")
    pill = FloatingPill()
    pill.set_tray_store(store)
    pill.show()
    QTest.qWait(100)
    pill.current_state = "idle"

    m = QMimeData()
    m.setUrls([QUrl.fromLocalFile(str(doc)), QUrl.fromLocalFile(str(tmp))])
    assert _drop(pill, m)
    t = QMimeData()
    t.setText("https://example.com/a")
    _drop(pill, t)
    t2 = QMimeData()
    t2.setText("  Pilot schools get the Odia beta on Friday.  ")
    _drop(pill, t2)
    _drop(pill, m)  # the same files again are not held twice
    assert [i["kind"] for i in store.items] == ["file", "folder", "link", "text"], store.items
    assert pill.indicator.count == 4
    assert pill.tray.isVisible(), "a drop opens the tray"

    QApplication.clipboard().setMimeData(TrayStore.mime([store.items[0]]))
    assert QApplication.clipboard().mimeData().urls()[0].toLocalFile().endswith("notes.txt")

    again = TrayStore(tmp / "tray.json")  # persisted
    assert len(again.items) == 4
    doc.unlink()
    assert [i["kind"] for i in TrayStore(tmp / "tray.json").items] == ["folder", "link", "text"], "moved files are pruned"

    # click on a resting capsule that holds items toggles the tray instead of the settings sheet
    pill.tray.hide_now()
    QTest.mouseClick(pill, Qt.MouseButton.LeftButton)
    assert pill.tray.isVisible() and not pill.is_expanded_hud
    pill.tray.hide_now()
    store.clear()
    assert pill.indicator.count == 0

    pill.set_tray_enabled(False)
    assert not _drop(pill, t2), "drops are refused when the container is off"


def test_island_drop_shares_items():
    from ui.island import IslandPill, CapsuleRouter
    tmp = Path(tempfile.mkdtemp())
    pill, island = FloatingPill(), IslandPill()
    pill.set_tray_store(TrayStore(tmp / "tray.json"))
    CapsuleRouter(pill, island, style="island")
    assert island.tray.store is pill.tray.store
    island.show()
    QTest.qWait(100)
    t = QMimeData()
    t.setText("Dropped on the island")
    assert _drop(island, t)
    assert [i["text"] for i in pill.tray.store.items] == ["Dropped on the island"]
    assert pill.indicator.count == 1, "the classic capsule counts what the island holds"
    assert island.tray.isVisible() and island.tray.pos().y() > island.tray_anchor().top(), "opens below a top island"


def test_dark_is_island_black():
    import ui.floating_pill as fp
    fp.use_theme("dark")
    try:
        assert fp.BG == "#000000" and fp.INK == "#FFFFFF" and "149" not in fp._tone("steel")
    finally:
        fp.use_theme("light")


def test_system_default_skips_bluetooth_headset():
    import engine.audio_recorder as ar
    devs = [{"name": "Microphone (Audio Array AM-C1)", "max_input_channels": 2},
            {"name": "Headset (CMF Buds 2 Plus)", "max_input_channels": 1},
            {"name": r"Headset (@System32\drivers\bthhfenum.sys,#2;%1 Hands-Free%0" "\n;(CMF Buds 2 Plus))",
             "max_input_channels": 1}]
    real_q, real_list = ar.sd.query_devices, ar.list_microphones
    ar.sd.query_devices = lambda *a, kind=None: devs[1] if kind == "input" else devs
    ar.list_microphones = lambda: [{"index": 1, "name": "Headset (CMF Buds 2 Plus)"}, {"index": 0, "name": "Audio Array AM-C1"}]
    try:
        r = ar.AudioRecorder.__new__(ar.AudioRecorder)
        r.device_index = None
        assert r._resolve_device() == 0, "Windows made the earbuds default: dictation keeps the laptop mic"
        r.device_index = 1
        assert r._resolve_device() == 1, "a mic the user picked is always used"
    finally:
        ar.sd.query_devices, ar.list_microphones = real_q, real_list


if __name__ == "__main__":
    test_battery_transitions()
    test_peripherals()
    test_drop_tray()
    test_island_drop_shares_items()
    test_dark_is_island_black()
    test_system_default_skips_bluetooth_headset()
    print("capsule extras ok")
