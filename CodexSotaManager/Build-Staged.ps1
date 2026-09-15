param(
    [switch]$SkipPreflight
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyinstaller = Join-Path $root '.venv-build\Scripts\pyinstaller.exe'
$validation = Join-Path $root 'Run-ThreeRoundValidation.ps1'
$candidateRoot = Join-Path $root ('dist-candidate-' + [guid]::NewGuid().ToString('N'))
$workRoot = Join-Path $root ('build-candidate-' + [guid]::NewGuid().ToString('N'))
$candidateApp = Join-Path $candidateRoot 'codex-sota'
$stagedRoot = Join-Path $root 'dist-staging'
$stagedApp = Join-Path $stagedRoot 'codex-sota'

if (-not (Test-Path -LiteralPath $pyinstaller)) {
    throw "PyInstaller was not found: $pyinstaller"
}
if (-not $SkipPreflight) {
    & $validation -SkipArtifact
    if ($LASTEXITCODE -ne 0) {
        throw "Preflight validation failed with exit code $LASTEXITCODE."
    }
}

try {
    & $pyinstaller --noconfirm --clean --distpath $candidateRoot --workpath $workRoot (Join-Path $root 'codex-sota.spec')
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
    & $validation -ArtifactRoot $candidateApp
    if ($LASTEXITCODE -ne 0) {
        throw "Candidate validation failed with exit code $LASTEXITCODE."
    }

    if (Test-Path -LiteralPath $stagedApp) {
        # The staged tree is fully regenerable from source and Apply-StagedBuild keeps its
        # own rollback copy of live, so replace it in place instead of parking another full
        # build snapshot in dist-staging.previous-* forever.
        Remove-Item -LiteralPath $stagedRoot -Recurse -Force -ErrorAction Stop
    }
    New-Item -ItemType Directory -Path $stagedRoot -Force | Out-Null
    Move-Item -LiteralPath $candidateApp -Destination $stagedApp -ErrorAction Stop
    Write-Host "Validated staged build is ready: $stagedApp"
}
finally {
    if (Test-Path -LiteralPath $candidateRoot) {
        Remove-Item -LiteralPath $candidateRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $workRoot) {
        Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
