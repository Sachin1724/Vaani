"""
training_dashboard.py — Live Interactive Training & Accuracy Dashboard for Vaani / MIRA
======================================================================================
Serves a real-time web dashboard at http://127.0.0.1:8765 reading:
  - vaani/data/indian_asr/training_status.json
  - dataset counts & sample pairs from Svarah + Raj Shamani / Nikhil Kamath podcasts
  - live NVIDIA RTX 4050 VRAM / utilization / temperature via nvidia-smi

Run from repo root: python -m vaani.training_dashboard
"""

import http.server
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "indian_asr"
STATUS_PATH = DATA_DIR / "training_status.json"
PORT = int(os.environ.get("VAANI_DASH_PORT", "8765"))


def gpu_stats():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
            text=True, timeout=2,
        ).strip().splitlines()[0]
        parts = [p.strip() for p in out.split(",")]
        return {
            "name": parts[0],
            "vram_used_mb": float(parts[1]),
            "vram_total_mb": float(parts[2]),
            "util_pct": float(parts[3]),
            "temp_c": float(parts[4]),
            "power_w": float(parts[5]),
        }
    except Exception:
        return {"name": "NVIDIA RTX 4050", "vram_used_mb": 0, "vram_total_mb": 6141, "util_pct": 0, "temp_c": 0, "power_w": 0}


def count_lines(p: Path) -> int:
    if not p.exists():
        return 0
    with open(p, encoding="utf-8") as f:
        return sum(1 for l in f if l.strip())


