"""
chatterbox_engine.py - Resemble AI Chatterbox 0.5B Conversational Speech Engine for Vaani.
Features:
- 0.5B Llama-backbone auto-regressive prosody modeling.
- Zero-shot voice cloning with prompt audio conditioning.
- Guided emotion and expressiveness exaggeration control.
- Cross-lingual Odia <-> Devanagari phonetic bridge for natural Odia conversational synthesis.
- Broadcast normalization to 0.890 peak (-1 dBFS).
"""
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import chatterbox
import numpy as np
import soundfile as sf
import torch

from vaani.engine.phonetic_translit import odia_to_devanagari, odia_to_roman


SENTENCE_PAUSE, CLAUSE_PAUSE = 0.30, 0.15  # seconds of silence between chunks


def split_for_tts(text: str, max_chars: int = 200):
    """Paragraph -> [(chunk, pause_after)]. Splits at sentence ends (। ॥ ? ! .), merges short
    sentences so prosody flows across them, and only splits a long sentence at commas."""
    import re
    sents = [s.strip() for s in re.split(r"(?<=[।॥?!.])\s+|\n+", text) if s.strip()]
    out = []
    for s in sents:
        parts = [s]
        if len(s) > max_chars:  # over-long sentence: break at commas, greedily packed
            parts, cur = [], ""
            for c in re.split(r"(?<=[,;:])\s+", s):
                if cur and len(cur) + len(c) + 1 > max_chars:
                    parts.append(cur); cur = c
                else:
                    cur = f"{cur} {c}".strip()
            parts.append(cur)
        for i, p in enumerate(parts):
            pause = SENTENCE_PAUSE if i == len(parts) - 1 else CLAUSE_PAUSE
            if out and out[-1][1] == SENTENCE_PAUSE and len(out[-1][0]) + len(p) + 1 <= max_chars // 2:
                out[-1] = (f"{out[-1][0]} {p}", pause)  # short sentences ride together
            else:
                out.append((p, pause))
    return out


