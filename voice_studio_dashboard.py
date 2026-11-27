"""
voice_studio_dashboard.py — Live Odia & Multilingual Voice Cloning Studio Dashboard
====================================================================================
Serves an interactive real-time voice cloning workbench at http://127.0.0.1:8788.
Run with the OmniVoice environment:
  vaani/alm/odia_finetune/.venv_omni/Scripts/python.exe -m vaani.voice_studio_dashboard
"""
import base64
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

ROOT = Path(__file__).resolve().parent
ENROLLED_DIR = ROOT / "data" / "enrolled_speakers"
OUT_DIR = ROOT / "data" / "studio_takes"
PORT = int(os.environ.get("VAANI_STUDIO_PORT", "8788"))

_MODEL = None
_MODEL_LOCK = threading.Lock()
_GEN_LOCK = threading.Lock()  # one take at a time: the GPU is shared and the seed is process-wide
_TAKES = []

BRAND_SAFE_MAP = [
    ("ବାଣୀ", "Vaani"),
    ("ମୀରା", "MIRA"),
    ("ସଚିନ", "Sachin"),
    ("ସଚିନ୍", "Sachin"),
    ("ଭଏସ୍ କ୍ଲୋନ୍", "voice clone"),
    ("ଭଏସ୍ କ୍ଲୋନ", "voice clone"),
]


def _get_model():
    global _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            from omnivoice import OmniVoice
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
            dtype = torch.float16 if device.startswith("cuda") else torch.float32
            print(f"[VoiceStudio] Loading OmniVoice on {device} ({dtype})...", flush=True)
            _MODEL = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map=device, dtype=dtype)
            print("[VoiceStudio] OmniVoice ready.", flush=True)
        return _MODEL


def list_speakers():
    ENROLLED_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for wav in sorted(ENROLLED_DIR.glob("*_ref.wav")):
        stem = wav.stem[:-4] if wav.stem.endswith("_ref") else wav.stem
        meta_p = ENROLLED_DIR / f"{stem}.json"
        ref_text, name = "", stem.replace("_", " ").title()
        if meta_p.exists():
            try:
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
                ref_text = meta.get("text") or meta.get("transcript") or ""
                name = meta.get("name") or name  # an Odia name keeps its script; the id is only for files
            except Exception:
                pass
        items.append({"id": stem, "name": name, "wav": str(wav), "ref_text": ref_text})
    return items


# Enrollment (quality checks + speaker fingerprint) needs onnxruntime, which lives in the app's Python, not this one.
ENROLL_SCRIPT = """
import json, sys
from vaani.engine.enrollment import get_enrollment_manager
p = get_enrollment_manager().enroll_speaker(sys.argv[1], sys.argv[2], speaker_id=sys.argv[3])
print(json.dumps({k: p[k] for k in ("speaker_id", "name", "duration_s", "snr_db", "gender")}))
"""


