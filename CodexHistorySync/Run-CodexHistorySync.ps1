param(
    [switch]$NoGui,
    [switch]$AuditOnly,
    [switch]$WaitForExisting,
    [ValidateSet('Current', 'Plus', 'Cockpit', 'Sota', 'None')]
    [string]$RestartProfile = 'Current'
)

$ErrorActionPreference = 'Stop'
$installDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptPath = Join-Path $installDir 'sync_codex_histories_three_way.py'
$profileSwitcherPath = Join-Path $installDir 'Switch-CodexProfile.ps1'
$sotaSwitcherPath = Join-Path $installDir 'Switch-CodexSota.ps1'
$activeProfilePath = Join-Path $installDir 'active-profile.json'
$profileRoots = @{
    Plus = Join-Path $env:USERPROFILE '.codex-plus'
    Cockpit = Join-Path $env:USERPROFILE '.codex-personal'
    Sota = Join-Path $env:USERPROFILE '.codex-sota'
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
    [Environment]::GetEnvironmentVariable('CODEX_PYTHON'),
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
)

$python = $pythonCandidates |
    Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
    Select-Object -First 1

if (-not $python) {
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) {
        $python = $command.Source
    }
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

    $mainProcesses = @($processes | Where-Object { $_.MainWindowHandle -ne 0 })
    if ($mainProcesses.Count -eq 0) {
        $mainProcesses = @($processes | Select-Object -First 1)
    }

    $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
    foreach ($process in $mainProcesses) {
        & $taskkill /PID $process.Id /T | Out-Null
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    do {
        $remaining = @(Get-OwnedCodexAppProcesses)
        if ($remaining.Count -eq 0) {
            return $true
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)

    foreach ($process in $remaining) {
        & $taskkill /PID $process.Id /T /F | Out-Null
    }
    $forceDeadline = [DateTime]::UtcNow.AddSeconds(8)
    do {
        Start-Sleep -Milliseconds 250
        $remaining = @(Get-OwnedCodexAppProcesses)
        if ($remaining.Count -eq 0) {
            return $true
        }
    } while ([DateTime]::UtcNow -lt $forceDeadline)
    throw 'OpenAI.Codex ChatGPT.exe processes remained after shutdown.'
}

function Start-CodexApp {
    param(
        [string]$ExecutablePath,
        [ValidateSet('Plus', 'Cockpit', 'Sota', 'None')]
        [string]$Profile
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
    if ($Profile -eq 'Sota') {
        if (-not (Test-Path -LiteralPath $sotaSwitcherPath)) {
            throw "SOTA profile switcher was not found: $sotaSwitcherPath"
        }
        $arguments = @(
            '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', $sotaSwitcherPath, '-SkipSync', '-NoGui'
        )
    }
    else {
        if (-not (Test-Path -LiteralPath $profileSwitcherPath)) {
            throw "Codex profile switcher was not found: $profileSwitcherPath"
        }
        $arguments = @(
            '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', $profileSwitcherPath, '-Profile', $Profile, '-SkipSync', '-NoGui'
        )
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $powershell @arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -ne 0) {
        $detail = @($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        throw "The $Profile profile switcher failed with exit code $exitCode.`n$detail"
    }
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
$resolvedRestartProfile = 'None'
$scriptExitCode = 0

try {
    if (-not $python) {
        throw 'Python runtime was not found. Keep or reinstall the ChatGPT/Codex local runtime.'
    }
    if (-not (Test-Path -LiteralPath $scriptPath)) {
        throw "Sync core file is missing: $scriptPath"
    }
    if (-not $NoGui -and -not $AuditOnly) {
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
    $raw = & $python $scriptPath @syncArguments 2>&1
    $exitCode = $LASTEXITCODE
    $jsonLine = $raw | Select-Object -Last 1
    $result = $null
    try {
        $result = $jsonLine | ConvertFrom-Json
    }
    catch {
        $result = $null
    }

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
True SOTA: $sotaMain $($messages.main_threads) + $sotaAuxiliary $($messages.auxiliary_sessions) ($($messages.total_records) $sotaCount)
$($messages.searchable_index): $($messages.cockpit) $cockpitSidebarVisible/$cockpitMain; $($messages.plus) $plusSidebarVisible/$plusMain; True SOTA $sotaSidebarVisible/$sotaMain
$($messages.projects): $($messages.cockpit) $cockpitProjects ($cockpitProjectChats $($messages.project_chats)); $($messages.plus) $plusProjects ($plusProjectChats $($messages.project_chats)); True SOTA $sotaProjects ($sotaProjectChats $($messages.project_chats))
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
True SOTA: $sotaMain main chats + $sotaAuxiliary auxiliary sessions ($sotaCount local records)
Searchable main chats: Cockpit $cockpitSidebarVisible/$cockpitMain; Plus $plusSidebarVisible/$plusMain; True SOTA $sotaSidebarVisible/$sotaMain
Projects: Cockpit $cockpitProjects ($cockpitProjectChats project chats); Plus $plusProjects ($plusProjectChats project chats); True SOTA $sotaProjects ($sotaProjectChats project chats)
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
    if ($restartAppWhenDone) {
        try {
            Start-CodexApp -ExecutablePath $appExecutablePath -Profile $resolvedRestartProfile
        }
        catch {
            $scriptExitCode = 1
            Show-SyncMessage -Message "Codex App could not be reopened.`n`n$($_.Exception.Message)" -Title 'Codex restart failed' -Icon 'Error'
        }
    }
}

exit $scriptExitCode
