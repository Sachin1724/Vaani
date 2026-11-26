"""
train_whisper_in.py — Fine-tune Whisper-small with PEFT LoRA on Indian English & Podcasts
=========================================================================================
Adapts openai/whisper-small acoustically to Indian accents across 19 states (AI4Bharat Svarah)
+ Raj Shamani ('Figuring Out') & Nikhil Kamath Indian podcast conversations, then exports
both float16 (~485 MB) and int8 (~245 MB) CTranslate2 builds for faster-whisper / Vaani.

Run from repo root: python -m vaani.train_whisper_in
"""

import glob
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import torch
import transformers
import numpy as np
import soundfile as sf
from peft import LoraConfig, get_peft_model
from torch.utils.data import DataLoader, Dataset
from transformers import (
    WhisperFeatureExtractor,
    WhisperForConditionalGeneration,
    WhisperTokenizer,
    get_cosine_schedule_with_warmup,
)

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "indian_asr"
MERGED_DIR = ROOT / "models" / "whisper_small_in_hf"
CT2_DIR = ROOT / "models" / "whisper_small_in_ct2"
STATUS_PATH = DATA_DIR / "training_status.json"


def update_status(**patch):
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    cur = {}
    if STATUS_PATH.exists():
        try:
            cur = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except Exception:
            cur = {}
    cur.update(patch)
    cur["updated_at"] = time.time()
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    tmp.replace(STATUS_PATH)


def find_whisper_small_base() -> str:
    snaps = glob.glob(os.path.expanduser("~/.cache/huggingface/hub/models--openai--whisper-small/snapshots/*"))
    return snaps[0] if snaps else "openai/whisper-small"


def load_jsonl(path: Path):
    items = []
    if not path.exists():
        return items
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                items.append(json.loads(line))
    return items


