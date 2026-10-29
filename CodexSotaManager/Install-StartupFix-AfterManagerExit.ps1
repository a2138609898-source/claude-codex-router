param(
    [Parameter(Mandatory = $true)][int]$ManagerProcessId,
    [Parameter(Mandatory = $true)][string]$ManagerCreationTicks,
    [Parameter(Mandatory = $true)][string]$ManagerExecutable,
    [Parameter(Mandatory = $true)][string]$CandidateRoot,
    [Parameter(Mandatory = $true)][string]$VerificationReport
)

$ErrorActionPreference = 'Stop'
$managerRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$logPath = Join-Path $managerRoot 'startup-fix-deferred-install.log'
$installScript = Join-Path $managerRoot 'Install-StartupFix.ps1'
$mutexName = 'Local\CodexSotaManager.StartupFixInstaller'

function Write-InstallLog([string]$Message) {
    $line = '{0:o} {1}' -f [DateTimeOffset]::Now, $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

try {
    Write-InstallLog "waiting for manager PID $ManagerProcessId to close normally"
    while ($true) {
        $sameProcess = $false
        try {
            # Use one Process object for both path and creation time.  The previous
            # CIM + Get-Process combination could throw a localized CIM conversion
            # error while the manager was exiting and leave the helper waiting forever.
            $started = Get-Process -Id $ManagerProcessId -ErrorAction Stop
            $path = [string]$started.Path
            if ($path -and [string]::Equals([IO.Path]::GetFullPath($path),
                    [IO.Path]::GetFullPath($ManagerExecutable), [StringComparison]::OrdinalIgnoreCase)) {
                $ticks = $started.StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
                $sameProcess = [string]::Equals($ticks, $ManagerCreationTicks, [StringComparison]::Ordinal)
            }
        }
        catch {
            # A missing PID, an access race, or a process that is already exiting
            # all mean the exact identity is no longer present.  The installer is
            # still guarded by full-bundle hashes and source verification.
            $sameProcess = $false
        }
        if (-not $sameProcess) { break }
        Start-Sleep -Seconds 5
    }

    $created = $false
    $mutex = [System.Threading.Mutex]::new($false, $mutexName, [ref]$created)
    try {
        # Multiple launch paths can arm the same manager identity. Serialize the
        # directory swap so the second helper observes an idempotent success.
        try {
            $mutex.WaitOne()
        }
        catch [System.Threading.AbandonedMutexException] {
            # The previous installer died while holding the lock; this helper now
            # owns the abandoned mutex and can safely retry the validated install.
        }
        Write-InstallLog 'manager identity exited; running validated rollback-safe install'
        # The installer rechecks every bundle file AFTER this wait and mutex acquisition;
        # a DLL changed while this helper was waiting cannot reuse an old EXE-only verdict.
        $output = @(& (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') `
            -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $installScript `
            -CandidateRoot $CandidateRoot -VerificationReport $VerificationReport 2>&1)
        $exitCode = $LASTEXITCODE
        foreach ($line in $output) { Write-InstallLog ([string]$line) }
        Write-InstallLog "installer exit code $exitCode"
        exit $exitCode
    }
    finally {
        if ($mutex) {
            try { $mutex.ReleaseMutex() } catch [System.ApplicationException] { }
            $mutex.Dispose()
        }
    }
}
catch {
    Write-InstallLog ('deferred installer failed: ' + $_.Exception.Message)
    exit 1
}
