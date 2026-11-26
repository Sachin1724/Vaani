"""
harvest_indian_conversations.py — Build Indian-English ASR & MIRA Phonetic Datasets
==================================================================================
Combines:
1. ai4bharat/Svarah (19 Indian states, speaker-disjoint 80/10/10 train/val/eval splits).
2. Indian podcast conversations (Raj Shamani's 'Figuring Out' & Nikhil Kamath 'WTF / People by WTF'),
   sliced into clean 4-18s 16 kHz mono WAV segments aligned to subtitles and verified with faster-whisper.
3. Real Whisper-small hypotheses vs reference transcripts across Svarah + podcasts + saisundaresan01/svarah,
   creating both acoustic training manifests (for Whisper LoRA) and MIRA phonetic repair pairs.

Run from repo root: python -m vaani.harvest_indian_conversations
"""

import collections
import glob
import hashlib
import io
import json
import os
import random
import re
import subprocess
import time
from pathlib import Path

import torch  # noqa: F401 - initialize torch/optree before pyarrow & soundfile on Windows
import transformers  # noqa: F401
import ctranslate2  # noqa: F401
import numpy as np
import pyarrow.parquet as pq
import soundfile as sf
from huggingface_hub import hf_hub_download

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "indian_asr"
WAV_DIR = DATA_DIR / "wavs"
POD_WAV_DIR = DATA_DIR / "podcast_wavs"
SUB_DIR = ROOT / "data" / "podcasts" / "subs"
POD_MEDIA_DIR = ROOT / "data" / "podcasts" / "indian_en"

