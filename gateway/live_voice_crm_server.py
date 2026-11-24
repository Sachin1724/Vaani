"""
live_voice_crm_server.py — Real-Time Voice Ordering CRM Server for MIRA
========================================================================
Integrates:
1. Fast local OrderStateMachine for natural Odia/English food ordering & non-linear diffs.
2. 24,000 Hz Studio Neural Voice Engine (Matcha-Vocos Odia) for crystal-clear voice replies.
3. Real-Time WebSockets broadcasting live cart updates to the Merchant CRM Dashboard.
"""

import os
import sys
import time
import json
import asyncio
import random
import re
import threading
from pathlib import Path
from typing import Dict, Any, List, Set, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import soundfile as sf
import uvicorn

for _s in (sys.stdout, sys.stderr):  # a cp1252 Windows console would crash every turn on Odia/emoji logs
    if _s is not None and hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

# Ensure repository root is on sys.path
_MODULE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _MODULE_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from gateway.order_session import OrderSession, roman
from engine.studio_voice import StudioVoiceEngine

app = FastAPI(title="MIRA Live Voice Ordering CRM", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMPLATES_DIR = _MODULE_DIR / "templates"
SCRATCH_AUDIO_DIR = _REPO_ROOT / "scratch" / "crm_audio"
SCRATCH_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# State
order_fsm = OrderSession(send_otp=lambda otp: _pending_otp.append(otp))
_pending_otp: List[str] = []  # demo only: a real build texts this to the caller
studio_voice = None
current_voice = "female"  # dashboard dropdown switches it; both personas share one engine
current_speed = 1.0  # dashboard slider; multiplies the per-voice pace calibration in studio_voice

def get_voice_engine() -> StudioVoiceEngine:
    global studio_voice
    if studio_voice is None:
        print("[MIRA Live CRM] Initializing 24kHz Studio Neural Voice Engine...", flush=True)
        studio_voice = StudioVoiceEngine(voice_persona="female")
        print("[MIRA Live CRM] Voice Engine Ready!", flush=True)
    return studio_voice

# WebSocket Connection Manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)

    async def broadcast(self, message: dict):
        dead_conns = set()
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                dead_conns.add(connection)
        for dead in dead_conns:
            self.active_connections.discard(dead)

manager = ConnectionManager()


# Models
class TurnRequest(BaseModel):
    utterance: str
    spoken: bool = False  # came from the microphone: a slow reply may be covered by a quick acknowledgement
    speaker_persona: Optional[str] = None  # None = whichever voice is picked on the dashboard


# ---- warm-up and quick acknowledgements ----------------------------------------------------------------
ACK_PHRASES = ["ହଁ", "ଠିକ୍ ଅଛି", "ଆଜ୍ଞା"]
_acks: Dict[str, List[Dict[str, Any]]] = {}  # voice -> rendered clips
_last_ack = [None]


def render_acks(voice_name: str):
    voice = get_voice_engine()
    out = []
    for i, phrase in enumerate(ACK_PHRASES):
        wav = voice.synthesize(phrase, voice=voice_name, style="conv")
        name = f"ack_{voice_name}_{i}.wav"
        sf.write(str(SCRATCH_AUDIO_DIR / name), wav, voice.sr)
        out.append({"url": f"/api/audio/{name}", "dur": len(wav) / voice.sr, "name": name})
    _acks[voice_name] = out


def warm_up():
    """First TTS call costs ~1 s and the first ASR load a download check: pay both before a customer is waiting."""
    try:
        t0 = time.perf_counter()
        for v in ("female", "male"):
            render_acks(v)
        mic._load_asr()
        print(f"[MIRA CRM] warm-up done in {time.perf_counter() - t0:.1f}s", flush=True)
    except Exception as e:
        print(f"[MIRA CRM] warm-up skipped: {e}", flush=True)


@app.on_event("startup")
async def _startup_warm():
    threading.Thread(target=warm_up, daemon=True, name="crm-warm").start()


def pick_ack(voice_name: str):
    clips = [c for c in _acks.get(voice_name, []) if c["name"] != _last_ack[0]]
    if not clips:
        return None
    pick = random.choice(clips)
    _last_ack[0] = pick["name"]
    return pick


# ---- conversation state: monotonic generation ids cancel stale speech --------------------------------
# Every customer turn (or interruption) bumps `_generation`. A reply being rendered or queued for an older
# generation checks it before each sentence and stops, so a late answer never plays over a newer one.
_generation = 0
_gen_lock = threading.Lock()


def bump_generation() -> int:
    global _generation
    with _gen_lock:
        _generation += 1
        return _generation


def split_sentences(text: str) -> List[str]:
    return [p.strip() for p in re.split(r"(?<=[।?!])\s*", text) if p.strip()]


# ---- live microphone: speech -> Odia ASR -> the same turn pipeline the typed box uses -------------------
SR_IN = 16000
MIN_TRIGGER_RMS, END_SILENCE_S, MIN_SPEECH_S, MAX_UTTERANCE_S = 0.003, 1.1, 0.4, 15.0
SHORT_SILENCE_S = 0.45  # a pause this long is enough to answer when the sentence already looks finished
ACK_AFTER_S = 0.25      # reply not ready this soon after the caller stopped: say a quick "ହଁ" first

try:
    from engine.semantic_turn_taking import INCOMPLETE_CONJUNCTIONS, INCOMPLETE_POSTPOSITIONS
except Exception:  # keep the server usable if that module changes
    INCOMPLETE_CONJUNCTIONS = INCOMPLETE_POSTPOSITIONS = set()


def looks_complete(text: str) -> bool:
    """Optimal-stopping stand-in: a short pause ends the turn only if the words so far could be a whole answer."""
    words = text.split()
    return bool(words) and len("".join(words)) >= 2 and words[-1] not in INCOMPLETE_CONJUNCTIONS | INCOMPLETE_POSTPOSITIONS


class MicListener:
    """Server-side mic loop. Muted while MIRA's reply plays so it never hears itself."""

    def __init__(self):
        self.on = False
        self._thread = None
        self._asr = None
        self.turn_t0 = 0.0         # when the caller stopped speaking (for the per-turn timeline)
        self.speaking_until = 0.0  # when Mira's queued audio finishes playing in the browser
        self.barge_in = False      # headphones: let the caller talk over Mira
        self.loop = None
        self.device = None  # sounddevice index; None = Windows default

    def mute_for(self, seconds: float):
        """Mira has `seconds` more audio queued: playback is back to back, so lengths add up."""
        self.speaking_until = max(time.time(), self.speaking_until) + seconds

    def interrupt(self):
        """Caller started talking over Mira: invalidate her in-flight reply and silence the browser."""
        gen = bump_generation()
        self.speaking_until = 0.0
        self._send({"type": "stop_audio", "gen": gen})

    def _load_asr(self):
        if self._asr is None:
            from huggingface_hub import snapshot_download
            from engine.indic_asr import IndicCTC
            self._send({"type": "mic", "state": "loading", "text": "Loading Odia speech model (first run downloads ~210 MB)..."})
            self._asr = IndicCTC(os.path.join(snapshot_download("sachinmallick/openflow-odia-asr-int8"), "or"), threads=4)
        return self._asr

    def _send(self, msg: dict):
        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(manager.broadcast(msg), self.loop)

    def start(self, loop, device=None, barge_in=False):
        if self.on:
            return
        self.loop, self.on, self.device, self.barge_in = loop, True, device, barge_in
        self._thread = threading.Thread(target=self._run, daemon=True, name="crm-mic")
        self._thread.start()

    def stop(self):
        self.on = False

    def _run(self):
        import numpy as np
        import sounddevice as sd
        try:
            asr = self._load_asr()
            chunks, speaking, last_voice, t0, last_lvl, last_partial = [], False, 0.0, 0.0, 0.0, 0.0
            name = sd.query_devices(self.device if self.device is not None else sd.default.device[0])["name"]
            with sd.InputStream(samplerate=SR_IN, channels=1, dtype="float32", blocksize=1600, device=self.device) as stream:
                # room noise sets the trigger, so a quiet laptop mic and a loud headset both work
                floor = max(float(np.sqrt(np.mean(stream.read(1600)[0] ** 2))) for _ in range(5))
                trigger = max(MIN_TRIGGER_RMS, floor * 1.6)
                print(f"[MIRA CRM] mic: {name} | noise floor {floor:.4f} | trigger {trigger:.4f}", flush=True)
                self._send({"type": "mic", "state": "listening", "text": f"👂 {name[:28]} (trigger {trigger:.3f})"})
                while self.on:
                    block, _ = stream.read(1600)
                    now = time.time()
                    mira_talking = now < self.speaking_until + 0.4
                    if mira_talking and not self.barge_in:  # speakers: she would hear herself
                        chunks, speaking = [], False
                        continue
                    rms = float(np.sqrt(np.mean(block ** 2)))
                    need = trigger * (3.0 if mira_talking else 1.0)  # headphones: only a clearly louder voice cuts in
                    if now - last_lvl > 0.1:
                        last_lvl = now
                        self._send({"type": "mic", "state": "mira" if mira_talking and not speaking else "speaking" if speaking else "listening",
                                    "level": min(1.0, rms / trigger / 4)})
                    if rms > need:
                        if not speaking:
                            speaking, t0 = True, now
                            if mira_talking:
                                self.interrupt()
                        last_voice = now
                    if speaking:
                        chunks.append(block.copy())
                        silence = now - last_voice
                        done = silence > END_SILENCE_S or now - t0 > MAX_UTTERANCE_S
                        if not done and silence > SHORT_SILENCE_S and now - last_partial > 0.2:
                            last_partial = now  # cheap greedy pass (~5 ms): is what they said so far a complete thought?
                            done = looks_complete(asr.text(np.concatenate(chunks).flatten(), beam=False))
                        if done:
                            audio = np.concatenate(chunks).flatten()
                            chunks, speaking = [], False
                            if len(audio) / SR_IN - silence >= MIN_SPEECH_S:
                                self.turn_t0 = last_voice  # when the caller actually stopped: the clock for time-to-first-audio
                                self._handle(asr, audio)
                    else:
                        chunks = chunks[-3:] + [block.copy()]  # keep a little lead-in so the first syllable isn't clipped
        except Exception as e:
            print(f"[MIRA CRM] mic error: {e}", flush=True)
            self._send({"type": "mic", "state": "error", "text": f"Microphone error: {e}"})
        finally:
            self.on = False
            self._send({"type": "mic", "state": "off"})

    def _handle(self, asr, audio):
        self._send({"type": "mic", "state": "transcribing"})
        peak = float(abs(audio).max())
        if peak > 0:
            audio = audio * (0.7 / peak)  # a quiet mic otherwise gives the recognizer a faint signal
        t_asr = time.perf_counter()
        print(f"[MIRA CRM] transcribing {len(audio) / SR_IN:.1f}s of audio...", flush=True)
        try:
            text = asr.text(audio, beam=True).strip()
        except Exception as e:
            print(f"[MIRA CRM] ASR failed: {e}", flush=True)
            text = ""
        print(f"[MIRA CRM] heard: {text!r} ({(time.perf_counter() - t_asr) * 1000:.0f} ms)", flush=True)
        if not text:
            self._send({"type": "mic", "state": "listening", "text": "(could not understand, please repeat)"})
            return
        fut = asyncio.run_coroutine_threadsafe(process_order_turn(TurnRequest(utterance=text, spoken=True)), self.loop)
        try:
            fut.result(timeout=60)
        except Exception as e:
            print(f"[MIRA CRM] turn failed: {e}", flush=True)
        self._send({"type": "mic", "state": "listening"})


mic = MicListener()


@app.post("/api/voice")
async def set_voice(name: str):
    global current_voice
    if name not in ("male", "female"):
        raise HTTPException(status_code=400, detail="voice must be male or female")
    current_voice = name
    return {"voice": current_voice}


@app.post("/api/speed")
async def set_speed(value: float):
    global current_speed
    current_speed = max(0.8, min(1.3, value))
    return {"speed": current_speed}


@app.get("/api/mic/devices")
async def mic_devices():
    import sounddevice as sd
    seen, out = set(), []
    for i, d in enumerate(sd.query_devices()):  # first (MME) entry per physical mic; skips loopback and Bluetooth headsets
        n = d["name"].strip()
        if d["max_input_channels"] > 0 and n not in seen and "Stereo Mix" not in n and "bthhfenum" not in n and n != "Input ()":
            seen.add(n)
            out.append({"id": i, "name": n})
    return {"devices": out, "default": sd.default.device[0]}


@app.post("/api/mic/start")
async def mic_start(device: Optional[int] = None, barge: bool = False):
    mic.start(asyncio.get_running_loop(), device, barge)
    return {"on": mic.on}


@app.post("/api/mic/stop")
async def mic_stop():
    mic.stop()
    return {"on": mic.on}


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_file = TEMPLATES_DIR / "crm_dashboard.html"
    if not html_file.exists():
        raise HTTPException(status_code=404, detail="Dashboard template not found")
    return HTMLResponse(content=html_file.read_text(encoding="utf-8"))


@app.websocket("/ws/crm")
async def websocket_crm_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    # Send initial state
    cart = order_fsm.get_cart_summary()
    await websocket.send_json({
        "type": "state_update",
        "cart": cart,
        "diffs": [],
        "stage": order_fsm.stage,
        "address": order_fsm.address,
        "name": order_fsm.name,
        "name_roman": roman(order_fsm.name) if order_fsm.name else ""
    })
    try:
        while True:
            # Keepalive / ping
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.post("/api/order/turn")
async def process_order_turn(req: TurnRequest):
    utterance = req.utterance.strip()
    if not utterance:
        raise HTTPException(status_code=400, detail="Empty utterance")

    print(f"\n[MIRA CRM] 🗣️ Customer: \"{utterance}\"", flush=True)

    # 1. Process with Non-Linear Order State Machine
    t0 = time.perf_counter()
    res = order_fsm.process_turn(utterance)
    cart = order_fsm.get_cart_summary()
    fsm_time_ms = (time.perf_counter() - t0) * 1000

    reply_odia = res["reply_odia"]

    gen = bump_generation()  # any reply still being rendered for an older turn is now stale

    turn_time = time.strftime("%I:%M %p")
    print(f"[MIRA CRM] 🤖 MIRA: \"{reply_odia}\"", flush=True)
    await manager.broadcast({"type": "new_turn", "turn": {"role": "customer", "text": utterance, "time": turn_time}})
    await manager.broadcast({"type": "new_turn", "turn": {"role": "mira", "text": reply_odia, "time": turn_time}})
    await manager.broadcast({
        "type": "state_update", "cart": cart, "diffs": res.get("diffs", []),
        "stage": res["stage"], "address": res["address"], "name": res["name"], "name_roman": res["name_roman"]
    })
    while _pending_otp:  # demo: show the SMS on the dashboard since there is no SMS provider yet
        await manager.broadcast({"type": "otp_sms", "otp": _pending_otp.pop(0)})
    if res["stage"] == "placed":
        await manager.broadcast({"type": "kot_dispatched", "cart": cart, "time": time.strftime("%I:%M %p")})

    # 2. Speak it one sentence at a time: the first sentence is audible while the rest is still rendering
    voice = await asyncio.to_thread(get_voice_engine)
    t_start = time.perf_counter()
    first_audio_ms = None
    for seq, sentence in enumerate(split_sentences(reply_odia)):
        render = asyncio.ensure_future(asyncio.to_thread(voice.synthesize, sentence, voice=req.speaker_persona or current_voice, style="conv", speed=current_speed))
        if seq == 0 and req.spoken:
            done, _ = await asyncio.wait({render}, timeout=ACK_AFTER_S)
            ack = None if done else pick_ack(req.speaker_persona or current_voice)
            if ack and gen == _generation:  # slow render: cover the gap with a real "ହଁ" while it finishes
                mic.mute_for(ack["dur"])
                await manager.broadcast({"type": "audio_chunk", "gen": gen, "seq": -1, "url": ack["url"], "text": ""})
                print(f"[MIRA CRM] ack played ({ack['name']})", flush=True)
        wav = await render
        if gen != _generation:  # the caller spoke again (or interrupted) while this was rendering: drop it
            print(f"[MIRA CRM] dropped stale reply (gen {gen} < {_generation})", flush=True)
            break
        name = f"mira_reply_{int(time.time() * 1000)}_{seq}.wav"
        sf.write(str(SCRATCH_AUDIO_DIR / name), wav, voice.sr)
        mic.mute_for(len(wav) / voice.sr)
        if first_audio_ms is None:
            first_audio_ms = (time.perf_counter() - t_start) * 1000
            if req.spoken and mic.turn_t0:
                since_stop = (time.time() - mic.turn_t0) * 1000
                print(f"[MIRA CRM] ⏱ caller stopped -> first reply audio queued: {since_stop:.0f} ms", flush=True)
        await manager.broadcast({"type": "audio_chunk", "gen": gen, "seq": seq, "url": f"/api/audio/{name}", "text": sentence})
    print(f"[MIRA CRM] ⚡ FSM={fsm_time_ms:.1f}ms | time to first audio={first_audio_ms or 0:.0f}ms", flush=True)

    return {
        "status": "success",
        "customer_utterance": utterance,
        "mira_reply_odia": reply_odia,
        "mira_reply_en": res["reply_en"],
        "stage": res["stage"],
        "audio_url": None,
        "cart": cart,
        "diffs": res.get("diffs", []),
        "latency_ms": round(fsm_time_ms + (first_audio_ms or 0), 1)
    }


@app.get("/api/audio/{filename}")
async def get_audio_file(filename: str):
    file_path = SCRATCH_AUDIO_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(file_path, media_type="audio/wav")


@app.post("/api/order/kot")
async def dispatch_kot():
    cart = order_fsm.get_cart_summary()
    await manager.broadcast({
        "type": "kot_dispatched",
        "cart": cart,
        "time": time.strftime("%I:%M %p")
    })
    return {"status": "DISPATCHED", "cart": cart}


@app.post("/api/order/reset")
async def reset_order_session():
    order_fsm.reset()
    cart = order_fsm.get_cart_summary()
    await manager.broadcast({
        "type": "state_update",
        "cart": cart,
        "diffs": [],
        "stage": order_fsm.stage,
        "address": "",
        "name": "",
        "name_roman": ""
    })
    return {"status": "RESET", "cart": cart}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"\n==================================================================")
    print(f"🚀 MIRA LIVE VOICE ORDERING CRM SERVER LAUNCHING ON http://127.0.0.1:{port}")
    print(f"==================================================================\n")
    uvicorn.run("live_voice_crm_server:app", host="127.0.0.1", port=port, reload=False, app_dir=str(_MODULE_DIR))

