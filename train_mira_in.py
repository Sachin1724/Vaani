"""
train_mira_in.py — Fine-tune Lightweight MIRA (Qwen2.5-0.5B-Instruct LoRA -> CT2 int8)
=======================================================================================
Trains MIRA on real Whisper-small errors from AI4Bharat Svarah + Raj Shamani / Nikhil Kamath
Indian English podcasts + Indian entity phonetic repair pairs.
Writes live progress telemetry to vaani/data/indian_asr/training_status.json for the dashboard.

Run from repo root: python -m vaani.train_mira_in
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
from peft import LoraConfig, TaskType, get_peft_model
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, get_cosine_schedule_with_warmup

from vaani.engine.local_llm import MIRA_SYSTEM, POLISH_SYSTEM, polish_prompt

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "data" / "mira_indian_train.jsonl"
MERGED_DIR = ROOT / "models" / "mira_in_merged"
CT2_DIR = ROOT / "models" / "mira_in_ct2_int8"
STATUS_PATH = ROOT / "data" / "indian_asr" / "training_status.json"


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


def find_qwen05_base() -> str:
    snaps = glob.glob(os.path.expanduser("~/.cache/huggingface/hub/models--Qwen--Qwen2.5-0.5B-Instruct/snapshots/*"))
    return snaps[0] if snaps else "Qwen/Qwen2.5-0.5B-Instruct"


class MiraIndianDataset(Dataset):
    def __init__(self, jsonl_path: Path, tok, max_len: int = 192, max_samples: int = 2500):
        rows = []
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
        rng = random.Random(42)
        rng.shuffle(rows)
        rows = rows[:max_samples]

        self.examples = []
        im_end = "<|im_end|>"
        for i, row in enumerate(rows):
            raw_in = row["input"].strip()
            target = row["target"].strip()
            if not raw_in or not target:
                continue
            if i % 5 != 0:
                sys_msg = POLISH_SYSTEM
                usr_msg = polish_prompt(raw_in)
            else:
                sys_msg = MIRA_SYSTEM.format(style="smart_clean")
                usr_msg = f"Raw speech: {raw_in}"

            msgs = [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": usr_msg},
            ]
            prompt_str = tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
            p_ids = tok(prompt_str, add_special_tokens=False)["input_ids"]
            a_ids = tok(target + im_end + "\n", add_special_tokens=False)["input_ids"]
            ids = (p_ids + a_ids)[:max_len]
            labels = ([-100] * len(p_ids) + a_ids)[:max_len]
            if any(x != -100 for x in labels):
                self.examples.append((ids, labels))

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        return self.examples[idx]


def collate_fn(batch, pad_id: int):
    max_len = max(len(x) for x, _ in batch)
    ids = torch.tensor([x + [pad_id] * (max_len - len(x)) for x, _ in batch], dtype=torch.long)
    labels = torch.tensor([y + [-100] * (max_len - len(y)) for _, y in batch], dtype=torch.long)
    mask = torch.tensor([[1] * len(x) + [0] * (max_len - len(x)) for x, _ in batch], dtype=torch.long)
    return {"input_ids": ids, "attention_mask": mask, "labels": labels}


def main():
    base = find_qwen05_base()
    print("=" * 80)
    print(f"TRAINING LIGHTWEIGHT MIRA INDIAN PHONETIC REFINER ({base})")
    print("=" * 80)

    tok = AutoTokenizer.from_pretrained(base)
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token

    ds = MiraIndianDataset(DATA_PATH, tok, max_len=192, max_samples=2500)
    print(f"[Data] {len(ds)} encoded training pairs")
    # batch_size=4 + grad_accum=4 keeps VRAM around ~3.3 GB (fits alongside live Vaani app) at ~3x speed
    loader = DataLoader(ds, batch_size=4, shuffle=True, collate_fn=lambda b: collate_fn(b, tok.pad_token_id))

    model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.bfloat16).cuda()
    lora_cfg = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    )
    model = get_peft_model(model, lora_cfg)
    model.print_trainable_parameters()

    epochs = 2
    grad_accum = 4
    total_steps = (len(loader) * epochs + grad_accum - 1) // grad_accum
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=2e-4, weight_decay=0.01)
    sched = get_cosine_schedule_with_warmup(opt, num_warmup_steps=max(10, total_steps // 12), num_training_steps=total_steps)

    history = []
    update_status(
        active_stage="mira",
        mira={"state": "training", "step": 0, "total_steps": total_steps, "epoch": 1, "epochs": epochs, "history": history, "pairs": len(ds)},
    )

    model.train()
    opt.zero_grad()
    t0 = time.perf_counter()
    step = 0

    for ep in range(epochs):
        for b_idx, batch in enumerate(loader, 1):
            out = model(
                input_ids=batch["input_ids"].cuda(),
                attention_mask=batch["attention_mask"].cuda(),
                labels=batch["labels"].cuda(),
            )
            loss = out.loss / grad_accum
            loss.backward()

            if b_idx % grad_accum == 0 or b_idx == len(loader):
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
                        active_stage="mira",
                        mira={"state": "training", "step": step, "total_steps": total_steps, "epoch": ep + 1, "epochs": epochs, "loss": loss_val, "elapsed_s": round(elapsed, 1), "eta_s": round(eta, 1), "history": history, "pairs": len(ds)},
                    )

    print(f"[Export] Merging LoRA weights -> {MERGED_DIR}")
    merged = model.merge_and_unload()
    MERGED_DIR.mkdir(parents=True, exist_ok=True)
    merged.half().save_pretrained(str(MERGED_DIR))
    tok.save_pretrained(str(MERGED_DIR))

    print(f"[Quantize] Converting to CTranslate2 int8 -> {CT2_DIR}")
    subprocess.run(
        [
            sys.executable, "-m", "ctranslate2.converters.transformers",
            "--model", str(MERGED_DIR),
            "--output_dir", str(CT2_DIR),
            "--quantization", "int8",
            "--copy_files", "tokenizer.json", "tokenizer_config.json", "generation_config.json",
            "--force",
        ],
        check=True,
    )
    bin_mb = round(os.path.getsize(CT2_DIR / "model.bin") / 1e6, 1)
    update_status(
        active_stage="mira_done",
        mira={"state": "done", "step": step, "total_steps": total_steps, "epoch": epochs, "epochs": epochs, "loss": history[-1]["loss"], "elapsed_s": round(time.perf_counter() - t0, 1), "eta_s": 0, "history": history, "pairs": len(ds), "size_mb": bin_mb},
    )
    print(f"SUCCESS: Lightweight MIRA-IN saved -> {CT2_DIR} ({bin_mb:.1f} MB)")


if __name__ == "__main__":
    main()