def enroll_voice(name: str, wav_bytes: bytes, text: str = "") -> dict:
    """Save a recording as an enrolled speaker (a new one, or re-enroll an existing id)."""
    name = name.strip()
    if not name:
        raise ValueError("Name the voice first.")
    # file id: English letters as typed, else a stable hash, so the same Odia name re-enrolls the same voice
    sid = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "voice_" + hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_DIR / f"enroll_{uuid.uuid4().hex[:8]}.wav"
    tmp.write_bytes(wav_bytes)
    py = os.environ.get("VAANI_APP_PYTHON") or shutil.which("python") or "python"
    # this venv's launcher variables would make the other Python load this one's standard library
    env = {k: v for k, v in os.environ.items() if k not in ("__PYVENV_LAUNCHER__", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")}
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run([py, "-c", ENROLL_SCRIPT, name, str(tmp), sid], cwd=str(ROOT.parent), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=180, env=env)
    finally:
        tmp.unlink(missing_ok=True)
    if r.returncode != 0:
        last = (r.stderr.strip().splitlines() or ["Enrollment failed"])[-1]
        raise ValueError(last.split("quality check failed:", 1)[-1].split("Error: ", 1)[-1].strip())
    profile = json.loads(r.stdout.strip().splitlines()[-1])
    meta_p = ENROLLED_DIR / f"{sid}.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["text"] = text.strip()  # what was read aloud: OmniVoice clones better with the reference transcript
    meta_p.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return profile


def normalize_brand_names(text: str, enabled: bool = True) -> str:
    if not enabled:
        return text
    # longest spelling first, or "ସଚିନ" would match inside "ସଚିନ୍" and strand its virama
    for bad, good in sorted(BRAND_SAFE_MAP, key=lambda p: -len(p[0])):
        text = text.replace(bad, good)
    return text


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Vaani Voice Studio — Live Odia Cloning &amp; Paragraph Workbench</title>
<link rel="preconnect" href="https://api.fontshare.com">
<link rel="stylesheet" href="https://api.fontshare.com/v2/css?f[]=general-sans@400,500,600&display=swap">
<style>
  :root {
    --bg: #F5EFE6; --surface: #EFE8DA; --surface-2: #E8DFCA; --ink: #1E2A36;
    --body: #2F3D4B; --soft: #4A5868; --accent: #7895B2; --deep: #2E4560;
    --ok: #15803D; --edge: rgba(30,42,54,.12); --r: 20px;
    --font: 'General Sans', 'Segoe UI', system-ui, sans-serif;
  }
  [data-theme="dark"] {
    --bg: #121820; --surface: #1A232E; --surface-2: #222E3C; --ink: #F5EFE6;
    --body: #D6DFEA; --soft: #8FA0B3; --accent: #7895B2; --deep: #9BB7D4;
    --ok: #4ADE80; --edge: rgba(245,239,230,.12);
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--body); font: 15px/1.5 var(--font); padding: 24px; }
  .wrap { max-width: 1240px; margin: 0 auto; display: grid; gap: 18px; }
  header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
  .brand { display: flex; align-items: center; gap: 12px; }
  .mark { width: 48px; height: 22px; border-radius: 999px; background: var(--accent); display: flex; align-items: center; justify-content: center; gap: 2.5px; }
  .mark i { width: 2.5px; border-radius: 2px; background: #1E2A36; height: calc(var(--h) * .15px); display: block; }
  h1 { margin: 0; font-size: 22px; font-weight: 600; color: var(--ink); letter-spacing: -0.025em; }
  .sub { font-size: 13px; color: var(--soft); }
  .head-r { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .grid { display: grid; grid-template-columns: 390px minmax(0, 1fr); gap: 18px; align-items: start; }
  .card { background: var(--surface); border: 1px solid var(--edge); border-radius: var(--r); padding: 20px; display: grid; gap: 16px; }
  .card h2 { margin: 0; font-size: 15px; font-weight: 600; color: var(--ink); letter-spacing: -0.01em; }
  label.fld { display: grid; gap: 6px; font-size: 13px; font-weight: 500; color: var(--ink); }
  select, input[type="number"], textarea {
    width: 100%; border-radius: 12px; border: 1px solid var(--edge);
    background: var(--bg); color: var(--ink); font: inherit; padding: 10px 12px;
  }
  textarea { min-height: 150px; resize: vertical; font-size: 16px; line-height: 1.55; }
  .sl-row { display: grid; gap: 4px; }
  .sl-top { display: flex; justify-content: space-between; font-size: 13px; color: var(--soft); }
  .sl-top b { color: var(--ink); font-variant-numeric: tabular-nums; }
  input[type="range"] { width: 100%; accent-color: var(--deep); cursor: pointer; }
  .row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .btn { min-height: 42px; padding: 0 18px; border-radius: 999px; border: 0; font: inherit; font-weight: 500; cursor: pointer; background: var(--ink); color: var(--bg); transition: transform .15s, opacity .15s; }
  .btn:hover { opacity: .92; }
  .btn:active { transform: scale(.98); }
  .btn:disabled { opacity: .45; cursor: wait; }
  .btn.quiet { background: var(--surface-2); color: var(--ink); border: 1px solid var(--edge); }
  .btn.rec { background: #B42318; color: #fff; }
  .presets { display: flex; gap: 8px; flex-wrap: wrap; }
  .pbtn { border: 1px solid var(--edge); background: var(--bg); color: var(--ink); border-radius: 999px; padding: 6px 12px; font-size: 12.5px; cursor: pointer; }
  .pbtn:hover { background: var(--surface-2); }
  .player-box { background: var(--bg); border: 1px solid var(--edge); border-radius: 16px; padding: 16px; display: grid; gap: 12px; }
  audio { width: 100%; }
  .stats { display: flex; gap: 12px; flex-wrap: wrap; font-size: 12.5px; color: var(--soft); font-variant-numeric: tabular-nums; }
  .stats b { color: var(--ink); }
  .take { background: var(--bg); border: 1px solid var(--edge); border-radius: 14px; padding: 12px 14px; display: grid; gap: 8px; }
  .take-top { display: flex; justify-content: space-between; align-items: center; gap: 10px; font-size: 12.5px; color: var(--soft); }
  .chk { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--ink); cursor: pointer; }
  .rec-box { display: grid; gap: 10px; padding: 14px; border-radius: 16px; background: var(--bg); border: 1px solid var(--edge); transition: border-color .2s, box-shadow .2s; }
  .rec-box.live { border-color: #B42318; box-shadow: 0 0 0 3px rgba(180,35,24,.14); }
  .rec-box textarea { min-height: 72px; font-size: 15px; background: var(--surface); }
  .rec-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .rec-time { font-size: 13px; color: var(--soft); font-variant-numeric: tabular-nums; display: flex; align-items: center; gap: 6px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--soft); opacity: .5; }
  .rec-box.live .dot { background: #B42318; opacity: 1; animation: blink 1s ease-in-out infinite; }
  @keyframes blink { 50% { opacity: .25; } }
  .meter { height: 8px; border-radius: 99px; background: var(--surface-2); overflow: hidden; }
  .meter span { display: block; height: 100%; background: var(--ok); transform-origin: left; transform: scaleX(0); transition: transform .08s linear; }
  .meter.hot span { background: #B42318; }
  .enroll-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
  .enroll-row input { width: 100%; border-radius: 12px; border: 1px solid var(--edge); background: var(--surface); color: var(--ink); font: inherit; padding: 10px 12px; }
  @media (prefers-reduced-motion: reduce) { .rec-box.live .dot { animation: none; } .meter span { transition: none; } }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <span class="mark" aria-hidden="true"><i style="--h:40"></i><i style="--h:85"></i><i style="--h:100"></i><i style="--h:60"></i><i style="--h:80"></i></span>
      <div>
        <h1>Vaani Voice Studio</h1>
        <div class="sub">Real-time Odia &amp; Multilingual Voice Cloning Workbench</div>
      </div>
    </div>
    <div class="head-r">
      <button class="btn quiet" type="button" id="themeBtn">Theme</button>
    </div>
  </header>

  <div class="grid">
    <aside class="card">
      <h2>1. Voice &amp; Acoustic Controls</h2>
      <label class="fld">
        Enrolled Speaker
        <select id="speakerSel"></select>
      </label>

      <div class="rec-box" id="recBox">
        <label class="fld" for="recScript">Read this aloud while recording</label>
        <textarea id="recScript" spellcheck="false">ନମସ୍କାର, ମୁଁ ମୋ ନିଜ ସ୍ୱରରେ କଥା ହେଉଛି। ଆଜି ପାଗ ବହୁତ ଭଲ ଅଛି, and I am recording my voice for Vaani.</textarea>
        <div class="meter" id="meter" aria-hidden="true"><span id="meterFill"></span></div>
        <div class="rec-row">
          <button class="btn quiet" type="button" id="recBtn">Record</button>
          <span class="rec-time"><span class="dot"></span><span id="recTimer">Up to 10 s</span></span>
          <label class="btn quiet" style="display:inline-flex;align-items:center;justify-content:center;cursor:pointer;margin-left:auto">
            Upload Audio
            <input type="file" id="wavUpload" accept="audio/*" hidden>
          </label>
        </div>
        <audio id="recPreview" controls hidden></audio>
        <div class="enroll-row" id="enrollRow" hidden>
          <input id="enrollName" placeholder="Voice name, e.g. sachin…" autocomplete="off" spellcheck="false" aria-label="Voice name">
          <button class="btn" type="button" id="enrollBtn">Save as Voice</button>
        </div>
        <div class="sub" id="recStatus" aria-live="polite">Using the enrolled voice above.</div>
      </div>

      <label class="fld">
        Language
        <select id="langSel">
          <option value="ory">Odia (ଓଡ଼ିଆ)</option>
          <option value="en">English (Indian / Global)</option>
          <option value="hi">Hindi (हिंदी)</option>
        </select>
      </label>

      <div class="sl-row">
        <div class="sl-top"><span>Voice Temperature (Variety)</span><b id="ctVal">0.20</b></div>
        <input type="range" id="ctSlider" min="0" max="1" step="0.05" value="0.20">
      </div>
      <div class="sl-row">
        <div class="sl-top"><span>Prosody Temperature (Pacing)</span><b id="ptVal">5.0</b></div>
        <input type="range" id="ptSlider" min="0" max="8" step="0.5" value="5.0">
      </div>
      <div class="sl-row">
        <div class="sl-top"><span>Speed</span><b id="spVal">1.00×</b></div>
        <input type="range" id="spSlider" min="0.80" max="1.25" step="0.05" value="1.00">
      </div>
      <div class="sl-row">
        <div class="sl-top"><span>Guidance Scale</span><b id="gsVal">2.2</b></div>
        <input type="range" id="gsSlider" min="1.0" max="4.0" step="0.2" value="2.2">
      </div>
      <div class="sl-row">
        <div class="sl-top"><span>Quality Steps</span><b id="stVal">28</b></div>
        <input type="range" id="stSlider" min="16" max="40" step="4" value="28">
      </div>
      <label class="fld">
        Seed (-1 for new random inflection)
        <input type="number" id="seedInp" value="0" min="-1" max="9999">
      </label>
      <label class="chk">
        <input type="checkbox" id="brandNorm" checked>
        <span>Brand-safe pronunciation (Vaani, MIRA, Sachin stay crisp)</span>
      </label>
    </aside>

    <main style="display:grid;gap:18px">
      <section class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
          <h2>2. Paragraph Retest Workbench</h2>
          <div class="presets">
            <button type="button" class="pbtn" data-p="intro">Vaani Intro (Odia)</button>
            <button type="button" class="pbtn" data-p="weather">Odisha Weather</button>
            <button type="button" class="pbtn" data-p="farmer">Farmer Helpline</button>
            <button type="button" class="pbtn" data-p="mix">Odia + English Tech</button>
          </div>
        </div>

        <textarea id="paraTxt">ନମସ୍କାର, ମୁଁ Sachin। ଆଜି ଆମେ ଓଡ଼ିଆରେ କଥା ହେଉଛୁ। Vaani ଏବେ ଆପଣଙ୍କ ନିଜ ସ୍ୱରରେ ଓଡ଼ିଆ ପାରାଗ୍ରାଫ୍ ପଢ଼ିପାରିବ ଏବଂ ଆପଣଙ୍କ voice clone କରିପାରିବ। ଧନ୍ୟବାଦ।</textarea>

        <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
          <button class="btn" type="button" id="genBtn">Generate Cloned Speech</button>
          <span class="sub" id="genStatus">Ready on local GPU/CPU</span>
        </div>

        <div class="player-box" id="playerBox" style="display:none">
          <audio id="audioEl" controls autoplay></audio>
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
            <div class="stats" id="outStats"></div>
            <a class="btn quiet" id="dlLink" download="vaani_cloned.wav">Download .WAV</a>
          </div>
          <div class="sub" id="normText"></div>
        </div>
      </section>

      <section class="card">
        <h2>3. Real-Time Retest Takes (Compare Parameters)</h2>
        <div id="takesList" style="display:grid;gap:10px">
          <div class="sub">No takes generated yet — tweak Temperature or Speed above and click Generate.</div>
        </div>
      </section>
    </main>
  </div>
</div>

<script>
const PRESETS = {
  intro: 'ନମସ୍କାର, ମୁଁ Sachin। ଆଜି ଆମେ ଓଡ଼ିଆରେ କଥା ହେଉଛୁ। Vaani ଏବେ ଆପଣଙ୍କ ନିଜ ସ୍ୱରରେ ଓଡ଼ିଆ ପାରାଗ୍ରାଫ୍ ପଢ଼ିପାରିବ ଏବଂ ଆପଣଙ୍କ voice clone କରିପାରିବ। ଧନ୍ୟବାଦ।',
  weather: 'ଆଜି ଭୁବନେଶ୍ୱରରେ ପାଗ ଖୁବ୍ ସୁନ୍ଦର ଅଛି। ସକାଳୁ ହାଲୁକା ପବନ ବହୁଛି ଆଉ ସନ୍ଧ୍ୟା ବେଳକୁ ବର୍ଷା ହେବାର ସମ୍ଭାବନା ରହିଛି।',
  farmer: 'ନମସ୍କାର ଚାଷୀ ଭାଇ, ଧାନ ଫସଲରେ ପୋକ ଲାଗିଥିଲେ ସକାଳ କିମ୍ବା ସନ୍ଧ୍ୟାରେ ନିମ୍ବ ତେଲ ସ୍ପ୍ରେ କରନ୍ତୁ।',
  mix: 'ଆଜି ଆମେ ଓଡ଼ିଆ ପଡକାଷ୍ଟ ପାଇଁ MIRA ଏବଂ Vaani Studio ରେ ନୂଆ voice clone test କରୁଛୁ, ଯାହା very fast କାମ କରୁଛି।'
};
const $ = id => document.getElementById(id);
let customRefB64 = null, customRefText = '';

$('themeBtn').onclick = () => {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  document.documentElement.setAttribute('data-theme', dark ? 'light' : 'dark');
};
for (const [sl, lbl, fmt] of [
  ['ctSlider','ctVal', v => Number(v).toFixed(2)],
  ['ptSlider','ptVal', v => Number(v).toFixed(1)],
  ['spSlider','spVal', v => Number(v).toFixed(2) + '×'],
  ['gsSlider','gsVal', v => Number(v).toFixed(1)],
  ['stSlider','stVal', v => v],
]) $(sl).oninput = () => $(lbl).textContent = fmt($(sl).value);

document.querySelectorAll('.pbtn').forEach(b => b.onclick = () => { $('paraTxt').value = PRESETS[b.dataset.p]; });

async function loadSpeakers(pick) {
  const list = await (await fetch('/api/speakers')).json();
  $('speakerSel').innerHTML = list.map(s => `<option value="${s.id}">${s.name}</option>`).join('');
  const want = pick || 'sachin';
  if (list.some(s => s.id === want)) $('speakerSel').value = want;
}
loadSpeakers();
$('speakerSel').onchange = () => {  // picking a saved voice drops an unsaved recording
  customRefB64 = null; customRefText = '';
  $('recStatus').textContent = 'Using the enrolled voice above.';
};

const b64 = u8 => { let bin = ''; for (let i = 0; i < u8.length; i += 8192) bin += String.fromCharCode(...u8.subarray(i, i + 8192)); return btoa(bin); };
function useRef(bytes, type, label, text) {
  customRefB64 = b64(bytes); customRefText = text;
  $('recPreview').src = URL.createObjectURL(new Blob([bytes], { type }));
  $('recPreview').hidden = false;
  $('enrollRow').hidden = false;
  $('enrollName').value = $('enrollName').value || $('speakerSel').value || '';
  $('recStatus').textContent = label + ' · the next take uses it. Save it to keep it as a voice.';
}

$('wavUpload').onchange = async e => {
  const f = e.target.files[0]; if (!f) return;
  useRef(new Uint8Array(await f.arrayBuffer()), f.type || 'audio/wav', 'Upload ready: ' + f.name, '');
  e.target.value = '';
};

const REC_MAX = 10, REC_MIN = 3;
let rec = null;
$('recBtn').onclick = () => rec ? stopRec() : startRec();

async function startRec() {
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: true } }); }
  catch (err) { $('recStatus').textContent = 'Microphone blocked (' + err.message + '). Allow it from the address bar, then press Record.'; return; }
  const ctx = new AudioContext();
  const src = ctx.createMediaStreamSource(stream);
  const an = ctx.createAnalyser(); an.fftSize = 1024;
  const proc = ctx.createScriptProcessor(4096, 1, 1);
  const chunks = [];
  proc.onaudioprocess = e => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  src.connect(an); src.connect(proc); proc.connect(ctx.destination);
  rec = { ctx, stream, src, proc, chunks, t0: performance.now() };
  $('recBtn').textContent = 'Stop'; $('recBtn').classList.add('rec'); $('recBox').classList.add('live');
  $('recPreview').hidden = true; $('enrollRow').hidden = true;
  $('recStatus').textContent = 'Recording. Read the line above in your normal voice.';
  const buf = new Float32Array(an.fftSize);
  const tick = () => {
    if (!rec) return;
    an.getFloatTimeDomainData(buf);
    let peak = 0; for (const x of buf) peak = Math.max(peak, Math.abs(x));
    $('meterFill').style.transform = `scaleX(${Math.min(1, peak * 1.6)})`;
    $('meter').classList.toggle('hot', peak > 0.95);
    const sec = (performance.now() - rec.t0) / 1000;
    $('recTimer').textContent = `${sec.toFixed(1)} / ${REC_MAX} s`;
    if (sec >= REC_MAX) return stopRec();
    requestAnimationFrame(tick);
  };
  tick();
}

function stopRec() {
  const { ctx, stream, src, proc, chunks } = rec; rec = null;
  proc.disconnect(); src.disconnect(); stream.getTracks().forEach(t => t.stop());
  const rate = ctx.sampleRate; ctx.close();
  $('recBtn').textContent = 'Record Again'; $('recBtn').classList.remove('rec'); $('recBox').classList.remove('live');
  $('meterFill').style.transform = 'scaleX(0)'; $('meter').classList.remove('hot');
  const len = chunks.reduce((a, c) => a + c.length, 0);
  $('recTimer').textContent = `${(len / rate).toFixed(1)} s recorded`;
  if (len / rate < REC_MIN) { $('recStatus').textContent = `Too short. Record at least ${REC_MIN} seconds.`; return; }
  const pcm = new Int16Array(len);
  let off = 0;
  for (const c of chunks) for (let i = 0; i < c.length; i++) pcm[off++] = Math.max(-1, Math.min(1, c[i])) * 32767;
  const wav = new ArrayBuffer(44 + pcm.byteLength), v = new DataView(wav);
  const ws = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  ws(0,'RIFF'); v.setUint32(4,36+pcm.byteLength,true); ws(8,'WAVEfmt '); v.setUint32(16,16,true);
  v.setUint16(20,1,true); v.setUint16(22,1,true); v.setUint32(24,rate,true); v.setUint32(28,rate*2,true);
  v.setUint16(32,2,true); v.setUint16(34,16,true); ws(36,'data'); v.setUint32(40,pcm.byteLength,true);
  new Int16Array(wav, 44).set(pcm);
  useRef(new Uint8Array(wav), 'audio/wav', `New recording (${(len / rate).toFixed(1)} s)`, $('recScript').value);
}

$('enrollBtn').onclick = async () => {
  const name = $('enrollName').value.trim();
  if (!name) { $('enrollName').focus(); $('recStatus').textContent = 'Name the voice first.'; return; }
  const id = name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
  if ([...$('speakerSel').options].some(o => (id && o.value === id) || o.textContent === name) && !confirm(`Replace the saved voice "${name}" with this recording?`)) return;
  $('enrollBtn').disabled = true; $('enrollBtn').textContent = 'Saving…';
  try {
    const r = await fetch('/api/enroll', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ name, wav_b64: customRefB64, text: customRefText }) });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    await loadSpeakers(d.speaker_id);
    customRefB64 = null; customRefText = '';
    $('enrollRow').hidden = true;
    $('recStatus').textContent = `Saved as ${d.name}: ${d.duration_s} s, ${d.snr_db} dB clean. It's selected above.`;
  } catch (err) {
    $('recStatus').textContent = 'Not saved: ' + err.message;
  } finally {
    $('enrollBtn').disabled = false; $('enrollBtn').textContent = 'Save as Voice';
  }
};

