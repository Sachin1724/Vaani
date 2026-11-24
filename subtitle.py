"""Batch subtitles: every audio/video file in a folder (or the files given) -> a .srt beside it.

    python -m vaani.subtitle D:\\podcasts --lang or
    python -m vaani.subtitle episode1.mp3 episode2.mp4

Uses the model and device from the app's settings; --lang overrides the language (or = Odia, auto, en...).
Files that already have a .srt are skipped, so a run that stopped halfway can simply be started again.
"""
import argparse
import os
import sys
import time

from .config import load_config
from .engine import hardware
from .engine.meeting import MeetingSession, srt_text
from .engine.transcriber import Transcriber

MEDIA = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".webm", ".mp4", ".mkv", ".mov")


def media_files(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                yield from (os.path.join(root, n) for n in sorted(names) if n.lower().endswith(MEDIA))
        else:
            yield p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Write .srt subtitles for audio and video files.")
    ap.add_argument("paths", nargs="+", help="files or folders")
    ap.add_argument("--lang", help="language code, e.g. or (Odia), auto, en; default: the app's setting")
    ap.add_argument("--force", action="store_true", help="redo files that already have a .srt")
    args = ap.parse_args(argv)

    cfg = load_config()
    if args.lang:
        cfg["language"] = args.lang
    rt = hardware.resolve(cfg)
    t = Transcriber(model_size=rt["model_size"], device=rt["device"], compute_type=rt["compute_type"],
                    language=cfg.get("language", "auto"))
    files = list(media_files(args.paths))
    failed = 0
    for i, path in enumerate(files, 1):
        out = os.path.splitext(path)[0] + ".srt"
        if os.path.exists(out) and not args.force:
            print(f"[{i}/{len(files)}] skip (has .srt): {path}")
            continue
        start = time.perf_counter()
        try:
            lines = MeetingSession(t, cfg.get("language", "auto"), lambda *_: None, mic=False).transcribe_file(path)
        except Exception as e:  # one broken file must not stop a folder of a hundred
            failed += 1
            print(f"[{i}/{len(files)}] FAILED {path}: {e}")
            continue
        with open(out, "w", encoding="utf-8-sig") as fh:
            fh.write(srt_text(lines))
        print(f"[{i}/{len(files)}] {len(lines)} lines in {time.perf_counter() - start:.0f}s -> {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
