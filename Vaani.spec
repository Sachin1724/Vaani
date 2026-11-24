# -*- mode: python ; coding: utf-8 -*-
"""
Vaani PyInstaller Specification
Created by Sachidananda
Builds standalone Windows distribution with CUDA/DirectML/CPU runtime,
PyQt6 glassmorphic UI, audio drivers, and assets.
"""
import os
import sys

# Prevent recursion depth error with large ASTs (transformers, torch, tokenizers)
sys.setrecursionlimit(50000)

from PyInstaller.utils.hooks import collect_all

block_cipher = None

datas = [
    ('vaani/assets', 'vaani/assets'),
    ('vaani/docs', 'vaani/docs'),
]
binaries = []
hiddenimports = [
    'PyQt6',
    'PyQt6.QtCore',
    'PyQt6.QtGui',
    'PyQt6.QtWidgets',
    'PyQt6.QtNetwork',
    'sounddevice',
    '_sounddevice_data',
    'numpy',
    'ctranslate2',
    'faster_whisper',
    'huggingface_hub',
    'tokenizers',
    'onnxruntime',
    'vaani',
    'vaani.config',
    'vaani.database',
    'vaani.engine',
    'vaani.engine.audio_recorder',
    'vaani.engine.transcriber',
    'vaani.engine.formatter',
    'vaani.engine.injector',
    'vaani.engine.hotkey_manager',
    'vaani.engine.sound_effects',
    'vaani.engine.context_detector',
    'vaani.engine.updater',
    'vaani.engine.meeting',
    'vaani.engine.hardware',
    'vaani.engine.model_manager',
    'vaani.engine.refiner',
    'vaani.engine.indic_asr',
    'vaani.engine.translator',
    'vaani.engine.local_llm',
    'sentencepiece',
    'vaani.ui',
    'vaani.ui.floating_pill',
    'vaani.ui.main_window',
    'vaani.ui.setup_dialog',
    'vaani.autostart',
]

for pkg in ['ctranslate2', 'faster_whisper', 'sounddevice', 'openvino', 'openvino_genai', 'openvino_tokenizers']:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:
        print(f"Hook note for {pkg}: {e}")

# Deduplicate hidden imports
hiddenimports = list(set(hiddenimports))

a = Analysis(
    ['run_app.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tensorflow', 'keras', 'torchvision', 'boto3', 'botocore',
        'uvicorn', 'websockets', 'numba', 'llvmlite',
        'tkinter', 'matplotlib', 'scipy', 'pandas', 'IPython', 'pytest'
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Vaani',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='vaani/assets/app_icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Vaani',
)
