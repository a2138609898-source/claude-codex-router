param(
    [string]$ArtifactRoot = "",
    [switch]$SkipArtifact
)

$ErrorActionPreference = 'Stop'
$managerRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$coreRoot = Join-Path (Split-Path -Parent $managerRoot) 'CodexHistorySync'
$pythonCandidates = @(
    [Environment]::GetEnvironmentVariable('CODEX_PYTHON'),
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
)
# Any locally installed CPython, newest first.  The previous hard-coded 3.13/3.12 pair missed
# this machine's 3.11 entirely and fell through to `python.exe` on PATH -- which is the Microsoft
# Store stub, so validation "passed" by never running.
$pythonCandidates += @(
    Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName 'python.exe' }
)
$python = $pythonCandidates |
    Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
    Select-Object -First 1
if (-not $python) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw 'Python runtime was not found.'
    }
    $python = $pythonCommand.Source
}
Write-Host ('Validation interpreter: ' + $python)

function Invoke-CheckedPython {
    param([string[]]$Arguments)
    & $python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python validation failed with exit code $LASTEXITCODE."
    }
}

Write-Host '[1/3] Static syntax and launcher parsing'
$pythonSources = @(
    (Join-Path $managerRoot 'CodexSotaManager.py'),
    (Join-Path $managerRoot 'test_codex_sota.py'),
    (Join-Path $managerRoot 'test_codex_sota_regressions.py'),
    (Join-Path $managerRoot 'test_sync_build_regressions.py')
)
$pythonSources += @(Get-ChildItem -LiteralPath $coreRoot -File -Filter '*.py' | Select-Object -ExpandProperty FullName)
$pythonSources = @($pythonSources | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -Unique)
Invoke-CheckedPython -Arguments (@('-m', 'py_compile') + $pythonSources)
foreach ($script in @(Get-ChildItem -LiteralPath $coreRoot, $managerRoot -File -Filter '*.ps1')) {
    $null = [scriptblock]::Create((Get-Content -Raw -LiteralPath $script.FullName))
}

Write-Host '[2/3] Isolated behavioral regression suite'
Push-Location $managerRoot
try {
    Invoke-CheckedPython -Arguments @(
        '-m', 'unittest', '-v',
        'test_codex_sota.py',
        'test_codex_sota_regressions.py',
        'test_sync_build_regressions.py'
    )
}
finally {
    Pop-Location
}

Write-Host '[3/3] Packaged artifact integrity and clean repeat'
if (-not $SkipArtifact) {
    if (-not $ArtifactRoot) {
        $staged = Join-Path $managerRoot 'dist-staging\codex-sota'
        $live = Join-Path $managerRoot 'dist\codex-sota'
        $ArtifactRoot = if (Test-Path -LiteralPath $staged) { $staged } else { $live }
    }
    if (-not (Test-Path -LiteralPath $ArtifactRoot)) {
        throw "Packaged artifact was not found: $ArtifactRoot"
    }
    $env:CODEX_SOTA_ARTIFACT_ROOT = (Resolve-Path -LiteralPath $ArtifactRoot).Path
}
else {
    Remove-Item Env:CODEX_SOTA_ARTIFACT_ROOT -ErrorAction SilentlyContinue
}
Push-Location $managerRoot
try {
    Invoke-CheckedPython -Arguments @(
        '-m', 'unittest', '-v',
        'test_codex_sota_regressions.ClaudeLibraryRegressionTests',
        'test_codex_sota_regressions.RouterRegressionTests',
        'test_codex_sota_regressions.ManagerRegressionTests',
        'test_codex_sota_regressions.ConfigRestoreTests',
        'test_codex_sota_regressions.HeaderValidationTests',
        'test_codex_sota_regressions.RegistryRecoveryRegressionTests',
        'test_codex_sota_regressions.LauncherAndArtifactTests',
        'test_sync_build_regressions.SyncAndBuildRegressionTests'
    )
}
finally {
    Pop-Location
    Remove-Item Env:CODEX_SOTA_ARTIFACT_ROOT -ErrorAction SilentlyContinue
}

Write-Host 'All three validation rounds passed.'
