@echo off
setlocal
set "CODEX_SOTA_CORE_ROOT=%~dp0_internal\CodexHistorySync"
set "CODEX_SOTA_PYTHON=%~dp0codex-sota-runtime.exe"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%CODEX_SOTA_CORE_ROOT%\Switch-CodexSota.ps1" %*
exit /b %errorlevel%