def sample_errors():
    p = DATA_DIR / "eval.jsonl"
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("baseline_wer", 0) > 0 and r.get("whisper_small"):
                out.append({
                    "state": r.get("state", "India"),
                    "ref": r["text"],
                    "hyp": r["whisper_small"],
                    "wer": round(r["baseline_wer"] * 100, 1),
                })
            if len(out) >= 12:
                break
    return out


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Vaani · MIRA & Whisper Indian Training Studio</title>
<style>
  :root {
    --bg: #f6f4ef; --card: #ffffff; --ink: #16181d; --muted: #646b77;
    --line: #e4dfd5; --accent: #2563eb; --accent2: #0f766e; --warn: #d97706;
    --ok: #15803d; --pill: #eef2ff;
  }
  [data-theme="dark"] {
    --bg: #111317; --card: #191c22; --ink: #f3f4f6; --muted: #9aa3b2;
    --line: #272c35; --accent: #60a5fa; --accent2: #2dd4bf; --warn: #f59e0b;
    --ok: #4ade80; --pill: #1e293b;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: "Poppins", "Segoe UI", system-ui, sans-serif; background: var(--bg); color: var(--ink); padding: 24px; transition: background .2s, color .2s; }
  .wrap { max-width: 1180px; margin: 0 auto; }
  header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
  h1 { font-size: 22px; margin: 0; font-weight: 600; letter-spacing: -0.02em; }
  .sub { color: var(--muted); font-size: 13px; margin-top: 2px; }
  .controls { display: flex; align-items: center; gap: 10px; }
  .badge { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 999px; background: var(--card); border: 1px solid var(--line); font-size: 12px; font-weight: 500; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); animation: pulse 1.4s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
  button { cursor: pointer; border: 1px solid var(--line); background: var(--card); color: var(--ink); padding: 7px 13px; border-radius: 999px; font-size: 12px; font-weight: 500; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; margin-bottom: 16px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 18px; }
  .label { font-size: 12px; color: var(--muted); font-weight: 500; text-transform: uppercase; letter-spacing: .04em; }
  .val { font-size: 26px; font-weight: 600; margin: 6px 0 4px; }
  .meta { font-size: 12px; color: var(--muted); }
  .bar-bg { height: 8px; background: var(--line); border-radius: 999px; overflow: hidden; margin-top: 10px; }
  .bar-fill { height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent2)); transition: width .35s ease; }
  .two { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
  @media (max-width: 860px) { .two { grid-template-columns: 1fr; } }
  svg.chart { width: 100%; height: 170px; display: block; margin-top: 10px; }
  .tabs { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
  .tab { padding: 6px 12px; border-radius: 10px; border: 1px solid var(--line); background: var(--pill); font-size: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { color: var(--muted); font-weight: 500; font-size: 12px; }
  code { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
  .err { color: #dc2626; } .good { color: var(--ok); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Vaani Live Training & Indian Speech Studio</h1>
      <div class="sub">MIRA (Qwen2.5-0.5B LoRA · CT2 int8) & Whisper-Small-IN · Svarah 19 States + Raj Shamani / Nikhil Kamath</div>
    </div>
    <div class="controls">
      <span class="badge"><span class="dot"></span><span id="stage-pill">Connecting...</span></span>
      <button onclick="toggleTheme()">Toggle Cream / Dark</button>
    </div>
  </header>

  <div class="grid">
    <div class="card">
      <div class="label">GPU Hardware</div>
      <div class="val" id="gpu-vram">--</div>
      <div class="meta" id="gpu-meta">Loading nvidia-smi...</div>
      <div class="bar-bg"><div class="bar-fill" id="gpu-bar" style="width:0%"></div></div>
    </div>
    <div class="card">
      <div class="label">MIRA-IN (Phonetic Refiner)</div>
      <div class="val" id="mira-step">Queued</div>
      <div class="meta" id="mira-meta">Qwen2.5-0.5B LoRA · 2,500 pairs</div>
      <div class="bar-bg"><div class="bar-fill" id="mira-bar" style="width:0%"></div></div>
    </div>
    <div class="card">
      <div class="label">Whisper-Small-IN (Acoustic)</div>
      <div class="val" id="wh-step">Queued</div>
      <div class="meta" id="wh-meta">4,179 Svarah + Raj Shamani clips</div>
      <div class="bar-bg"><div class="bar-fill" id="wh-bar" style="width:0%"></div></div>
    </div>
    <div class="card">
      <div class="label">Harvested Corpus</div>
      <div class="val" id="ds-total">--</div>
      <div class="meta" id="ds-meta">Speaker-disjoint splits</div>
    </div>
  </div>

  <div class="two">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <div class="label">MIRA-IN Training Loss</div>
          <div class="meta" id="mira-loss-txt">Waiting for first step...</div>
        </div>
        <span class="tab" id="mira-size-pill">~496 MB int8</span>
      </div>
      <svg class="chart" id="mira-chart" viewBox="0 0 500 160"></svg>
    </div>
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <div class="label">Whisper-Small-IN Training Loss</div>
          <div class="meta" id="wh-loss-txt">Queued after MIRA-IN finishes...</div>
        </div>
        <span class="tab" id="wh-size-pill">~245 MB int8</span>
      </div>
      <svg class="chart" id="wh-chart" viewBox="0 0 500 160"></svg>
    </div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px">
      <div>
        <div class="label">Held-Out Baseline Errors Targetted by Training</div>
        <div class="meta">Real Indian speaker mishearings from Whisper-small (Baseline Svarah WER: 9.96% @ 236 ms)</div>
      </div>
      <div class="tabs">
        <span class="tab">Raj Shamani · Business Journey (97)</span>
        <span class="tab">FO1 · Kunal Shah (120)</span>
        <span class="tab">FO583 · Sam Altman (120)</span>
        <span class="tab">Kumar Birla (120)</span>
        <span class="tab">FO5 · Nikhil Kamath Eval (120)</span>
      </div>
    </div>
    <table>
      <thead><tr><th>State</th><th>WER</th><th>Stock Whisper-small</th><th>Gold Reference</th></tr></thead>
      <tbody id="err-rows"></tbody>
    </table>
  </div>
</div>

<script>
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme');
  document.documentElement.setAttribute('data-theme', cur === 'dark' ? 'light' : 'dark');
}

function drawChart(svgId, points, valPoints) {
  const svg = document.getElementById(svgId);
  if (!points || points.length === 0) {
    svg.innerHTML = '<text x="250" y="85" text-anchor="middle" fill="#888" font-size="13">Waiting for training steps...</text>';
    return;
  }
  const W = 500, H = 160, pad = 22;
  const maxStep = Math.max(...points.map(p => p.step), 2);
  const allLosses = points.map(p => p.loss).concat((valPoints || []).map(p => p.loss));
  const minL = Math.min(...allLosses) * 0.92;
  const maxL = Math.max(...allLosses, minL + 0.2) * 1.05;
  const sx = s => pad + (s / maxStep) * (W - 2 * pad);
  const sy = l => H - pad - ((l - minL) / Math.max(1e-6, maxL - minL)) * (H - 2 * pad);
  const poly = points.map(p => `${sx(p.step).toFixed(1)},${sy(p.loss).toFixed(1)}`).join(' ');
  let extra = '';
  if (valPoints && valPoints.length) {
    valPoints.forEach(v => {
      extra += `<circle cx="${sx(v.step).toFixed(1)}" cy="${sy(v.loss).toFixed(1)}" r="4.5" fill="#0f766e"/><text x="${(sx(v.step)+7).toFixed(1)}" y="${(sy(v.loss)-6).toFixed(1)}" font-size="11" fill="#0f766e">val ${v.loss.toFixed(2)}</text>`;
    });
  }
  svg.innerHTML = `
    <line x1="${pad}" y1="${H-pad}" x2="${W-pad}" y2="${H-pad}" stroke="currentColor" stroke-opacity=".18"/>
    <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${H-pad}" stroke="currentColor" stroke-opacity=".18"/>
    <text x="${pad}" y="14" font-size="11" fill="#888">max ${maxL.toFixed(2)}</text>
    <text x="${pad}" y="${H-5}" font-size="11" fill="#888">step 0 → ${maxStep} · min ${minL.toFixed(2)}</text>
    <polyline fill="none" stroke="#2563eb" stroke-width="2.5" stroke-linejoin="round" points="${poly}"/>
    ${extra}
  `;
}

async function refresh() {
  try {
    const res = await fetch('/api/status');
    const d = await res.json();
    const g = d.gpu || {};
    const vPct = g.vram_total_mb ? (g.vram_used_mb / g.vram_total_mb * 100) : 0;
    document.getElementById('gpu-vram').textContent = `${Math.round(g.vram_used_mb || 0)} / ${Math.round(g.vram_total_mb || 6141)} MB`;
    document.getElementById('gpu-meta').textContent = `${g.name || 'RTX 4050'} · ${g.util_pct || 0}% GPU · ${g.temp_c || 0}°C · ${g.power_w || 0}W`;
    document.getElementById('gpu-bar').style.width = `${Math.min(100, vPct).toFixed(0)}%`;

    const c = d.counts || {};
    document.getElementById('ds-total').textContent = `${(c.train || 0) + (c.val || 0) + (c.eval || 0)} clips`;
    document.getElementById('ds-meta').textContent = `Train ${c.train || 0} · Val ${c.val || 0} · Eval ${c.eval || 0} · MIRA ${c.mira || 0}`;

    const st = d.status || {};
    document.getElementById('stage-pill').textContent = `Stage: ${st.active_stage || 'starting'}`;

    const m = st.mira || {};
    if (m.state) {
      const pct = m.total_steps ? (m.step / m.total_steps * 100) : 0;
      document.getElementById('mira-step').textContent = m.state === 'done' ? `Done (${m.size_mb || 496} MB)` : `${m.step || 0} / ${m.total_steps || '?'}`;
      document.getElementById('mira-meta').textContent = `Epoch ${m.epoch || 1}/${m.epochs || 2} · loss ${m.loss ?? '--'} · ${m.elapsed_s || 0}s (ETA ${m.eta_s || 0}s)`;
      document.getElementById('mira-bar').style.width = `${pct.toFixed(0)}%`;
      document.getElementById('mira-loss-txt').textContent = `Step ${m.step || 0}/${m.total_steps || '?'} · current loss ${m.loss ?? '--'}`;
      if (m.size_mb) document.getElementById('mira-size-pill').textContent = `${m.size_mb} MB int8`;
      drawChart('mira-chart', m.history, []);
    }

    const w = st.whisper || {};
    if (w.state) {
      const pct = w.total_steps ? (w.step / w.total_steps * 100) : 0;
      document.getElementById('wh-step').textContent = w.state === 'done' ? `Done (${w.size_mb || 245} MB)` : `${w.step || 0} / ${w.total_steps || '?'}`;
      document.getElementById('wh-meta').textContent = `Epoch ${w.epoch || 1}/${w.epochs || 2} · loss ${w.loss ?? '--'} · val ${w.val_loss ?? '--'} · ETA ${w.eta_s || 0}s`;
      document.getElementById('wh-bar').style.width = `${pct.toFixed(0)}%`;
      document.getElementById('wh-loss-txt').textContent = `Step ${w.step || 0}/${w.total_steps || '?'} · train ${w.loss ?? '--'} · val ${w.val_loss ?? '--'}`;
      if (w.size_mb) document.getElementById('wh-size-pill').textContent = `${w.size_mb} MB int8`;
      drawChart('wh-chart', w.history, w.val_history);
    }

    const tbody = document.getElementById('err-rows');
    if (d.samples && tbody.children.length === 0) {
      tbody.innerHTML = d.samples.map(s => `
        <tr>
          <td>${s.state || 'India'}</td>
          <td><b>${s.wer}%</b></td>
          <td class="err">${s.hyp}</td>
          <td class="good">${s.ref}</td>
        </tr>`).join('');
    }
  } catch (e) {}
}
refresh();
setInterval(refresh, 1500);
</script>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path == "/api/status":
            st = {}
            if STATUS_PATH.exists():
                try:
                    st = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
                except Exception:
                    st = {}
            payload = {
                "gpu": gpu_stats(),
                "status": st,
                "counts": {
                    "train": count_lines(DATA_DIR / "train.jsonl"),
                    "val": count_lines(DATA_DIR / "val.jsonl"),
                    "eval": count_lines(DATA_DIR / "eval.jsonl"),
                    "mira": count_lines(ROOT / "data" / "mira_indian_train.jsonl"),
                },
                "samples": sample_errors(),
                "ts": time.time(),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Vaani Training Dashboard listening on http://127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
