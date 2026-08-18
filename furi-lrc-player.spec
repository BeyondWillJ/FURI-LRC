# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules, collect_all

datas = [
    ('fonts', 'fonts'),
    ('player_data/icon-player.ico', 'player_data'),
    # Loaded through importlib at runtime; make it available in _MEIPASS.
    ('furi-lrc_rubi.py', '.'),
]
binaries = []
# furi-lrc_rubi.py is dynamically loaded, so PyInstaller cannot analyse its
# imports from the entry point.
hiddenimports = [
    'PyQt6.QtMultimedia', 'PyQt6.QtMultimediaWidgets',
    'asyncio', 'base64', 'ctypes', 'datetime', 'gzip', 'math', 're',
    'threading', 'time', 'zlib',
]
hiddenimports += collect_submodules('mutagen')
tmp_ret = collect_all('PyQt6')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

a = Analysis(
    ['furi-lrc-player.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='furi-lrc-player',
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
    icon='icon-player.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='furi-lrc-player',
)
