"""
train_mira_multilingual.py — Train MIRA V2 (3-Way English + Odia + Hindi/Hinglish Refiner)
==========================================================================================
Trains Qwen2.5-0.5B-Instruct LoRA -> CTranslate2 int8 on:
1. vaani/data/mira_train.jsonl        (Odia + Dialect standardization + general cleanup)
2. vaani/data/mira_indian_train.jsonl (Indian English Svarah + podcast ASR repair)
3. Hindi / Hinglish cleanup & cross-app refinement pairs
4. Personal learned_corrections from ~/.vaani/openflow.db

Outputs: vaani/models/mira_v2_ct2_int8/
Run: python -m vaani.train_mira_multilingual
"""
import glob
import json
import os
import random
import sqlite3
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
MERGED_DIR = ROOT / "models" / "mira_v2_merged"
CT2_DIR = ROOT / "models" / "mira_v2_ct2_int8"

HINDI_HINGLISH_PAIRS = [
    ("aaj hum vaani test kar rahe hain aur ye kaafi fast hai", "आज हम Vaani टेस्ट कर रहे हैं और यह काफी फास्ट है।"),
    ("kya tum mujhe kal subah 10 baje call kar sakte ho", "क्या तुम मुझे कल सुबह 10 बजे कॉल कर सकते हो?"),
    ("bhai meeting 5 minute me start hone wali hai", "भाई, मीटिंग 5 मिनट में स्टार्ट होने वाली है।"),
    ("please isko hindi me translate kar do", "कृपया इसे हिंदी में अनुवाद कर दें।"),
    ("um matlab mujhe lagta hai ki model ab sahi kaam kar raha hai", "मुझे लगता है कि मॉडल अब सही काम कर रहा है।"),
    ("मैं कल भुवनेश्वर जा रहा हूँ और फिर मुंबई जाऊँगा", "मैं कल भुवनेश्वर जा रहा हूँ और फिर मुंबई जाऊँगा।"),
    ("क्या आप इस ईमेल को थोड़ा फॉर्मल बना सकते हैं", "क्या आप इस ईमेल को थोड़ा फॉर्मल बना सकते हैं?"),
    ("haan theek hai main abhi pull request merge kar deta hoon", "हाँ ठीक है, मैं अभी pull request merge कर देता हूँ।"),
]


def find_qwen05_base() -> str:
    snaps = glob.glob(os.path.expanduser("~/.cache/huggingface/hub/models--Qwen--Qwen2.5-0.5B-Instruct/snapshots/*"))
    return snaps[0] if snaps else "Qwen/Qwen2.5-0.5B-Instruct"


