"""
Vaani — Unlimited Local AI Voice Dictation for Windows
"""
import os as _os

# One-time move of the pre-rename data folder (models, history, logs) so nothing re-downloads.
_home = _os.path.expanduser("~")
try:
    if _os.path.isdir(_os.path.join(_home, ".vaani")) and not _os.path.exists(_os.path.join(_home, ".vaani")):
        _os.rename(_os.path.join(_home, ".vaani"), _os.path.join(_home, ".vaani"))
except OSError:  # an old copy still running holds files open: close it and start Vaani again
    pass

# The installed exe has no console (sys.stderr is None), which makes tqdm progress bars crash
# model downloads. Vaani reports download progress itself, so switch the bars off up front,
# before huggingface_hub is imported anywhere.
_os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
_os.environ.setdefault("TQDM_DISABLE", "1")
_os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

__version__ = "1.6.0"
