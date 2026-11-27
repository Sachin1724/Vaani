"""Compare live mic recordings or saved .wav files across Whisper small (auto), small-in (Indian EN), and MIRA.

Usage:
  python -m vaani.bench_multilingual_compare --record --seconds 6 --truth "aaj hum Vaani test kar rahe hain"
  python -m vaani.bench_multilingual_compare --dir vaani/data/my_clips
"""
import argparse
import glob
import os
import time
import wave
from pathlib import Path
import numpy as np

from vaani.engine import model_manager
from vaani.engine.transcriber import Transcriber
from vaani.engine import local_llm


def _wer(ref: str, hyp: str) -> float:
    r, h = ref.lower().split(), hyp.lower().split()
    if not r:
        return 0.0 if not h else 1.0
    dp = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        dp[i][0] = i
    for j in range(len(h) + 1):
        dp[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            cost = 0 if r[i - 1] == h[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost)
    return round(100.0 * dp[-1][-1] / len(r), 2)


def _load_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    if sr != 16000 and len(pcm) > 0:
        x_old = np.linspace(0, 1, len(pcm))
        x_new = np.linspace(0, 1, int(len(pcm) * 16000 / sr))
        pcm = np.interp(x_new, x_old, pcm).astype(np.float32)
    return pcm


def _record_clip(seconds: float) -> np.ndarray:
    import sounddevice as sd
    print(f"Recording {seconds:.1f}s at 16 kHz... Speak now!")
    audio = sd.rec(int(seconds * 16000), samplerate=16000, channels=1, dtype="float32")
    sd.wait()
    print("Recording complete.")
    return audio.reshape(-1)


def compare_audio(audio: np.ndarray, truth: str = "", label: str = "clip"):
    print(f"\n=== {label} ({len(audio) / 16000:.2f}s) ===")
    if truth:
        print(f"Truth   : {truth}")

    mira_path = model_manager.cached_path("mira")
    mira = local_llm.LocalLLM(mira_path, threads=4) if mira_path else None

    for model_name, lang in [("small", "auto"), ("small-in", "en-in")]:
        if not model_manager.cached_path(model_name):
            print(f"[{model_name} / {lang}] not cached locally — skipping")
            continue
        t = Transcriber(model_size=model_name, device="cpu", compute_type="int8", language=lang)
        t0 = time.perf_counter()
        text, _, lat_ms, det_lang = t.transcribe(audio, language=lang)
        total_ms = int((time.perf_counter() - t0) * 1000)
        wer_str = f" | WER {_wer(truth, text)}%" if truth else ""
        print(f"[{model_name} / {lang} -> det={det_lang}] ({total_ms} ms{wer_str}): {text}")
        if mira and text:
            polished = mira.polish(text, "benchmark", "", [])
            p_wer = f" | WER {_wer(truth, polished)}%" if truth else ""
            print(f"  + MIRA ({p_wer.strip(' |')}): {polished}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true", help="Record a live mic clip and compare outputs")
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--truth", type=str, default="")
    ap.add_argument("--dir", type=str, default="", help="Directory of .wav files (optional matching .txt truth)")
    ap.add_argument("--wav", type=str, default="", help="Single .wav file to evaluate")
    args = ap.parse_args()

    if args.record:
        audio = _record_clip(args.seconds)
        compare_audio(audio, args.truth, "live_mic")
    elif args.wav:
        txt = Path(args.wav).with_suffix(".txt")
        truth = args.truth or (txt.read_text(encoding="utf-8").strip() if txt.exists() else "")
        compare_audio(_load_wav(args.wav), truth, Path(args.wav).name)
    elif args.dir:
        wavs = sorted(glob.glob(os.path.join(args.dir, "*.wav")))
        if not wavs:
            print(f"No .wav files in {args.dir}")
            return
        for w in wavs:
            txt = Path(w).with_suffix(".txt")
            truth = txt.read_text(encoding="utf-8").strip() if txt.exists() else ""
            compare_audio(_load_wav(w), truth, Path(w).name)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
