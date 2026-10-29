param(
    [switch]$NoGui,
    [switch]$AuditOnly,
    [switch]$WaitForExisting,
    [switch]$DeferIfAppRunning,
    [ValidateRange(0, 900)][int]$LockWaitSeconds = 0,
    [ValidateSet('Current', 'Plus', 'Cockpit', 'Sota', 'None')]
    [string]$RestartProfile = 'Current'
)

$ErrorActionPreference = 'Stop'
function Resolve-ConfiguredRoot {
    param([string]$EnvironmentName, [string]$DefaultName)
    $configured = [Environment]::GetEnvironmentVariable($EnvironmentName)
    if (-not [string]::IsNullOrWhiteSpace($configured)) {
        return [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($configured))
    }
    return [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE $DefaultName))
}
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8; $OutputEncoding = [Text.Encoding]::UTF8 } catch { }
$env:PYTHONIOENCODING = 'utf-8'
$installDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptPath = Join-Path $installDir 'sync_codex_histories_three_way.py'
$profileSwitcherPath = Join-Path $installDir 'Switch-CodexProfile.ps1'
$sotaSwitcherPath = Join-Path $installDir 'Switch-CodexSota.ps1'
$activeProfilePath = Join-Path $installDir 'active-profile.json'
$profileRoots = @{
    Plus = Resolve-ConfiguredRoot 'CODEX_SOTA_PLUS_ROOT' '.codex-plus'
    Cockpit = Resolve-ConfiguredRoot 'CODEX_SOTA_COCKPIT_ROOT' '.codex-personal'
    Sota = Resolve-ConfiguredRoot 'CODEX_SOTA_CODEX_ROOT' '.codex-sota'
}
$apiEnvironmentNames = @(
    'OPENAI_API_KEY',
    'CODEX_API_KEY',
    'OPENAI_API_BASE',
    'OPENAI_BASE_URL',
    'OPENAI_ORG_ID',
    'OPENAI_PROJECT_ID',
    'CODEX_ACCESS_TOKEN',
    'CODEX_PROVIDER_A_API_KEY',
    'CODEX_PROVIDER_B_API_KEY',
    'CODEX_PROVIDER_C_API_KEY',
    'CODEX_PROVIDER_D_API_KEY',
    'CODEX_PROVIDER_E_API_KEY',
    'CODEX_PROVIDER_F_API_KEY'
)
$messagesPath = Join-Path $installDir 'messages.zh-CN.json'
$messages = $null
if (Test-Path -LiteralPath $messagesPath) {
    $messages = Get-Content -Raw -Encoding UTF8 -LiteralPath $messagesPath | ConvertFrom-Json
}

$pythonCandidates = @(
    [Environment]::GetEnvironmentVariable('CODEX_SOTA_PYTHON'),
    (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'codex-sota-runtime.exe'),
    [Environment]::GetEnvironmentVariable('CODEX_PYTHON'),
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
)
# Any locally installed CPython, newest first, instead of a hard-coded 3.13/3.12 pair. On this
# machine neither of those exists and only 3.11 does, so the list fell through to `python.exe` on
# PATH -- which on Windows is usually the Microsoft Store stub: it starts, runs nothing, and exits
# 0. A history sync that "succeeded" without ever executing is the worst possible outcome here.
# Run-ThreeRoundValidation.ps1 already carries this same fix for the same reason.
$pythonCandidates += @(
    Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName 'python.exe' }
)

$pythonCandidates += Join-Path (Split-Path -Parent $PSScriptRoot) 'CodexSotaManager\.venv-build\Scripts\python.exe'
$command = Get-Command python.exe -ErrorAction SilentlyContinue
if ($command) { $pythonCandidates += $command.Source }
$python = $null
foreach ($candidate in @($pythonCandidates | Where-Object { $_ } | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf) -or $candidate -match '\\WindowsApps\\') { continue }
    try {
        $probe = @(& $candidate -I -S -B -c 'import sys; print(193731 if sys.version_info >= (3,11) else 0)' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $probe -contains '193731') { $python = $candidate; break }
    }
    catch { continue }
}

