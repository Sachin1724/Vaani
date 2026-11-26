import ctypes
import time
import re
import threading
import numpy as np
import sounddevice as sd
from typing import Callable, Optional, List, Dict

SAMPLE_RATE = 16000
# Guard PortAudio device enumeration and stream lifecycle against sd._terminate()/_initialize() races
_pa_lock = threading.RLock()

# Pre-roll: keep this many samples in a ring buffer so the first
# syllable is never clipped when the user presses Ctrl+Shift.
PREROLL_SAMPLES = int(SAMPLE_RATE * 0.25)  # 250 ms @ 16 kHz
# ---------------------------------------------------------------------------
# NEAR-FIELD DOMINANT SPEAKER GATE
#
# Whoever speaks closest to the mic is loudest (inverse-square law).
# We calibrate dominant speaker energy from the first 0.6 s, then zero
# any 20 ms frame that falls more than GATE_DB dB below that level.
# No enrolment needed: the nearest, loudest voice wins automatically.
# ---------------------------------------------------------------------------
GATE_DB     = 20.0        # dB below dominant speaker -> silence
GATE_HOLD_S = 0.35        # seconds to hold gate open after dominant speech ends
GATE_FRAME  = 320         # 20 ms frames @ 16 kHz
GATE_INIT_S = 0.60        # seconds of audio to calibrate from
GATE_MIN_CLIP_S = 15.0    # gate only long recordings (meetings, rooms); short dictation is passed through


def apply_dominant_speaker_gate(
    audio,
    gate_db=GATE_DB,
    hold_s=GATE_HOLD_S,
):
    """Frame-level near-field dominant-speaker gate.

    Calibrates from the loudest 90th-percentile energy in the first
    GATE_INIT_S seconds. Any 20 ms frame below gate_db dB of that floor
    is zeroed. A hold timer preserves inter-word silences.
    Clips shorter than GATE_INIT_S are returned untouched.
    """
    import numpy as _np
    if len(audio) < int(SAMPLE_RATE * GATE_MIN_CLIP_S):
        return audio  # short dictation: one speaker; gating only ate the user's own quieter words

    n_frames = len(audio) // GATE_FRAME
    if n_frames < 4:
        return audio

    frames = audio[:n_frames * GATE_FRAME].reshape(n_frames, GATE_FRAME)
    rms_per_frame = _np.sqrt(
        _np.mean(frames.astype(_np.float64) ** 2, axis=1)
    ).astype(_np.float32)

    cal_frames = max(4, int(GATE_INIT_S * SAMPLE_RATE / GATE_FRAME))
    dominant_rms = float(_np.percentile(rms_per_frame[:cal_frames], 90))

    if dominant_rms < 1e-6:
        return audio  # dead mic / silence

    gate_floor = dominant_rms * (10.0 ** (-gate_db / 20.0))
    hold_frames = max(1, int(hold_s * SAMPLE_RATE / GATE_FRAME))
    out = audio.copy()
    hold_countdown = 0

    for i, rms_i in enumerate(rms_per_frame):
        if rms_i >= gate_floor:
            hold_countdown = hold_frames
        elif hold_countdown > 0:
            hold_countdown -= 1
        else:
            out[i * GATE_FRAME:(i + 1) * GATE_FRAME] = 0.0

    return out



# Microphones whose audio is already cleaned or rerouted by other software (the same list Wispr Flow
# checks). Their output has no room noise left to gate, and gating it again only cuts the user's words.
PROCESSED_MIC_HINTS = (
    "krisp", "nvidia broadcast", "rtx voice", "voicemeeter", "steelseries sonar", "sonar",
    "blackhole", "vb-audio", "vb-cable", "cable output", "zoom audio device", "nahimic",
    "elgato wave link", "wave link", "broadcast voice",
)


def is_processed_mic(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in PROCESSED_MIC_HINTS)


def clean_device_name(raw: str) -> str:
    """Extract clean friendly name from raw Windows Bluetooth/Audio GUID strings without stripping model names."""
    if not raw:
        return "Microphone"
    raw = raw.replace("\r", " ").replace("\n", " ")
    raw = raw.replace("@System32\\drivers\\bthhfenum.sys,#", "").replace("@System32", "")
    raw = re.sub(r"%\d+\s*Hands-Free( HF Audio)?%0", "", raw)
    raw = raw.replace(";", "").strip()

    # Specific known brands
    if "Realtek" in raw:
        return "Realtek Audio"
    if "NVIDIA Broadcast" in raw:
        return "NVIDIA Broadcast"

    # If it contains model information in parentheses, e.g.
    # 'Microphone (EMEET SmartCam C950' or 'Microphone (2- Audio Array AM-C1 Device)'
    if "(" in raw:
        inside = raw.split("(", 1)[1].rstrip(")").strip()
        if inside and len(inside) >= 3 and "Windows" not in inside and "System" not in inside:
            inside = re.sub(r"\(R\)", "", inside)
            return inside.strip()

    # Clean generic prefixes like 'Microphone - ' or 'Microphone Array - '
    for prefix in ["Microphone - ", "Microphone Array - ", "Microphone: ", "Input: "]:
        if raw.startswith(prefix):
            raw = raw[len(prefix):].strip()

    raw = re.sub(r"\(R\)", "", raw)
    return raw.strip() or "Microphone"


