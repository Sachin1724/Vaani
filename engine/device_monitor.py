"""Power and peripheral watcher behind the capsule's device alerts.

DeviceMonitor.poll() diffs the battery, the audio outputs and the removable drives against the
previous call and returns one alert per change:
    {"kind", "title", "sub", "badge", "tone", "level"}
kind: charging | battery | audio | buds | usb; tone: green | red | steel | neutral.
The first poll only records a baseline, so starting Vaani never fires alerts.
"""
import ctypes
import string
import subprocess
from ctypes import wintypes

try:
    import psutil
except ImportError:  # the alerts are optional; dictation never needs psutil
    psutil = None

LOW_PCT = 15
FULL_PCT = 100
_EARBUD_WORDS = ("bluetooth", "buds", "airpods", "headphone", "headset", "hands-free", "earbud", "earphone")


def battery():
    """(percent, plugged, secsleft) or None on a machine without a battery."""
    if psutil is None:
        return None
    try:
        b = psutil.sensors_battery()
    except Exception:
        return None
    if b is None:
        return None
    return round(b.percent), b.power_plugged, b.secsleft


class _WAVEOUTCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                ("szPname", wintypes.WCHAR * 32), ("dwFormats", wintypes.DWORD), ("wChannels", wintypes.WORD),
                ("wReserved1", wintypes.WORD), ("dwSupport", wintypes.DWORD)]


def device_name(endpoint: str) -> str:
    """'Headphones (CMF Buds 2 Plus)' -> 'CMF Buds 2 Plus'. waveOut names stop at 31 characters."""
    s = endpoint.strip()
    if "(" in s:
        inner = s.split("(", 1)[1].rstrip(")").strip()
        if inner:
            return inner
    return s


def audio_outputs() -> set:
    """Speaker / headphone device names, one per device (a headset's stereo and hands-free endpoints merge)."""
    try:
        winmm = ctypes.windll.winmm
    except AttributeError:
        return set()
    names = set()
    for i in range(winmm.waveOutGetNumDevs()):
        caps = _WAVEOUTCAPSW()
        if winmm.waveOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            names.add(device_name(caps.szPname))
    return names


def removable_drives() -> dict:
    """{"E:": "SanDisk Ultra"} for USB sticks and card readers (USB hard disks report as fixed and are skipped)."""
    try:
        k32 = ctypes.windll.kernel32
    except AttributeError:
        return {}
    old = k32.SetErrorMode(1)  # SEM_FAILCRITICALERRORS: an empty card slot must not pop a dialog
    try:
        mask, out = k32.GetLogicalDrives(), {}
        for i, letter in enumerate(string.ascii_uppercase):
            root = f"{letter}:\\"
            if mask >> i & 1 and k32.GetDriveTypeW(root) == 2:  # DRIVE_REMOVABLE
                buf = ctypes.create_unicode_buffer(261)
                k32.GetVolumeInformationW(root, buf, 261, None, None, None, None, 0)
                out[f"{letter}:"] = buf.value or "USB drive"
        return out
    finally:
        k32.SetErrorMode(old)


def bluetooth_battery(name: str, timeout: float = 5.0):
    """Battery % a Bluetooth device reports to Windows (DEVPKEY_Bluetooth_Battery), or None.
    Slow (PowerShell, ~1 s): call it off the UI thread. Many earbuds never report it."""
    pattern = "".join(ch for ch in name if ch.isalnum() or ch in " -_.")
    if not pattern.strip():
        return None
    ps = ("Get-PnpDevice -FriendlyName '*%s*' -ErrorAction SilentlyContinue | ForEach-Object { "
          "(Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName '{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2' "
          "-ErrorAction SilentlyContinue).Data } | Where-Object { $_ -ne $null } | Select-Object -First 1") % pattern
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                           text=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        pct = int(r.stdout.strip().splitlines()[0])
        return pct if 0 <= pct <= 100 else None
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def _left(secs) -> str:
    # psutil's UNKNOWN / UNLIMITED are negative; right after unplugging Windows reports 0xFFFFFFFF seconds
    # (1193046 h) until it has an estimate. Anything past two days is not a real estimate.
    if not isinstance(secs, (int, float)) or secs <= 0 or secs > 48 * 3600:
        return ""
    h, m = int(secs // 3600), int(secs % 3600 // 60)
    return f" · about {h} h {m} min left" if h else f" · about {m} min left"


def _alert(kind, title, sub, badge, tone, level=0):
    return {"kind": kind, "title": title, "sub": sub, "badge": badge, "tone": tone, "level": level}


class DeviceMonitor:
    def __init__(self, battery_fn=battery, outputs_fn=audio_outputs, drives_fn=removable_drives):
        self._battery_fn, self._outputs_fn, self._drives_fn = battery_fn, outputs_fn, drives_fn
        self._bat: tuple | None = None
        self._outs: set | None = None
        self._drives: dict | None = None
        self._low_fired = self._full_fired = False

    def poll(self, power: bool = True, devices: bool = True) -> list:
        alerts = []
        if power:
            alerts += self._poll_battery()
        if devices:
            alerts += self._poll_outputs() + self._poll_drives()
        return alerts

    def _poll_battery(self) -> list:
        now = self._battery_fn()
        prev, self._bat = self._bat, now
        if now is None or prev is None:
            if now is not None:  # baseline: an already-low or already-full battery does not alert at start
                self._low_fired = now[0] <= LOW_PCT and not now[1]
                self._full_fired = now[0] >= FULL_PCT and now[1]
            return []
        pct, plugged, secs = now
        out = []
        if plugged and not prev[1]:
            out.append(_alert("charging", "Charging", f"Battery {pct}%", "CHARGING", "green", pct))
            self._low_fired = False
        elif prev[1] and not plugged:
            out.append(_alert("battery", "On battery", f"{pct}%{_left(secs)}", "BATTERY", "neutral", pct))
            self._full_fired = False
        if not plugged and pct <= LOW_PCT and not self._low_fired:
            out.append(_alert("battery", "Battery low", f"{pct}% · plug in soon", "LOW", "red", pct))
            self._low_fired = True
        if plugged and pct >= FULL_PCT and not self._full_fired:
            out.append(_alert("battery", "Fully charged", f"{pct}% · you can unplug", "FULL", "green", pct))
            self._full_fired = True
        return out

    def _poll_outputs(self) -> list:
        now = self._outputs_fn()
        prev, self._outs = self._outs, now
        if prev is None:
            return []
        out = []
        for name in sorted(now - prev):
            kind = "buds" if any(w in name.lower() for w in _EARBUD_WORDS) else "audio"
            out.append(_alert(kind, name, "Connected", "AUDIO", "steel"))
        for name in sorted(prev - now):
            kind = "buds" if any(w in name.lower() for w in _EARBUD_WORDS) else "audio"
            out.append(_alert(kind, name, "Disconnected", "OFF", "neutral"))
        return out

    def _poll_drives(self) -> list:
        now = self._drives_fn()
        prev, self._drives = self._drives, now
        if prev is None:
            return []
        out = [_alert("usb", f"{label} ({letter})", "USB drive connected", "USB", "steel")
               for letter, label in sorted(now.items()) if letter not in prev]
        out += [_alert("usb", f"{label} ({letter})", "USB drive removed", "OFF", "neutral")
                for letter, label in sorted(prev.items()) if letter not in now]
        return out