def _trim_silence(w: np.ndarray, sr: int, thresh: float = 0.01, pad: float = 0.04) -> np.ndarray:
    """Cut leading/trailing near-silence so joins get exactly the pause we insert; 10 ms fades stop clicks."""
    idx = np.flatnonzero(np.abs(w) > thresh)
    if not len(idx):
        return w
    p = int(pad * sr)
    w = w[max(0, idx[0] - p): idx[-1] + p].copy()
    f = min(len(w) // 2, int(0.01 * sr))
    if f:
        ramp = np.linspace(0, 1, f, dtype=np.float32)
        w[:f] *= ramp; w[-f:] *= ramp[::-1]
    return w


class ChatterboxEngine:
    """Chatterbox 0.5B ALM voice engine with zero-shot voice cloning and cross-lingual Odia support."""

    PERSONA_REFS = {
        "smita": Path("vaani/models/female_voice_reference.wav"),
        "kishore": Path("vaani/scratch/mira_alm_kishore_expressive.wav"),
        "gulua": Path("vaani/scratch/mira_alm_gulua_comedy.wav"),
        "sambalpuri": Path("vaani/scratch/mira_alm_sambalpuri_juhar.wav"),
    }

    def __init__(self, device: Optional[str] = None):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = None
        self.sample_rate = 24000
        self._load_model()

    FT_DIR = Path(__file__).resolve().parent / "odia_finetune"

    def _best_odia_ckpt(self):
        """Best Odia-finetuned T3 checkpoint by ASR round-trip CER (eval/results.jsonl), else newest."""
        import json
        ck = self.FT_DIR / "checkpoints_v2"
        have = {p.name: p for p in ck.glob("*.safetensors")}
        pin = self.FT_DIR / "eval/pinned.txt"  # human override: short-sentence CER alone can pick a worse paragraph reader
        if pin.exists() and pin.read_text().strip() in have:
            return have[pin.read_text().strip()]
        res =self.FT_DIR / "eval/results.jsonl"
        if res.exists():
            rs = [json.loads(l) for l in res.read_text(encoding="utf8").splitlines() if l.strip()]
            rs = [r for r in rs if r["ckpt"] in have and not r["capped"]]
            if rs:
                return have[min(rs, key=lambda r: r["cer"])["ckpt"]]
        return max(have.values(), key=lambda p: p.stat().st_mtime) if have else None

    def _load_odia_finetuned(self):
        """Chatterbox with the Odia-extended tokenizer + finetuned T3 (speaks Odia script directly)."""
        from safetensors.torch import load_file
        from chatterbox.models.t3 import T3
        sys.path.insert(0, str(self.FT_DIR))
        from model_resize import resize_and_load_t3_weights
        ck = self._best_odia_ckpt()
        if ck is None:
            return None
        m = chatterbox.ChatterboxTTS.from_local(ckpt_dir=str(self.FT_DIR / "model_odia_init"), device="cpu")
        hp = m.t3.hp
        if hp.text_tokens_dict_size != 781:
            old = m.t3.state_dict(); hp.text_tokens_dict_size = 781
            m.t3 = resize_and_load_t3_weights(T3(hp=hp), old)
        m.t3.load_state_dict(load_file(str(ck)), strict=False)
        m.t3.to(self.device).eval(); m.ve.to(self.device); m.s3gen.to(self.device); m.device = self.device
        print(f"[ChatterboxEngine] Odia-finetuned T3 loaded: {ck.name}", flush=True)
        return m

    def _load_model(self):
        """Loads ChatterboxTTS weights (Odia-finetuned if a checkpoint exists, else stock English model)."""
        self.odia_native = False
        try:
            self.model = self._load_odia_finetuned()
            self.odia_native = self.model is not None
        except Exception as e:
            print(f"[ChatterboxEngine] Odia finetune unavailable ({e}); falling back to stock model", flush=True)
            self.model = None
        if self.model is not None:
            return
        print(f"[ChatterboxEngine] Initializing ChatterboxTTS on {self.device}...", flush=True)
        try:
            import chatterbox
            self.model = chatterbox.ChatterboxTTS.from_pretrained(device=self.device)
            print(f"[ChatterboxEngine] ChatterboxTTS loaded successfully on {self.device}!", flush=True)
        except Exception as e:
            print(f"[ChatterboxEngine] ERROR loading ChatterboxTTS: {e}", flush=True)
            import traceback
            traceback.print_exc()
            raise

    def contains_odia(self, text: str) -> bool:
        """Checks if text contains any Odia characters (Unicode 0x0B00-0x0B7F)."""
        return any('\u0B00' <= ch <= '\u0B7F' for ch in text)

    def synthesize(
        self,
        text: str,
        audio_prompt_path: Optional[str] = None,
        persona: Optional[str] = None,
        exaggeration: float = 0.5,
        cfg_weight: float = 0.5,
        temperature: float = 0.8,
    ) -> Tuple[np.ndarray, int, float]:
        """
        Synthesizes speech from text.
        If text is Odia, automatically applies phonetic transliteration to Devanagari
        for pronunciation via Chatterbox multilingual vocabulary.
        
        Returns:
            audio_np: float32 numpy array at 24kHz
            sample_rate: 24000
            rtf: Real-Time Factor (synthesis_time / audio_duration)
        """
        t0 = time.perf_counter()

        # Resolve voice prompt
        ref_path = None
        if audio_prompt_path and Path(audio_prompt_path).exists():
            ref_path = str(audio_prompt_path)
        elif persona and persona in self.PERSONA_REFS:
            candidate = self.PERSONA_REFS[persona]
            if candidate.exists():
                ref_path = str(candidate)

        # Voice conditioning once per call: every chunk of a paragraph uses the same cloned voice
        if ref_path:
            self.model.prepare_conditionals(ref_path, exaggeration=exaggeration)

        # Paragraphs: the model was trained on <10 s clips, so one long generate() drifts or hits the
        # token cap. Speak sentence-sized chunks and join them with natural pauses.
        pieces = []
        for chunk, pause in split_for_tts(text):
            text_to_speak = chunk
            if self.contains_odia(chunk) and not self.odia_native:
                # Phonetic bridge: transliterate to Roman phonetics for the English ALM vocabulary
                text_to_speak = odia_to_roman(chunk)
            with torch.inference_mode():
                w = self.model.generate(text=text_to_speak, exaggeration=exaggeration,
                                        cfg_weight=cfg_weight, temperature=temperature)
            w = (w.detach().cpu().squeeze().numpy() if isinstance(w, torch.Tensor) else np.asarray(w).squeeze()).astype(np.float32)
            pieces.append(_trim_silence(w, self.sample_rate))
            pieces.append(np.zeros(int(pause * self.sample_rate), np.float32))
        audio_np = np.concatenate(pieces[:-1]) if pieces else np.zeros(0, np.float32)

        # Equal-power peak normalization to 0.890 (-1 dBFS broadcast standard)
        peak = np.max(np.abs(audio_np))
        if peak > 0.890:
            audio_np = (audio_np / peak) * 0.890

        elapsed = time.perf_counter() - t0
        dur_s = len(audio_np) / self.sample_rate
        rtf = elapsed / dur_s if dur_s > 0 else 0.0

        return audio_np, self.sample_rate, rtf

    def synthesize_to_file(
        self,
        text: str,
        out_path: str,
        audio_prompt_path: Optional[str] = None,
        persona: Optional[str] = "smita",
        exaggeration: float = 0.5,
    ) -> float:
        """Synthesizes speech and writes WAV file, returning duration."""
        wav, sr, rtf = self.synthesize(
            text=text,
            audio_prompt_path=audio_prompt_path,
            persona=persona,
            exaggeration=exaggeration,
        )
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        sf.write(out_path, wav, sr)
        dur = len(wav) / sr
        print(f"[ChatterboxEngine] Saved {dur:.2f}s audio to {out_path} (RTF={rtf:.4f})")
        return dur