class AudioRecorder:
    def __init__(
        self,
        device_index: Optional[int] = None,
        on_amplitude: Optional[Callable[[float], None]] = None,
        on_auto_silence: Optional[Callable[[], None]] = None,
        on_stream_chunk: Optional[Callable[[np.ndarray], None]] = None,
        auto_silence_duration: float = 1.1,
    ):
        self.device_index = device_index
        self.on_amplitude = on_amplitude
        self.on_auto_silence = on_auto_silence
        self.on_stream_chunk = on_stream_chunk
        self.auto_silence_duration = auto_silence_duration

        self._is_recording = False
        self._stream: Optional[sd.InputStream] = None
        self._audio_chunks = []
        self._lock = threading.Lock()

        # 250 ms pre-roll ring buffer - always collecting even before Ctrl+Shift
        # so the first syllable is never clipped when the user presses the hotkey.
        self._preroll: np.ndarray = np.zeros(PREROLL_SAMPLES, dtype=np.float32)
        self._preroll_lock = threading.Lock()

        # VAD & Auto-write tracking: sensitive down to 0.0045 RMS so soft/whispered speech still starts live preview
        self._has_spoken = False
        self._last_speech_time = 0.0
        self._last_chunk_emit_time = 0.0
        self._speech_energy_threshold = 0.0045
        self._silence_triggered = False
        self._chunk_emitting = False

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    def peak_so_far(self) -> float:
        """Loudest sample recorded since start_recording (the pre-roll chunk excluded)."""
        with self._lock:
            chunks = self._audio_chunks[1:]
        return max((float(np.abs(c).max()) for c in chunks if len(c)), default=0.0)

    def _resolve_device(self) -> Optional[int]:
        """The device to open. A mic the user picked is used as is. "System default" follows Windows, except
        when Windows' default is a Bluetooth headset (it often becomes default on connect): then the first other
        live microphone is used, so earbuds stay for listening and dictation keeps full-quality audio."""
        if self.device_index is not None:
            return self.device_index
        try:
            with _pa_lock:
                default = sd.query_devices(kind="input")
            bt = bluetooth_headset_names()
            if not is_bluetooth_headset(default.get("name", ""), bt):
                return None
            for mic in list_microphones():
                if not is_bluetooth_headset(mic.get("name", ""), bt):
                    if getattr(self, "_bt_note", None) != mic["name"]:
                        self._bt_note = mic["name"]
                        print(f"[Vaani] Windows' default mic is a Bluetooth headset (call quality): using {mic['name']}")
                    return mic["index"]
        except Exception as e:
            print(f"[Vaani] Default mic check skipped ({e})")
        return None

    def set_device(self, device_index: Optional[int]):
        if device_index != self.device_index:
            self.device_index = device_index
            if not self._is_recording:  # PortAudio can block; never on the UI thread
                threading.Thread(target=self.close_stream, daemon=True).start()

    def open_stream(self):
        """Keep the mic open between dictations: opening takes ~230 ms, which swallowed the first
        word, and the pre-roll is only real audio while the stream runs."""
        with _pa_lock:
            if self._stream is not None:
                return
            with self._preroll_lock:
                self._preroll = np.zeros(PREROLL_SAMPLES, dtype=np.float32)  # drop audio from a previous session
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                device=self._resolve_device(),
                callback=self._audio_callback,
                blocksize=1024,
            )
            self._stream.start()

    def close_stream(self):
        """Release the mic (idle, device change); the next dictation reopens it."""
        with _pa_lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

    def refresh_devices(self) -> Optional[int]:
        """PortAudio lists devices once at startup; restart it so newly plugged mics show up.
        Indexes shift on restart, so the chosen mic is found again by name. Returns its new index."""
        with _pa_lock:
            raw = None
            if self.device_index is not None:
                try:
                    raw = sd.query_devices(self.device_index)["name"]
                except Exception:
                    pass
            was_open = self._stream is not None
            self.close_stream()
            sd._terminate()
            sd._initialize()
            self.device_index = None
            if raw:
                for i, d in enumerate(sd.query_devices()):
                    if d["name"] == raw and d["max_input_channels"] > 0:
                        self.device_index = i
                        break
            if was_open:
                self.open_stream()
            return self.device_index

    def get_current_device_name(self) -> str:
        with _pa_lock:
            if self.device_index is None:
                try:
                    idx = self._resolve_device()
                    d = sd.query_devices(kind="input") if idx is None else sd.query_devices(idx)
                    return clean_device_name(d.get("name", "Default Mic"))
                except Exception:
                    return "Default Mic"
            try:
                d = sd.query_devices(self.device_index)
                return clean_device_name(d.get("name", f"Mic {self.device_index}"))
            except Exception:
                return "Microphone"

    def set_auto_silence_duration(self, duration: float):
        self.auto_silence_duration = max(0.6, duration)

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            pass

        mono_data = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()

        # Always update the pre-roll ring buffer, even before recording starts.
        # This ensures the 250 ms before Ctrl+Shift is captured.
        with self._preroll_lock:
            n = len(mono_data)
            if n >= PREROLL_SAMPLES:
                self._preroll = mono_data[-PREROLL_SAMPLES:].copy()
            else:
                self._preroll = np.roll(self._preroll, -n)
                self._preroll[-n:] = mono_data

        if not self._is_recording:
            return

        with self._lock:
            self._audio_chunks.append(mono_data)

        # RMS calculation with dynamic non-linear scaling for natural vocal sensitivity
        rms = float(np.sqrt(np.mean(mono_data ** 2)))
        # Perceptual loudness: square root expansion ensures quiet/normal speech
        # (RMS 0.008 to 0.06) clearly animates the waveform (levels 0.35 - 0.85)
        level = min(1.0, max(0.0, float((rms ** 0.52) * 3.8)))

        now = time.time()

        # Send amplitude for UI visualizer
        if self.on_amplitude:
            try:
                self.on_amplitude(level)
            except Exception:
                pass

        # VAD Detection
        if rms >= self._speech_energy_threshold:
            self._has_spoken = True
            self._last_speech_time = now

        # Periodic real-time streaming preview (every ~0.55s of speech)
        if (
            self._has_spoken
            and self.on_stream_chunk
            and not self._chunk_emitting
            and (now - self._last_chunk_emit_time > 0.55)
        ):
            self._last_chunk_emit_time = now
            with self._lock:
                if len(self._audio_chunks) > 1:
                    accumulated = np.concatenate(self._audio_chunks, axis=0)
                    self._audio_chunks = [accumulated]  # compact so repeated previews stay O(1) per interval
                else:
                    accumulated = self._audio_chunks[0].copy() if self._audio_chunks else None
            if accumulated is not None and len(accumulated) > 6000:
                # Dispatch in background so PortAudio audio stream callback never blocks
                self._chunk_emitting = True
                chunk_cb = self.on_stream_chunk

                def _run_chunk(c=accumulated):
                    try:
                        chunk_cb(c)
                    finally:
                        self._chunk_emitting = False

                threading.Thread(target=_run_chunk, daemon=True).start()

        # Auto-Silence Detection
        if (
            self._has_spoken
            and not self._silence_triggered
            and self.on_auto_silence
            and (now - self._last_speech_time >= self.auto_silence_duration)
        ):
            self._silence_triggered = True
            threading.Thread(target=self.on_auto_silence, daemon=True).start()

    def start_recording(self):
        with self._lock:
            # Seed audio_chunks with pre-roll so the 250 ms before key-press is included
            with self._preroll_lock:
                self._audio_chunks = [self._preroll.copy()]
            self._is_recording = True
            self._has_spoken = False
            self._silence_triggered = False
            self._last_speech_time = time.time()
            self._last_chunk_emit_time = time.time()

        try:
            self.open_stream()
        except Exception as e:
            self._is_recording = False
            raise RuntimeError(f"Failed to start audio stream: {e}")

    def stop_recording(self) -> np.ndarray:
        self._is_recording = False
        if self.on_amplitude:
            try:
                self.on_amplitude(0.0)
            except Exception:
                pass

        with self._lock:
            if not self._audio_chunks:
                return np.empty((0,), dtype=np.float32)
            audio_data = np.concatenate(self._audio_chunks, axis=0)
            self._audio_chunks = []
            if not is_processed_mic(self.get_current_device_name()):
                audio_data = apply_dominant_speaker_gate(audio_data)
            return audio_data


