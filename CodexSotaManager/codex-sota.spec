# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import copy_metadata


manager_root = Path(SPECPATH).resolve()
core_root = manager_root.parent / 'CodexHistorySync'
entrypoint = manager_root / 'manager_entrypoint.py'
icon_path = manager_root / 'codex-sota.ico'

# Only ship named runtime sources, never arbitrary JSON or private files from a
# working installation. PyInstaller's second field is a DIRECTORY, not a filename.
core_runtime_names = (
    'sota_registry.py', 'sota_network.py', 'sota_compaction.py',
    'claude_desktop.py', 'codex_sota_router.py',
    'codex_app_lifecycle.py', 'sync_codex_histories.py',
    'sync_chatgpt_account_history.py',
    'sync_codex_histories_three_way.py', 'sync_after_codex_exit.py',
    'repair_sota_launch_history.py', 'repair_archived_sidebar.py',
    'validate_codex_profile.py', 'release_claude_slot_after_exit.py',
    'repair_claude_slot_claim.py', 'Build-CodexSotaModelCatalog.py',
    'Start-CodexSotaRouter.ps1', 'Start-ClaudeSotaRouter.ps1',
    'Start-CodexStoreProcess.ps1',
    'Switch-CodexSota.ps1', 'Switch-CodexProfile.ps1',
    'Run-CodexHistorySync.ps1', 'Login-CodexSota.ps1', 'messages.zh-CN.json',
)
core_datas = [
    (str(core_root / name), 'CodexHistorySync')
    for name in core_runtime_names
]

a = Analysis(
    [str(entrypoint)],
    pathex=[str(core_root)],
    binaries=[],
    datas=[(str(icon_path), '.')] + core_datas + copy_metadata('zstandard'),
    hiddenimports=['CodexSotaManager', 'codex_launch_controller', 'sota_registry',
                   'sota_network', 'sota_compaction', 'claude_desktop',
                   'codex_sota_router', 'codex_app_lifecycle',
                   'sync_codex_histories', 'sync_codex_histories_three_way',
                   'sync_chatgpt_account_history', 'zstandard',
                   'sync_after_codex_exit', 'repair_sota_launch_history',
                   'repair_archived_sidebar', 'validate_codex_profile',
                   'release_claude_slot_after_exit', 'repair_claude_slot_claim',
                   'json.tool', 'encodings', 'sqlite3', 'ssl', 'tomllib'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(manager_root / 'artifact_runtime_probe.py')],
    excludes=[],
    noarchive=False,
    optimize=0,
)
runtime = Analysis(
    [str(manager_root / 'runtime_entrypoint.py')],
    pathex=[str(core_root), str(manager_root)],
    binaries=[], datas=[], hiddenimports=['json.tool', 'tkinter', 'sota_compaction'],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure + runtime.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='codex-sota',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(icon_path)],
)
runtime_exe = EXE(
    pyz, runtime.scripts, [], exclude_binaries=True,
    name='codex-sota-runtime', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=True,
    disable_windowed_traceback=False, icon=[str(icon_path)],
)
coll = COLLECT(
    exe,
    runtime_exe,
    a.binaries,
    runtime.binaries,
    a.datas,
    runtime.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='codex-sota',
)
