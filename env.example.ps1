# Copy to a private file such as codex-sota.env.ps1 and dot-source it before launching.
# Do not put API keys, auth.json, providers.json, or session data in this file.
$env:CODEX_SOTA_CORE_ROOT = (Join-Path $PSScriptRoot 'CodexHistorySync')
$env:CODEX_SOTA_CODEX_ROOT = (Join-Path $env:USERPROFILE '.codex-sota')
$env:CODEX_SOTA_CLAUDE_ROOT = (Join-Path $env:USERPROFILE '.claude-sota')
$env:CODEX_SOTA_COCKPIT_ROOT = (Join-Path $env:USERPROFILE '.codex-personal')
$env:CODEX_SOTA_PLUS_ROOT = (Join-Path $env:USERPROFILE '.codex-plus')
# Optional overrides for non-default installations:
# $env:CODEX_SOTA_PYTHON = 'C:\Python311\python.exe'
# $env:CODEX_SOTA_BUILD_PYTHON = 'C:\BuildEnv\Scripts\python.exe'
# $env:CODEX_APP_EXE = 'C:\Path\To\ChatGPT.exe'
# $env:CODEX_COCKPIT_TOOLS_EXE = 'C:\Path\To\cockpit-tools.exe'
# $env:CODEX_COCKPIT_API_PORT = '56319'