def load_examples(tok, max_len: int = 224) -> list:
    rows = []

    mira_p = ROOT / "data" / "mira_train.jsonl"
    if mira_p.exists():
        odia_rows = []
        with open(mira_p, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    odia_rows.append(json.loads(line))
        random.Random(42).shuffle(odia_rows)
        for obj in odia_rows[:1400]:
            msgs = obj.get("messages", [])
            if len(msgs) >= 3:
                rows.append((msgs[0]["content"], msgs[1]["content"], msgs[2]["content"]))

    in_p = ROOT / "data" / "mira_indian_train.jsonl"
    if in_p.exists():
        in_rows = []
        with open(in_p, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    in_rows.append(json.loads(line))
        random.Random(43).shuffle(in_rows)
        for i, obj in enumerate(in_rows[:1200]):
            raw_in, target = obj.get("input", "").strip(), obj.get("target", "").strip()
            if raw_in and target:
                if i % 4 != 0:
                    rows.append((POLISH_SYSTEM, polish_prompt(raw_in), target))
                else:
                    rows.append((MIRA_SYSTEM.format(style="smart_clean"), f"Raw speech: {raw_in}", target))

    for _ in range(40):
        for raw_in, target in HINDI_HINGLISH_PAIRS:
            rows.append((POLISH_SYSTEM, polish_prompt(raw_in), target))
            rows.append((MIRA_SYSTEM.format(style="smart_clean"), f"Raw speech: {raw_in}", target))

    db_path = Path.home() / ".vaani" / "openflow.db"
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            for heard, corrected, ctx in conn.execute(
                "SELECT heard, corrected, context FROM learned_corrections ORDER BY id DESC LIMIT 300"
            ).fetchall():
                if heard and corrected:
                    rows.append((POLISH_SYSTEM, polish_prompt(heard), corrected))
                    if ctx:
                        rows.append((POLISH_SYSTEM, polish_prompt(ctx.replace(corrected, heard)), ctx))
            conn.close()
        except Exception:
            pass

    random.Random(42).shuffle(rows)
    examples = []
    im_end = "<|im_end|>"
    for sys_msg, usr_msg, target in rows:
        msgs = [{"role": "system", "content": sys_msg}, {"role": "user", "content": usr_msg}]
        prompt_str = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        p_ids = tok(prompt_str, add_special_tokens=False)["input_ids"]
        t_ids = tok(target + im_end, add_special_tokens=False)["input_ids"]
        ids = (p_ids + t_ids)[:max_len]
        labels = ([-100] * len(p_ids) + t_ids)[:max_len]
        if any(x != -100 for x in labels):
            examples.append((torch.tensor(ids, dtype=torch.long), torch.tensor(labels, dtype=torch.long)))
    return examples


class _ListDS(Dataset):
    def __init__(self, items):
        self.items = items

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        return self.items[idx]


def collate(batch, pad_id: int):
    ml = max(len(x[0]) for x in batch)
    inp = torch.full((len(batch), ml), pad_id, dtype=torch.long)
    lbl = torch.full((len(batch), ml), -100, dtype=torch.long)
    attn = torch.zeros((len(batch), ml), dtype=torch.long)
    for i, (x, y) in enumerate(batch):
        inp[i, : len(x)] = x
        lbl[i, : len(y)] = y
        attn[i, : len(x)] = 1
    return {"input_ids": inp, "labels": lbl, "attention_mask": attn}


def main():
    base = find_qwen05_base()
    print(f"[MIRA V2] Base model: {base}")
    tok = AutoTokenizer.from_pretrained(base)
    if tok.pad_token_id is None:
        tok.pad_token_id = tok.eos_token_id

    examples = load_examples(tok)
    print(f"[MIRA V2] Training examples (EN + Odia + Hindi): {len(examples)}")
    dl = DataLoader(_ListDS(examples), batch_size=8, shuffle=True, collate_fn=lambda b: collate(b, tok.pad_token_id))

    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    model = AutoModelForCausalLM.from_pretrained(base, torch_dtype=dtype).to("cuda")
    model = get_peft_model(model, LoraConfig(
        task_type=TaskType.CAUSAL_LM, r=16, lora_alpha=32, lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    ))
    model.print_trainable_parameters()

    epochs, grad_accum = 2, 2
    total_steps = (len(dl) // grad_accum) * epochs
    opt = torch.optim.AdamW(model.parameters(), lr=1.5e-4, weight_decay=0.01)
    sched = get_cosine_schedule_with_warmup(opt, max(10, total_steps // 10), total_steps)

    t0 = time.perf_counter()
    step = 0
    model.train()
    for ep in range(epochs):
        for bi, batch in enumerate(dl):
            batch = {k: v.to("cuda") for k, v in batch.items()}
            loss = model(**batch).loss / grad_accum
            loss.backward()
            if (bi + 1) % grad_accum == 0 or (bi + 1) == len(dl):
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
                sched.step()
                opt.zero_grad()
                step += 1
                if step % 25 == 0 or step == total_steps:
                    print(f"  step {step}/{total_steps}  loss={loss.item() * grad_accum:.4f}  ({time.perf_counter() - t0:.1f}s)")

    MERGED_DIR.mkdir(parents=True, exist_ok=True)
    merged = model.merge_and_unload()
    merged.save_pretrained(str(MERGED_DIR))
    tok.save_pretrained(str(MERGED_DIR))

    CT2_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "ctranslate2.converters.transformers",
        "--model", str(MERGED_DIR), "--output_dir", str(CT2_DIR),
        "--quantization", "int8", "--force",
    ]
    subprocess.run(cmd, check=True)
    print(f"[MIRA V2] Exported int8 CT2 model to {CT2_DIR}")


if __name__ == "__main__":
    main()