def hardware_input_count() -> int:
    """Live count of capture devices straight from Windows (cheap, unlike PortAudio's cached list)."""
    try:
        import ctypes
        return ctypes.windll.winmm.waveInGetNumDevs()
    except Exception:
        return -1


def get_live_hardware_microphones() -> List[str]:
    """Query Windows winmm kernel directly for currently connected active audio input devices."""
    try:
        import ctypes
        from ctypes import wintypes
        class WAVEINCAPSW(ctypes.Structure):
            _fields_ = [
                ('wMid', wintypes.WORD),
                ('wPid', wintypes.WORD),
                ('vDriverVersion', wintypes.DWORD),
                ('szPname', wintypes.WCHAR * 32),
                ('dwFormats', wintypes.DWORD),
                ('wChannels', wintypes.WORD),
                ('wReserved1', wintypes.WORD),
            ]
        winmm = ctypes.windll.winmm
        num_devs = winmm.waveInGetNumDevs()
        devs = []
        for i in range(num_devs):
            caps = WAVEINCAPSW()
            if winmm.waveInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
                name = caps.szPname.strip()
                if name:
                    cleaned = clean_device_name(name)
                    if cleaned and cleaned not in devs:
                        devs.append(cleaned)
        return devs
    except Exception:
        return []


