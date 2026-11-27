"""Meeting mode: spot a call, record your mic plus the call audio, caption it live with
speaker labels, and turn the transcript into notes."""
import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np
import psutil  # type: ignore[import]
import sounddevice as sd

SR = 16000

# Exe-name fragment -> meeting app. Matched against processes that hold a mic open right now,
# so Teams sitting in the tray all day doesn't count; only a live call does.
_APPS = [
    ("zoom", "Zoom"),
    ("teams", "Microsoft Teams"),
    ("webex", "Webex"),
    ("ciscocollabhost", "Webex"),
    ("slack", "Slack Huddle"),
    ("discord", "Discord"),
    ("whatsapp", "WhatsApp"),
    ("skype", "Skype"),
    ("telegram", "Telegram"),
    ("gotomeeting", "GoToMeeting"),
    ("bluejeans", "BlueJeans"),
    ("ringcentral", "RingCentral"),
    ("chime", "Amazon Chime"),
    ("lark", "Lark"),
    ("feishu", "Feishu"),
    ("dingtalk", "DingTalk"),
]
_BROWSERS = (
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "arc.exe",
    "vivaldi.exe", "zen.exe", "waterfox.exe", "sidekick.exe"
)
# Browser window title -> meeting app (only the active tab's title is visible).
_WEB = [(re.compile(r"^Meet\b|meet\.google\.com", re.I), "Google Meet"),
        (re.compile(r"\bZoom\b", re.I), "Zoom"),
        (re.compile(r"\bTeams\b", re.I), "Microsoft Teams"),
        (re.compile(r"\bWebex\b", re.I), "Webex")]


def app_for(process: str, titles: List[str]) -> Optional[str]:
    """Which meeting app a mic-using process is. Browsers are judged by their window titles."""
    p = process.lower()
    if p in _BROWSERS:
        for title in titles:
            for pat, name in _WEB:
                if pat.search(title):
                    return name
        return None
    for frag, name in _APPS:
        if frag in p:
            return name
    return None


_MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
# Our own process holds the mic for dictation and meeting capture; never mistake it for a call.
_SELF = {"python.exe", "pythonw.exe", "py.exe", "vaani.exe", "vaani.exe", "openflow.exe", os.path.basename(sys.executable).lower()}


def _is_self(exe: str) -> bool:
    return exe in _SELF or "vaani" in exe or "antigravity" in exe


def _mic_process_names() -> set:
    """Exe names actively recording from any microphone via Windows CapabilityAccessManager."""
    names = set()
    roots = [_MIC_KEY + r"\NonPackaged", _MIC_KEY]
    import winreg
    for base in roots:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as key:
                count = winreg.QueryInfoKey(key)[0]
                for i in range(count):
                    subkey_name = winreg.EnumKey(key, i)
                    if subkey_name == "NonPackaged":
                        continue
                    try:
                        with winreg.OpenKey(key, subkey_name) as sub:
                            stop, _ = winreg.QueryValueEx(sub, "LastUsedTimeStop")
                            # If stop == 0, the microphone is actively capturing audio right now
                            if stop == 0:
                                exe = subkey_name.replace("#", "\\").split("\\")[-1].lower()
                                names.add(exe)
                    except Exception:
                        pass
        except Exception:
            pass
    return names


def _window_titles(names: set) -> List[str]:
    """Titles of visible top-level windows owned by processes with these exe names."""
    user32 = ctypes.windll.user32
    titles: List[str] = []
    pid_names: dict = {}  # a browser owns dozens of windows; look each pid up once

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pid_names:
                try:
                    pid_names[pid.value] = psutil.Process(pid.value).name().lower()
                except psutil.Error:
                    pid_names[pid.value] = ""
            if pid_names[pid.value] in names:
                buf = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, buf, 512)
                if buf.value:
                    titles.append(buf.value)
        return True

    user32.EnumWindows(cb, 0)
    return titles


def active_meeting_app() -> Optional[str]:
    """Name of the meeting app holding a microphone right now, else None.

    Only a live mic counts: an open Teams window or a browser tab titled "Zoom" is not a call,
    and treating it as one kept meetings from ever auto-stopping. One registry read when no
    browser is on the mic, plus one window walk when one is."""
    try:
        mic_procs = {p for p in _mic_process_names() if not _is_self(p)}
        browsers = {p for p in mic_procs if p in _BROWSERS}
        titles = _window_titles(browsers) if browsers else []
        for p in mic_procs:
            app = app_for(p, titles)
            if app:
                return app
    except Exception as e:
        print(f"[Vaani Meeting] Detection error: {e}")
    return None


