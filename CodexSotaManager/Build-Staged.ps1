param(
    [switch]$SkipPreflight,
    [string]$PythonExecutable = $env:CODEX_SOTA_BUILD_PYTHON
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
. (Join-Path $root 'Artifact-Integrity.ps1')
$buildCandidates = if ($PythonExecutable) { @($PythonExecutable) } else {
    @((Join-Path (Split-Path -Parent $root) '.runtime\.venv-build\Scripts\python.exe'),
      (Join-Path $root '.venv-build\Scripts\python.exe'))
}
$buildPython = $buildCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
$validation = Join-Path $root 'Run-ThreeRoundValidation.ps1'
$candidateRoot = Join-Path $root ('dist-candidate-' + [guid]::NewGuid().ToString('N'))
$workRoot = Join-Path $root ('build-candidate-' + [guid]::NewGuid().ToString('N'))
$candidateApp = Join-Path $candidateRoot 'codex-sota'
$stagedRoot = Join-Path $root 'dist-staging'
$stagedApp = Join-Path $stagedRoot 'codex-sota'
$verificationReport = Join-Path $stagedRoot 'codex-sota-verification.json'
foreach ($ownedPath in @($candidateRoot, $workRoot, $candidateApp, $stagedRoot, $stagedApp)) {
    $null = Assert-ScopedArtifactPath $ownedPath $root
}

function Get-BuildSourceHashes {
    $coreRoot = Join-Path (Split-Path -Parent $root) 'CodexHistorySync'
    $sourceFiles = @(Get-ChildItem -LiteralPath $root, $coreRoot -File |
        Where-Object { $_.Extension.ToLowerInvariant() -in @('.py', '.ps1', '.cmd', '.bat', '.spec', '.ico') } |
        Sort-Object FullName)
    $hashes = [ordered]@{}
    foreach ($source in $sourceFiles) { $hashes[$source.FullName] = Get-ArtifactSha256 $source.FullName }
    return $hashes
}

if (-not $buildPython) {
    throw 'Build Python was not found. Run setup.ps1 or pass -PythonExecutable pointing to a Python with requirements-build.txt installed.'
}
& $buildPython -I -B -c 'import sys, PyInstaller; sys.exit(0 if sys.version_info >= (3, 11) else 2)'
if ($LASTEXITCODE -ne 0) {
    throw 'Build Python must be CPython 3.11+ with PyInstaller installed.'
}
if (-not $SkipPreflight) {
    & $validation -SkipArtifact
    if ($LASTEXITCODE -ne 0) {
        throw "Preflight validation failed with exit code $LASTEXITCODE."
    }
}

try {
    $sourceHashes = Get-BuildSourceHashes
    & $buildPython -B -m PyInstaller --noconfirm --clean --distpath $candidateRoot --workpath $workRoot (Join-Path $root 'codex-sota.spec')
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
    Copy-Item -LiteralPath (Join-Path (Split-Path -Parent $root) 'packaging\codex-sota.cmd') -Destination $candidateApp
    $null = New-ArtifactManifest $candidateApp
    $validatedFiles = Assert-ArtifactBundle $candidateApp
    & $validation -ArtifactRoot $candidateApp
    if ($LASTEXITCODE -ne 0) {
        throw "Candidate validation failed with exit code $LASTEXITCODE."
    }
    # Full verification includes the four core check_* modules and the two
    # PowerShell behavioral harnesses, in addition to runtime readiness. A
    # runtime-only result must never be promoted to a full installation receipt.
    $verificationOutput = Assert-ScopedArtifactPath (Join-Path $workRoot 'full-verification') $root
    & $buildPython -B (Join-Path $root 'Verify-StartupFix.py') --output $verificationOutput --artifact-root $candidateApp
    if ($LASTEXITCODE -ne 0) { throw 'The candidate failed full source, core, launcher, or runtime verification.' }
    $verificationResult = Get-Content -LiteralPath (Join-Path $verificationOutput 'result.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($verificationResult.status -ne 'passed' -or $verificationResult.validation_scope -ne 'full-bundle-and-regressions') {
        throw 'The candidate did not produce a successful full verification result.'
    }
    $null = Assert-ArtifactBundle $candidateApp $validatedFiles
    if (((Get-BuildSourceHashes) | ConvertTo-Json -Compress) -ne ($sourceHashes | ConvertTo-Json -Compress)) {
        throw 'Build sources changed during compilation or validation; rebuild from a stable source tree.'
    }

    New-Item -ItemType Directory -Path $stagedRoot -Force | Out-Null
    $previousStage = Assert-ScopedArtifactPath (Join-Path $stagedRoot ('codex-sota.previous-' + [guid]::NewGuid().ToString('N'))) $root
    $receiptTemporary = Assert-ScopedArtifactPath (Join-Path $stagedRoot ('.verification-' + [guid]::NewGuid().ToString('N') + '.tmp')) $root
    $stageRetired = $false
    $stagePromoted = $false
    try {
        if (Test-Path -LiteralPath $stagedApp) {
            Move-Item -LiteralPath $stagedApp -Destination $previousStage -ErrorAction Stop
            $stageRetired = $true
        }
        Move-Item -LiteralPath $candidateApp -Destination $stagedApp -ErrorAction Stop
        $stagePromoted = $true
        $receipt = [ordered]@{
            verification_schema = 2; status = 'passed'; validated_by = 'Verify-StartupFix.py'
            validation_scope = $verificationResult.validation_scope; runtime_probe = $verificationResult.runtime_probe
            tests_run = $verificationResult.tests_run; powershell = $verificationResult.powershell
            sources_unchanged_during_tests = $true; artifact_unchanged_during_tests = $true
            source_sha256 = $sourceHashes; artifact_root = [IO.Path]::GetFullPath($stagedApp)
            artifact_sha256 = $validatedFiles['codex-sota.exe']; artifact_files_sha256 = $validatedFiles
        } | ConvertTo-Json -Depth 6
        [IO.File]::WriteAllText($receiptTemporary, $receipt + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
        $null = Assert-ScopedArtifactPath $verificationReport $root
        Move-Item -LiteralPath $receiptTemporary -Destination $verificationReport -Force -ErrorAction Stop
    }
    catch {
        if ($stagePromoted -and -not (Test-Path -LiteralPath $candidateApp)) {
            Move-Item -LiteralPath $stagedApp -Destination $candidateApp -ErrorAction Stop
        }
        if ($stageRetired -and -not (Test-Path -LiteralPath $stagedApp)) {
            Move-Item -LiteralPath $previousStage -Destination $stagedApp -ErrorAction Stop
        }
        throw
    }
    finally {
        if (Test-Path -LiteralPath $receiptTemporary) { Remove-Item -LiteralPath $receiptTemporary -Force -ErrorAction SilentlyContinue }
    }
    if ($stageRetired) {
        try {
            $null = Assert-ScopedArtifactPath $previousStage $root
            Remove-Item -LiteralPath $previousStage -Recurse -Force -ErrorAction Stop
        }
        catch { Write-Warning ('Could not remove superseded staged bundle: ' + $_.Exception.Message) }
    }
    Write-Host "Validated staged build is ready: $stagedApp"
}
finally {
    if (Test-Path -LiteralPath $candidateRoot) {
        $null = Assert-ScopedArtifactPath $candidateRoot $root
        Remove-Item -LiteralPath $candidateRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $workRoot) {
        $null = Assert-ScopedArtifactPath $workRoot $root
        Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