def bluetooth_headset_names() -> set:
    """Lower-case names of Bluetooth hands-free mics, e.g. {"cmf buds 2 plus"}. These record call-quality
    audio (8-16 kHz, compressed) and speech recognition makes far more mistakes on them. Windows lists each
    one through its hands-free driver as "Headset (@System32\\drivers\\bthhfenum.sys,...;(CMF Buds 2 Plus))"."""
    names = set()
    try:
        with _pa_lock:
            devs = sd.query_devices()
    except Exception:
        return names
    for d in devs:
        raw = d.get("name", "")
        if "bthhfenum" in raw.lower():
            m = re.search(r";\(([^()]+)\)\)?\s*$", raw.replace("\n", " "))
            if m:
                names.add(m.group(1).strip().lower())
    return names


def is_bluetooth_headset(name: str, bt_names=None) -> bool:
    low = name.lower()
    return any(n in low for n in (bluetooth_headset_names() if bt_names is None else bt_names))


def list_microphones() -> List[Dict]:
    devices = []
    live_hw = get_live_hardware_microphones()

    def _base_key(name: str) -> str:
        clean = clean_device_name(name)
        clean = re.sub(r'^\d+\s*[-]\s*', '', clean)
        clean = re.sub(r'^\d+\s+', '', clean)
        clean = re.sub(r'\s+Device$', '', clean, flags=re.I)
        return clean.strip().lower()

    try:
        with _pa_lock:
            all_devs = sd.query_devices()
        candidates = []
        for idx, dev in enumerate(all_devs):
            if dev.get("max_input_channels", 0) > 0:
                raw_name = dev.get("name", "")
                # Ignore Windows virtual mappers
                if "sound mapper" in raw_name.lower() or "primary sound" in raw_name.lower():
                    continue
                friendly = clean_device_name(raw_name)
                base = _base_key(friendly)

                # Filter against currently connected live hardware microphones
                if live_hw:
                    matched = False
                    for hw in live_hw:
                        hw_base = _base_key(hw)
                        if hw_base in base or base in hw_base or base.startswith(hw_base) or hw_base.startswith(base):
                            matched = True
                            break
                    if not matched:
                        continue

                candidates.append({
                    "index": idx,
                    "hostapi": dev.get("hostapi", 99),
                    "name": friendly,
                    "base": base,
                    "raw_name": raw_name,
                    "channels": dev.get("max_input_channels"),
                    "default_samplerate": dev.get("default_samplerate"),
                })

        # Deduplicate candidates sharing the same base device (e.g. MME vs DirectSound vs WASAPI).
        # Prefer MME (hostapi=0) so 16 kHz capture shares cleanly, while keeping the longest untruncated display name.
        for c in candidates:
            found = False
            for i, ex in enumerate(devices):
                b1, b2 = c["base"], ex["base"]
                if b1 == b2 or b1.startswith(b2) or b2.startswith(b1):
                    found = True
                    best_name = c["name"] if len(c["name"]) > len(ex["name"]) else ex["name"]
                    if c["hostapi"] < ex["hostapi"] or (c["hostapi"] == ex["hostapi"] and len(c["name"]) > len(ex["name"])):
                        devices[i] = c
                    devices[i]["name"] = best_name
                    break
            if not found:
                devices.append(c)

    except Exception as e:
        print(f"[Vaani] Error querying devices: {e}")
    return devices