function Show-SyncMessage {
    param(
        [string]$Message,
        [string]$Title,
        [string]$Icon
    )

    if ($NoGui) {
        return
    }

    Add-Type -AssemblyName PresentationFramework
    $iconValue = [System.Enum]::Parse([System.Windows.MessageBoxImage], $Icon)
    [System.Windows.MessageBox]::Show(
        $Message,
        $Title,
        [System.Windows.MessageBoxButton]::OK,
        $iconValue
    ) | Out-Null
}

function Get-CodexAppExecutable {
    $configured = [Environment]::GetEnvironmentVariable('CODEX_APP_EXE')
    if (-not [string]::IsNullOrWhiteSpace($configured)) {
        $configured = [Environment]::ExpandEnvironmentVariables($configured)
        if ($configured -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+)' -or [IO.Path]::GetFileName($configured) -notin @('ChatGPT.exe', 'Codex.exe')) {
            throw 'CODEX_APP_EXE must be an absolute path to ChatGPT.exe or Codex.exe.'
        }
        $configured = [System.IO.Path]::GetFullPath($configured)
        if (Test-Path -LiteralPath $configured -PathType Leaf) { return $configured }
        throw 'CODEX_APP_EXE must point to an existing Codex App executable.'
    }
    $package = Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction SilentlyContinue |
        Sort-Object Version -Descending |
        Select-Object -First 1
    if ($package) {
        $candidate = Join-Path $package.InstallLocation 'app\ChatGPT.exe'
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    $mainProcess = Get-Process -Name 'ChatGPT' -ErrorAction SilentlyContinue |
        Where-Object {
            $_.MainWindowHandle -ne 0 -and
            $_.Path -match '\\WindowsApps\\OpenAI\.Codex_'
        } |
        Select-Object -First 1
    if ($mainProcess -and $mainProcess.Path -and (Test-Path -LiteralPath $mainProcess.Path)) {
        return $mainProcess.Path
    }

    return $null
}

function Get-OwnedCodexAppProcesses {
    $expectedExecutable = Get-CodexAppExecutable
    if (-not $expectedExecutable) {
        return @()
    }
    $expectedExecutable = [IO.Path]::GetFullPath($expectedExecutable)
    $processInfo = @(Get-CimInstance Win32_Process -Filter "Name = 'ChatGPT.exe'" -ErrorAction Stop)
    $unowned = @($processInfo | Where-Object {
        -not $_.ExecutablePath -or
        -not [string]::Equals(
            [IO.Path]::GetFullPath([string]$_.ExecutablePath),
            $expectedExecutable,
            [StringComparison]::OrdinalIgnoreCase
        )
    })
    if ($unowned.Count -gt 0) {
        throw "Refusing to stop unowned ChatGPT.exe process IDs: $(@($unowned.ProcessId) -join ', ')"
    }
    $ownedIds = @($processInfo | ForEach-Object { [int]$_.ProcessId })
    if ($ownedIds.Count -eq 0) {
        return @()
    }
    return @(Get-Process -Id $ownedIds -ErrorAction SilentlyContinue)
}

