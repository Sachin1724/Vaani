"""Self-check for the hotkey -> record -> write flow: python -m vaani.test_dictation_flow (from the repo's parent)."""
import os
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication

from vaani.main import OpenFlowApp

_qapp = QApplication.instance() or QApplication([])


class _Recorder:
    def __init__(self):
        self.is_recording = False

    def stop_recording(self):
        self.is_recording = False
        # 1 s of a quiet tone: digital silence is now dropped as "Microphone silent" before any worker runs
        return (0.1 * np.sin(np.arange(16000) * 2 * np.pi * 220 / 16000)).astype(np.float32)


def _app(worker):
    app = OpenFlowApp.__new__(OpenFlowApp)  # skip the Qt/model setup; only the flow logic runs
    QObject.__init__(app)
    errors = []
    app.config = {"trigger_mode": "push_to_talk", "auto_write_on_silence": False}
    app.audio_recorder = _Recorder()
    app.hotkey_mgr = SimpleNamespace(_is_active=True)
    app.bridge = SimpleNamespace(sig_error=SimpleNamespace(emit=errors.append),
                                 sig_stop_recording=SimpleNamespace(emit=lambda: None),
                                 sig_status=SimpleNamespace(emit=lambda *a: None))
    app._is_processing, app._job, app._stream_session = False, 0, None
    app._hands_free, app._swallow_release, app._last_tap = False, False, 0.0
    app._dictation_lock = threading.RLock()

    def start():
        app._dictate_start_monotonic = time.monotonic()
        app.audio_recorder.is_recording = True
    app._start_dictating = start
    app._process_worker = worker
    return app, errors


def test_tap_is_discarded():
    app, errors = _app(lambda *a: None)
    app._on_hotkey_press()
    app._on_hotkey_release()
    assert not app.audio_recorder.is_recording and not app._is_processing and errors == ["Hold to speak · double-tap for hands-free"]


def test_released_while_mic_opened():
    app, errors = _app(lambda *a: None)
    app.hotkey_mgr._is_active = False
    app._on_hotkey_press()
    assert not app.audio_recorder.is_recording and errors == ["Hold to speak"]


def test_hold_writes_and_stuck_worker_times_out():
    hang = threading.Event()
    app, errors = _app(lambda audio, session, job: hang.wait())
    app._on_hotkey_press()
    app._dictate_start_monotonic -= 1  # held for a second
    real_timer = threading.Timer
    # shorten the 0.3 s release tail to 20 ms and the watchdog to 120 ms so both stages are deterministic
    threading.Timer = lambda secs, fn, **kw: real_timer(0.02 if secs < 1 else 0.12, fn)
    try:
        app._on_hotkey_release()
        time.sleep(0.06)
        assert app._is_processing
        time.sleep(0.15)
    finally:
        threading.Timer = real_timer
        hang.set()
    assert not app._is_processing and errors == ["Took too long · try again"] and app._job == 2


if __name__ == "__main__":
    test_tap_is_discarded()
    test_released_while_mic_opened()
    test_hold_writes_and_stuck_worker_times_out()
    print("ok")