class WhisperIndianDataset(Dataset):
    def __init__(self, items, feat_ext: WhisperFeatureExtractor, tok: WhisperTokenizer, max_label_len: int = 128):
        self.items = items
        self.feat_ext = feat_ext
        self.tok = tok
        self.max_label_len = max_label_len
        self.prefix_ids = tok.convert_tokens_to_ids(["<|startoftranscript|>", "<|en|>", "<|transcribe|>", "<|notimestamps|>"])
        self.eos_id = tok.eos_token_id

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        rec = self.items[idx]
        audio, sr = sf.read(rec["wav"], dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        feats = self.feat_ext(audio, sampling_rate=16000, return_tensors="pt").input_features[0]
        text_ids = self.tok(rec["text"], add_special_tokens=False).input_ids[: self.max_label_len - len(self.prefix_ids) - 1]
        full_ids = self.prefix_ids + text_ids + [self.eos_id]
        labels = [-100] * len(self.prefix_ids) + text_ids + [self.eos_id]
        return feats, full_ids, labels


def collate_batch(batch, pad_id: int):
    input_features = torch.stack([b[0] for b in batch])
    max_len = max(len(b[1]) for b in batch)
    labels = torch.tensor([b[2] + [-100] * (max_len - len(b[2])) for b in batch], dtype=torch.long)
    return {"input_features": input_features, "labels": labels}


def evaluate_loss(model, val_loader):
    model.eval()
    tot, n = 0.0, 0
    with torch.no_grad():
        for batch in val_loader:
            with torch.amp.autocast("cuda", dtype=torch.bfloat16):
                out = model(
                    input_features=batch["input_features"].cuda(),
                    labels=batch["labels"].cuda(),
                )
            tot += float(out.loss.item())
            n += 1
    model.train()
    return tot / max(1, n)


def main():
    base = find_whisper_small_base()
    print("=" * 80)
    print(f"TRAINING LIGHTWEIGHT WHISPER-SMALL INDIAN ENGLISH ({base})")
    print("=" * 80)

    train_items = load_jsonl(DATA_DIR / "train.jsonl")
    val_items = load_jsonl(DATA_DIR / "val.jsonl")[:240]
    rng = random.Random(42)
    rng.shuffle(train_items)
    # Upweight clean Raj Shamani / Nikhil Kamath podcast turns 2x alongside 2,800 Svarah clips (~10 min run)
    pod_items = [x for x in train_items if str(x.get("source", "")).startswith("podcast:")]
    sv_items = [x for x in train_items if not str(x.get("source", "")).startswith("podcast:")]
    train_items = (pod_items * 2) + sv_items[:2800]
    rng.shuffle(train_items)
    print(f"[Data] train={len(train_items)} clips ({len(pod_items)*2} podcast + {len(train_items)-len(pod_items)*2} svarah), val={len(val_items)} clips")

    feat_ext = WhisperFeatureExtractor.from_pretrained(base)
    tok = WhisperTokenizer.from_pretrained(base, language="english", task="transcribe")

    train_ds = WhisperIndianDataset(train_items, feat_ext, tok)
    val_ds = WhisperIndianDataset(val_items, feat_ext, tok)
    pad_id = tok.pad_token_id or tok.eos_token_id
    train_loader = DataLoader(train_ds, batch_size=4, shuffle=True, num_workers=0, collate_fn=lambda b: collate_batch(b, pad_id))
    val_loader = DataLoader(val_ds, batch_size=4, shuffle=False, num_workers=0, collate_fn=lambda b: collate_batch(b, pad_id))

    model = WhisperForConditionalGeneration.from_pretrained(base, dtype=torch.bfloat16).cuda()
    model.config.forced_decoder_ids = None
    model.config.suppress_tokens = []
    model.config.use_cache = False

    lora_cfg = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "out_proj", "fc1", "fc2"],
    )
    model = get_peft_model(model, lora_cfg)
    model.print_trainable_parameters()

    epochs = 1
    grad_accum = 4
    total_steps = (len(train_loader) * epochs + grad_accum - 1) // grad_accum
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=1e-4, weight_decay=0.01)
    sched = get_cosine_schedule_with_warmup(opt, num_warmup_steps=max(15, total_steps // 10), num_training_steps=total_steps)

    base_val = round(evaluate_loss(model, val_loader), 4)
    print(f"[Val] Baseline pre-train loss: {base_val:.4f}")
    history = []
    val_history = [{"step": 0, "loss": base_val}]
    update_status(
        active_stage="whisper",
        whisper={"state": "training", "step": 0, "total_steps": total_steps, "epoch": 1, "epochs": epochs, "val_loss": base_val, "history": history, "val_history": val_history, "clips": len(train_items)},
    )

    model.train()
    opt.zero_grad()
    t0 = time.perf_counter()
    step = 0

    for ep in range(epochs):
        for b_idx, batch in enumerate(train_loader, 1):
            with torch.amp.autocast("cuda", dtype=torch.bfloat16):
                out = model(
                    input_features=batch["input_features"].cuda(),
                    labels=batch["labels"].cuda(),
                )
                loss = out.loss / grad_accum
            loss.backward()

            if b_idx % grad_accum == 0 or b_idx == len(train_loader):
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
                sched.step()
                opt.zero_grad()
                step += 1
                loss_val = round(float(out.loss.item()), 4)
                elapsed = time.perf_counter() - t0
                history.append({"step": step, "loss": loss_val})
                if step % 10 == 0 or step == total_steps or step == 1:
                    eta = (elapsed / step) * max(0, total_steps - step)
                    print(f"  ep {ep+1}/{epochs} step {step}/{total_steps} | loss {loss_val:.4f} | {elapsed:.0f}s (eta {eta:.0f}s)")
                    update_status(
                        active_stage="whisper",
                        whisper={"state": "training", "step": step, "total_steps": total_steps, "epoch": ep + 1, "epochs": epochs, "loss": loss_val, "val_loss": val_history[-1]["loss"], "elapsed_s": round(elapsed, 1), "eta_s": round(eta, 1), "history": history, "val_history": val_history, "clips": len(train_items)},
                    )

        v_loss = round(evaluate_loss(model, val_loader), 4)
        val_history.append({"step": step, "loss": v_loss})
        print(f"[Val] Epoch {ep+1} loss: {v_loss:.4f} (elapsed {time.perf_counter()-t0:.0f}s)")

    print(f"[Export] Merging LoRA weights -> {MERGED_DIR}")
    lora_backup = MERGED_DIR.parent / "whisper_small_in_lora"
    lora_backup.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(lora_backup))
    merged = model.merge_and_unload()
    # transformers 5.3+ rejects generation keys like suppress_tokens on model.config during save_pretrained
    for k in ("suppress_tokens", "begin_suppress_tokens", "forced_decoder_ids"):
        if hasattr(merged.config, k):
            setattr(merged.config, k, None)
    MERGED_DIR.mkdir(parents=True, exist_ok=True)
    merged.half().save_pretrained(str(MERGED_DIR))
    feat_ext.save_pretrained(str(MERGED_DIR))
    tok.save_pretrained(str(MERGED_DIR))

    print(f"[Quantize] Converting to CTranslate2 int8 -> {CT2_DIR}")
    subprocess.run(
        [
            sys.executable, "-m", "ctranslate2.converters.transformers",
            "--model", str(MERGED_DIR),
            "--output_dir", str(CT2_DIR),
            "--quantization", "int8",
            "--copy_files", "tokenizer.json", "preprocessor_config.json",
            "--force",
        ],
        check=True,
    )
    bin_mb = round(os.path.getsize(CT2_DIR / "model.bin") / 1e6, 1)
    update_status(
        active_stage="done",
        whisper={"state": "done", "step": step, "total_steps": total_steps, "epoch": epochs, "epochs": epochs, "loss": history[-1]["loss"], "val_loss": val_history[-1]["loss"], "elapsed_s": round(time.perf_counter() - t0, 1), "eta_s": 0, "history": history, "val_history": val_history, "clips": len(train_items), "size_mb": bin_mb},
    )
    print(f"SUCCESS: Lightweight Whisper-Small-IN saved -> {CT2_DIR} ({bin_mb:.1f} MB)")


if __name__ == "__main__":
    main()