function Stop-CodexApp {
    $processes = @(Get-OwnedCodexAppProcesses)
    if ($processes.Count -eq 0) {
        return $false
    }

    foreach ($process in $processes | Where-Object { $_.MainWindowHandle -ne 0 }) {
        $expectedPath = $process.Path
        $expectedBirth = $process.StartTime.ToUniversalTime().ToFileTimeUtc()
        $fresh = Get-Process -Id $process.Id -ErrorAction Stop
        if (-not [string]::Equals($fresh.Path, $expectedPath, [StringComparison]::OrdinalIgnoreCase) -or
            $fresh.StartTime.ToUniversalTime().ToFileTimeUtc() -ne $expectedBirth) {
            throw 'Codex process identity changed before shutdown. No recycled process was closed.'
        }
        $null = $fresh.CloseMainWindow()
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    do {
        $remaining = @(Get-OwnedCodexAppProcesses)
        if ($remaining.Count -eq 0) {
            return $true
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)

    throw 'Codex has not finished closing. It was not force-terminated. Finish or cancel active work, close its window, then retry history sync.'
}

function Set-RestartLaunchRequest {
    param([string]$RequestId)
    $work = Join-Path $installDir 'work'
    $requests = Join-Path $work 'codex-launch-requests'
    [IO.Directory]::CreateDirectory($requests) | Out-Null
    $path = Join-Path $requests ('restart-' + $RequestId + '-' + $PID + '.json')
    $guard = [IO.File]::Open((Join-Path $work 'codex-launch-requests.lock'),
        [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
    $held = $false
    $temporary = $path + '.new-' + $PID
    try {
        $clock = [Diagnostics.Stopwatch]::StartNew()
        do {
            try { $guard.Lock(0, 1); $held = $true }
            catch [IO.IOException] {
                if ($clock.Elapsed.TotalSeconds -ge 1) { throw 'Could not publish the Codex restart request.' }
                Start-Sleep -Milliseconds 100
            }
        } while (-not $held)
        $payload = @{
            request_id = $RequestId; request_source = 'restart-runner'; launcher_pid = $PID
            launcher_creation_ticks = (Get-Process -Id $PID -ErrorAction Stop).StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
            expires_at = ([DateTime]::UtcNow.AddMinutes(30) - [DateTime]'1970-01-01Z').TotalSeconds
        } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($temporary, $payload, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $path -Force
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        if ($held) { $guard.Unlock(0, 1) }
        $guard.Dispose()
    }
}

function Clear-RestartLaunchRequest {
    param([string]$RequestId)
    # This filename is private to this runner PID and one immutable request.
    $path = Join-Path $installDir ('work\codex-launch-requests\restart-' + $RequestId + '-' + $PID + '.json')
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
}

function Start-CodexApp {
    param(
        [string]$ExecutablePath,
        [ValidateSet('Plus', 'Cockpit', 'Sota', 'None')]
        [string]$Profile,
        [ValidateRange(1, 30)][int]$MaxAttempts = 5,
        [ValidateRange(0, 60000)][int]$RetryDelayMilliseconds = 1000
    )

    if (-not $ExecutablePath -or -not (Test-Path -LiteralPath $ExecutablePath)) {
        throw 'Codex App executable was not found for restart.'
    }
    if ($Profile -eq 'None') {
        return
    }

    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $powershell)) {
        throw 'Windows PowerShell was not found for the isolated Codex restart.'
    }
    $requestId = [guid]::NewGuid().ToString('N')
    if ($Profile -eq 'Sota') {
        if (-not (Test-Path -LiteralPath $sotaSwitcherPath)) {
            throw "SOTA profile switcher was not found: $sotaSwitcherPath"
        }
        $arguments = @(
            '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', $sotaSwitcherPath, '-SkipSync', '-NoGui',
            '-LaunchRequestId', $requestId
        )
    }
    else {
        if (-not (Test-Path -LiteralPath $profileSwitcherPath)) {
            throw "Codex profile switcher was not found: $profileSwitcherPath"
        }
        $arguments = @(
            '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', $profileSwitcherPath, '-Profile', $Profile, '-SkipSync', '-NoGui',
            '-LaunchRequestId', $requestId
        )
    }
    Set-RestartLaunchRequest -RequestId $requestId
    try {
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = @(& $powershell @arguments 2>&1)
            $exitCode = $LASTEXITCODE
        }
        finally { $ErrorActionPreference = $previous }
        $launchResult = $null
        foreach ($line in $output) {
            try {
                $parsed = ([string]$line | ConvertFrom-Json -ErrorAction Stop)
                if ($parsed.status) { $launchResult = $parsed }
            } catch { }
        }
        if ($launchResult -and $launchResult.status -eq 'deferred' -and $exitCode -in @(0, 75)) {
            if ($attempt -eq $MaxAttempts) {
                throw "The $Profile restart is still deferred after $MaxAttempts attempts ($($launchResult.phase)). Codex was not confirmed reopened; retry once synchronization has finished."
            }
            Start-Sleep -Milliseconds $RetryDelayMilliseconds
            continue
        }
        if ($exitCode -ne 0 -or -not $launchResult -or $launchResult.status -ne 'ready') {
            $detail = @($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
            throw "The $Profile profile switcher did not confirm restart (exit code $exitCode).`n$detail"
        }
        return
    }
    }
    finally { Clear-RestartLaunchRequest -RequestId $requestId }
}

function Resolve-RestartProfile {
    param([ValidateSet('Current', 'Plus', 'Cockpit', 'Sota', 'None')][string]$RequestedProfile)

    if ($RequestedProfile -ne 'Current') {
        return $RequestedProfile
    }
    foreach ($scope in @('Process', 'User')) {
        $configuredHome = [Environment]::GetEnvironmentVariable('CODEX_HOME', $scope)
        if ([string]::IsNullOrWhiteSpace($configuredHome)) {
            continue
        }
        try {
            $configuredFullPath = [IO.Path]::GetFullPath($configuredHome)
            foreach ($entry in $profileRoots.GetEnumerator()) {
                if ($configuredFullPath -eq [IO.Path]::GetFullPath([string]$entry.Value)) {
                    return [string]$entry.Key
                }
            }
        }
        catch {
            continue
        }
    }
    if (Test-Path -LiteralPath $activeProfilePath) {
        try {
            $marker = Get-Content -Raw -Encoding UTF8 -LiteralPath $activeProfilePath | ConvertFrom-Json
            if ([string]$marker.profile -in @('Plus', 'Cockpit', 'Sota')) {
                return [string]$marker.profile
            }
        }
        catch {
            # A malformed marker must never guess a different login channel.
        }
    }
    throw 'The active Codex profile could not be determined. Pass -RestartProfile Plus, Cockpit, Sota, or None explicitly.'
}

$appExecutablePath = $null
$restartAppWhenDone = $false
$syncSafeToRestart = $false
$resolvedRestartProfile = 'None'
$scriptExitCode = 0

try {
    if (-not $python) {
        throw 'Python runtime was not found. Keep or reinstall the ChatGPT/Codex local runtime.'
    }
    if (-not (Test-Path -LiteralPath $scriptPath)) {
        throw "Sync core file is missing: $scriptPath"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $profileRoots.Cockpit 'state_5.sqlite')) -or
        -not (Test-Path -LiteralPath (Join-Path $profileRoots.Plus 'state_5.sqlite'))) {
        # Detect a standalone installation before considering any app shutdown.
        # The CLI owns availability semantics and does not create missing stores.
        $availability = @(& $python $scriptPath --json --audit-only 2>&1)
        $availabilityResult = $null
        foreach ($line in $availability) {
            try {
                $parsed = ([string]$line | ConvertFrom-Json -ErrorAction Stop)
                if ($parsed.status) { $availabilityResult = $parsed }
            } catch { }
        }
        if ($availabilityResult -and $availabilityResult.status -eq 'skipped' -and
            $availabilityResult.reason -eq 'optional_profiles_absent') {
            $availabilityResult.mode = if ($AuditOnly) { 'three-way-audit' } else { 'three-way' }
            if ($NoGui) { Write-Output ($availabilityResult | ConvertTo-Json -Depth 8 -Compress) }
            else { Show-SyncMessage -Message ([string]$availabilityResult.message) -Title 'Cross-profile history sync inactive' -Icon 'Information' }
            exit 0
        }
    }
    if (-not $NoGui -and -not $AuditOnly -and -not $DeferIfAppRunning) {
        $resolvedRestartProfile = Resolve-RestartProfile -RequestedProfile $RestartProfile
        $appExecutablePath = Get-CodexAppExecutable
        if (-not $appExecutablePath) {
            throw 'Codex App executable was not found.'
        }
        $restartAppWhenDone = $true
        Stop-CodexApp | Out-Null
    }

    $syncArguments = @('--json')
    if ($AuditOnly) {
        $syncArguments += '--audit-only'
    }
    if ($WaitForExisting) {
        $syncArguments += '--wait-for-existing'
    }
    if ($DeferIfAppRunning) { $syncArguments += '--defer-if-app-running' }
    if ($PSBoundParameters.ContainsKey('LockWaitSeconds') -or -not $WaitForExisting) {
        $syncArguments += @('--lock-wait-seconds', $LockWaitSeconds.ToString())
    }
    # Native stderr is diagnostic data, not a PowerShell terminating exception.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $raw = @(& $python $scriptPath @syncArguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousPreference }
    $jsonLine = $raw | Select-Object -Last 1
    $result = $null
    try {
        $result = $jsonLine | ConvertFrom-Json
    }
    catch {
        $result = $null
    }

    if ($result -and $result.status -eq 'deferred') {
        # Busy/running is a normal coordination result. Preserve the core JSON
        # instead of turning it into "sync failed" and a launch error dialog.
        if ($NoGui) { Write-Output ($result | ConvertTo-Json -Depth 8 -Compress) }
        else { Show-SyncMessage -Message 'History sync is deferred until Codex is safely closed and other synchronization has finished.' -Title 'Codex history sync deferred' -Icon 'Information' }
        exit 0
    }

    if ($result -and $result.status -eq 'skipped' -and $result.reason -eq 'optional_profiles_absent') {
        $syncSafeToRestart = $true
        if ($NoGui) { Write-Output ($result | ConvertTo-Json -Depth 8 -Compress) }
        else { Show-SyncMessage -Message ([string]$result.message) -Title 'Cross-profile history sync inactive' -Icon 'Information' }
        exit 0
    }

    $syncSafeToRestart = $result -and ($result.status -eq 'ok' -or $result.recovery_status -in @('restored', 'not_needed'))

    if ($exitCode -ne 0 -or -not $result -or $result.status -ne 'ok') {
        $detail = if ($result -and $result.error) {
            $result.error
        }
        else {
            $raw -join [Environment]::NewLine
        }
        $logPath = if ($result -and $result.log_path) {
            $result.log_path
        }
        else {
            Join-Path $installDir 'logs\sync.log'
        }
        if ($messages) {
            $message = "$($messages.sync_failed)`n`n$($messages.details):`n$detail`n`n$($messages.log):`n$logPath"
            $title = $messages.failure_title
        }
        else {
            $message = "Sync failed.`n`n$detail`n`nLog:`n$logPath"
        }
        throw $message
    }

    if ($AuditOnly) {
        if ($NoGui) {
            Write-Output ($result | ConvertTo-Json -Depth 8 -Compress)
        }
        else {
            Show-SyncMessage -Message 'Three-way sync runner audit passed.' -Title 'Codex history sync audit' -Icon 'Information'
        }
        exit 0
    }

    $cockpitVerification = $result.verification.($result.cockpit_root)
    $plusVerification = $result.verification.($result.plus_root)
    $sotaVerification = $result.verification.($result.sota_root)
    $cockpitCount = $cockpitVerification.threads
    $plusCount = $plusVerification.threads
    $sotaCount = $sotaVerification.threads
    $cockpitMain = $cockpitVerification.unarchived_main_threads
    $plusMain = $plusVerification.unarchived_main_threads
    $sotaMain = $sotaVerification.unarchived_main_threads
    $cockpitAuxiliary = $cockpitVerification.auxiliary_sessions
    $plusAuxiliary = $plusVerification.auxiliary_sessions
    $sotaAuxiliary = $sotaVerification.auxiliary_sessions
    $cockpitSidebarVisible = $cockpitVerification.provider_visible_main_threads
    $plusSidebarVisible = $plusVerification.provider_visible_main_threads
    $sotaSidebarVisible = $sotaVerification.provider_visible_main_threads
    $cockpitProjects = $cockpitVerification.projects
    $plusProjects = $plusVerification.projects
    $sotaProjects = $sotaVerification.projects
    $cockpitProjectChats = $cockpitVerification.project_assigned_main_threads
    $plusProjectChats = $plusVerification.project_assigned_main_threads
    $sotaProjectChats = $sotaVerification.project_assigned_main_threads

    if ($messages) {
        $message = @"
$($messages.success_intro)

$($messages.cockpit): $cockpitMain $($messages.main_threads) + $cockpitAuxiliary $($messages.auxiliary_sessions) ($($messages.total_records) $cockpitCount)
$($messages.plus): $plusMain $($messages.main_threads) + $plusAuxiliary $($messages.auxiliary_sessions) ($($messages.total_records) $plusCount)
Tango Relay: $sotaMain $($messages.main_threads) + $sotaAuxiliary $($messages.auxiliary_sessions) ($($messages.total_records) $sotaCount)
$($messages.searchable_index): $($messages.cockpit) $cockpitSidebarVisible/$cockpitMain; $($messages.plus) $plusSidebarVisible/$plusMain; Tango Relay $sotaSidebarVisible/$sotaMain
$($messages.projects): $($messages.cockpit) $cockpitProjects ($cockpitProjectChats $($messages.project_chats)); $($messages.plus) $plusProjects ($plusProjectChats $($messages.project_chats)); Tango Relay $sotaProjects ($sotaProjectChats $($messages.project_chats))
$($messages.new_copies): $($result.new_files)
$($messages.incremental_updates): $($result.updated_files)
$($messages.conflict_copies): $($result.conflicts_preserved)
$($messages.exact_duplicates_removed): $($result.exact_duplicates_removed)（其中主对话 $($result.duplicate_main_threads_removed)）
$($messages.duration): $($result.duration_seconds) $($messages.seconds)

$($messages.verified)
$($messages.sidebar_note)
"@
        $title = $messages.success_title
    }
    else {
        $message = @"
Codex history sync completed.

Cockpit: $cockpitMain main chats + $cockpitAuxiliary auxiliary sessions ($cockpitCount local records)
Plus: $plusMain main chats + $plusAuxiliary auxiliary sessions ($plusCount local records)
Tango Relay: $sotaMain main chats + $sotaAuxiliary auxiliary sessions ($sotaCount local records)
Searchable main chats: Cockpit $cockpitSidebarVisible/$cockpitMain; Plus $plusSidebarVisible/$plusMain; Tango Relay $sotaSidebarVisible/$sotaMain
Projects: Cockpit $cockpitProjects ($cockpitProjectChats project chats); Plus $plusProjects ($plusProjectChats project chats); Tango Relay $sotaProjects ($sotaProjectChats project chats)
New copies: $($result.new_files)
Incremental updates: $($result.updated_files)
Conflict copies: $($result.conflicts_preserved)
Exact duplicates removed: $($result.exact_duplicates_removed) (main chats: $($result.duplicate_main_threads_removed))
Duration: $($result.duration_seconds) seconds

All three databases and session files passed verification.
The original Projects + Recent sidebar layout is preserved. Sync no longer creates a custom all-chats section.
"@
        $title = 'Codex history sync completed'
    }

    if ($result.conflicts_preserved -gt 0) {
        if ($messages) {
            $message += "`n$($messages.conflict_note)"
        }
        else {
            $message += "`nA thread was continued on both sides. Both branches were preserved."
        }
    }

    Show-SyncMessage -Message $message -Title $title -Icon 'Information'
    if ($NoGui) {
        Write-Output ($result | ConvertTo-Json -Depth 8 -Compress)
    }
}
catch {
    $scriptExitCode = 1
    if ($messages) {
        $message = "$($messages.sync_start_failed)`n`n$($_.Exception.Message)"
        $title = $messages.failure_title
    }
    else {
        $message = "Sync could not start.`n`n$($_.Exception.Message)"
        $title = 'Codex history sync failed'
    }
    if ($NoGui) {
        $errorResult = [ordered]@{
            status = 'error'
            mode = 'three-way-runner'
            error = $_.Exception.Message
        }
        Write-Output ($errorResult | ConvertTo-Json -Depth 4 -Compress)
    }
    else {
        Show-SyncMessage -Message $message -Title $title -Icon 'Error'
    }
}
finally {
    if ($restartAppWhenDone -and $syncSafeToRestart) {
        try {
            Start-CodexApp -ExecutablePath $appExecutablePath -Profile $resolvedRestartProfile
        }
        catch {
            $scriptExitCode = 1
            Show-SyncMessage -Message "Codex App could not be reopened.`n`n$($_.Exception.Message)" -Title 'Codex restart failed' -Icon 'Error'
        }
    }
    elseif ($restartAppWhenDone) {
        Show-SyncMessage -Message 'Codex was left closed because synchronization did not confirm a safe completed or restored state. Review the sync result before reopening it.' -Title 'Codex restart deferred' -Icon 'Warning'
    }
}

exit $scriptExitCode