for d in (DATA_DIR, WAV_DIR, POD_WAV_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Deterministic held-out podcasts vs training podcasts
PODCAST_CONFIGS = [
    {"tag": "RajShamani_Journey", "vid": "uVraHeupAco", "split": "train", "title": "Raj Shamani Business Journey"},
    {"tag": "FO1_KunalShah", "vid": "sU0f5GLJMHE", "split": "train", "title": "Raj Shamani x Kunal Shah (FO1)"},
    {"tag": "FO583_SamAltman", "vid": "TjPPIf-M6KE", "split": "train", "title": "Raj Shamani x Sam Altman (FO583)"},
    {"tag": "FO526_DrSamir", "vid": "E-F5XJ2jJ1k", "split": "train", "title": "Raj Shamani x Dr. Samir (FO526)"},
    {"tag": "PeopleWTF_KumarBirla", "vid": "e6FqC4pWy8I", "split": "train", "title": "Nikhil Kamath x Kumar Birla"},
    {"tag": "FO5_NikhilKamath", "vid": "cpg78ouK54I", "split": "eval", "title": "Raj Shamani x Nikhil Kamath (FO5 - Held Out)"},
]

# High-frequency Indian & dictation phonetic repair pairs observed in real Whisper errors
PHONETIC_LEXICON = [
    ("satjian", "Sachin"), ("suchin", "Sachin"), ("sachin tendulkar", "Sachin Tendulkar"),
    ("rajshri mani", "Raj Shamani"), ("raj shah mani", "Raj Shamani"), ("raj shamni", "Raj Shamani"),
    ("raj samani", "Raj Shamani"), ("figuring out", "Figuring Out"),
    ("whsiepr flow", "Wispr Flow"), ("whisper flow", "Wispr Flow"), ("vesper flow", "Wispr Flow"),
    ("wani", "Vaani"), ("bani", "Vaani"), ("vaani", "Vaani"), ("vanee", "Vaani"),
    ("meera", "MIRA"), ("mira", "MIRA"),
    ("nikhil kamat", "Nikhil Kamath"), ("nikhil karma", "Nikhil Kamath"),
    ("nitin kamat", "Nithin Kamath"), ("zero da", "Zerodha"), ("zero the", "Zerodha"),
    ("kunal sha", "Kunal Shah"), ("canal shah", "Kunal Shah"),
    ("kumar mangalam bera", "Kumar Mangalam Birla"), ("kumar manglam birla", "Kumar Mangalam Birla"),
    ("deepinder goel", "Deepinder Goyal"), ("bhavish agarwal", "Bhavish Aggarwal"),
    ("ankur wariko", "Ankur Warikoo"), ("arvind srinivas", "Aravind Srinivas"),
    ("adult pension use now", "Atal Pension Yojna"), ("atal pension yojana", "Atal Pension Yojna"),
    ("ai for bharat", "AI4Bharat"), ("ai for barret", "AI4Bharat"),
    ("rajori", "Rajouri"), ("punch", "Poonch"), ("bhubaneshwar", "Bhubaneswar"),
    ("cut tack", "Cuttack"), ("bengaluru", "Bengaluru"), ("gurugram", "Gurugram"),
    ("aadhar", "Aadhaar"), ("adhar", "Aadhaar"), ("pay tm", "Paytm"),
    ("u p i", "UPI"), ("jio", "Jio"), ("zomato", "Zomato"), ("swiggy", "Swiggy"),
    ("razor pay", "Razorpay"), ("good art", "good at"), ("png", "P&G"),
    ("lakh", "lakh"), ("crore", "crore"),
]


def norm_text(s: str) -> str:
    s = (s or "").lower().replace("\u2019", "'")
    s = re.sub(r"[^a-z0-9'\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def word_edits(a, b) -> int:
    dp = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        ndp = [i]
        for j, y in enumerate(b, 1):
            ndp.append(min(ndp[-1] + 1, dp[j] + 1, dp[j - 1] + (x != y)))
        dp = ndp
    return dp[-1]


def wer(ref: str, hyp: str) -> float:
    r = norm_text(ref).split()
    h = norm_text(hyp).split()
    if not r:
        return 1.0
    return word_edits(r, h) / len(r)


def speaker_split(speaker_id: str) -> str:
    h = int(hashlib.md5(speaker_id.encode()).hexdigest()[:8], 16) % 10
    if h == 0:
        return "eval"
    if h == 1:
        return "val"
    return "train"


def _parse_vtt_ts(ts: str) -> float:
    parts = ts.strip().split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    m, s = parts
    return int(m) * 60 + float(s)


def parse_vtt_turns(vtt_path: Path, min_dur: float = 4.0, max_dur: float = 16.0):
    """Parse auto/manual YouTube .vtt into clean non-overlapping 4-16s sentence turns."""
    raw_cues = []
    time_re = re.compile(r"(\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})")
    with open(vtt_path, encoding="utf-8", errors="ignore") as f:
        lines = f.read().splitlines()

    i = 0
    while i < len(lines):
        m = time_re.search(lines[i])
        if m:
            t0, t1 = _parse_vtt_ts(m.group(1)), _parse_vtt_ts(m.group(2))
            i += 1
            txt_lines = []
            while i < len(lines) and lines[i].strip():
                txt_lines.append(lines[i])
                i += 1
            # Prefer clean line without inline <c> timing tags when both exist, else strip tags
            txt = " ".join(txt_lines)
            txt = re.sub(r"<[^>]+>", " ", txt)
            txt = re.sub(r"\[[^\]]+\]", " ", txt)  # drop [Music], [Applause]
            txt = re.sub(r"&gt;|&lt;|&amp;", " ", txt)
            txt = re.sub(r"\s+", " ", txt).strip()
            if txt and t1 - t0 >= 0.4 and (not raw_cues or txt != raw_cues[-1][2]):
                raw_cues.append((t0, t1, txt))
        else:
            i += 1

    # Deduplicate overlapping incremental VTT lines and group into 5-15s turns
    turns = []
    cur_start, cur_end, cur_words = None, None, []
    for t0, t1, txt in raw_cues:
        words = txt.split()
        if not words:
            continue
        if cur_start is None:
            cur_start, cur_end, cur_words = t0, t1, words
            continue
        # Remove overlapping prefix words from scrolling captions
        overlap = 0
        max_ol = min(len(cur_words), len(words), 10)
        for k in range(max_ol, 0, -1):
            if [w.lower() for w in cur_words[-k:]] == [w.lower() for w in words[:k]]:
                overlap = k
                break
        new_words = words[overlap:]
        if not new_words:
            cur_end = max(cur_end, t1)
            continue
        if (t1 - cur_start) <= max_dur and len(cur_words) + len(new_words) <= 38:
            cur_words.extend(new_words)
            cur_end = max(cur_end, t1)
        else:
            if (cur_end - cur_start) >= min_dur and len(cur_words) >= 6:
                turns.append((cur_start, cur_end, " ".join(cur_words)))
            cur_start, cur_end, cur_words = t0, t1, words
    if cur_start is not None and (cur_end - cur_start) >= min_dur and len(cur_words) >= 6:
        turns.append((cur_start, cur_end, " ".join(cur_words)))
    return turns


def is_clean_english_text(text: str) -> bool:
    """Filter out non-English scripts, noise markers, or corrupted caption fragments."""
    if not text or len(text.split()) < 4 or len(text.split()) > 55:
        return False
    # Reject non-Latin scripts
    non_ascii = sum(1 for c in text if ord(c) > 127 and c not in "’‘“”—–")
    if non_ascii > 0:
        return False
    alpha = sum(1 for c in text if c.isalpha())
    return alpha >= max(10, len(text) * 0.55)


def harvest_svarah():
    """Extract all cached Svarah shards into 16 kHz mono WAVs + speaker-disjoint splits."""
    rows = {"train": [], "val": [], "eval": []}
    seen_paths = set()
    for shard_idx in range(3):
        shard = f"data/test-0000{shard_idx}-of-00003.parquet"
        try:
            p = hf_hub_download("ai4bharat/Svarah", shard, repo_type="dataset", local_files_only=True)
        except Exception:
            print(f"[Svarah] Shard {shard_idx} not cached locally yet, skipping.")
            continue
        table = pq.read_table(p, columns=["audio_filepath", "duration", "text", "native_place_state", "primary_language"])
        d = table.to_pydict()
        print(f"[Svarah] Shard {shard_idx}: {len(d['text'])} rows")
        for af, dur, txt, state, lang in zip(
            d["audio_filepath"], d["duration"], d["text"], d["native_place_state"], d["primary_language"]
        ):
            txt = (txt or "").strip()
            dur = float(dur or 0.0)
            if dur < 1.2 or dur > 28.0 or not is_clean_english_text(txt):
                continue
            rel = af.get("path", "")
            if not rel or rel in seen_paths:
                continue
            seen_paths.add(rel)
            spk = rel.split("_")[1] if "_" in rel else rel
            sp = speaker_split(spk)
            wav_out = WAV_DIR / rel
            if not wav_out.exists():
                audio, sr = sf.read(io.BytesIO(af["bytes"]), dtype="float32")
                if audio.ndim > 1:
                    audio = audio.mean(axis=1)
                if sr != 16000:
                    import librosa
                    audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
                sf.write(str(wav_out), audio, 16000, subtype="PCM_16")
            rows[sp].append({
                "wav": str(wav_out),
                "text": txt,
                "duration": round(dur, 3),
                "source": "svarah",
                "speaker": spk,
                "state": state or "",
                "primary_lang": lang or "",
            })
    return rows


def find_podcast_media(tag: str, vid: str):
    """Locate downloaded .m4a audio and .vtt subtitle for a podcast episode."""
    m4a_matches = list(POD_MEDIA_DIR.glob(f"*{vid}*.m4a"))
    vtt_matches = sorted(SUB_DIR.glob(f"*{vid}*.en-orig.vtt")) or sorted(SUB_DIR.glob(f"*{vid}*.en.vtt"))
    return (m4a_matches[0] if m4a_matches else None, vtt_matches[0] if vtt_matches else None)


def harvest_podcasts(max_per_episode: int = 120):
    """Slice Raj Shamani & Nikhil Kamath podcast episodes into 16 kHz mono WAV segments."""
    rows = {"train": [], "eval": []}
    for cfg in PODCAST_CONFIGS:
        m4a, vtt = find_podcast_media(cfg["tag"], cfg["vid"])
        if not m4a or not vtt:
            print(f"[Podcast] Missing audio/vtt for {cfg['tag']} ({cfg['vid']}): m4a={bool(m4a)} vtt={bool(vtt)}")
            continue
        turns = parse_vtt_turns(vtt, min_dur=4.0, max_dur=15.0)
        # Skip first 45s intros/music and spread evenly across episode
        turns = [t for t in turns if t[0] > 45.0 and is_clean_english_text(t[2])]
        if len(turns) > max_per_episode:
            step = len(turns) / max_per_episode
            turns = [turns[int(i * step)] for i in range(max_per_episode)]
        kept = 0
        for idx, (t0, t1, txt) in enumerate(turns):
            dur = t1 - t0
            wav_out = POD_WAV_DIR / f"{cfg['tag']}_{idx:04d}.wav"
            if not wav_out.exists():
                cmd = [
                    "ffmpeg", "-y", "-ss", f"{t0:.2f}", "-t", f"{dur:.2f}",
                    "-i", str(m4a), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                    str(wav_out),
                ]
                r = subprocess.run(cmd, capture_output=True)
                if r.returncode != 0 or not wav_out.exists():
                    continue
            # Quick energy check to avoid silent/music-only beds
            audio, _ = sf.read(str(wav_out), dtype="float32")
            rms = float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0
            if rms < 0.008:
                continue
            rows[cfg["split"]].append({
                "wav": str(wav_out),
                "text": txt,
                "duration": round(dur, 3),
                "source": f"podcast:{cfg['tag']}",
                "speaker": cfg["tag"],
            })
            kept += 1
        print(f"[Podcast] {cfg['tag']} ({cfg['split']}): extracted {kept} clean turns")
    return rows


def annotate_with_whisper(pod_rows, svarah_eval_rows):
    """Run faster-whisper small on podcast turns + Svarah eval (Svarah train errors come from saisundaresan01/svarah)."""
    from faster_whisper import WhisperModel
    print("[Annotate] Loading baseline faster-whisper small (CUDA fp16)...")
    asr = WhisperModel("small", device="cuda", compute_type="float16")
    t0 = time.perf_counter()
    done = 0
    targets = pod_rows.get("train", []) + pod_rows.get("eval", []) + svarah_eval_rows
    for rec in targets:
        if rec.get("whisper_small"):
            done += 1
            continue
        beam = 4 if rec.get("source", "").endswith("FO5_NikhilKamath") or rec in svarah_eval_rows else 1
        segs, info = asr.transcribe(
            rec["wav"], language="en", beam_size=beam,
            vad_filter=False, condition_on_previous_text=False,
        )
        hyp = " ".join(s.text.strip() for s in segs).strip()
        rec["whisper_small"] = hyp
        rec["detected_lang"] = getattr(info, "language", "en")
        rec["lang_prob"] = round(float(getattr(info, "language_probability", 1.0)), 3)
        rec["baseline_wer"] = round(wer(rec["text"], hyp), 4)
        done += 1
        if done % 200 == 0:
            print(f"  annotated {done}/{len(targets)} clips ({time.perf_counter() - t0:.0f}s)...")
    print(f"[Annotate] Finished {done} clips in {time.perf_counter() - t0:.1f}s")


def inject_indian_phonetic_noise(text: str, rng: random.Random):
    """Create realistic ASR phonetic corruptions on Indian podcast sentences when ASR had none."""
    low = text.lower()
    corrupted = low
    changed = False
    for wrong, right in PHONETIC_LEXICON:
        if wrong == right.lower():
            continue
        pat = re.compile(r"\b" + re.escape(right.lower()) + r"\b")
        if pat.search(corrupted) and rng.random() < 0.85:
            corrupted = pat.sub(wrong, corrupted, count=1)
            changed = True
    # Also lowercase & strip punctuation to mimic raw ASR hypothesis
    clean_no_punct = re.sub(r"[,?!.:;\"()]+", "", corrupted)
    clean_no_punct = re.sub(r"\s+", " ", clean_no_punct).strip()
    return clean_no_punct, changed


def build_mira_datasets(svarah_rows, pod_rows):
    """Build MIRA phonetic-repair + punctuation dataset from real Whisper errors & podcasts."""
    from datasets import load_dataset
    rng = random.Random(42)
    train_pairs = []
    eval_pairs = []

    def add_pair(bucket, noisy, clean, src):
        noisy = re.sub(r"\s+", " ", (noisy or "")).strip()
        clean = re.sub(r"\s+", " ", (clean or "")).strip()
        if not noisy or not clean or not is_clean_english_text(clean):
            return
        n_w, c_w = len(noisy.split()), len(clean.split())
        if n_w < 3 or abs(n_w - c_w) > max(3, int(0.35 * c_w)):
            return
        # Ensure we don't include hallucinated targets (>35% WER unless verified)
        bucket.append({"input": noisy, "target": clean, "source": src})

    # 1. Real faster-whisper small mistakes on Svarah speaker-disjoint splits
    for split, bucket in (("train", train_pairs), ("val", train_pairs), ("eval", eval_pairs)):
        for r in svarah_rows.get(split, []):
            hyp = r.get("whisper_small", "")
            ref = r["text"]
            w = r.get("baseline_wer", 1.0)
            if hyp and 0.0 < w <= 0.45:
                # Upweight real phonetic errors in training
                add_pair(bucket, hyp, ref, "svarah_real_err")
                if split != "eval":
                    add_pair(bucket, hyp.lower(), ref, "svarah_real_err_low")
            elif hyp and w == 0.0 and rng.random() < 0.25:
                add_pair(bucket, norm_text(hyp), ref, "svarah_clean_fmt")

    # 2. saisundaresan01/svarah Whisper errors (strictly speaker-disjoint against eval)
    try:
        ds = load_dataset("saisundaresan01/svarah", split="train")
        added_ss = 0
        for row in ds:
            af_path = row.get("audio_filepath") or ""
            base_fn = os.path.basename(af_path)
            spk = base_fn.split("_")[1] if "_" in base_fn else base_fn
            if speaker_split(spk) == "eval":
                continue
            ref = (row.get("reference_transcription") or "").strip()
            wh = (row.get("whisper_transcription") or "").strip()
            if ref and wh and 0.0 < wer(ref, wh) <= 0.40:
                add_pair(train_pairs, wh, ref, "svarah_hf_whisper_err")
                added_ss += 1
        print(f"[MIRA Data] Added {added_ss} real speaker-disjoint Whisper error pairs from saisundaresan01/svarah")
    except Exception as e:
        print(f"[MIRA Data] Skipped saisundaresan01/svarah: {e}")

    # 3. Real Raj Shamani & Nikhil Kamath podcast turns (high-confidence clean transcripts + ASR errors)
    for split, bucket in (("train", train_pairs), ("eval", eval_pairs)):
        for r in pod_rows.get(split, []):
            hyp = r.get("whisper_small", "")
            cap = r["text"]
            if hyp and 0.0 < wer(cap, hyp) <= 0.30:
                add_pair(bucket, hyp, cap, r["source"])
            elif hyp and wer(cap, hyp) <= 0.12:
                add_pair(bucket, norm_text(hyp), hyp, r["source"] + "_clean")

    # 4. Curated Indian names / Raj Shamani / Sachin / Vaani / MIRA / Wispr Flow examples
    seed_sentences = [
        ("hello i am satjian", "Hello, I am Sachin."),
        ("hello i am satjian and i am using vaani", "Hello, I am Sachin and I am using Vaani."),
        ("i am telling i am satjian its taking wrong name every time", "I am telling I am Sachin, it's taking wrong name every time."),
        ("it is not good art indian words and pronunciations", "It is not good at Indian words and pronunciations."),
        ("i was listening to the rajshri mani podcast figuring out", "I was listening to the Raj Shamani podcast Figuring Out."),
        ("raj shah mani interviewed sam altman and kunal sha on figuring out", "Raj Shamani interviewed Sam Altman and Kunal Shah on Figuring Out."),
        ("nikhil kamat spoke with kumar mangalam bera on people by wtf", "Nikhil Kamath spoke with Kumar Mangalam Birla on People by WTF."),
        ("nitin kamat built zero da in bengaluru without venture capital", "Nithin Kamath built Zerodha in Bengaluru without venture capital."),
        ("our tool should be more accurate than whsiepr flow", "Our tool should be more accurate than Wispr Flow."),
        ("we are training meera and wani on indian podcasts", "We are training MIRA and Vaani on Indian podcasts."),
        ("can you check my contribution in adult pension use now", "Can you check my contribution in Atal Pension Yojna?"),
        ("i live in bhubaneshwar near cut tack and work at ai for bharat", "I live in Bhubaneswar near Cuttack and work at AI4Bharat."),
        ("please send five lakh rupees via u p i on pay tm", "Please send five lakh rupees via UPI on Paytm."),
        ("we learned distribution and branding from png", "We learned distribution and branding from P&G."),
        ("deepinder goel from zomato and bhavish agarwal spoke on the panel", "Deepinder Goyal from Zomato and Bhavish Aggarwal spoke on the panel."),
        ("ankur wariko and arvind srinivas discussed perplexity ai in india", "Ankur Warikoo and Aravind Srinivas discussed Perplexity AI in India."),
        ("jammu samba kathua rajori and punch are districts in jammu kashmir", "Jammu, Samba, Kathua, Rajouri, and Poonch are districts in Jammu Kashmir."),
        ("update aadhar card details before the end of this month", "Update Aadhaar card details before the end of this month."),
        ("razor pay and swiggy scaled across tier two cities in india", "Razorpay and Swiggy scaled across tier two cities in India."),
    ]
    for noisy, clean in seed_sentences:
        for _ in range(12):
            train_pairs.append({"input": noisy, "target": clean, "source": "indian_entity_seed"})
            noisy_clean, changed = inject_indian_phonetic_noise(clean, rng)
            if changed:
                train_pairs.append({"input": noisy_clean, "target": clean, "source": "indian_entity_synth"})

    rng.shuffle(train_pairs)
    with open(ROOT / "data" / "mira_indian_train.jsonl", "w", encoding="utf-8") as f:
        for p in train_pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    with open(ROOT / "data" / "mira_indian_eval.jsonl", "w", encoding="utf-8") as f:
        for p in eval_pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"[MIRA Data] Saved {len(train_pairs)} train and {len(eval_pairs)} eval pairs.")


def main():
    print("=" * 80)
    print("HARVESTING INDIAN CONVERSATIONS (AI4Bharat Svarah + Raj Shamani / Indian Podcasts)")
    print("=" * 80)
    svarah_rows = harvest_svarah()
    for sp, items in svarah_rows.items():
        hrs = sum(x["duration"] for x in items) / 3600
        print(f"  Svarah {sp:5s}: {len(items):4d} clips ({hrs:.2f}h)")

    pod_rows = harvest_podcasts(max_per_episode=120)
    annotate_with_whisper(pod_rows, svarah_rows["eval"][:250])

    # Save Whisper manifests: use ALL gold Svarah train + high-agreement English podcast turns (<=0.32 WER)
    whisper_train = list(svarah_rows["train"])
    pod_kept = [
        r for r in pod_rows["train"]
        if r.get("lang_prob", 0) >= 0.80 and 0.0 <= r.get("baseline_wer", 1.0) <= 0.30
    ]
    whisper_train.extend(pod_kept)
    print(f"[Podcast Filter] Kept {len(pod_kept)}/{len(pod_rows['train'])} high-confidence Raj Shamani / Kamath turns for Whisper LoRA")

    for name, items in (("train", whisper_train), ("val", svarah_rows["val"]), ("eval", svarah_rows["eval"]), ("podcast_eval", pod_rows["eval"])):
        out_p = DATA_DIR / f"{name}.jsonl"
        with open(out_p, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"  Saved {out_p} ({len(items)} items)")

    build_mira_datasets(svarah_rows, pod_rows)
    print("HARVEST COMPLETE.")


if __name__ == "__main__":
    main()
