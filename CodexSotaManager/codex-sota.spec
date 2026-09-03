# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


manager_root = Path(SPECPATH).resolve()
core_root = manager_root.parent / 'CodexHistorySync'
entrypoint = manager_root / 'CodexSotaManager.py'
icon_path = manager_root / 'codex-sota.ico'

a = Analysis(
    [str(entrypoint)],
    pathex=[str(core_root)],
    binaries=[],
    datas=[(str(icon_path), '.')],
    hiddenimports=[],
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
    name='codex-sota',
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
    icon=[str(icon_path)],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='codex-sota',
)