$('genBtn').onclick = async () => {
  $('genBtn').disabled = true;
  $('genStatus').textContent = 'Synthesizing in your cloned voice...';
  try {
    const body = {
      speaker: $('speakerSel').value,
      custom_wav_b64: customRefB64,
      custom_ref_text: customRefText,
      text: $('paraTxt').value,
      language: $('langSel').value,
      class_temperature: parseFloat($('ctSlider').value),
      position_temperature: parseFloat($('ptSlider').value),
      speed: parseFloat($('spSlider').value),
      guidance_scale: parseFloat($('gsSlider').value),
      num_step: parseInt($('stSlider').value, 10),
      seed: parseInt($('seedInp').value, 10),
      brand_safe: $('brandNorm').checked,
    };
    const r = await fetch('/api/generate', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    $('playerBox').style.display = 'grid';
    $('audioEl').src = d.audio_url;
    $('dlLink').href = d.audio_url;
    $('outStats').innerHTML = `<span>Audio: <b>${d.sec}s</b></span><span>Latency: <b>${d.took}s</b></span><span>RTF: <b>${d.rtf}</b></span><span>Temp: <b>${body.class_temperature}</b></span>`;
    $('normText').textContent = 'Spoken text: ' + d.spoken_text;
    $('genStatus').textContent = 'Ready';
    const card = document.createElement('div');
    card.className = 'take';
    card.innerHTML = `<div class="take-top"><span><b>${body.speaker}</b> · T=${body.class_temperature} · Speed=${body.speed}× · ${d.sec}s</span><span>${d.took}s</span></div><div>${d.spoken_text}</div><audio controls src="${d.audio_url}"></audio>`;
    if ($('takesList').querySelector('.sub')) $('takesList').innerHTML = '';
    $('takesList').prepend(card);
  } catch (e) {
    $('genStatus').textContent = 'Error: ' + e.message;
  } finally {
    $('genBtn').disabled = false;
  }
};
</script>
</body>
</html>
"""


class StudioHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            raw = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == "/api/speakers":
            self._json(list_speakers())
        elif self.path.startswith("/takes/"):
            fname = os.path.basename(self.path.split("?", 1)[0])
            fpath = OUT_DIR / fname
            if not fpath.exists():
                self.send_error(404)
                return
            data = fpath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/enroll":
            try:
                ln = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(ln).decode("utf-8"))
                self._json(enroll_voice(req.get("name") or "", base64.b64decode(req.get("wav_b64") or ""), req.get("text") or ""))
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                self._json({"error": f"Enrollment failed: {e}"}, 500)
            return
        if self.path != "/api/generate":
            self.send_error(404)
            return
        tmp_ref = None
        try:
            ln = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(ln).decode("utf-8"))
            text = normalize_brand_names((req.get("text") or "").strip(), req.get("brand_safe", True))
            if not text:
                self._json({"error": "Enter a paragraph first."}, 400)
                return

            OUT_DIR.mkdir(parents=True, exist_ok=True)
            ref_path = None
            ref_text = None
            if req.get("custom_wav_b64"):  # an unsaved recording: used for this take, then deleted
                tmp_ref = OUT_DIR / f"ref_{uuid.uuid4().hex[:8]}.wav"
                tmp_ref.write_bytes(base64.b64decode(req["custom_wav_b64"]))
                ref_path = str(tmp_ref)
                ref_text = (req.get("custom_ref_text") or "").strip() or None
            else:
                spk = next((s for s in list_speakers() if s["id"] == req.get("speaker")), None)
                if not spk:
                    self._json({"error": "Speaker reference not found."}, 404)
                    return
                ref_path, ref_text = spk["wav"], spk["ref_text"] or None

            from omnivoice.models.omnivoice import OmniVoiceGenerationConfig
            model = _get_model()
            cfg = OmniVoiceGenerationConfig(
                num_step=int(req.get("num_step", 28)),
                guidance_scale=float(req.get("guidance_scale", 2.2)),
                position_temperature=float(req.get("position_temperature", 5.0)),
                class_temperature=float(req.get("class_temperature", 0.20)),
            )
            seed = int(req.get("seed", 0))
            if seed < 0:
                seed = int(time.time() * 1000) % 100000
            t0 = time.perf_counter()
            with _GEN_LOCK:
                # generate() drops **kwargs such as seed= when a generation_config is given: seed torch itself
                torch.manual_seed(seed)
                w = model.generate(
                    text=text,
                    language=req.get("language", "ory"),
                    ref_audio=ref_path,
                    ref_text=ref_text,
                    speed=float(req.get("speed", 1.0)),
                    generation_config=cfg,
                )[0]
            w = w.detach().cpu().numpy() if hasattr(w, "detach") else np.asarray(w)
            w = w.squeeze().astype(np.float32)
            sr = model.sampling_rate
            out_name = f"take_{uuid.uuid4().hex[:10]}.wav"
            sf.write(str(OUT_DIR / out_name), w, sr)
            took = round(time.perf_counter() - t0, 2)
            sec = round(len(w) / sr, 2)
            self._json({
                "audio_url": f"/takes/{out_name}",
                "sec": sec,
                "took": took,
                "rtf": round(took / max(0.01, sec), 2),
                "spoken_text": text,
                "seed": seed,
            })
        except Exception as e:
            self._json({"error": str(e)}, 500)
        finally:
            if tmp_ref is not None:
                tmp_ref.unlink(missing_ok=True)


def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), StudioHandler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Vaani Voice Studio listening on {url}", flush=True)
    if os.environ.get("VAANI_STUDIO_NO_OPEN") != "1":
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
