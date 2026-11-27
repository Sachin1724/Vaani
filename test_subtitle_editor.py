"""Subtitle Studio: cue helpers, and the editor's edit / undo / word-fix / project flow on a real window."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from vaani.database import Database
from vaani.config import load_config
import vaani.ui.main_window as mw
from vaani.ui.main_window import (MainWindow, parse_ts, cue_issues, cues_export, replace_in_cues, word_swaps,
                                  split_cue, shift_cues, parse_subtitles, audio_peaks, norm_cue)

mw.save_config = lambda *_: None
app = QApplication.instance() or QApplication([])


def test_helpers():
    assert parse_ts("00:01:02,500") == 62.5 and parse_ts("1:02.5") == 62.5 and parse_ts("62.5") == 62.5
    assert parse_ts("1:75") is None and parse_ts("abc") is None

    cues = [norm_cue(c) for c in ([0.0, 1.0, "x" * 30, "Person 1"], [0.5, 3.0, "ok line", "Person 2"], [3.0, 3.2, ""])]
    kinds = lambda i, d=0.0: {k for k, _ in cue_issues(cues, i, d)}
    assert {"fast", "overlap"} <= kinds(0) and kinds(1) == set() and "empty" in kinds(2)
    assert "past_end" in kinds(1, 2.0)

    srt = cues_export(cues, "srt")
    assert srt.startswith("1\n00:00:00,000 --> 00:00:01,000\n") and "\n3\n" not in srt  # the empty line is dropped
    vtt = cues_export(cues, "vtt")
    assert vtt.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n<v Person 1>")
    assert cues_export(cues, "txt").startswith("Person 1: xxx")
    assert '"speaker": "Person 2"' in cues_export(cues, "json")
    back = parse_subtitles(vtt)  # a WebVTT export reads back with its speakers
    assert [(c[0], c[1], c[3]) for c in back] == [(0.0, 1.0, "Person 1"), (0.5, 3.0, "Person 2")]
    assert parse_subtitles(srt)[1][2] == "ok line"
    assert audio_peaks([0, 0.5, -1, 0.25], buckets=2) == [0.5, 1.0] and audio_peaks([]) == []

    # whole-word matching must not stop inside an Odia word (its vowel signs aren't \w)
    new, n = replace_in_cues([[0, 1, "କର କରି", "", None]], "କର", "X", whole=True)
    assert (n, new[0][2]) == (1, "X କରି")
    assert replace_in_cues([[0, 1, "Odisa is odisa.", "", None]], "odisa", "Odisha", whole=True)[1] == 2

    assert word_swaps("we went to odisa today", "we went to Odisha today") == [("odisa", "Odisha")]
    assert word_swaps("use pi torch now", "use PyTorch now") == [("pi torch", "PyTorch")]

    a, b = split_cue([0.0, 4.0, "one two three four", "Ana", "x"], at=1.0)
    assert a == [0.0, 1.0, "one two", "Ana", None] and b == [1.0, 4.0, "three four", "Ana", None]
    assert split_cue([0.0, 1.0, "word", "", "word"]) is None
    assert shift_cues([[1, 2, "a", "", "a"], [3, 4, "b", "", "b"]], -3.5, 1) == [[1, 2, "a", "", "a"], [0.0, 0.5, "b", "", "b"]]


def test_editor_flow():
    w = MainWindow(Database(":memory:"), dict(load_config()))
    w._media_path = "clip.mp4"
    w._generating = True  # as after Generate Subtitles; lines from no run are dropped
    learned = []
    w.sig_subtitle_corrected.connect(lambda *a: learned.append(a))
    for i, text in enumerate(("we flew to odisa", "odisa is green", "the end")):
        w.add_subtitle_segment(i * 2.0, i * 2.0 + 1.8, "Person 1", text)
    t = w._cue_table
    assert t.rowCount() == 3

    t.item(0, w.C_TEXT).setText("we flew to Odisha")  # what a user's cell edit does
    app.processEvents()
    assert w._cues[0][2] == "we flew to Odisha" and learned and learned[0][2] == "we flew to odisa"
    assert not w._fix_bar.isHidden() and w._fix[:2] == ("odisa", "Odisha")
    w._fix_everywhere()
    assert w._cues[1][2] == "Odisha is green"

    w._undo_edit()
    assert w._cues[1][2] == "odisa is green"
    w._redo_edit()
    assert w._cues[1][2] == "Odisha is green"
    t.selectRow(1)
    w._restore_original()  # back to what the model heard
    assert w._cues[1][2] == "odisa is green"
    w._undo_edit()

    t.item(2, w.C_START).setText("nonsense")  # a bad time snaps back
    app.processEvents()
    assert w._cues[2][0] == 4.0 and t.item(2, w.C_START).text() == "00:00:04,000"

    t.selectRow(0)
    w._merge_cues()
    assert len(w._cues) == 2 and w._cues[0] == [0.0, 3.8, "we flew to Odisha Odisha is green", "Person 1", None]
    t.selectRow(1)
    w._delete_cues()
    assert len(w._cues) == 1
    w._undo_edit()
    assert len(w._cues) == 2
    w._clear_cues()
    assert w._cues == []
    w._undo_edit()
    assert len(w._cues) == 2

    w._show_lines([])  # a new file: the old run's late lines must not land in it
    w.add_subtitle_segment(9.0, 10.0, "Person 1", "late line")
    assert w._cues == []


def test_project_round_trip(tmp_path):
    w = MainWindow(Database(":memory:"), dict(load_config()))
    w._generating = True
    w.add_subtitle_segment(0.0, 1.5, "Person 1", "hello there friend")
    w._dirty = True
    path = str(tmp_path / "talk.vaani.json")
    w._project_path = path  # skip the save dialog
    w._save_project()
    assert not w._dirty
    w2 = MainWindow(Database(":memory:"), dict(load_config()))
    w2._open_project(path)
    assert w2._cues == [[0.0, 1.5, "hello there friend", "Person 1", "hello there friend"]]
    assert w2._tx_stack.currentIndex() == 1


if __name__ == "__main__":
    import pathlib
    import tempfile
    test_helpers()
    test_editor_flow()
    test_project_round_trip(pathlib.Path(tempfile.mkdtemp()))
    print("ok")
