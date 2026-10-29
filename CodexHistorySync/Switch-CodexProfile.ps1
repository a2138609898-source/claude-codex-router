param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Plus', 'Cockpit')]
    [string]$Profile,
    [switch]$NoGui,
    [switch]$AuditOnly,
    [switch]$SkipSync,
    [switch]$PrepareOnly,
    [switch]$ChatGptDirect,
    [switch]$Restart,
    [string]$LaunchRequestId,
    [ValidateRange(0, 30)][int]$LifecycleWaitSeconds = 3,
    [ValidateRange(1, 120)][int]$WindowReadyTimeoutSeconds = 30
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
if (-not $LaunchRequestId) { $LaunchRequestId = [guid]::NewGuid().ToString('N') }
if ($LaunchRequestId -notmatch '^[a-zA-Z0-9_-]{1,80}$') { throw 'Invalid launch request id.' }
$installDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$lifecycleWorkDir = Join-Path $installDir 'work'
$lifecycleLockPath = Join-Path $lifecycleWorkDir 'codex-app-lifecycle.lock'
$launchRequestDir = Join-Path $lifecycleWorkDir 'codex-launch-requests'
$launchRequestGuardPath = Join-Path $lifecycleWorkDir 'codex-launch-requests.lock'
$shellLaunchRequestPath = Join-Path $launchRequestDir ('shell-' + $LaunchRequestId + '-' + $PID + '.json')
$launchStatusPath = Join-Path $lifecycleWorkDir ('launch-status-' + $LaunchRequestId + '.json')
$runnerPath = Join-Path $installDir 'Run-CodexHistorySync.ps1'
$syncCorePath = Join-Path $installDir 'sync_codex_histories.py'
$lastResultPath = Join-Path $installDir 'last-result.json'
$postExitWatcherPath = Join-Path $installDir 'sync_after_codex_exit.py'
$accountHistorySyncPath = Join-Path $installDir 'sync_chatgpt_account_history.py'
$script:accountHistorySyncResult = $null
$configValidatorPath = Join-Path $installDir 'validate_codex_profile.py'
$activeProfilePath = Join-Path $installDir 'active-profile.json'
$canonicalCockpitRoot = Resolve-ConfiguredRoot 'CODEX_SOTA_COCKPIT_ROOT' '.codex-personal'
$canonicalPlusRoot = Resolve-ConfiguredRoot 'CODEX_SOTA_PLUS_ROOT' '.codex-plus'
$canonicalSotaRoot = Resolve-ConfiguredRoot 'CODEX_SOTA_CODEX_ROOT' '.codex-sota'
$legacyPlusRoot = Resolve-ConfiguredRoot 'CODEX_SOTA_LEGACY_PLUS_ROOT' '.codex-plus-legacy'
$cockpitToolsExecutable = [Environment]::GetEnvironmentVariable('CODEX_COCKPIT_TOOLS_EXE')
if ([string]::IsNullOrWhiteSpace($cockpitToolsExecutable)) {
    $cockpitToolsExecutable = Join-Path $env:LOCALAPPDATA 'Cockpit Tools\cockpit-tools.exe'
}
$cockpitToolsExecutable = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($cockpitToolsExecutable))
$cockpitServicePort = 56319
$configuredCockpitPort = [Environment]::GetEnvironmentVariable('CODEX_COCKPIT_API_PORT')
if (-not [string]::IsNullOrWhiteSpace($configuredCockpitPort)) {
    $parsedCockpitPort = 0
    if (-not [int]::TryParse($configuredCockpitPort, [ref]$parsedCockpitPort) -or $parsedCockpitPort -lt 1 -or $parsedCockpitPort -gt 65535) {
        throw 'CODEX_COCKPIT_API_PORT must be an integer from 1 to 65535.'
    }
    $cockpitServicePort = $parsedCockpitPort
}
$profileRoots = @{
    Plus = $canonicalPlusRoot
    Cockpit = $canonicalCockpitRoot
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

function Show-ProfileMessage {
    param(
        [string]$Message,
        [string]$Title,
        [ValidateSet('Information', 'Warning', 'Error')]
        [string]$Icon = 'Information'
    )

    if ($NoGui) {
        Write-Output $Message
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

function Enter-AppLifecycleLock {
    param([string]$LockPath = $lifecycleLockPath, [int]$WaitSeconds = $LifecycleWaitSeconds)
    [IO.Directory]::CreateDirectory($lifecycleWorkDir) | Out-Null
    $stream = [IO.File]::Open($LockPath, [IO.FileMode]::OpenOrCreate,
        [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
    $clock = [Diagnostics.Stopwatch]::StartNew()
    do {
        try { $stream.Lock(0, 1); return $stream }
        catch [IO.IOException] {
            if ($clock.Elapsed.TotalSeconds -ge $WaitSeconds) { $stream.Dispose(); return $null }
            Start-Sleep -Milliseconds 100
        }
    } while ($true)
}

function Set-LaunchRequest {
    [IO.Directory]::CreateDirectory($launchRequestDir) | Out-Null
    $payload = @{
        request_id = $LaunchRequestId; request_source = 'shell'; launcher_pid = $PID
        launcher_creation_ticks = (Get-Process -Id $PID -ErrorAction Stop).StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
        expires_at = ([DateTime]::UtcNow.AddMinutes(30) - [DateTime]'1970-01-01Z').TotalSeconds
    } | ConvertTo-Json -Compress
    $guard = Enter-AppLifecycleLock -LockPath $launchRequestGuardPath -WaitSeconds 1
    if (-not $guard) { throw 'Could not publish the Codex profile launch request.' }
    $temporary = $shellLaunchRequestPath + '.new-' + $PID
    try {
        [IO.File]::WriteAllText($temporary, $payload, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $shellLaunchRequestPath -Force
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        $guard.Unlock(0, 1); $guard.Dispose()
    }
}

function Clear-LaunchRequest {
    $guard = $null
    try {
        $guard = Enter-AppLifecycleLock -LockPath $launchRequestGuardPath -WaitSeconds 1
        if (-not $guard) { return }
        Remove-Item -LiteralPath $shellLaunchRequestPath -Force -ErrorAction SilentlyContinue
    }
    catch { }
    finally { if ($guard) { $guard.Unlock(0, 1); $guard.Dispose() } }
}

function Write-ProfileLaunchResult {
    param([string]$Status, [string]$Phase, [int]$AppProcessId = 0, [string]$ErrorText)
    $app = if ($AppProcessId -gt 0) { Get-Process -Id $AppProcessId -ErrorAction SilentlyContinue } else { $null }
    $result = [ordered]@{
        status = $Status; profile = $Profile; request_id = $LaunchRequestId; phase = $Phase
        launcher_pid = $PID
        launcher_creation_ticks = (Get-Process -Id $PID -ErrorAction SilentlyContinue).StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
        app_process_id = if ($app) { $app.Id } else { $null }
        app_creation_ticks = if ($app) { $app.StartTime.ToUniversalTime().ToFileTimeUtc().ToString() } else { $null }
        window_owned = [bool]($app -and $app.MainWindowHandle -ne 0)
        error = $ErrorText
        account_history_sync = $script:accountHistorySyncResult
    } | ConvertTo-Json -Compress
    if (-not $AuditOnly -and -not $PrepareOnly) {
        $temporary = $launchStatusPath + '.new-' + $PID
        try {
            [IO.Directory]::CreateDirectory($lifecycleWorkDir) | Out-Null
            [IO.File]::WriteAllText($temporary, $result, [Text.UTF8Encoding]::new($false))
            Move-Item -LiteralPath $temporary -Destination $launchStatusPath -Force
        }
        catch { Write-Warning 'Could not persist profile launch status; structured stdout remains available.' }
        finally { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
    }
    Write-Output $result
}

function Get-ActiveCodexAppBackends {
    return @(Get-CimInstance Win32_Process -Filter "Name = 'codex.exe'" -ErrorAction Stop |
        Where-Object {
            $_.CommandLine -match '(?:^|\s)"?app-server"?(?:\s|$)' -and
            ($_.ExecutablePath -match '\\OpenAI\\(?:CodexCliBundled|Codex)\\' -or
             $_.ExecutablePath -match '\\WindowsApps\\OpenAI\.Codex_[^\\]+\\app\\resources\\')
        })
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
    $expectedExecutable = [System.IO.Path]::GetFullPath($expectedExecutable)
    $processInfo = @(Get-CimInstance Win32_Process -Filter "Name = 'ChatGPT.exe'" -ErrorAction Stop)
    $unowned = @($processInfo | Where-Object {
        -not $_.ExecutablePath -or
        -not [string]::Equals(
            [System.IO.Path]::GetFullPath([string]$_.ExecutablePath),
            $expectedExecutable,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    })
    if ($unowned.Count -gt 0) {
        throw "Refusing to stop unowned ChatGPT.exe process IDs: $(@($unowned.ProcessId) -join ', ')"
    }
    $ownedIds = @($processInfo | ForEach-Object { [int]$_.ProcessId })
    if ($ownedIds.Count -eq 0) {
        # Get-Process -Id @() fails during parameter binding, which -ErrorAction cannot suppress,
        # so the common "App is not running yet" case must return before the call.
        return @()
    }
    return @(Get-Process -Id $ownedIds -ErrorAction SilentlyContinue)
}

function Stop-CodexApp {
    $processes = @(Get-OwnedCodexAppProcesses)
    if ($processes.Count -eq 0) {
        return
    }

    foreach ($process in $processes | Where-Object { $_.MainWindowHandle -ne 0 }) {
        $null = $process.CloseMainWindow()
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    do {
        $remaining = @(Get-OwnedCodexAppProcesses)
        if ($remaining.Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)

    throw 'Codex has not finished closing. It was not force-terminated. Finish or cancel active work, close its window, then retry the profile switch.'
}

function Stop-CockpitTools {
    $processNames = @('cockpit-tools', 'cockpit-cliproxy')
    $processes = @(Get-Process -Name $processNames -ErrorAction SilentlyContinue)
    if ($processes.Count -eq 0) {
        return $false
    }

    foreach ($process in $processes | Where-Object { $_.MainWindowHandle -ne 0 }) {
        $null = $process.CloseMainWindow()
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        $remaining = @(Get-Process -Name $processNames -ErrorAction SilentlyContinue)
        if ($remaining.Count -eq 0) {
            break
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)

    foreach ($process in $remaining) {
        Stop-Process -Id $process.Id -Force -ErrorAction Stop
    }

    $portDeadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        if (-not (Test-LocalTcpPort -Port $cockpitServicePort)) {
            return $true
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $portDeadline)

    throw "Cockpit API port $cockpitServicePort is still listening after Cockpit Tools was stopped."
}

function Get-ProfileAuthMode {
    param([string]$Root)

    $authPath = Join-Path $Root 'auth.json'
    if (-not (Test-Path -LiteralPath $authPath)) {
        return 'missing'
    }
    try {
        $auth = Get-Content -Raw -LiteralPath $authPath | ConvertFrom-Json
        if ($auth.auth_mode) {
            return [string]$auth.auth_mode
        }
        return 'unknown'
    }
    catch {
        return 'invalid'
    }
}

function Test-ProfileConfig {
    param(
        [string]$Root,
        [ValidateSet('Plus', 'Cockpit')]
        [string]$SelectedProfile
    )

    $result = Invoke-ProfileConfigValidator -Root $Root -SelectedProfile $SelectedProfile
    return $null -ne $result -and $result.valid -eq $true
}

function Test-ExplicitPortableCodexApp {
    param([string]$ExecutablePath)
    $configured = [Environment]::GetEnvironmentVariable('CODEX_APP_EXE')
    if ([string]::IsNullOrWhiteSpace($configured)) { return $false }
    $configured = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($configured))
    $selected = [IO.Path]::GetFullPath($ExecutablePath)
    if (-not [string]::Equals($configured, $selected, [StringComparison]::OrdinalIgnoreCase)) { return $false }
    # A Store executable always needs its package identity, including an old
    # package selected explicitly. Never fall back after a Store launch fails.
    if ($selected -match '\\WindowsApps\\') { return $false }
    $packages = @(Invoke-WithComRetry -Action { Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction Stop })
    foreach ($package in $packages) {
        foreach ($image in @('ChatGPT.exe', 'Codex.exe')) {
            $packaged = Join-Path $package.InstallLocation ('app\' + $image)
            if ([string]::Equals($selected, [IO.Path]::GetFullPath($packaged), [StringComparison]::OrdinalIgnoreCase)) {
                return $false
            }
        }
    }
    return $true
}

function Assert-ChatGptDirectProfile {
    # This entry is opt-in. Existing Plus/Cockpit launchers retain their behavior.
    # Validate before closing any app; no config, credentials, or registry is written.
    if ($Profile -ne 'Plus') { throw 'ChatGptDirect only supports the separate Plus account profile.' }
    if (-not (Test-Path -LiteralPath $accountHistorySyncPath -PathType Leaf)) {
        throw 'The account-only history helper is missing. No app was closed and SOTA was not changed.'
    }
    if (-not (Test-Path -LiteralPath $canonicalPlusRoot -PathType Container)) {
        throw "The separate account profile is missing: $canonicalPlusRoot. SOTA was not changed."
    }
    $authMode = Get-ProfileAuthMode -Root $canonicalPlusRoot
    if ($authMode -notin @('chatgpt', 'missing')) {
        throw "The account profile has incompatible credentials ($authMode). They were not cleared or replaced."
    }
    $validation = Invoke-ProfileConfigValidator -Root $canonicalPlusRoot -SelectedProfile Plus
    if (-not $validation -or $validation.valid -ne $true) {
        $reason = if ($validation) { $validation.reason } else { 'validator_failed' }
        throw "The separate account profile failed read-only isolation validation ($reason). No configuration was changed."
    }
}

function Set-ProfileStartEnvironment {
    param([System.Diagnostics.ProcessStartInfo]$StartInfo, [string]$Root)
    $childEnvironment = $StartInfo.EnvironmentVariables
    if ($null -eq $childEnvironment) { throw 'The Codex child process environment could not be initialized.' }
    $childEnvironment['CODEX_HOME'] = $Root
    foreach ($name in $apiEnvironmentNames) { $childEnvironment.Remove($name) }
    if ($ChatGptDirect) {
        # Only the new child is changed. Machine/user/manager/SOTA environments
        # and stored credentials stay intact, including any global CLI pin.
        foreach ($name in @($childEnvironment.Keys)) {
            if ($name -match '^(?i:OPENAI_.*|CODEX_(?:API_KEY|ACCESS_TOKEN|CLI_PATH|.*_API_KEY))$') {
                $childEnvironment.Remove($name)
            }
        }
    }
}


function Start-CodexAppForProfile {
    param(
        [string]$ExecutablePath,
        [ValidateSet('Plus', 'Cockpit')]
        [string]$SelectedProfile
    )

    if (-not $ExecutablePath -or -not (Test-Path -LiteralPath $ExecutablePath)) {
        throw 'Codex App executable was not found.'
    }

    $profileRoot = $profileRoots[$SelectedProfile]
    if (-not (Test-Path -LiteralPath $profileRoot)) {
        throw "Codex profile directory was not found: $profileRoot"
    }

    $authMode = Get-ProfileAuthMode -Root $profileRoot
    $expectedAuthMode = if ($SelectedProfile -eq 'Plus') { 'chatgpt' } else { 'apikey' }
    $signedOutAccount = $ChatGptDirect -and $SelectedProfile -eq 'Plus' -and $authMode -eq 'missing'
    if ($authMode -ne $expectedAuthMode -and -not $signedOutAccount) {
        throw "The $SelectedProfile profile has the wrong authentication mode: $authMode (expected $expectedAuthMode)."
    }
    if (-not (Test-ProfileConfig -Root $profileRoot -SelectedProfile $SelectedProfile)) {
        throw "The $SelectedProfile profile configuration failed isolation validation."
    }

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $ExecutablePath
    $startInfo.WorkingDirectory = Split-Path -Parent $ExecutablePath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $false
    Set-ProfileStartEnvironment -StartInfo $startInfo -Root $profileRoot

    $storeLauncher = Join-Path $PSScriptRoot 'Start-CodexStoreProcess.ps1'
    if (Test-ExplicitPortableCodexApp -ExecutablePath $ExecutablePath) {
        $startedProcess = [System.Diagnostics.Process]::Start($startInfo)
    }
    else { $startedProcess = & $storeLauncher -StartInfo $startInfo }
    if (-not $startedProcess) {
        throw 'Windows did not return a Codex App process.'
    }

    $deadline = [DateTime]::UtcNow.AddSeconds($WindowReadyTimeoutSeconds)
    $lastOwned = @()
    do {
        Start-Sleep -Milliseconds 250
        $lastOwned = @(Get-OwnedCodexAppProcesses)
        $window = @($lastOwned | Where-Object { $_.MainWindowHandle -ne 0 } | Sort-Object StartTime | Select-Object -First 1)
        if ($window.Count -gt 0) { return [int]$window[0].Id }
    } while ([DateTime]::UtcNow -lt $deadline)

    if ($lastOwned.Count -gt 0) {
        $script:launchWindowPending = $true
        return [int](@($lastOwned | Sort-Object StartTime | Select-Object -First 1)[0].Id)
    }
    throw 'The owned Codex desktop process exited before creating its window.'
}

function Get-SessionFileCount {
    param([string]$Root)

    $sessions = Join-Path $Root 'sessions'
    if (-not (Test-Path -LiteralPath $sessions)) {
        return 0
    }
    return @(Get-ChildItem -LiteralPath $sessions -Recurse -File -Filter '*.jsonl' -ErrorAction SilentlyContinue).Count
}

function Test-LocalTcpPort {
    param([int]$Port)

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        return $task.Wait(500) -and $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Ensure-CockpitApiService {
    $owners = @(Get-LocalTcpOwnerIds -Port $cockpitServicePort)
    if ($owners.Count -gt 0 -and -not (Test-CockpitListenerOwnership -OwnerIds $owners)) {
        throw "Cockpit API port $cockpitServicePort is owned by an unrelated process; it was not used."
    }
    if ($owners.Count -eq 0 -and (Test-LocalTcpPort -Port $cockpitServicePort)) {
        throw "Cockpit API port $cockpitServicePort is open, but its owner could not be verified."
    }
    if ($owners.Count -eq 0) {
        if (-not (Test-Path -LiteralPath $cockpitToolsExecutable)) {
            return $false
        }
        $workingDirectory = Split-Path -Parent $cockpitToolsExecutable
        Start-Process -FilePath $cockpitToolsExecutable -WorkingDirectory $workingDirectory | Out-Null
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(12)
    do {
        Start-Sleep -Milliseconds 300
        $owners = @(Get-LocalTcpOwnerIds -Port $cockpitServicePort)
        if ($owners.Count -gt 0 -and -not (Test-CockpitListenerOwnership -OwnerIds $owners)) {
            throw "Cockpit API port $cockpitServicePort was taken by an unrelated process during startup."
        }
        if ($owners.Count -gt 0 -and (Test-CockpitApiHealth)) {
            return $true
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Get-PythonExecutable {
    $candidates = @(
        [Environment]::GetEnvironmentVariable('CODEX_SOTA_PYTHON'),
        (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'codex-sota-runtime.exe'),
        [Environment]::GetEnvironmentVariable('CODEX_PYTHON'),
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    )
    # Any locally installed CPython, newest first -- see Switch-CodexSota.ps1 for why:
    # PATH python.exe on Windows is usually the Microsoft Store stub, which starts, runs
    # nothing, and exits 0, so a missing runtime python reads as an invalid profile.
    $candidates += @(
        Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName 'python.exe' }
    )
    $candidates += Join-Path (Split-Path -Parent $PSScriptRoot) 'CodexSotaManager\.venv-build\Scripts\python.exe'
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    foreach ($candidate in @($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf) -or $candidate -match '\\WindowsApps\\') { continue }
        try {
            $probe = @(& $candidate -I -S -B -c 'import sys; print(193731 if sys.version_info >= (3,11) else 0)' 2>$null)
            if ($LASTEXITCODE -eq 0 -and $probe -contains '193731') { return $candidate }
        }
        catch { continue }
    }
    return $null
}

function Get-PythonwExecutable {
    $python = Get-PythonExecutable
    if (-not $python) {
        return $null
    }
    $pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
    if (Test-Path -LiteralPath $pythonw) {
        return $pythonw
    }
    return $null
}

function Invoke-ProfileConfigValidator {
    param(
        [string]$Root,
        [ValidateSet('Plus', 'Cockpit')]
        [string]$SelectedProfile,
        [switch]$ProbeCockpit
    )

    if (-not (Test-Path -LiteralPath $configValidatorPath)) {
        return [pscustomobject]@{ valid = $false; reason = 'validator_missing' }
    }
    $python = Get-PythonExecutable
    if (-not $python) {
        return [pscustomobject]@{ valid = $false; reason = 'python_missing' }
    }
    $arguments = @(
        $configValidatorPath,
        '--profile', $SelectedProfile,
        '--root', $Root
    )
    if ($ProbeCockpit) {
        $arguments += @('--probe-cockpit', '--timeout-seconds', '1.5')
    }
    if ($ChatGptDirect) { $arguments += '--chatgpt-direct' }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $python @arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    for ($index = $output.Count - 1; $index -ge 0; $index--) {
        try {
            $result = ([string]$output[$index]) | ConvertFrom-Json
            if ($null -ne $result.valid) {
                if ($exitCode -ne 0) {
                    $result.valid = $false
                }
                return $result
            }
        }
        catch {
            continue
        }
    }
    return [pscustomobject]@{ valid = $false; reason = 'validator_failed' }
}

function Get-LocalTcpOwnerIds {
    param([int]$Port)

    $ids = @()
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        try {
            $ids = @(
                Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop |
                    Select-Object -ExpandProperty OwningProcess
            )
        }
        catch {
            $ids = @()
        }
    }
    if ($ids.Count -eq 0) {
        foreach ($line in @(& netstat.exe -ano -p tcp 2>$null)) {
            if ($line -notmatch '^\s*TCP\s+(\S+)\s+\S+\s+LISTENING\s+(\d+)\s*$') {
                continue
            }
            if ($matches[1] -notmatch (':' + [regex]::Escape([string]$Port) + '$')) {
                continue
            }
            $owner = 0
            if ([int]::TryParse($matches[2], [ref]$owner) -and $owner -gt 0) {
                $ids += $owner
            }
        }
    }
    return @($ids | Sort-Object -Unique)
}

function Test-CockpitListenerOwnership {
    param([int[]]$OwnerIds)

    if ($OwnerIds.Count -eq 0) {
        return $false
    }
    foreach ($ownerId in $OwnerIds) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerId" -ErrorAction SilentlyContinue
        if (-not $process) {
            return $false
        }
        $imageName = [IO.Path]::GetFileName([string]$process.ExecutablePath)
        if (-not $imageName) {
            $imageName = [string]$process.Name
        }
        if ($imageName -notmatch '^(?i:cockpit-tools|cockpit-cliproxy)(?:\.exe)?$') {
            return $false
        }
    }
    return $true
}

function Test-CockpitApiHealth {
    $result = Invoke-ProfileConfigValidator -Root $canonicalCockpitRoot -SelectedProfile Cockpit -ProbeCockpit
    return $null -ne $result -and $result.valid -eq $true
}

function Set-ActiveProfileMarker {
    param([ValidateSet('Plus', 'Cockpit')][string]$SelectedProfile, [int]$AppProcessId)

    $app = Get-Process -Id $AppProcessId -ErrorAction Stop
    $payload = [ordered]@{
        profile = $SelectedProfile
        updated_utc = [DateTime]::UtcNow.ToString('o')
        app_process_id = $app.Id
        app_creation_ticks = $app.StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
        creation_request_id = $LaunchRequestId
        request_id = $LaunchRequestId
    } | ConvertTo-Json -Compress
    $temporaryPath = $activeProfilePath + '.new-' + $PID + '-' + [guid]::NewGuid().ToString('N')
    try {
        [IO.File]::WriteAllText($temporaryPath, $payload, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporaryPath -Destination $activeProfilePath -Force -ErrorAction Stop
    }
    finally {
        Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
    }
}

function Get-RequestedProfileApp {
    foreach ($path in @($activeProfilePath, $launchStatusPath)) {
        try {
            $marker = Get-Content -Raw -Encoding UTF8 -LiteralPath $path -ErrorAction Stop | ConvertFrom-Json
            if ($marker.profile -ne $Profile) { continue }
            if (-not $ChatGptDirect -and $marker.creation_request_id -ne $LaunchRequestId -and
                $marker.request_id -ne $LaunchRequestId) { continue }
            if (-not $marker.app_process_id -or -not $marker.app_creation_ticks) { continue }
            foreach ($app in @(Get-OwnedCodexAppProcesses)) {
                if ($app.Id -eq [int]$marker.app_process_id -and
                    $app.StartTime.ToUniversalTime().ToFileTimeUtc().ToString() -eq [string]$marker.app_creation_ticks) { return $app }
            }
        }
        catch { }
    }
    return $null
}

function Start-PostExitSyncWatcher {
    param([int]$AppProcessId)

    if (-not (Test-Path -LiteralPath $postExitWatcherPath)) {
        throw "Post-exit sync watcher is missing: $postExitWatcherPath"
    }
    $pythonw = Get-PythonwExecutable
    if (-not $pythonw) {
        throw 'pythonw.exe was not found for post-exit history sync.'
    }
    Start-Process -FilePath $pythonw -ArgumentList @(
        ('"' + $postExitWatcherPath + '"'),
        '--pid',
        $AppProcessId.ToString(),
        '--app-executable', ('"' + (Get-Process -Id $AppProcessId -ErrorAction Stop).Path + '"'),
        '--creation-ticks', (Get-Process -Id $AppProcessId -ErrorAction Stop).StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
    ) -WorkingDirectory $installDir -WindowStyle Hidden | Out-Null
}

function Recover-LegacyPlusHistory {
    $legacyCount = Get-SessionFileCount -Root $legacyPlusRoot
    if ($legacyCount -eq 0) {
        return 0
    }

    $python = Get-PythonExecutable
    if (-not $python) {
        throw 'Python runtime was not found for legacy Plus history recovery.'
    }
    $legacyBackupRoot = Join-Path $installDir 'backups\legacy-plus'
    $coreArguments = @(
        '--left', $legacyPlusRoot,
        '--right', $canonicalPlusRoot,
        '--backup-base', $legacyBackupRoot,
        '--json'
    )
    $raw = & $python $syncCorePath @coreArguments 2>&1
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
        $detail = if ($result -and $result.error) { $result.error } else { $raw -join [Environment]::NewLine }
        throw "Legacy .codex-plus recovery failed: $detail"
    }
    return $legacyCount
}

function Get-StructuredSyncResult {
    param([object[]]$OutputLines)

    for ($index = $OutputLines.Count - 1; $index -ge 0; $index--) {
        try {
            $candidate = ([string]$OutputLines[$index]) | ConvertFrom-Json
            if ($candidate -and $candidate.status) {
                return $candidate
            }
        }
        catch {
            continue
        }
    }
    return $null
}

function Test-StructuredSyncSuccess {
    param(
        [object]$Result,
        [switch]$AuditOnly
    )

    if ($Result -and $Result.status -eq 'skipped' -and
        $Result.reason -eq 'optional_profiles_absent' -and $Result.sync_performed -eq $false -and
        @($Result.missing_optional_profiles).Count -gt 0 -and
        $Result.mode -eq $(if ($AuditOnly) { 'three-way-audit' } else { 'three-way' })) {
        return $true
    }
    if (-not $Result -or $Result.status -ne 'ok') {
        return $false
    }
    if ($AuditOnly) {
        if ($Result.mode -ne 'three-way-audit' -or -not $Result.roots) {
            return $false
        }
        foreach ($root in @($canonicalCockpitRoot, $canonicalPlusRoot, $canonicalSotaRoot)) {
            $rootAudit = $Result.roots.($root)
            if (-not $rootAudit -or -not $rootAudit.exists -or -not $rootAudit.state_db_exists -or -not $rootAudit.sessions_exists) {
                return $false
            }
        }
        return $true
    }

    if (
        $Result.mode -ne 'three-way' -or
        $Result.cockpit_root -ne $canonicalCockpitRoot -or
        $Result.plus_root -ne $canonicalPlusRoot -or
        $Result.sota_root -ne $canonicalSotaRoot -or
        -not $Result.verification -or
        -not $Result.verification.three_way_same_thread_ids
    ) {
        return $false
    }
    foreach ($root in @($canonicalCockpitRoot, $canonicalPlusRoot, $canonicalSotaRoot)) {
        $rootVerification = $Result.verification.($root)
        if (
            -not $rootVerification -or
            $rootVerification.integrity -ne 'ok' -or
            $rootVerification.wrong_model_provider_threads -ne 0 -or
            $rootVerification.wrong_rollout_provider_sessions -ne 0 -or
            $rootVerification.missing_rollout_paths -ne 0 -or
            $rootVerification.missing_sidebar_main_threads -ne 0
        ) {
            return $false
        }
    }
    return $true
}

function Invoke-CanonicalHistorySync {
    param([switch]$AuditOnly)

    if (-not (Test-Path -LiteralPath $runnerPath)) {
        throw "Sync runner is missing: $runnerPath"
    }
    $powershell = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $powershell)) {
        $powershell = 'powershell.exe'
    }
    $lastExitCode = 1
    $lastDetail = ''
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        $runnerArguments = @(
            '-NoLogo',
            '-NoProfile',
            '-File', $runnerPath,
            '-NoGui',
            '-RestartProfile', 'None'
        )
        if ($AuditOnly) {
            $runnerArguments += '-AuditOnly'
        }
        $raw = @(& $powershell @runnerArguments 2>&1)
        $lastExitCode = $LASTEXITCODE
        $syncResult = Get-StructuredSyncResult -OutputLines $raw
        if (Test-StructuredSyncSuccess -Result $syncResult -AuditOnly:$AuditOnly) {
            return
        }
        $lastDetail = if ($syncResult -and $syncResult.error) {
            [string]$syncResult.error
        }
        else {
            $raw -join [Environment]::NewLine
        }
        if ($lastDetail -notmatch '已经在运行|already running') {
            break
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Canonical history sync failed with exit code $lastExitCode.`n$lastDetail"
}

function Get-LastSyncResult {
    if (-not (Test-Path -LiteralPath $lastResultPath)) {
        return $null
    }
    try {
        return Get-Content -Raw -Encoding UTF8 -LiteralPath $lastResultPath | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Invoke-ChatGptAccountHistorySync {
    # The caller keeps lifecycle protection while lending sync.lock to the
    # child. The child owns that history lock even if this launcher exits.
    if (-not $ChatGptDirect -or -not $lifecycleLease -or $historyWriteLease) {
        throw 'Account history sync requires the account launcher lifecycle lease and a released history lease.'
    }
    $python = Get-PythonExecutable
    if (-not $python) { throw 'Python 3.11+ was not found for account history synchronization.' }
    $ticks = (Get-Process -Id $PID -ErrorAction Stop).StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
    $arguments = @('-B', $accountHistorySyncPath, '--source', $canonicalSotaRoot,
        '--target', $canonicalPlusRoot, '--launcher-pid', [string]$PID,
        '--launcher-creation-ticks', $ticks, '--launch-request-id', $LaunchRequestId)
    $raw = @(& $python @arguments 2>&1)
    $syncExit = $LASTEXITCODE
    $result = Get-StructuredSyncResult -OutputLines $raw
    if ($result -and $result.status -eq 'deferred' -and $syncExit -eq 2) { return $false }
    if ($syncExit -ne 0 -or -not $result -or $result.status -ne 'ok' -or
        $result.mode -ne 'account-history-one-way' -or $result.source_read_only -ne $true -or
        $result.source -ne $canonicalSotaRoot -or $result.target -ne $canonicalPlusRoot -or
        $result.mapped_source_threads -ne $result.source_threads) {
        $detail = if ($result -and $result.error) { [string]$result.error } else { $raw -join [Environment]::NewLine }
        throw "Account history synchronization did not complete safely. SOTA was not written. $detail"
    }
    $script:accountHistorySyncResult = $result
    return $true
}


$targetRoot = $profileRoots[$Profile]
# Direct mode excludes the existing bidirectional engine. Its separate,
# one-way catch-up below is mandatory even when a legacy button passes SkipSync.
if ($ChatGptDirect) { $SkipSync = $true }
$plusLauncherCommand = Get-Command 'codex-plus.cmd' -ErrorAction SilentlyContinue
$plusLauncher = if ($plusLauncherCommand) { $plusLauncherCommand.Source } else { $null }
$launcherText = if ($plusLauncher -and (Test-Path -LiteralPath $plusLauncher)) {
    Get-Content -Raw -Encoding UTF8 -LiteralPath $plusLauncher
}
else {
    ''
}
$machineApiSignals = @($apiEnvironmentNames | Where-Object {
    -not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($_, 'Machine'))
}).Count

if ($PrepareOnly -and -not $AuditOnly) {
    try {
        if (-not (Test-Path -LiteralPath $targetRoot)) {
            throw "Codex profile directory was not found: $targetRoot"
        }
        $targetAuthMode = Get-ProfileAuthMode -Root $targetRoot
        $expectedAuthMode = if ($Profile -eq 'Plus') { 'chatgpt' } else { 'apikey' }
        if ($targetAuthMode -ne $expectedAuthMode) {
            throw "The $Profile profile has the wrong authentication mode: $targetAuthMode (expected $expectedAuthMode)."
        }
        if (-not (Test-ProfileConfig -Root $targetRoot -SelectedProfile $Profile)) {
            throw "The $Profile profile configuration failed isolation validation."
        }
        if ($Profile -eq 'Cockpit' -and -not (Ensure-CockpitApiService)) {
            throw "Cockpit API did not become healthy on 127.0.0.1:$cockpitServicePort."
        }
        if ($NoGui) {
            [ordered]@{ status = 'ready'; profile = $Profile } | ConvertTo-Json -Compress
        }
        exit 0
    }
    catch {
        if ($NoGui) {
            [ordered]@{ status = 'error'; profile = $Profile; error = $_.Exception.Message } |
                ConvertTo-Json -Compress
        }
        else {
            Show-ProfileMessage -Title 'Codex 配置准备失败' -Icon Error -Message $_.Exception.Message
        }
        exit 1
    }
}

if ($AuditOnly) {
    $syncRunnerAuditReady = $false
    $syncRunnerAuditError = $null
    try {
        Invoke-CanonicalHistorySync -AuditOnly
        $syncRunnerAuditReady = $true
    }
    catch {
        $syncRunnerAuditError = $_.Exception.Message
    }
    $targetAuthMode = Get-ProfileAuthMode -Root $targetRoot
    $profileConfigValid = Test-ProfileConfig -Root $targetRoot -SelectedProfile $Profile
    $authReady = if ($Profile -eq 'Plus') {
        $targetAuthMode -eq 'chatgpt'
    }
    else {
        $targetAuthMode -eq 'apikey'
    }
    $appReady = -not [string]::IsNullOrEmpty((Get-CodexAppExecutable))
    $targetReady = Test-Path -LiteralPath $targetRoot
    $audit = [ordered]@{
        status = if ($targetReady -and $authReady -and $profileConfigValid -and $appReady -and $syncRunnerAuditReady) { 'ready' } else { 'needs_repair' }
        profile = $Profile
        target_root = $targetRoot
        target_exists = Test-Path -LiteralPath $targetRoot
        profile_config_valid = $profileConfigValid
        cockpit_session_files = Get-SessionFileCount -Root $canonicalCockpitRoot
        plus_session_files = Get-SessionFileCount -Root $canonicalPlusRoot
        legacy_plus_session_files = Get-SessionFileCount -Root $legacyPlusRoot
        cockpit_auth_mode = Get-ProfileAuthMode -Root $canonicalCockpitRoot
        plus_auth_mode = Get-ProfileAuthMode -Root $canonicalPlusRoot
        user_codex_home = [Environment]::GetEnvironmentVariable('CODEX_HOME', 'User')
        plus_launcher_uses_app_switcher = (
            $launcherText -match [regex]::Escape('Switch-CodexProfile.ps1') -and
            $launcherText -match '-Profile Plus'
        )
        plus_launcher_runs_codex_cli = $launcherText -match [regex]::Escape('%~dp0codex.exe')
        machine_api_signal_count = $machineApiSignals
        cockpit_tools_exists = Test-Path -LiteralPath $cockpitToolsExecutable
        cockpit_api_port_open = Test-LocalTcpPort -Port $cockpitServicePort
        chatgpt_app_exists = -not [string]::IsNullOrEmpty((Get-CodexAppExecutable))
        post_exit_watcher_exists = Test-Path -LiteralPath $postExitWatcherPath
        pythonw_exists = -not [string]::IsNullOrEmpty((Get-PythonwExecutable))
        sync_runner_audit_ready = $syncRunnerAuditReady
        sync_runner_audit_error = $syncRunnerAuditError
        app_launch_method = 'ChatGPT.exe+UseShellExecuteFalse'
        launch_policy = 'clear-inherited-api-environment-use-profile-config'
        layout_policy = 'native-projects-and-recent'
    }
    $json = $audit | ConvertTo-Json -Depth 4 -Compress
    if ($NoGui) {
        Write-Output $json
    }
    else {
        Show-ProfileMessage -Title 'Codex 配置审查' -Icon Information -Message ($audit | Format-List | Out-String)
    }
    if ($audit.status -eq 'ready') {
        exit 0
    }
    exit 1
}

$lifecycleLease = $null
$historyWriteLease = $null
try {
# Announce intent before Appx/config probes or normal shutdown. These requests
# are independent of SOTA launches and share the same synchronization guards.
Set-LaunchRequest
$appExecutable = Get-CodexAppExecutable
if (-not $appExecutable) {
    Show-ProfileMessage -Title 'Codex 切换失败' -Icon Error -Message '找不到 Codex App 程序。'
    exit 1
}

$legacyRecovered = 0
$cockpitServiceReady = $null
try {
    if ($ChatGptDirect) { Assert-ChatGptDirectProfile }
    $lifecycleLease = Enter-AppLifecycleLock
    if ($lifecycleLease) {
        $historyWriteLease = Enter-AppLifecycleLock -LockPath (Join-Path $installDir 'sync.lock') -WaitSeconds 0
    }
    if (-not $lifecycleLease -or -not $historyWriteLease) {
        Write-ProfileLaunchResult -Status 'deferred' -Phase 'waiting_for_history_sync'
        exit 0
    }
    $requestedApp = Get-RequestedProfileApp
    if (-not $requestedApp) {
        if ($ChatGptDirect -and -not $Restart -and @(Get-OwnedCodexAppProcesses).Count -gt 0) {
            throw 'Another Codex profile is running. It was not closed. Finish its work, close it, or explicitly confirm the account switch in the manager.'
        }
        Stop-CodexApp
        if ($Profile -eq 'Plus' -and -not $ChatGptDirect) {
            $null = Stop-CockpitTools
        }
        if (-not $SkipSync) {
            # The explicit cross-profile sync acquires both leases itself.
            $historyWriteLease.Unlock(0, 1); $historyWriteLease.Dispose(); $historyWriteLease = $null
            $lifecycleLease.Unlock(0, 1); $lifecycleLease.Dispose(); $lifecycleLease = $null
            Clear-LaunchRequest
            Invoke-CanonicalHistorySync
            Set-LaunchRequest
            $lifecycleLease = Enter-AppLifecycleLock
            if ($lifecycleLease) {
                $historyWriteLease = Enter-AppLifecycleLock -LockPath (Join-Path $installDir 'sync.lock') -WaitSeconds 0
            }
            if (-not $lifecycleLease -or -not $historyWriteLease) {
                Write-ProfileLaunchResult -Status 'deferred' -Phase 'waiting_for_history_sync'
                exit 0
            }
        }
        if (@(Get-ActiveCodexAppBackends).Count -gt 0) {
            Write-ProfileLaunchResult -Status 'deferred' -Phase 'waiting_for_previous_app'
            exit 0
        }
        if ($ChatGptDirect) {
            Write-ProfileLaunchResult -Status 'starting' -Phase 'account_history_sync'
            $historyWriteLease.Unlock(0, 1); $historyWriteLease.Dispose(); $historyWriteLease = $null
            $accountHistoryReady = Invoke-ChatGptAccountHistorySync
            $historyWriteLease = Enter-AppLifecycleLock -LockPath (Join-Path $installDir 'sync.lock') -WaitSeconds 0
            if (-not $accountHistoryReady -or -not $historyWriteLease) {
                Write-ProfileLaunchResult -Status 'deferred' -Phase 'waiting_for_history_sync'
                exit 0
            }
            if (@(Get-OwnedCodexAppProcesses).Count -gt 0 -or @(Get-ActiveCodexAppBackends).Count -gt 0) {
                Write-ProfileLaunchResult -Status 'deferred' -Phase 'waiting_for_previous_app'
                exit 0
            }
        }
    }
}
catch {
    Show-ProfileMessage -Title 'Codex 切换失败' -Icon Error -Message "无法安全地准备登录环境。`n`n$($_.Exception.Message)"
    if ($NoGui) { Write-ProfileLaunchResult -Status 'error' -Phase 'preflight' -ErrorText $_.Exception.Message }
    exit 1
}

if ($Profile -eq 'Cockpit') {
    try {
        $cockpitServiceReady = Ensure-CockpitApiService
        if (-not $cockpitServiceReady) {
            throw "Cockpit API did not become healthy on 127.0.0.1:$cockpitServicePort."
        }
    }
    catch {
        Show-ProfileMessage -Title 'Codex 切换失败' -Icon Error -Message "Cockpit API 服务未通过健康与归属检查。`n`n$($_.Exception.Message)"
        exit 1
    }
}

try {
    if ($requestedApp) {
        $appProcessId = $requestedApp.Id
        $script:launchWindowPending = $requestedApp.MainWindowHandle -eq 0
    }
    else { $appProcessId = Start-CodexAppForProfile -ExecutablePath $appExecutable -SelectedProfile $Profile }
}
catch {
    $launchError = $_.Exception.Message
    Show-ProfileMessage -Title 'Codex 切换失败' -Icon Error -Message "无法打开 Codex App。`n`n$launchError"
    exit 1
}

try {
    Set-ActiveProfileMarker -SelectedProfile $Profile -AppProcessId $appProcessId
}
catch {
    Write-Warning ("The active profile marker could not be saved: " + $_.Exception.Message)
}
try {
    # The new account-only entry must not start a writer for other profiles.
    # SOTA's existing launcher/watchers and legacy Plus/Cockpit paths are unchanged.
    if (-not $ChatGptDirect) { Start-PostExitSyncWatcher -AppProcessId $appProcessId }
}
catch {
    # Optional post-exit maintenance must not close a successfully opened app.
    Write-Warning ("Post-exit history maintenance could not be armed: " + $_.Exception.Message)
}

if ($script:launchWindowPending) {
    Write-ProfileLaunchResult -Status 'deferred' -Phase 'waiting_for_window' -AppProcessId $appProcessId
    exit 0
}
if ($NoGui) {
    Write-ProfileLaunchResult -Status 'ready' -Phase 'window_ready' -AppProcessId $appProcessId
    exit 0
}

$result = Get-LastSyncResult
$mainCount = $null
$totalCount = $null
if ($result -and $result.verification) {
    $verification = $result.verification.($targetRoot)
    if ($verification) {
        $mainCount = $verification.unarchived_main_threads
        $totalCount = $verification.threads
    }
}
if ($ChatGptDirect -and $script:accountHistorySyncResult) {
    $mainCount = $script:accountHistorySyncResult.unarchived_main_threads
    $totalCount = $script:accountHistorySyncResult.account_threads
}
$profileLabel = if ($Profile -eq 'Plus') { 'GPT Plus' } else { 'Cockpit API' }
$environmentNote = if ($Profile -eq 'Plus') {
    if ($ChatGptDirect) {
        '已只读读取 SOTA 历史并补齐账号侧副本；SOTA 配置、原始对话和后台路由保持不变。'
    } else {
        '已停止 Cockpit 后台，并通过独立子进程把 Plus 配置和无 API 环境传入 Codex App。'
    }
}
else {
    '已启动 Cockpit API 服务，并通过独立子进程把 Cockpit 配置传入 Codex App；全局第三方 API 环境已隔离。'
}
$countLine = if ($null -ne $mainCount) {
    "本地 Codex 历史：$mainCount 条主对话（含子代理共 $totalCount 条记录）。"
}
else {
    "本地会话文件：$(Get-SessionFileCount -Root $targetRoot) 条。"
}
$legacyLine = if ($legacyRecovered -gt 0) {
    "已从停用的 .codex-plus 目录抢救 $legacyRecovered 条会话。`n"
}
else {
    ''
}
$serviceLine = ''
$watcherLine = "`n关闭 $profileLabel App 后会自动再执行一次 Cockpit、GPT Plus 与 Tango Relay 三方同步。"
if ($ChatGptDirect) { $watcherLine = "`n下次从管理器切换到账号版时，会先安全补齐 SOTA 的新增历史；账号侧独立续写会保留。" }
$messageIcon = 'Information'

Show-ProfileMessage -Title "Codex 已切换到 $profileLabel" -Icon $messageIcon -Message @"
Codex 已切换到 $profileLabel。

$countLine
$legacyLine$environmentNote
已保留原生“项目 + 最近”侧栏布局。$serviceLine$watcherLine
"@
}
finally {
    if ($historyWriteLease) { $historyWriteLease.Unlock(0, 1); $historyWriteLease.Dispose() }
    if ($lifecycleLease) { $lifecycleLease.Unlock(0, 1); $lifecycleLease.Dispose() }
    Clear-LaunchRequest
}

exit 0