def watch_mic(on_change: Callable[[], None]) -> None:
    """Call on_change whenever any app starts or stops using a microphone.

    Windows rewrites the CapabilityAccessManager key at that moment, so a blocking
    RegNotifyChangeKeyValue wakes us within milliseconds with no polling. Returns at once;
    the watch runs on a daemon thread and quietly ends if the key can't be watched."""
    import winreg

    def run():
        notify = ctypes.windll.advapi32.RegNotifyChangeKeyValue
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _MIC_KEY)
        except OSError:
            return
        with key:
            while True:
                # watch subtree, REG_NOTIFY_CHANGE_LAST_SET, synchronous (blocks until a write)
                if notify(wintypes.HKEY(key.handle), True, 0x4, None, False) != 0:
                    return
                time.sleep(0.15)  # an app writes Start then Stop values; read once they settle
                on_change()

    threading.Thread(target=run, daemon=True, name="openflow-mic-watch").start()


class _Segmenter:
    """Cuts a continuous audio feed into utterances at pauses, so each caption is a sentence or so."""

    def __init__(self, threshold: float, emit: Callable[[np.ndarray, float], None]):
        self.threshold = threshold
        self.emit = emit
        self._buf: List[np.ndarray] = []
        self._lead = np.zeros(0, dtype=np.float32)  # 0.3 s before speech, so first syllables survive
        self._speech = 0.0
        self._silence = 0.0
        self._t0 = 0.0
        self._voiced: List[Tuple[float, float]] = []  # (time, seconds) of loud chunks, last 30 s
        self.clock = time.time  # a file's position replaces wall time when transcribing from disk

    def voiced_between(self, t0: float, t1: float) -> float:
        return sum(d for t, d in self._voiced if t0 <= t <= t1)

    def feed(self, x: np.ndarray):
        dur = len(x) / SR
        loud = float(np.sqrt(np.mean(x ** 2))) >= self.threshold
        if loud:
            now = self.clock()
            self._voiced = [v for v in self._voiced if v[0] > now - 30] + [(now, dur)]
        if not self._buf:
            if loud:
                self._buf, self._speech, self._silence, self._t0 = [self._lead, x], dur, 0.0, self.clock()
            else:
                self._lead = np.concatenate([self._lead, x])[-int(SR * 0.3):]
            return
        self._buf.append(x)
        if loud:
            self._speech += dur
            self._silence = 0.0
        else:
            self._silence += dur
        total = sum(len(b) for b in self._buf) / SR
        if self._silence >= 0.7 or total >= 15:
            self.flush()

    def flush(self):
        if self._buf and self._speech >= 0.4:
            self.emit(np.concatenate(self._buf), self._t0)
        self._buf, self._lead = [], np.zeros(0, dtype=np.float32)


_SPEAKER_MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
                      "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")
_SPEAKER_MODEL = Path.home() / ".vaani" / "models" / "speaker-eres2net.onnx"


