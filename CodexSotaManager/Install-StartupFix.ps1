param(
    [Parameter(Mandatory = $true)][string]$CandidateRoot,
    [Parameter(Mandatory = $true)][string]$VerificationReport
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$managerRoot = [IO.Path]::GetFullPath($PSScriptRoot)
. (Join-Path $managerRoot 'Artifact-Integrity.ps1')

$candidate = (Assert-ScopedArtifactPath $CandidateRoot $managerRoot).TrimEnd('\', '/')
$live = Assert-ScopedArtifactPath (Join-Path $managerRoot 'dist\codex-sota') $managerRoot
if ($candidate -eq $live) { throw 'Candidate and live directories must be different.' }
$expectedFiles = Read-ArtifactVerification $VerificationReport $candidate (Split-Path -Parent $managerRoot)
if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
    # A duplicate deferred helper can arrive after the first helper has already
    # moved the candidate to live. Treat that exact state as success instead of
    # turning an installed build into a false failure.
    if (Test-Path -LiteralPath $live -PathType Container) {
        $null = Assert-ArtifactBundle $live $expectedFiles
        [ordered]@{
            status = 'already-installed'; live = $live; rollback = $null
            codex_app_stopped = $false; deleted_files = 0
        } | ConvertTo-Json -Compress
        exit 0
    }
    throw "The validated candidate directory is missing: $candidate"
}
$null = Assert-ArtifactBundle $candidate $expectedFiles

$rollback = Assert-ScopedArtifactPath (Join-Path $managerRoot ('dist\codex-sota.before-startup-fix-' + [guid]::NewGuid().ToString('N'))) $managerRoot
$retired = $false
try {
    if (Test-Path -LiteralPath $live) {
        # Same-volume renames; keep the entire original bundle, including every
        # DLL, for rollback. Never prune or delete the user's older backups.
        Move-Item -LiteralPath $live -Destination $rollback -ErrorAction Stop
        $retired = $true
    }
    Move-Item -LiteralPath $candidate -Destination $live -ErrorAction Stop
}
catch {
    $installError = $_.Exception.Message
    if ($retired -and -not (Test-Path -LiteralPath $live)) {
        Move-Item -LiteralPath $rollback -Destination $live -ErrorAction Stop
    }
    [ordered]@{
        status = 'not-installed'; reason = $installError; live = $live; candidate = $candidate
        hint = 'If the manager has locked its files, close only codex-sota and rerun. Codex App was not stopped.'
    } | ConvertTo-Json -Compress
    exit 2
}

[ordered]@{
    status = 'installed'; live = $live; rollback = if ($retired) { $rollback } else { $null }
    codex_app_stopped = $false; deleted_files = 0
} | ConvertTo-Json -Compress
