param(
    [int]$TimeoutMinutes = 120,
    [switch]$Wait
)

$ErrorActionPreference = 'Stop'
# Emit UTF-8 regardless of the host's console codepage: callers capture this output as
# UTF-8, and localized Windows error text (zh-CN Move-Item failures and the like) otherwise
# goes out as GBK bytes and crashes the capturing reader.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$live = Join-Path $root 'dist\codex-sota'
$staged = Join-Path $root 'dist-staging\codex-sota'
$log = Join-Path $root 'apply-staged-build.log'

function Note([string]$text) {
    $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '  ' + $text
    try {
        Add-Content -LiteralPath $log -Value $line -Encoding UTF8 -ErrorAction Stop
    }
    catch {
        # Applying a validated build must not fail merely because the diagnostic log is locked.
        Write-Warning ('could not write apply-staged-build.log: ' + $_.Exception.Message)
    }
}

function Remove-EmptyDirectory([string]$path) {
    try {
        if (-not (Test-Path -LiteralPath $path -PathType Container)) {
            return
        }
        $children = @(Get-ChildItem -LiteralPath $path -Force -ErrorAction Stop)
        if ($children.Count -eq 0) {
            Remove-Item -LiteralPath $path -Force -ErrorAction Stop
        }
    }
    catch {
        # This is cosmetic cleanup after the transaction has committed.
        Note ('could not remove empty staging directory: ' + $_.Exception.Message)
    }
}

function Invoke-Swap {
    if (-not (Test-Path -LiteralPath $staged)) {
        Note 'nothing to apply: dist-staging\codex-sota is missing'
        return $true
    }
    $swapId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
    $distRoot = Split-Path -Parent $live
    $stagedRoot = Split-Path -Parent $staged
    $incoming = Join-Path $distRoot ('codex-sota.incoming-' + $swapId)
    $retired = Join-Path $distRoot ('codex-sota.old-' + $swapId)
    $liveRetired = $false
    $incomingPrepared = $false
    try {
        New-Item -ItemType Directory -Path $distRoot -Force | Out-Null
        # Prepare the incoming tree first so a bad staged directory cannot disturb live.
        Move-Item -LiteralPath $staged -Destination $incoming -ErrorAction Stop
        $incomingPrepared = $true
        if (Test-Path -LiteralPath $live) {
            Move-Item -LiteralPath $live -Destination $retired -ErrorAction Stop
            $liveRetired = $true
        }
        Move-Item -LiteralPath $incoming -Destination $live -ErrorAction Stop
        $incomingPrepared = $false
    }
    catch {
        $swapError = $_.Exception.Message
        $rollbackErrors = [System.Collections.Generic.List[string]]::new()
        if ($liveRetired -and -not (Test-Path -LiteralPath $live) -and (Test-Path -LiteralPath $retired)) {
            try {
                Move-Item -LiteralPath $retired -Destination $live -ErrorAction Stop
                $liveRetired = $false
            }
            catch {
                $rollbackErrors.Add('restore live: ' + $_.Exception.Message)
            }
        }
        if ($incomingPrepared -and (Test-Path -LiteralPath $incoming) -and -not (Test-Path -LiteralPath $staged)) {
            try {
                New-Item -ItemType Directory -Path $stagedRoot -Force | Out-Null
                Move-Item -LiteralPath $incoming -Destination $staged -ErrorAction Stop
                $incomingPrepared = $false
            }
            catch {
                $rollbackErrors.Add('restore staged: ' + $_.Exception.Message)
            }
        }
        if ($rollbackErrors.Count -gt 0) {
            $detail = $rollbackErrors -join '; '
            Note ("swap failed and rollback was incomplete: $swapError; $detail")
            throw "Staged build swap failed and rollback was incomplete: $swapError; $detail"
        }
        Note ("swap failed; live and staged builds were restored: $swapError")
        return $false
    }

    # The new live tree is committed at this point. Logging and removal of an empty staging
    # parent are best-effort post-commit work and must never trigger transaction rollback.
    Remove-EmptyDirectory $stagedRoot
    # Keep only the two newest rollback copies: every apply otherwise parks another full
    # build snapshot in dist\ codex-sota.old-* forever.
    try {
        $staleRollbacks = @(Get-ChildItem -LiteralPath $distRoot -Directory -Filter 'codex-sota.old-*' |
            Sort-Object Name -Descending |
            Select-Object -Skip 2)
        foreach ($stale in $staleRollbacks) {
            Remove-Item -LiteralPath $stale.FullName -Recurse -Force -ErrorAction Stop
        }
    }
    catch {
        Note ('could not prune old build snapshots: ' + $_.Exception.Message)
    }
    if ($liveRetired) {
        Note ('applied staged build; previous build retired to ' + $retired)
    }
    else {
        Note 'applied staged build; there was no previous live build'
    }
    return $true
}

if (Invoke-Swap) {
    exit 0
}

if (-not $Wait) {
    Note 'staged build swap failed; rerun with -Wait for transient locks or inspect the preceding error'
    exit 1
}

Note 'staged build swap failed; waiting for transient locks or other temporary filesystem errors to clear'
$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 5
    if (Invoke-Swap) {
        exit 0
    }
}

Note ('gave up after ' + $TimeoutMinutes + ' minutes; staged build left in dist-staging')
exit 1