class _Speakers:
    """Labels call voices Person 1, Person 2... by comparing voice fingerprints."""

    MATCH = 0.55  # ERes2Net cosine threshold for distinct speaker clusters
    # 1.5 s windows score lower than whole clips; a clip is cut only after three windows (3 s) of a voice
    # this far from the 3 s before it. ponytail: calibration knob, tune on real calls if turns still merge or split.
    SPLIT = 0.45

    def __init__(self):
        self._extractor = None
        self._sums: List[np.ndarray] = []  # sum of every clip's embedding per speaker: no drift to the latest clip
        self._last = "Person 1"
        try:
            import sherpa_onnx
            if not _SPEAKER_MODEL.exists():
                _SPEAKER_MODEL.parent.mkdir(parents=True, exist_ok=True)
                tmp = _SPEAKER_MODEL.with_suffix(".part")
                urllib.request.urlretrieve(_SPEAKER_MODEL_URL, tmp)
                tmp.replace(_SPEAKER_MODEL)
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(_SPEAKER_MODEL), num_threads=2))
        except Exception as e:
            print(f"[Vaani Meeting] Speaker labels unavailable ({e}); call audio will be one speaker")

    def _embed(self, audio: np.ndarray) -> np.ndarray:
        stream = self._extractor.create_stream()
        stream.accept_waveform(SR, audio)
        stream.input_finished()
        emb = np.array(self._extractor.compute(stream), dtype=np.float32)
        return emb / (np.linalg.norm(emb) + 1e-9)

    def split(self, audio: np.ndarray, at: int = 0) -> List[Tuple[int, np.ndarray]]:
        """(sample offset, piece) per voice. Back-to-back turns with no pause between them reach us as
        one clip; labelled whole, the second person's words went under the first person's name."""
        win, hop = int(1.5 * SR), int(0.75 * SR)
        if self._extractor is None or len(audio) < 2 * win:
            return [(at, audio)]
        starts = range(0, len(audio) - win + 1, hop)
        rms = [float(np.sqrt(np.mean(audio[i:i + win] ** 2))) for i in starts]
        floor = 0.3 * float(np.median(rms))  # a pause or trailing silence fingerprints as "someone else"
        embs = [self._embed(audio[i:i + win]) if r >= floor else None for i, r in zip(starts, rms)]
        voiced = [k for k, e in enumerate(embs) if e is not None]
        if len(voiced) < 6:  # 3 s of one voice, then 3 s of another: shorter turns cost more in false cuts
            return [(at, audio)]
        ref = sum(embs[k] for k in voiced[:3])
        for i, k in enumerate(voiced[3:-2], start=3):
            r = ref / np.linalg.norm(ref)
            if all(r @ embs[j] < self.SPLIT for j in voiced[i:i + 3]):
                # window k is the new voice, k-1 was not: cut at the quietest 0.1 s around k's start,
                # where people take a breath between turns
                lo, hi = max(SR // 2, (k - 1) * hop), min(len(audio) - SR // 2, (k + 1) * hop)
                frames = audio[lo:lo + max(0, hi - lo) // 1600 * 1600].reshape(-1, 1600)
                cut = lo + int(np.argmin((frames ** 2).mean(axis=1))) * 1600 + 800 if len(frames) else k * hop
                return [(at, audio[:cut])] + self.split(audio[cut:], at + cut)
            ref = ref + embs[k]
        return [(at, audio)]

    def label(self, audio: np.ndarray) -> str:
        if self._extractor is None or len(audio) < SR:  # under a second is too little to fingerprint
            return self._last
        emb = self._embed(audio)
        scores = [float(s @ emb) / float(np.linalg.norm(s)) for s in self._sums]
        if scores and max(scores) >= self.MATCH:
            i = int(np.argmax(scores))
            self._sums[i] = self._sums[i] + emb
        else:
            self._sums.append(emb)
            i = len(self._sums) - 1
        self._last = f"Person {i + 1}"
        return self._last


# Speaker accent themes: Apple-grade palette for clean differentiation
SPEAKER_PALETTE = {
    "You": {"color": "#38bdf8", "border": "#0284c7", "bg": "rgba(56, 189, 248, 0.14)"},
    "Person 1": {"color": "#34d399", "border": "#059669", "bg": "rgba(52, 211, 153, 0.14)"},
    "Person 2": {"color": "#fbbf24", "border": "#d97706", "bg": "rgba(251, 191, 36, 0.14)"},
    "Person 3": {"color": "#c084fc", "border": "#9333ea", "bg": "rgba(192, 132, 252, 0.14)"},
    "Person 4": {"color": "#fb7185", "border": "#e11d48", "bg": "rgba(251, 113, 133, 0.14)"},
    "Person 5": {"color": "#22d3ee", "border": "#0891b2", "bg": "rgba(34, 211, 238, 0.14)"},
}


def speaker_theme(who: str) -> dict:
    """Retrieve consistent color theme for any speaker name."""
    for k, v in SPEAKER_PALETTE.items():
        if k.lower() == who.lower() or (k == "You" and "you" in who.lower()):
            return v
    # Person N extraction
    m = re.search(r"person\s*(\d+)", who, re.I)
    if m:
        idx = int(m.group(1))
        keys = list(SPEAKER_PALETTE.keys())
        key = f"Person {idx}" if f"Person {idx}" in SPEAKER_PALETTE else keys[1 + (idx % 4)]
        return SPEAKER_PALETTE[key]
    # Fallback pseudo-random stable choice based on name hash
    choices = list(SPEAKER_PALETTE.values())
    return choices[abs(hash(who)) % len(choices)]


INDIC_MEETING_PROMPT = (
    "Meeting notes in Indian English, Hinglish, Hindi, and Odia. "
    "Accurately transcribe Hindi and Odia phrases in clean Latin/Roman script or Devanagari. "
    "Common terms: kya, kyu, kaise, kuch, nahi, haan, theek, accha, bhai, yaar, "
    "kete, kan, kemiti, kichi, ghare, aama, bhala, namaskar, dhanyabad. "
    "Never output Urdu or Arabic script."
)


class Line(tuple):
    """(seconds, speaker, text) plus .end, the second the voice stopped; unpacks like a plain 3-tuple."""

    def __new__(cls, start: float, who: str, text: str, end: float):
        line = super().__new__(cls, (start, who, text))
        line.end = end
        return line


def _voice_seconds(audio: np.ndarray) -> float:
    """Length up to the last 20 ms frame with voice in it: an utterance ends with ~0.7 s of the pause
    that cut it, and a subtitle shouldn't stay up through that."""
    n = 320
    rms = np.sqrt((audio[: len(audio) // n * n].reshape(-1, n) ** 2).mean(axis=1))
    if not len(rms) or rms.max() <= 0:
        return len(audio) / SR
    return (int(np.nonzero(rms >= 0.1 * rms.max())[0][-1]) + 1) * n / SR


class MeetingSession:
    """Records mic + call audio, transcribes each utterance in the background, reports captions."""

    def __init__(self, transcriber, language: str, on_line: Callable[[str, str], None],
                 mic_device: Optional[int] = None, mic: bool = True,
                 on_segment: Optional[Callable[[float, float, str, str], None]] = None):
        self.transcriber = transcriber
        self.mic = mic  # False: only what the speakers play (podcasts, videos)
        self.language = language
        self.on_line = on_line
        self.on_segment = on_segment
        self.mic_device = mic_device
        self.lines: List[Tuple[float, str, str]] = []
        self.started = 0.0
        self._q: "queue.Queue" = queue.Queue()
        self._mic = _Segmenter(0.010, self._mic_clip)
        self._call = _Segmenter(0.004, lambda a, t: self._q.put(("call", a, t)))
        self._mic_stream = None
        self._pa = None
        self._call_stream = None
        self._worker = None

    def _mic_clip(self, audio: np.ndarray, t0: float):
        # Without headphones the mic also hears the call. Drop mic clips the call was mostly talking over.
        dur = len(audio) / SR
        if self._call.voiced_between(t0, t0 + dur) > 0.5 * dur:
            return
        self._q.put(("mic", audio, t0))

    def start(self):
        self.started = time.time()
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()
        if not self.mic:
            self._open_call_audio()  # nothing else to record: let a failure reach the caller
            return
        self._mic_stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", device=self.mic_device,
                                          blocksize=1600, callback=lambda d, *_: self._mic.feed(d[:, 0].copy()))
        self._mic_stream.start()
        try:
            self._open_call_audio()
        except Exception as e:
            print(f"[Vaani Meeting] Call audio unavailable ({e}); recording mic only")

    def transcribe_file(self, path: str) -> List[Tuple[float, str, str]]:
        """An audio or video file (podcast, recording): same utterance cuts and speaker labels as a call."""
        from faster_whisper.audio import decode_audio  # PyAV: mp3, m4a, webm, mp4, wav...
        audio = decode_audio(path, sampling_rate=SR)  # ponytail: whole file in memory, ~230 MB an hour
        pos = [0.0]
        self.started, self._call.clock = 0.0, lambda: pos[0]
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()
        for i in range(0, len(audio), 1600):
            self._call.feed(audio[i:i + 1600])
            pos[0] += 0.1
        return self.stop()

    def _open_call_audio(self):
        """What the speakers play (WASAPI loopback): everyone else on the call."""
        import pyaudiowpatch as pyaudio
        self._pa = pyaudio.PyAudio()
        dev = self._pa.get_default_wasapi_loopback()
        rate, ch = int(dev["defaultSampleRate"]), int(dev["maxInputChannels"])

        def cb(data, frames, info, status):
            x = np.frombuffer(data, dtype=np.float32).reshape(-1, ch).mean(axis=1)
            n = int(len(x) * SR / rate)
            self._call.feed(np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32))
            return (None, pyaudio.paContinue)

        self._call_stream = self._pa.open(format=pyaudio.paFloat32, channels=ch, rate=rate, input=True,
                                          input_device_index=dev["index"], frames_per_buffer=rate // 10,
                                          stream_callback=cb)

    def _work(self):
        speakers = _Speakers()
        while True:
            item = self._q.get()
            if item is None:
                return
            src, audio, t0 = item
            for start, piece in (speakers.split(audio) if src == "call" else [(0, audio)]):
                self._caption(src, piece, t0 + start / SR, speakers)

    def _caption(self, src: str, audio: np.ndarray, t0: float, speakers: "_Speakers"):
        try:
            # Prime Whisper with anti-Urdu / Indic biasing prompt so Hindi/Odia speech is never misclassified
            text = self.transcriber.transcribe(
                audio,
                language=self.language,
                initial_prompt=INDIC_MEETING_PROMPT
            )[0].strip()
        except Exception as e:
            print(f"[Vaani Meeting] Transcription failed: {e}")
            return
        if not text:
            return
        who = "You" if src == "mic" else speakers.label(audio)
        start = t0 - self.started
        end = start + _voice_seconds(audio)
        self.lines.append(Line(start, who, text, end))
        if self.on_segment is not None:
            try:
                self.on_segment(start, end, who, text)
            except Exception:
                pass
        try:
            self.on_line(who, text)
        except Exception:
            pass

    def stop(self) -> List[Tuple[float, str, str]]:
        """Stops recording, finishes transcribing what is queued, returns (seconds, speaker, text) lines."""
        for s in (self._mic_stream, self._call_stream):
            try:
                if s is not None:
                    s.stop_stream() if hasattr(s, "stop_stream") else s.stop()
                    s.close()
            except Exception:
                pass
        if self._pa is not None:
            self._pa.terminate()
        self._mic.flush()
        self._call.flush()
        self._q.put(None)
        if self._worker:
            self._worker.join()
        # mic and call clips finish out of order (a 15 s call clip lands after a later mic one): sort by start
        self.lines.sort(key=lambda line: line[0])
        return self.lines


def group_speaker_turns(lines: List[Tuple[float, str, str]]) -> List[dict]:
    """
    Groups consecutive utterances by the same speaker into dialog turns. A pause over 30 s, or a turn
    running past a minute, starts a new one, so a long monologue still gets a heading and time tag each minute.
    """
    turns: List[dict] = []
    for t, who, text in lines:
        if not text or not text.strip():
            continue
        cleaned = text.strip()
        # If the same speaker spoke again within 30 seconds, append to the current turn
        if turns and turns[-1]["who"] == who and (t - turns[-1]["t_end"] <= 30.0) and (t - turns[-1]["t_start"] <= 60.0):
            turns[-1]["t_end"] = t
            turns[-1]["texts"].append(cleaned)
        else:
            turns.append({
                "who": who,
                "t_start": t,
                "t_end": t,
                "texts": [cleaned],
            })
    return turns


def _srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _cue_lines(text: str, width: int = 42) -> str:
    """One cue's text as at most two lines of ~42 characters (the usual broadcast limit)."""
    words, rows = text.split(), [""]
    for w in words:
        if rows[-1] and len(rows[-1]) + 1 + len(w) > width:
            rows.append(w)
        else:
            rows[-1] = f"{rows[-1]} {w}".strip()
    return "\n".join(rows)


def srt_text(lines: List[Tuple[float, str, str]], max_chars: int = 84) -> str:
    """SubRip subtitles. A cue runs until the voice stopped (Line.end; plain tuples get an estimate of
    ~15 characters a second, 1.2-7 s), held at least 1 s so it can be read, never past the next line's
    start. A long utterance is split into two-line cues with its time shared out by length."""
    cues = []
    for i, line in enumerate(lines):
        start, _who, text = line
        nxt = lines[i + 1][0] if i + 1 < len(lines) else float("inf")
        end = getattr(line, "end", None)
        chunks, cur = [], ""
        for w in text.split():
            if cur and len(cur) + 1 + len(w) > max_chars:
                chunks.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            chunks.append(cur)
        said = end - start if end is not None else sum(min(7.0, max(1.2, len(c) / 15.0)) for c in chunks)
        span = max(0.1, min(nxt - start, max(1.0, said)))
        total, t = sum(len(c) for c in chunks) or 1, start
        for c in chunks:
            stop = t + span * len(c) / total
            cues.append((t, stop, c))
            t = stop
    return "\n".join(f"{n}\n{_srt_time(a)} --> {_srt_time(b)}\n{_cue_lines(c)}\n"
                     for n, (a, b, c) in enumerate(cues, 1))


def transcript_text(lines: List[Tuple[float, str, str]]) -> str:
    """Format lines into cleanly structured Markdown transcript with separated speaker turns."""
    turns = group_speaker_turns(lines)
    if not turns:
        return "\n\n".join(f"[{int(t) // 60:02d}:{int(t) % 60:02d}] {who}: {text}" for t, who, text in lines)

    blocks: List[str] = []
    for turn in turns:
        s_min, s_sec = int(turn["t_start"]) // 60, int(turn["t_start"]) % 60
        e_min, e_sec = int(turn["t_end"]) // 60, int(turn["t_end"]) % 60
        time_tag = f"[{s_min:02d}:{s_sec:02d}]" if turn["t_start"] == turn["t_end"] else f"[{s_min:02d}:{s_sec:02d} - {e_min:02d}:{e_sec:02d}]"
        speech = "\n\n".join(turn["texts"])  # one paragraph per utterance, never run together
        blocks.append(f"### {turn['who']} · {time_tag}\n\n{speech}")
    return "\n\n".join(blocks)


_DECISION = re.compile(r"\b(decided|decide|agreed|agree|final|approved|going with|we will|we'll)\b", re.I)
_ACTION = re.compile(r"\b(action item|todo|to-do|follow up|need to|needs to|will|by (tomorrow|monday|tuesday|"
                     r"wednesday|thursday|friday|eod|end of day|next week))\b", re.I)


def _ollama(prompt: str) -> Optional[str]:
    """Local LLM through Ollama, if it is running. Nothing leaves the machine."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as r:
            models = json.load(r).get("models", [])
        if not models:
            return None
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=json.dumps({"model": models[0]["name"], "prompt": prompt, "stream": False}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r).get("response", "").strip() or None
    except Exception:
        return None


def format_meeting_notes(transcript: str, app_name: str = "Meeting", llm=None) -> str:
    """Markdown notes: summary, decisions, action items, then the full transcript with clean separation."""
    prompt = (
        "You are writing meeting notes from a transcript. The transcript may mix languages; write the notes "
        "in English. Use exactly these markdown headings and nothing before them:\n"
        "## Meeting Summary\n(3-5 sentences)\n## Key Decisions\n(bullets, or 'None recorded')\n"
        "## Action Items\n(- [ ] task — owner if named)\n\nTranscript:\n" + transcript
    )
    notes = None
    if llm is not None:  # built-in writing model (engine/local_llm.py)
        try:
            notes = llm.chat("You write concise, accurate meeting notes.", prompt, max_tokens=700)
        except Exception as e:
            print(f"[Vaani] Local notes model failed ({e})")
    notes = notes or _ollama(prompt)
    if not notes or "## Action Items" not in notes:
        # Keyword rules when no local LLM; clean offline and instant
        body = re.sub(r"^### [^\n]+\n+", "", transcript, flags=re.M)
        body = re.sub(r"^\[\d+:\d+\] [^:]+: ", "", body, flags=re.M)
        sentences = [s.strip() for s in re.split(r"(?<=[.!?।])\s+|\n+", body) if s.strip()]
        decisions = [s for s in sentences if _DECISION.search(s)]
        actions = [s for s in sentences if _ACTION.search(s) and s not in decisions]
        notes = "\n\n".join([
            "## Meeting Summary\n" + (" ".join(sentences[:3]) or "Nothing was transcribed."),
            "## Key Decisions\n" + ("\n".join(f"- {s}" for s in decisions) or "None recorded"),
            "## Action Items\n" + ("\n".join(f"- [ ] {s}" for s in actions) or "None recorded"),
        ])
    return f"# {app_name} notes\n\n{notes}\n\n## Transcript\n\n{transcript}\n"


def render_meeting_html(markdown_notes: str, app_name: str = "Meeting") -> str:
    """
    Renders structured meeting notes into a rich, beautiful Apple-grade dark UI document.
    Differentiates speakers with high-contrast color badges, avatar tags, and dedicated speech cards.
    """
    esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Parse sections out of the markdown
    summary_text = ""
    decisions_list: List[str] = []
    actions_list: List[str] = []
    transcript_raw = ""

    lines = markdown_notes.split("\n")
    curr_sec = ""
    transcript_lines: List[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## Meeting Summary"):
            curr_sec = "summary"
            continue
        elif stripped.startswith("## Key Decisions"):
            curr_sec = "decisions"
            continue
        elif stripped.startswith("## Action Items"):
            curr_sec = "actions"
            continue
        elif stripped.startswith("## Transcript"):
            curr_sec = "transcript"
            continue

        if curr_sec == "summary":
            if stripped and not stripped.startswith("#"):
                summary_text += (" " if summary_text else "") + stripped
        elif curr_sec == "decisions":
            if stripped.startswith("-"):
                decisions_list.append(stripped[1:].strip())
            elif stripped and "None recorded" not in stripped:
                decisions_list.append(stripped)
        elif curr_sec == "actions":
            if stripped.startswith("- [ ]") or stripped.startswith("- []"):
                actions_list.append(stripped[5:].strip())
            elif stripped.startswith("-"):
                actions_list.append(stripped[1:].strip())
            elif stripped and "None recorded" not in stripped:
                actions_list.append(stripped)
        elif curr_sec == "transcript":
            transcript_lines.append(line)

    transcript_raw = "\n".join(transcript_lines).strip()

    # Qt rich text has no flexbox or grid, so this is plain headings, paragraphs and lists in the dashboard's own greys.
    muted = "#8e8e93"

    def section(title: str, body: str) -> str:
        return f'<p style="margin:22px 0 6px 0;"><span style="font-size:12px;font-weight:600;color:{muted};">{title}</span></p>{body}'

    empty = f'<p style="color:{muted};margin:0;">None recorded</p>'
    decisions_html = ("<ul style='margin:0 0 0 18px;padding:0;'>" + "".join(f"<li style='margin:0 0 4px 0;'>{esc(d)}</li>" for d in decisions_list) + "</ul>") if decisions_list else empty
    actions_html = "".join(f"<p style='margin:0 0 6px 0;'><span style='color:#0a84ff;'>&#9744;</span>&nbsp; {esc(x)}</p>" for x in actions_list) or empty

    # Transcript: a heading line per speaker turn, then one paragraph per utterance
    pattern_turn = re.compile(r"###\s+([^·\n]+)\s*·\s*(\[[^\]]+\])")
    pattern_old = re.compile(r"^\[(\d+:\d+)\]\s*([^:]+):\s*(.*)$")
    turns: List[Tuple[str, str, List[str]]] = []
    for line in transcript_raw.split("\n"):
        line_s = line.strip()
        if not line_s:
            continue
        m_turn = pattern_turn.match(line_s)
        m_old = pattern_old.match(line_s)
        if m_turn:
            turns.append((m_turn.group(1).strip(), m_turn.group(2).strip(), []))
        elif m_old:
            turns.append((m_old.group(2).strip(), f"[{m_old.group(1)}]", [m_old.group(3).strip()]))
        elif turns:
            turns[-1][2].append(line_s)
        else:
            turns.append(("You", "", [line_s]))
    parts = []
    for who, when, speech in turns:
        if not speech:
            continue
        parts.append(
            f'<p style="margin:16px 0 4px 0;"><b style="color:{speaker_theme(who)["color"]};">{esc(who)}</b>'
            f'&nbsp; <span style="color:{muted};font-size:12px;">{esc(when)}</span></p>'
            + "".join(f'<p style="margin:0 0 8px 0;">{esc(x)}</p>' for x in speech))
    transcript_html = "".join(parts) or f'<p style="color:{muted};">No transcript recorded</p>'

    return (
        '<html><head><meta charset="utf-8"></head>'
        '<body style="background:#1c1c1e;color:#f5f5f7;font-family:Inter,Segoe UI,sans-serif;font-size:13px;'
        'line-height:150%;margin:0;padding:6px 12px;">'
        f'<h1 style="font-size:20px;font-weight:600;margin:6px 0 0 0;">{esc(app_name)} notes</h1>'
        + section("Summary", f"<p style='margin:0;'>{esc(summary_text) or 'Nothing was transcribed.'}</p>")
        + section("Decisions", decisions_html)
        + section("Action items", actions_html)
        + section("Transcript", transcript_html)
        + "</body></html>")
