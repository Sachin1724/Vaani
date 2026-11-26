"""
bench_indian_pipeline.py — Compare Baseline vs Whisper-Small-IN vs Whisper-Small-IN + MIRA-IN
=============================================================================================
Evaluates held-out speaker-disjoint AI4Bharat Svarah clips, Raj Shamani x Nikhil Kamath (FO5)
podcast turns, and real Indian proper-noun / pronunciation dictation regression prompts.

Run from repo root: python -m vaani.bench_indian_pipeline
"""

import json
import os
import re
import time
from pathlib import Path

from faster_whisper import WhisperModel
from vaani.engine.local_llm import LocalLLM, accept_polish

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "indian_asr"
WHISPER_IN_DIR = ROOT / "models" / "whisper_small_in_ct2"
MIRA_IN_DIR = ROOT / "models" / "mira_in_ct2_int8"


def norm(s: str) -> str:
    s = (s or "").lower().replace("\u2019", "'")
    s = re.sub(r"[^a-z0-9'\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def edits(a, b) -> int:
    dp = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        ndp = [i]
        for j, y in enumerate(b, 1):
            ndp.append(min(ndp[-1] + 1, dp[j] + 1, dp[j - 1] + (x != y)))
        dp = ndp
    return dp[-1]


def compute_wer(refs, hyps):
    e = sum(edits(norm(r).split(), norm(h).split()) for r, h in zip(refs, hyps))
    n = sum(len(norm(r).split()) for r in refs)
    return 100.0 * e / max(1, n), e, n


def load_jsonl(path: Path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def transcribe_all(model, rows):
    hyps, lats = [], []
    # warm up
    if rows:
        model.transcribe(rows[0]["wav"], language="en", beam_size=4, vad_filter=False)
    for r in rows:
        t0 = time.perf_counter()
        segs, _ = model.transcribe(r["wav"], language="en", beam_size=4, vad_filter=False, condition_on_previous_text=False)
        txt = " ".join(s.text.strip() for s in segs).strip()
        lats.append((time.perf_counter() - t0) * 1000)
        hyps.append(txt)
    med = sorted(lats)[len(lats) // 2] if lats else 0.0
    return hyps, med


def main():
    eval_rows = load_jsonl(DATA_DIR / "eval.jsonl")[:200]
    pod_eval = load_jsonl(DATA_DIR / "podcast_eval.jsonl")[:50]
    refs = [r["text"] for r in eval_rows]

    print("=" * 80)
    print(f"BENCHMARK ON HELD-OUT INDIAN SPEECH ({len(eval_rows)} Svarah + {len(pod_eval)} Raj Shamani x Nikhil Kamath)")
    print("=" * 80)

    # 1. Baseline Whisper-small
    base_asr = WhisperModel("small", device="cuda", compute_type="float16")
    base_hyps, base_ms = transcribe_all(base_asr, eval_rows)
    w, e, n = compute_wer(refs, base_hyps)
    print(f"[1] Baseline Whisper-small        : WER {w:5.2f}% ({e}/{n}) | median {base_ms:.0f} ms")
    pod_base_hyps, _ = transcribe_all(base_asr, pod_eval) if pod_eval else ([], 0)
    del base_asr

    # 2. Whisper-Small-IN (LoRA fine-tuned -> int8 CT2)
    if WHISPER_IN_DIR.exists():
        # int8 CT2 model can run on cuda with int8_float16 or int8, or cpu with int8
        try:
            in_asr = WhisperModel(str(WHISPER_IN_DIR), device="cuda", compute_type="int8_float16")
        except Exception:
            in_asr = WhisperModel(str(WHISPER_IN_DIR), device="cuda", compute_type="int8")
        in_hyps, in_ms = transcribe_all(in_asr, eval_rows)
        w2, e2, n2 = compute_wer(refs, in_hyps)
        print(f"[2] Whisper-Small-IN (Svarah+Pod) : WER {w2:5.2f}% ({e2}/{n2}) | median {in_ms:.0f} ms")
        pod_in_hyps, _ = transcribe_all(in_asr, pod_eval) if pod_eval else ([], 0)
        del in_asr
    else:
        in_hyps, pod_in_hyps = base_hyps, pod_base_hyps
        print("[2] Whisper-Small-IN not found yet; skipping acoustic stage.")

    # 3. + MIRA-IN Phonetic Refiner
    if MIRA_IN_DIR.exists():
        mira = LocalLLM(str(MIRA_IN_DIR), threads=min(8, os.cpu_count() or 4))
        mira.polish("warm up test")
        polished_hyps, mira_ms_list = [], []
        for h in in_hyps:
            t0 = time.perf_counter()
            raw_p = mira.polish(h)
            mira_ms_list.append((time.perf_counter() - t0) * 1000)
            polished_hyps.append(accept_polish(h, raw_p))
        w3, e3, n3 = compute_wer(refs, polished_hyps)
        med_m = sorted(mira_ms_list)[len(mira_ms_list) // 2] if mira_ms_list else 0.0
        print(f"[3] Whisper-Small-IN + MIRA-IN    : WER {w3:5.2f}% ({e3}/{n3}) | MIRA median {med_m:.0f} ms")

        # Held-out entity & pronunciation probe
        print("\n[Entity & Pronunciation Probe (User Reported Errors)]")
        probes = [
            "hello i am satjian and i am using vaani",
            "i was listening to the rajshri mani podcast figuring out",
            "our tool should be more accurate than whsiepr flow",
            "its not good art indian words and pronunciations",
            "can you check my contribution in adult pension use now",
            "we are working for ai for barret in bhubaneshwar",
            "nikhil kamat spoke with kumar mangalam bera",
        ]
        for p in probes:
            out = accept_polish(p, mira.polish(p), max_change=0.45)
            print(f"  IN : {p}\n  OUT: {out}")

    if pod_eval:
        p_refs = [r["text"] for r in pod_eval]
        pw1, _, _ = compute_wer(p_refs, pod_base_hyps)
        pw2, _, _ = compute_wer(p_refs, pod_in_hyps)
        print(f"\n[Held-Out Raj Shamani x Nikhil Kamath Podcast] Base WER {pw1:.2f}% -> Whisper-IN WER {pw2:.2f}%")


if __name__ == "__main__":
    main()
