[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '0.2.4',
    [string]$PythonExecutable = $env:CODEX_SOTA_BUILD_PYTHON,
    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'release'),
    [string]$InnoCompiler = '',
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
$managerRoot = Join-Path $repoRoot 'CodexSotaManager'
. (Join-Path $managerRoot 'Artifact-Integrity.ps1')

function Get-ReleaseSourceFingerprint {
    $sourceFiles = @(Get-ChildItem -LiteralPath $managerRoot,(Join-Path $repoRoot 'CodexHistorySync') -File |
        Where-Object { $_.Extension.ToLowerInvariant() -in @('.py', '.ps1', '.cmd', '.bat', '.spec', '.ico') } |
        Sort-Object FullName)
    $sourceFiles += Get-Item -LiteralPath (Join-Path $repoRoot 'Build-Release.ps1'),(Join-Path $repoRoot 'packaging\codex-sota.cmd'),(Join-Path $repoRoot 'packaging\codex-sota.iss')
    $fingerprint = [ordered]@{}
    foreach ($source in $sourceFiles) { $fingerprint[$source.FullName] = Get-ArtifactSha256 $source.FullName }
    return ($fingerprint | ConvertTo-Json -Compress)
}
$sourceFingerprint = Get-ReleaseSourceFingerprint
$candidates = @($PythonExecutable, $env:CODEX_SOTA_PYTHON,
    (Join-Path $repoRoot '.runtime\.venv-build\Scripts\python.exe'),
    (Join-Path $managerRoot '.venv-build\Scripts\python.exe'))
if (-not $PythonExecutable) {
    foreach ($name in @('python.exe', 'py.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { $candidates += $command.Source }
    }
} else { $candidates = @($PythonExecutable) }
$python = $null
foreach ($candidate in @($candidates | Where-Object { $_ } | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf) -or $candidate -match '\\WindowsApps\\') { continue }
    $probeArguments = @('-I', '-B', '-c', 'import struct,sys,tkinter,PyInstaller,cryptography,zstandard; print(sys.executable) if sys.version_info >= (3,11) and struct.calcsize("P") == 8 else sys.exit(2)')
    if ([IO.Path]::GetFileName($candidate) -ieq 'py.exe') { $probeArguments = @('-3') + $probeArguments }
    try {
        $probe = @(& $candidate @probeArguments 2>$null)
        if ($LASTEXITCODE -eq 0 -and $probe.Count -eq 1) { $python = [string]$probe[0]; break }
    } catch { continue }
}
if (-not $python) { throw 'A 64-bit Python 3.11+ with tkinter and requirements-build.txt is required. Run setup.ps1 or pass -PythonExecutable.' }

function Invoke-CheckedPython([string[]]$Arguments) {
    & $python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed (exit $LASTEXITCODE)." }
}

if (-not $SkipTests) {
    Invoke-CheckedPython @('-B', '-m', 'unittest', 'discover', '-v', '-s', $managerRoot, '-p', 'test_*.py')
}
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
$buildId = [guid]::NewGuid().ToString('N')
$workRoot = Join-Path $repoRoot ('.runtime\release-build-' + $buildId)
$distRoot = Join-Path $workRoot 'dist'
$bundle = Join-Path $distRoot 'codex-sota'
New-Item -ItemType Directory -Path $workRoot -Force | Out-Null
Invoke-CheckedPython @('-B', '-m', 'PyInstaller', '--noconfirm', '--clean', '--log-level', 'WARN', '--distpath', $distRoot,
    '--workpath', (Join-Path $workRoot 'work'), (Join-Path $managerRoot 'codex-sota.spec'))
Copy-Item -LiteralPath (Join-Path $repoRoot 'packaging\codex-sota.cmd') -Destination $bundle
Copy-Item -LiteralPath (Join-Path $repoRoot 'LICENSE') -Destination $bundle
Copy-Item -LiteralPath (Join-Path $repoRoot 'docs\WINDOWS-INSTALL.md') -Destination (Join-Path $bundle 'README.txt')

$runtime = Join-Path $bundle 'codex-sota-runtime.exe'
$null = New-ArtifactManifest $bundle
$validatedFiles = Assert-ArtifactBundle $bundle
$verificationOutput = Join-Path $workRoot 'runtime-verification'
Invoke-CheckedPython @('-B', (Join-Path $managerRoot 'Verify-StartupFix.py'), '--output', $verificationOutput, '--artifact-root', $bundle, '--runtime-only')
$probeCode = 'import json,sys,sqlite3,ssl,tomllib,tkinter,cryptography,zstandard,sota_registry,sota_network,sota_compaction,codex_sota_router,claude_desktop,codex_app_lifecycle,sync_codex_histories_three_way,sync_chatgpt_account_history,CodexSotaManager; assert zstandard.ZstdDecompressor().decompress(zstandard.ZstdCompressor().compress(b"history-runtime-probe")) == b"history-runtime-probe"; print(json.dumps({"status":"ok","version":sys.version.split()[0],"compressed_history":"ok"}))'
$runtimeProbe = @(& $runtime -I -S -B -c $probeCode 2>&1)
if ($LASTEXITCODE -ne 0 -or -not ($runtimeProbe -match '"status": "ok"')) {
    throw "The bundled runtime failed its isolated import check: $($runtimeProbe -join ' ')"
}
$env:CODEX_SOTA_ARTIFACT_ROOT = $bundle
try {
    Invoke-CheckedPython @('-B', '-m', 'unittest', 'discover', '-v', '-s', $managerRoot, '-p', 'test_product_packaging.py')
} finally { Remove-Item Env:CODEX_SOTA_ARTIFACT_ROOT -ErrorAction SilentlyContinue }
$null = Assert-ArtifactBundle $bundle $validatedFiles
if ((Get-ReleaseSourceFingerprint) -ne $sourceFingerprint) {
    throw 'Sources changed during release validation. Build again from a stable source tree; no downloads were published.'
}
$zipPath = Join-Path $outputRoot ("codex-sota-$Version-windows-x64-portable.zip")
Compress-Archive -LiteralPath $bundle -DestinationPath $zipPath -CompressionLevel Optimal -Force

if (-not $SkipInstaller) {
    $compilerCandidates = @($InnoCompiler, $env:INNO_COMPILER,
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 7\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 7\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'))
    $compilerCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($compilerCommand) { $compilerCandidates += $compilerCommand.Source }
    $compiler = $compilerCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
    if (-not $compiler) { throw 'Inno Setup 6.4+ was not found. Install Inno Setup or pass -InnoCompiler. The portable ZIP has been built.' }
    & $compiler '/Q' ("/DAppVersion=$Version") ("/DBundleRoot=$bundle") ("/DOutputRoot=$outputRoot") (Join-Path $repoRoot 'packaging\codex-sota.iss')
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed (exit $LASTEXITCODE)." }
}
$releaseFiles = @(Get-ChildItem -LiteralPath $outputRoot -File | Where-Object { $_.Name -like "codex-sota-$Version-windows-x64-*" -and $_.Extension -in @('.zip', '.exe') } | Sort-Object Name)
$checksums = foreach ($file in $releaseFiles) { (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant() + '  ' + $file.Name }
$checksums | Set-Content -LiteralPath (Join-Path $outputRoot 'SHA256SUMS.txt') -Encoding Ascii
Write-Host "Validated release bundle: $bundle"
Write-Host "Release downloads: $outputRoot"
