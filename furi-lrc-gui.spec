# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules, collect_all

datas = [
    ('fonts', 'fonts'),
    ('icon-i.ico', '.'),
    ('icon-i.png', '.'),
]
binaries = []
hiddenimports = [
    'PyQt6.QtMultimedia', 'PyQt6.QtMultimediaWidgets',
    'asyncio', 'ctypes', 'datetime', 're', 'threading', 'time',
    'numpy', 'librosa', 'soundfile',
]
hiddenimports += collect_submodules('mutagen')
tmp_ret = collect_all('PyQt6')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

a = Analysis(
    ['furi-lrc-gui.py'],
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
    name='furi-lrc-gui',
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
    icon='icon-i.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='furi-lrc-gui',
)
