[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $PSScriptRoot '.runtime'),
    [switch]$SkipBuildTools,
    [string]$PythonExecutable = ''
)

$ErrorActionPreference = 'Stop'
$installRoot = [IO.Path]::GetFullPath($InstallRoot)
$repoRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$escapedRepoRoot = $repoRoot.Replace("'", "''")
$requestedPython = $PythonExecutable
if (-not $requestedPython) { $requestedPython = $env:CODEX_SOTA_PYTHON }
if (-not $requestedPython) { $requestedPython = $env:CODEX_PYTHON }
$candidates = @()
if ($requestedPython) {
    $candidates = @([Environment]::ExpandEnvironmentVariables($requestedPython))
} else {
    foreach ($name in @('py.exe', 'python.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { $candidates += $command.Source }
    }
}
$python = $null
foreach ($candidate in $candidates) {
    if ($candidate -match '\\WindowsApps\\' -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    try {
        $arguments = @('-I', '-B', '-c', 'import sys, tkinter; print(sys.executable); print(sys.version_info.major); print(sys.version_info.minor)')
        if ([IO.Path]::GetFileName($candidate) -ieq 'py.exe') { $arguments = @('-3') + $arguments }
        $probe = @(& $candidate @arguments 2>$null)
        if ($LASTEXITCODE -eq 0 -and $probe.Count -ge 3 -and [int]$probe[-2] -eq 3 -and [int]$probe[-1] -ge 11) {
            $python = [IO.Path]::GetFullPath(([string]$probe[-3]).Trim())
            break
        }
    } catch { continue }
}
if (-not $python) { throw 'CPython 3.11+ with tkinter is required. Install it or pass -PythonExecutable with its full path.' }
$escapedPython = $python.Replace("'", "''")

New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
$envFile = Join-Path $installRoot 'codex-sota.env.ps1'
if (-not (Test-Path -LiteralPath $envFile)) {
    @"
# Optional local deployment settings. Keep this file private; it is ignored by Git.
`$env:CODEX_SOTA_CORE_ROOT = '$escapedRepoRoot\CodexHistorySync'
# `$env:CODEX_SOTA_CODEX_ROOT = Join-Path `$env:USERPROFILE '.codex-sota'
# `$env:CODEX_SOTA_CLAUDE_ROOT = Join-Path `$env:USERPROFILE '.claude-sota'
# `$env:CODEX_SOTA_COCKPIT_ROOT = Join-Path `$env:USERPROFILE '.codex-personal'
# `$env:CODEX_SOTA_PLUS_ROOT = Join-Path `$env:USERPROFILE '.codex-plus'
`$env:CODEX_SOTA_PYTHON = '$escapedPython'
# `$env:CODEX_APP_EXE = 'C:\Path\To\ChatGPT.exe'
"@ | Set-Content -LiteralPath $envFile -Encoding UTF8
}

if (-not $SkipBuildTools) {
    $venv = Join-Path $installRoot '.venv-build'
    & $python -B -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the build virtual environment.' }
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'Could not update pip in the build environment.' }
    & $venvPython -m pip install -r (Join-Path $PSScriptRoot 'requirements-build.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Build dependencies failed to install.' }
}

Write-Host "Codex-SOTA source deployment prepared under $((Resolve-Path $PSScriptRoot).Path)."
Write-Host "Review and dot-source the private environment file when needed: $envFile"
if (-not $SkipBuildTools) { Write-Host "Build interpreter: $venvPython (pass -PythonExecutable to Build-Staged.ps1 for a custom InstallRoot)." }
Write-Host 'No user profile, provider registry, authentication file, or history database was created or modified.'
