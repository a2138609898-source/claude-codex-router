param(
    [switch]$AuditOnly,
    [switch]$NoGui,
    [switch]$SkipSync,
    [switch]$PrepareOnly,
    [switch]$Restart,
    [switch]$SyncBeforeLaunch,
    [string]$LaunchRequestId,
    [ValidateRange(0, 30)][int]$LifecycleWaitSeconds = 3,
    [ValidateRange(1, 120)][int]$WindowReadyTimeoutSeconds = 30
)

$ErrorActionPreference = 'Stop'
$launchClock = [System.Diagnostics.Stopwatch]::StartNew()
if (-not $LaunchRequestId) { $LaunchRequestId = [guid]::NewGuid().ToString('N') }
if ($LaunchRequestId -notmatch '^[a-zA-Z0-9_-]{1,80}$') { throw 'Invalid launch request id.' }
$lifecycleWorkDir = Join-Path $PSScriptRoot 'work'
$lifecycleLockPath = Join-Path $lifecycleWorkDir 'codex-app-lifecycle.lock'
$launchRequestPath = Join-Path $lifecycleWorkDir 'codex-launch-request.json'
$launchStatusPath = Join-Path $lifecycleWorkDir ('launch-status-' + $LaunchRequestId + '.json')
# Read the previous identity before any "starting" phase overwrites this file.
# It is an independent recovery record if writing active-profile.json failed.
$script:previousRequestStatus = $null
try {
    if (Test-Path -LiteralPath $launchStatusPath) {
        $previousStatus = Get-Content -Raw -Encoding UTF8 -LiteralPath $launchStatusPath | ConvertFrom-Json
        if ($previousStatus.request_id -eq $LaunchRequestId) { $script:previousRequestStatus = $previousStatus }
    }
} catch { }
$launchWarnings = [System.Collections.Generic.List[string]]::new()
$script:resolvedAppExecutable = $null
$script:resolvedPythonExecutable = $null
$script:launchPhase = 'preflight'
# Native output (python, nested PowerShell) is decoded through [Console]::OutputEncoding.
# Pin everything to UTF-8 so Chinese diagnostics and the JSON sync result survive the
# capture instead of turning into mojibake in the error dialogs.
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
}
catch {
    # A console-less context cannot change its codepage; python still emits UTF-8 because
    # of PYTHONIOENCODING below, which is the side that matters for captured output.
}
$env:PYTHONIOENCODING = 'utf-8'
$sotaRoot = Join-Path $env:USERPROFILE '.codex-sota'
$cockpitRoot = Join-Path $env:USERPROFILE '.codex-personal'
$plusRoot = Join-Path $env:USERPROFILE '.codex-plus'
$configPath = Join-Path $sotaRoot 'config.toml'
$authPath = Join-Path $sotaRoot 'auth.json'
$registryPath = Join-Path $sotaRoot 'providers.json'
$catalogPath = Join-Path $sotaRoot 'sota-multi-vendor-model-catalog.json'
$syncLogPath = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'logs\sync.log'
$runnerPath = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'Run-CodexHistorySync.ps1'
$postExitWatcherPath = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'sync_after_codex_exit.py'
$archiveSidebarRepairPath = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'repair_archived_sidebar.py'
$routerStarterPath = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'Start-CodexSotaRouter.ps1'
$configValidatorPath = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'validate_codex_profile.py'
$activeProfilePath = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'active-profile.json'
$apiEnvironmentNames = @(
    'OPENAI_API_KEY',
    'CODEX_API_KEY',
    'OPENAI_API_BASE',
    'OPENAI_BASE_URL',
    'OPENAI_ORG_ID',
    'OPENAI_PROJECT_ID',
    'CODEX_ACCESS_TOKEN',
    'CODEX_AGENTROUTER_API_KEY',
    'CODEX_LINGZHAN_API_KEY',
    'CODEX_AISHENJI_API_KEY',
    'CODEX_CICADAS_API_KEY',
    'CODEX_MAIXUN_API_KEY',
    'CODEX_MIAOMIAOCODE_API_KEY'
)

function Show-SotaMessage {
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

function Invoke-WithComRetry {
    <#
        Retry a WMI / AppX / COM call that failed with a TRANSIENT COM error.

        "Object server is stopping when OLE service contacts it" (CO_E_SERVER_STOPPING, 0x80080008)
        and its RPC/WMI siblings are raised when the DCOM host the call reaches is mid-recycle --
        WmiPrvSE, the AppX deployment server, or the RPC endpoint mapper between two calls. They are
        transient: the identical query succeeds a moment later. Every Get-CimInstance / Get-AppxPackage
        in the launch path runs under $ErrorActionPreference='Stop', so a single such hiccup was a
        terminating error that aborted the whole launch at preflight with a bare OLE message and no
        retry -- exactly the "preflight failed: Object server is stopping" the user hit. Retry a few
        times with a short linear backoff, then surface the original error if it never clears.
    #>
    param(
        [Parameter(Mandatory)] [scriptblock] $Action,
        [int] $Attempts = 4,
        [int] $DelayMilliseconds = 400
    )
    $transientCodes = @(
        '80080008',  # CO_E_SERVER_STOPPING  "Object server is stopping when OLE service contacts it"
        '800706BA',  # RPC_S_SERVER_UNAVAILABLE
        '800706BE',  # RPC_S_CALL_FAILED
        '800706BF',  # RPC_S_CALL_FAILED_DNE
        '80010108',  # RPC_E_DISCONNECTED  "The object invoked has disconnected from its clients"
        '80010105',  # RPC_E_SERVERFAULT
        '80041033',  # WBEM_E_SHUTTING_DOWN
        '8004100A',  # WBEM_E_CRITICAL_ERROR
        '80041013',  # WBEM_E_PROVIDER_LOAD_FAILURE
        '80041001'   # WBEM_E_FAILED
    )
    for ($attempt = 1; ; $attempt++) {
        try { return & $Action }
        catch {
            $ex = $_.Exception
            $code = ''
            try { $code = '{0:X8}' -f [int]$ex.HResult } catch { $code = '' }
            $text = [string]$ex.Message
            # Message match is the reliable arm: the code surfaces on the .NET/WMI wrapper as a
            # generic HResult, but the wrapped OLE text is stable and, as seen in the launch log,
            # arrives in English even on this localized install.
            $isTransient = ($transientCodes -contains $code) -or
                ($text -match 'server is stopping|OLE service|RPC server is unavailable|disconnected from its clients|shutting down|being used by another')
            if (-not $isTransient -or $attempt -ge $Attempts) { throw }
            Start-Sleep -Milliseconds ($DelayMilliseconds * $attempt)
        }
    }
}

function Get-CodexAppExecutable {
    if ($script:resolvedAppExecutable -and (Test-Path -LiteralPath $script:resolvedAppExecutable)) {
        return $script:resolvedAppExecutable
    }
    $package = Invoke-WithComRetry -Action {
        Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction Stop |
            Sort-Object Version -Descending |
            Select-Object -First 1
    }
    if ($package) {
        foreach ($image in @('ChatGPT.exe', 'Codex.exe')) {
            $candidate = Join-Path $package.InstallLocation ('app\' + $image)
            if (Test-Path -LiteralPath $candidate) {
                $script:resolvedAppExecutable = $candidate
                return $candidate
            }
        }
    }

    $mainProcess = Get-Process -Name 'ChatGPT', 'Codex' -ErrorAction SilentlyContinue |
        Where-Object {
            $_.MainWindowHandle -ne 0 -and
            $_.Path -match '\\WindowsApps\\OpenAI\.Codex_'
        } |
        Select-Object -First 1
    if ($mainProcess -and $mainProcess.Path -and (Test-Path -LiteralPath $mainProcess.Path)) {
        $script:resolvedAppExecutable = $mainProcess.Path
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
    $imageName = [IO.Path]::GetFileName($expectedExecutable).Replace("'", "''")
    $processInfo = @(Invoke-WithComRetry -Action {
        Get-CimInstance Win32_Process -Filter "Name = '$imageName' OR Name = 'ChatGPT.exe' OR Name = 'Codex.exe'" -ErrorAction Stop
    } |
        Where-Object {
        $_.ExecutablePath -and ([string]::Equals(
            [System.IO.Path]::GetFullPath([string]$_.ExecutablePath),
            $expectedExecutable,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -or $_.ExecutablePath -match '\\WindowsApps\\OpenAI\.Codex_[^\\]+\\app\\(?:ChatGPT|Codex)\.exe$')
    })
    # An unrelated ChatGPT install must not be closed OR block this Codex launch.
    $ownedIds = @($processInfo | ForEach-Object { [int]$_.ProcessId })
    if ($ownedIds.Count -eq 0) {
        # Get-Process -Id @() fails during parameter binding, which -ErrorAction cannot suppress,
        # so the common "App is not running yet" case must return before the call.
        return @()
    }
    return @(Get-Process -Id $ownedIds -ErrorAction SilentlyContinue)
}

function Get-SotaAuthStatus {
    if (-not (Test-Path -LiteralPath $authPath)) {
        return [pscustomobject]@{
            state = 'missing'
            mode = 'missing'
            key_present = $false
            key_shape_valid = $false
        }
    }
    try {
        $auth = Get-Content -Raw -Encoding UTF8 -LiteralPath $authPath | ConvertFrom-Json
        $mode = if ($auth.auth_mode) { [string]$auth.auth_mode } else { 'unknown' }
        $key = if ($auth.OPENAI_API_KEY -is [string]) { [string]$auth.OPENAI_API_KEY } else { '' }
        $keyPresent = -not [string]::IsNullOrWhiteSpace($key)
        $commandLike = $key -match '(?i)Get-Clipboard|codex-sota|--with-api-key|[|\r\n]'
        $keyShapeValid = $keyPresent -and -not $commandLike
        $state = if ($mode -eq 'apikey' -and $keyShapeValid) {
            'ready'
        }
        elseif ($mode -ne 'apikey') {
            'wrong_auth_mode'
        }
        else {
            'invalid_api_key_value'
        }
        return [pscustomobject]@{
            state = $state
            mode = $mode
            key_present = $keyPresent
            key_shape_valid = $keyShapeValid
        }
    }
    catch {
        return [pscustomobject]@{
            state = 'invalid_auth_json'
            mode = 'invalid'
            key_present = $false
            key_shape_valid = $false
        }
    }
}

function Test-SotaConfig {
    $result = Invoke-SotaConfigValidator
    $script:configValidationReason = [string]$result.reason
    return $null -ne $result -and $result.valid -eq $true
}

function Stop-CodexApp {
    param([switch]$ForceHidden)
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

    if ($ForceHidden) {
        # A marker-matched process with no desktop window is a stale/incomplete
        # launch, not an active conversation. Remove only this hidden desktop
        # process so a new request cannot inherit its single-instance wait.
        foreach ($process in $remaining | Where-Object { $_.MainWindowHandle -eq 0 }) {
            try { Stop-Process -Id $process.Id -Force -ErrorAction Stop }
            catch { $launchWarnings.Add('Could not terminate stale hidden Codex process ' + $process.Id + ': ' + $_.Exception.Message) }
        }
        $killDeadline = [DateTime]::UtcNow.AddSeconds(5)
        do {
            $remaining = @(Get-OwnedCodexAppProcesses)
            if ($remaining.Count -eq 0) { return }
            Start-Sleep -Milliseconds 200
        } while ([DateTime]::UtcNow -lt $killDeadline)
        throw 'A stale hidden Codex process could not be cleared.'
    }

    # Restart requests authorize a normal close, not forced termination of
    # active conversations. Keep the existing app intact if it will not close.
    throw 'Codex has not finished closing. It was not force-terminated. Finish or cancel active work, close its window, then retry the restart.'
}

function Start-SotaApp {
    param([string]$ExecutablePath)

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $ExecutablePath
    $startInfo.WorkingDirectory = Split-Path -Parent $ExecutablePath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $false
    $startInfo.EnvironmentVariables['CODEX_HOME'] = $sotaRoot

    foreach ($name in $apiEnvironmentNames) {
        $startInfo.EnvironmentVariables.Remove($name)
    }

    $startedProcess = [System.Diagnostics.Process]::Start($startInfo)
    if (-not $startedProcess) {
        throw 'Windows did not return a Codex App process.'
    }

    $launcherProcessId = $startedProcess.Id
    $deadline = [DateTime]::UtcNow.AddSeconds($WindowReadyTimeoutSeconds)
    $lastOwned = @()
    do {
        Start-Sleep -Milliseconds 250
        $lastOwned = @(Get-OwnedCodexAppProcesses)
        $window = @($lastOwned | Where-Object { $_.MainWindowHandle -ne 0 } | Sort-Object StartTime | Select-Object -First 1)
        if ($window.Count -gt 0) {
            return [int]$window[0].Id
        }
    } while ([DateTime]::UtcNow -lt $deadline)

    if ($lastOwned.Count -gt 0) {
        # A live owned app without a window is pending, not failed. Do not kill
        # it or start another instance on a manager timeout/retry.
        $script:launchWindowPending = $true
        $root = @($lastOwned | Where-Object { $_.Id -eq $launcherProcessId } | Select-Object -First 1)
        if ($root.Count -eq 0) { $root = @($lastOwned | Sort-Object StartTime | Select-Object -First 1) }
        return [int]$root[0].Id
    }
    throw 'The owned Codex desktop process exited before creating its window.'
}

function Invoke-NativeCapture {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $FilePath @ArgumentList 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = @($output | ForEach-Object { $_.ToString() })
    }
}

function Invoke-SotaRouterManager {
    param([switch]$AuditOnly)

    if (-not (Test-Path -LiteralPath $routerStarterPath)) {
        throw "SOTA router starter is missing: $routerStarterPath"
    }
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $windowsPowerShell)) {
        throw 'Windows PowerShell was not found for the SOTA router manager.'
    }
    $arguments = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', $routerStarterPath
    )
    if ($AuditOnly) {
        $arguments += '-AuditOnly'
    }
    $invocation = Invoke-NativeCapture -FilePath $windowsPowerShell -ArgumentList $arguments
    $raw = $invocation.Output
    $exitCode = $invocation.ExitCode
    $result = Get-StructuredSyncResult -OutputLines $raw
    if (-not $AuditOnly -and $exitCode -eq 0 -and $result -and $result.status -eq 'deferred') { return $result }
    if ($exitCode -ne 0 -or -not $result -or $result.status -ne 'ready') {
        throw "SOTA router manager failed with exit code $exitCode.`n$($raw -join [Environment]::NewLine)"
    }
    return $result
}

function Repair-ModelCatalog {
    <#
        Rebuild the model catalog from providers.json.

        A stale catalog is not a reason to refuse to launch: it just means providers.json was
        edited more recently than the catalog was generated, and regenerating is deterministic.
        Failing hard here is what turned an ordinary edit into "catalog is missing" at launch.
    #>
    $python = Get-PythonExecutable
    if (-not $python) {
        return $false
    }
    $builder = Join-Path $PSScriptRoot 'Build-CodexSotaModelCatalog.py'
    if (-not (Test-Path -LiteralPath $builder)) {
        return $false
    }
    try {
        $null = & $python $builder 2>&1
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

function Get-PythonExecutable {
    if ($script:resolvedPythonExecutable) { return $script:resolvedPythonExecutable }
    $candidates = @(
        [Environment]::GetEnvironmentVariable('CODEX_PYTHON'),
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    )
    # Any locally installed CPython, newest first. Without this the list above falls
    # through to python.exe on PATH -- on Windows usually the Microsoft Store stub, which
    # starts, runs nothing, and exits 0, so a real interpreter going missing (no Codex
    # runtime under .cache, no 3.13/3.12, only 3.11 left) looked exactly like a broken
    # config.toml and refused every launch.
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
            if ($LASTEXITCODE -eq 0 -and $probe -contains '193731') { $script:resolvedPythonExecutable = $candidate; return $candidate }
        }
        catch { continue }
    }
    return $null
}

function Invoke-SotaConfigValidator {
    param([switch]$Repair)
    if (-not (Test-Path -LiteralPath $configValidatorPath)) {
        return [pscustomobject]@{ valid = $false; reason = 'validator_missing' }
    }
    $python = Get-PythonExecutable
    if (-not $python) {
        return [pscustomobject]@{ valid = $false; reason = 'python_missing' }
    }
    $validatorArguments = @(
        $configValidatorPath, '--profile', 'Sota', '--root', $sotaRoot, '--catalog', $catalogPath
    )
    if ($Repair) {
        $validatorArguments += '--repair'
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(
            & $python @validatorArguments 2>&1
        )
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

function Get-EffectiveModelPrefix {
    param([object]$Provider)

    $prefix = [string]$Provider.prefix
    if (-not [string]::IsNullOrWhiteSpace($prefix)) {
        return $prefix
    }

    # Codex/Responses providers may not publish a bare model.  This mirrors
    # sota_registry.derive_model_prefix for an old providers.json that has not
    # been rewritten yet; the catalog and router therefore converge on the same
    # safe slug during the next launch.
    $providerId = ([string]$Provider.id).Replace('_', '-')
    return $providerId + '--'
}

function Set-ActiveSotaProfileMarker {
    param([int]$AppProcessId, [switch]$PreserveCreationRequest)
    $app = Get-Process -Id $AppProcessId -ErrorAction Stop
    $creationTicks = $app.StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
    $creationRequestId = $LaunchRequestId
    if ($PreserveCreationRequest) {
        $creationRequestId = 'legacy-' + $creationTicks
        try {
            $previousMarker = Get-Content -Raw -Encoding UTF8 -LiteralPath $activeProfilePath -ErrorAction Stop | ConvertFrom-Json
            if ([int]$previousMarker.app_process_id -eq $AppProcessId -and
                [string]$previousMarker.app_creation_ticks -eq $creationTicks) {
                if ($previousMarker.creation_request_id) { $creationRequestId = [string]$previousMarker.creation_request_id }
                elseif ($previousMarker.request_id) { $creationRequestId = [string]$previousMarker.request_id }
            }
        } catch { }
        if ($creationRequestId -like 'legacy-*' -and $script:previousRequestStatus -and
            [int]$script:previousRequestStatus.app_process_id -eq $AppProcessId -and
            [string]$script:previousRequestStatus.app_creation_ticks -eq $creationTicks) {
            $creationRequestId = [string]$script:previousRequestStatus.request_id
        }
    }
    $payload = [ordered]@{
        profile = 'Sota'
        updated_utc = [DateTime]::UtcNow.ToString('o')
        app_process_id = $AppProcessId
        app_creation_ticks = $creationTicks
        app_executable = $app.Path
        creation_request_id = $creationRequestId
        request_id = $creationRequestId
        last_activation_request_id = $LaunchRequestId
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

function Start-PostExitSyncWatcher {
    param([int]$AppProcessId)

    if (-not (Test-Path -LiteralPath $postExitWatcherPath)) {
        throw "Post-exit sync watcher is missing: $postExitWatcherPath"
    }
    $python = Get-PythonExecutable
    if (-not $python) {
        throw 'Python runtime was not found for post-exit history sync.'
    }
    $pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) {
        $pythonw = $python
    }
    $app = Get-Process -Id $AppProcessId -ErrorAction Stop
    Start-Process -FilePath $pythonw -ArgumentList @(
        ('"' + $postExitWatcherPath + '"'),
        '--pid',
        $AppProcessId.ToString(),
        '--app-executable', ('"' + $app.Path + '"'),
        '--creation-ticks', $app.StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
    ) -WorkingDirectory (Split-Path -Parent $runnerPath) -WindowStyle Hidden | Out-Null
}

function Get-ActiveCodexAppBackends {
    # App-server may outlive Electron for a moment while flushing SQLite/WAL.
    # Match its role AND official image path; unrelated standalone CLI processes
    # are not a reason to stop or block the desktop.
    return @(Invoke-WithComRetry -Action {
        Get-CimInstance Win32_Process -Filter "Name = 'codex.exe'" -ErrorAction Stop
    } |
        Where-Object {
            $_.CommandLine -match '(?:^|\s)"?app-server"?(?:\s|$)' -and
            ($_.ExecutablePath -match '\\OpenAI\\(?:CodexCliBundled|Codex)\\' -or
             $_.ExecutablePath -match '\\WindowsApps\\OpenAI\.Codex_[^\\]+\\app\\resources\\')
        })
}

function Write-LaunchStatus {
    param([string]$Status, [string]$Phase, [int]$AppProcessId = 0,
          [switch]$Reused, [string]$ErrorText, [switch]$Emit)
    $script:launchPhase = $Phase
    $app = if ($AppProcessId -gt 0) { Get-Process -Id $AppProcessId -ErrorAction SilentlyContinue } else { $null }
    if (-not $app) { $app = Get-RequestedSotaApp }
    $ownedWindow = $app -and $app.MainWindowHandle -ne 0 -and
        ([string]::Equals($app.Path, (Get-CodexAppExecutable), [StringComparison]::OrdinalIgnoreCase) -or
         $app.Path -match '\\WindowsApps\\OpenAI\.Codex_[^\\]+\\app\\(?:ChatGPT|Codex)\.exe$')
    $payload = [ordered]@{
        status = $Status; profile = 'Sota'; phase = $Phase; request_id = $LaunchRequestId
        launcher_pid = $PID
        launcher_creation_ticks = (Get-ProcessCreationTicks -ProcessId $PID)
        elapsed_seconds = [Math]::Round($launchClock.Elapsed.TotalSeconds, 3)
        app_process_id = if ($app) { $app.Id } else { $null }
        app_executable = if ($app) { $app.Path } else { $null }
        app_started_utc = if ($app) { $app.StartTime.ToUniversalTime().ToString('o') } else { $null }
        app_creation_ticks = if ($app) { $app.StartTime.ToUniversalTime().ToFileTimeUtc().ToString() } else { $null }
        app_creation_time = if ($app) { ($app.StartTime.ToUniversalTime().ToFileTimeUtc() - 116444736000000000) / 10000000.0 } else { $null }
        window_owned = [bool]$ownedWindow; reused_existing = [bool]$Reused
        history_sync = if ($SyncBeforeLaunch) { 'explicit' } else { 'deferred_to_quiet_exit' }
        warnings = @($launchWarnings.ToArray()); error = $ErrorText
        updated_utc = [DateTime]::UtcNow.ToString('o')
    }
    $json = $payload | ConvertTo-Json -Depth 5 -Compress
    if ($app) { $script:previousRequestStatus = [pscustomobject]$payload }
    try {
        [IO.Directory]::CreateDirectory($lifecycleWorkDir) | Out-Null
        $temporary = $launchStatusPath + '.new-' + $PID
        [IO.File]::WriteAllText($temporary, $json, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $launchStatusPath -Force
    }
    catch { # Reporting must not undo a successful desktop launch.
        if ($Emit) { Write-Warning 'Could not persist launch status; structured stdout remains available.' }
    }
    if ($Emit) { Write-Output $json }
}

function Set-LaunchRequest {
    [IO.Directory]::CreateDirectory($lifecycleWorkDir) | Out-Null
    $payload = @{ request_id = $LaunchRequestId; launcher_pid = $PID
        launcher_creation_ticks = (Get-ProcessCreationTicks -ProcessId $PID)
        expires_at = ([DateTime]::UtcNow.AddMinutes(3) - [DateTime]'1970-01-01Z').TotalSeconds } | ConvertTo-Json -Compress
    $temporary = $launchRequestPath + '.new-' + $PID
    [IO.File]::WriteAllText($temporary, $payload, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $launchRequestPath -Force
}

function Clear-LaunchRequest {
    try {
        $request = Get-Content -Raw -Encoding UTF8 -LiteralPath $launchRequestPath -ErrorAction Stop | ConvertFrom-Json
        if ($request.request_id -eq $LaunchRequestId) { Remove-Item -LiteralPath $launchRequestPath -Force -ErrorAction SilentlyContinue }
    }
    catch { }
}

function Get-ProcessCreationTicks {
    param([int]$ProcessId)
    try {
        return (Get-Process -Id $ProcessId -ErrorAction Stop).StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
    }
    catch {
        return $null
    }
}

function Test-ProcessIdentity {
    param([object]$ProcessId, [string]$CreationTicks)
    try {
        if (-not $ProcessId -or [int]$ProcessId -le 0) { return $false }
        $actual = Get-ProcessCreationTicks -ProcessId ([int]$ProcessId)
        if (-not $actual) { return $false }
        if ($CreationTicks) { return [string]$actual -eq [string]$CreationTicks }
        return $true
    }
    catch {
        return $false
    }
}

function Remove-StaleLaunchArtifacts {
    <#
    Keep only launch records that belong to the current request or a process whose
    birth identity still exists.  The files are diagnostic, not a queue: leaving
    old ready/deferred records around only grows work\ and invites stale reuse.
    #>
    $now = [DateTime]::UtcNow
    $currentStatus = [IO.Path]::GetFullPath($launchStatusPath)
    foreach ($file in @(Get-ChildItem -LiteralPath $lifecycleWorkDir -Filter 'launch-status-*.json' -File -ErrorAction SilentlyContinue)) {
        if ([IO.Path]::GetFullPath($file.FullName) -eq $currentStatus) { continue }
        $ageMinutes = ($now - $file.LastWriteTimeUtc).TotalMinutes
        $payload = $null
        try { $payload = Get-Content -Raw -Encoding UTF8 -LiteralPath $file.FullName | ConvertFrom-Json } catch { }
        $remove = $false
        if ($payload -and $payload.status -eq 'ready' -and $payload.app_process_id -and $payload.app_creation_ticks) {
            # A ready marker is useful only while that exact app identity exists.
            $remove = -not (Test-ProcessIdentity -ProcessId $payload.app_process_id -CreationTicks ([string]$payload.app_creation_ticks))
            if ($remove -and $ageMinutes -lt 1) { $remove = $false }
        }
        elseif ($payload -and $payload.launcher_pid) {
            # A deferred/starting record with a dead launcher cannot become live again.
            $remove = (-not (Test-ProcessIdentity -ProcessId $payload.launcher_pid -CreationTicks ([string]$payload.launcher_creation_ticks))) -and $ageMinutes -ge 10
        }
        else {
            $remove = $ageMinutes -ge 10
        }
        if ($remove) { Remove-Item -LiteralPath $file.FullName -Force -ErrorAction SilentlyContinue }
    }

    # Atomic writers use .new-* and can leave one behind after a killed launcher.
    foreach ($file in @(Get-ChildItem -LiteralPath $lifecycleWorkDir -File -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -like 'launch-status-*.json.new-*' -or $_.Name -like 'codex-launch-request.json.new-*'
    })) {
        if (($now - $file.LastWriteTimeUtc).TotalMinutes -ge 5) {
            Remove-Item -LiteralPath $file.FullName -Force -ErrorAction SilentlyContinue
        }
    }

    if (Test-Path -LiteralPath $launchRequestPath) {
        $request = $null
        try { $request = Get-Content -Raw -Encoding UTF8 -LiteralPath $launchRequestPath | ConvertFrom-Json } catch { }
        $staleRequest = $false
        if (-not $request) { $staleRequest = $true }
        elseif ($request.request_id -eq $LaunchRequestId) { $staleRequest = $false }
        elseif ($request.expires_at -and [double]$request.expires_at -le (([DateTime]::UtcNow - [DateTime]'1970-01-01Z').TotalSeconds)) { $staleRequest = $true }
        elseif ($request.launcher_pid -and -not (Test-ProcessIdentity -ProcessId $request.launcher_pid -CreationTicks ([string]$request.launcher_creation_ticks))) { $staleRequest = $true }
        elseif (-not $request.launcher_pid -and ((Get-Item -LiteralPath $launchRequestPath).LastWriteTimeUtc -lt $now.AddMinutes(-10))) { $staleRequest = $true }
        if ($staleRequest) { Remove-Item -LiteralPath $launchRequestPath -Force -ErrorAction SilentlyContinue }
    }
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

function Get-ActiveSotaApp {
    try {
        $marker = Get-Content -Raw -Encoding UTF8 -LiteralPath $activeProfilePath -ErrorAction Stop | ConvertFrom-Json
        if ($marker.profile -ne 'Sota') { return $null }
        foreach ($app in @(Get-OwnedCodexAppProcesses | Sort-Object StartTime)) {
            if ($marker.app_process_id) {
                if ($app.Id -eq [int]$marker.app_process_id -and
                    $app.StartTime.ToUniversalTime().ToFileTimeUtc().ToString() -eq [string]$marker.app_creation_ticks) { return $app }
            }
            elseif ($app.MainWindowHandle -ne 0 -and $marker.updated_utc) {
                # Migrate the old marker only when it belongs to this launch time,
                # not a stale marker from a different profile or recycled PID.
                $age = [Math]::Abs((([datetime]$marker.updated_utc).ToUniversalTime() - $app.StartTime.ToUniversalTime()).TotalSeconds)
                if ($age -le 45) { return $app }
            }
        }
    }
    catch { }
    return $null
}

function Get-RequestedSotaApp {
    $previous = $script:previousRequestStatus
    if (-not $previous -or $previous.request_id -ne $LaunchRequestId -or
        -not $previous.app_process_id -or -not $previous.app_creation_ticks) { return $null }
    foreach ($app in @(Get-OwnedCodexAppProcesses)) {
        if ($app.Id -eq [int]$previous.app_process_id -and
            $app.StartTime.ToUniversalTime().ToFileTimeUtc().ToString() -eq [string]$previous.app_creation_ticks) {
            return $app
        }
    }
    return $null
}

function Show-OwnedCodexWindow {
    param([object]$App)
    if (-not $App -or $App.MainWindowHandle -eq 0) { return }
    try {
        if (-not ('CodexSotaWindowApi' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CodexSotaWindowApi {
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr h, int n);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
}
'@
        }
        [CodexSotaWindowApi]::ShowWindowAsync($App.MainWindowHandle, 9) | Out-Null
        [CodexSotaWindowApi]::SetForegroundWindow($App.MainWindowHandle) | Out-Null
    }
    catch { $launchWarnings.Add('Codex is running; Windows did not permit foreground focus.') }
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

    if (-not $Result -or $Result.status -ne 'ok') {
        return $false
    }
    if ($AuditOnly) {
        if ($Result.mode -ne 'three-way-audit' -or -not $Result.roots) {
            return $false
        }
        foreach ($root in @($cockpitRoot, $plusRoot, $sotaRoot)) {
            $rootAudit = $Result.roots.($root)
            if (-not $rootAudit -or -not $rootAudit.exists -or -not $rootAudit.state_db_exists -or -not $rootAudit.sessions_exists) {
                return $false
            }
        }
        return $true
    }

    if (
        $Result.mode -ne 'three-way' -or
        $Result.cockpit_root -ne $cockpitRoot -or
        $Result.plus_root -ne $plusRoot -or
        $Result.sota_root -ne $sotaRoot -or
        -not $Result.verification -or
        -not $Result.verification.three_way_same_thread_ids
    ) {
        return $false
    }
    foreach ($root in @($cockpitRoot, $plusRoot, $sotaRoot)) {
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

function Invoke-ThreeWayHistorySync {
    param([switch]$AuditOnly)

    if (-not (Test-Path -LiteralPath $runnerPath)) {
        throw "Three-way history sync runner is missing: $runnerPath"
    }
    $powershell = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $powershell)) {
        $powershell = 'powershell.exe'
    }

    # This function is now only used by an explicit sync request or read-only
    # audit. Never stack four 900-second waits on the ordinary launch path.
    $runnerArguments = @('-NoLogo', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass', '-File', $runnerPath, '-NoGui',
        '-RestartProfile', 'None', '-LockWaitSeconds', '0')
    if ($AuditOnly) { $runnerArguments += '-AuditOnly' }
    $invocation = Invoke-NativeCapture -FilePath $powershell -ArgumentList $runnerArguments
    $syncResult = Get-StructuredSyncResult -OutputLines $invocation.Output
    if (Test-StructuredSyncSuccess -Result $syncResult -AuditOnly:$AuditOnly) { return $syncResult }
    if ($syncResult -and $syncResult.status -eq 'deferred') { return $syncResult }
    $detail = if ($syncResult -and $syncResult.error) { [string]$syncResult.error } else { $invocation.Output -join [Environment]::NewLine }
    throw "Three-way history sync failed with exit code $($invocation.ExitCode).`n$detail"
}

function Invoke-ArchivedSidebarRepair {
    param([int]$TimeoutMilliseconds = 20000)

    if (-not (Test-Path -LiteralPath $archiveSidebarRepairPath)) { return $null }
    $python = Get-PythonExecutable
    if (-not $python) { return $null }
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $python
    $startInfo.Arguments = '"' + $archiveSidebarRepairPath + '"'
    $startInfo.WorkingDirectory = Split-Path -Parent $archiveSidebarRepairPath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { return $null }
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            try { $process.Kill() } catch { }
            return [pscustomobject]@{ completed = $false; changed = $false }
        }
        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        return [pscustomobject]@{
            completed = $process.ExitCode -eq 0
            changed = [bool]($stdout -match '(?im)(removed|detached)\s+')
            error = if ($process.ExitCode -eq 0) { $null } else { $stderr.Trim() }
        }
    }
    catch {
        return [pscustomobject]@{ completed = $false; changed = $false; error = $_.Exception.Message }
    }
    finally {
        $process.Dispose()
    }
}

try {
$appExecutable = Get-CodexAppExecutable
$authStatus = Get-SotaAuthStatus
$authMode = $authStatus.mode
$configValid = Test-SotaConfig
# A provider or model mapping that changed since the Codex App last pinned a model leaves
# config.toml pointing at a slug the catalog no longer offers.  That is a routine edit
# outcome, not a broken install, so repair it once (same provider namespace preferred)
# and re-check instead of refusing the launch.
if (-not $configValid -and -not $AuditOnly) {
    $validatorResult = Invoke-SotaConfigValidator -Repair
    $script:configValidationReason = [string]$validatorResult.reason
    if ($null -ne $validatorResult -and $validatorResult.valid -eq $true) {
        $configValid = $true
    }
}
$registryExists = Test-Path -LiteralPath $registryPath
$registryValid = $false
$registryProviders = @()
$expectedCatalogModels = @()
$defaultModel = $null
try {
    $configText = Get-Content -Raw -Encoding UTF8 -LiteralPath $configPath
    if ($configText -match '(?m)^model\s*=\s*"([^"]+)"\s*$') {
        $defaultModel = [string]$Matches[1]
    }
}
catch {
    $defaultModel = $null
}
if ($registryExists) {
    try {
        $registry = Get-Content -Raw -Encoding UTF8 -LiteralPath $registryPath | ConvertFrom-Json
        $registryProviders = @($registry.providers)
        $enabledProviders = @($registryProviders | Where-Object { $_.enabled -eq $true })
        $defaultProviders = @($enabledProviders | Where-Object { $_.is_default -eq $true })
        $registryValid = (
            [int]$registry.version -eq 1 -and
            $enabledProviders.Count -gt 0 -and
            $defaultProviders.Count -eq 1
        )
        if ($registryValid) {
            foreach ($provider in $enabledProviders) {
                if (
                    [string]::IsNullOrWhiteSpace([string]$provider.id) -or
                    [string]::IsNullOrWhiteSpace([string]$provider.name) -or
                    [string]::IsNullOrWhiteSpace([string]$provider.base_url)
                ) {
                    $registryValid = $false
                    break
                }
                foreach ($modelEntry in @($provider.models | Where-Object { $_.enabled -eq $true })) {
                    # publish_as (the model-mapping alias) overrides prefix+id, exactly
                    # like sota_registry.published_slug: the catalog is generated from
                    # the published slug, so a mapped model must be expected under its
                    # alias or every mapped provider fails the launch preflight with a
                    # misleading "catalog is missing".
                    $publishedAlias = [string]$modelEntry.publish_as
                    if (-not [string]::IsNullOrWhiteSpace($publishedAlias)) {
                        $expectedCatalogModels += $publishedAlias
                    }
                    else {
                        $expectedCatalogModels += ((Get-EffectiveModelPrefix -Provider $provider) + [string]$modelEntry.id)
                    }
                }
            }
        }
        if ($expectedCatalogModels.Count -eq 0) {
            $registryValid = $false
        }
    }
    catch {
        $registryValid = $false
        $registryProviders = @()
        $expectedCatalogModels = @()
    }
}
$catalogExists = Test-Path -LiteralPath $catalogPath
$catalogModels = @()
$catalogValid = $false
$catalogRegistryHash = $null
$catalogRepaired = $false
if ($catalogExists -and $registryValid) {
    try {
        $catalog = Get-Content -Raw -Encoding UTF8 -LiteralPath $catalogPath | ConvertFrom-Json
        $catalogModels = @($catalog.models | ForEach-Object { [string]$_.slug })
        $catalogRegistryHash = [string]$catalog.registry_hash
        $catalogValid = $catalogModels.Count -eq $expectedCatalogModels.Count
        if ($catalogValid) {
            for ($index = 0; $index -lt $catalogModels.Count; $index++) {
                if ($catalogModels[$index] -ne $expectedCatalogModels[$index]) {
                    $catalogValid = $false
                    break
                }
            }
        }
    }
    catch {
        $catalogValid = $false
    }
}
$routerPreflight = $null
if ($catalogValid) {
    try {
        $routerPreflight = Invoke-SotaRouterManager -AuditOnly
        if (
            [string]::IsNullOrWhiteSpace($catalogRegistryHash) -or
            $catalogRegistryHash -ne [string]$routerPreflight.registry_hash
        ) {
            $catalogValid = $false
        }
    }
    catch {
        $catalogValid = $false
    }
}

# A stale or missing catalog is regenerable from providers.json, so rebuild once and
# re-check instead of refusing to launch. Only a rebuild that still does not match is fatal.
if ($registryValid -and -not $catalogValid -and -not $catalogRepaired -and -not $AuditOnly) {
    $catalogRepaired = Repair-ModelCatalog
    if ($catalogRepaired) {
        $catalogExists = Test-Path -LiteralPath $catalogPath
        if ($catalogExists) {
            try {
                $catalog = Get-Content -Raw -Encoding UTF8 -LiteralPath $catalogPath | ConvertFrom-Json
                $catalogModels = @($catalog.models | ForEach-Object { [string]$_.slug })
                $catalogRegistryHash = [string]$catalog.registry_hash
                $catalogValid = $catalogModels.Count -eq $expectedCatalogModels.Count
                if ($catalogValid) {
                    for ($index = 0; $index -lt $catalogModels.Count; $index++) {
                        if ($catalogModels[$index] -ne $expectedCatalogModels[$index]) {
                            $catalogValid = $false
                            break
                        }
                    }
                }
                if ($catalogValid) {
                    $routerPreflight = Invoke-SotaRouterManager -AuditOnly
                    if (
                        [string]::IsNullOrWhiteSpace($catalogRegistryHash) -or
                        $catalogRegistryHash -ne [string]$routerPreflight.registry_hash
                    ) {
                        $catalogValid = $false
                    }
                }
            }
            catch {
                $catalogValid = $false
            }
        }
    }
}
$runnerExists = Test-Path -LiteralPath $runnerPath
$upstreamAudit = @()
foreach ($provider in @($registryProviders | Where-Object { $_.enabled -eq $true })) {
    $upstreamAudit += [ordered]@{
        id = [string]$provider.id
        name = [string]$provider.name
        base_url = [string]$provider.base_url
        prefix = Get-EffectiveModelPrefix -Provider $provider
        models = @($provider.models | Where-Object { $_.enabled -eq $true } | ForEach-Object { [string]$_.id })
    }
}

}
catch {
    Write-LaunchStatus -Status 'error' -Phase 'preflight' -ErrorText $_.Exception.Message -Emit
    if (-not $NoGui) { Show-SotaMessage -Title 'Multi-vendor SOTA Codex preflight failed' -Icon Error -Message $_.Exception.Message }
    exit 1
}

if ($AuditOnly) {
    $syncRunnerAuditReady = $false
    $syncRunnerAuditError = $null
    try {
        Invoke-ThreeWayHistorySync -AuditOnly | Out-Null
        $syncRunnerAuditReady = $true
    }
    catch {
        $syncRunnerAuditError = $_.Exception.Message
    }
    $routerAuditReady = $false
    $routerAudit = $routerPreflight
    $routerAuditError = $null
    if ($routerAudit) {
        $routerAuditReady = $true
    }
    else {
        try {
            $routerAudit = Invoke-SotaRouterManager -AuditOnly
            $routerAuditReady = $true
        }
        catch {
            $routerAuditError = $_.Exception.Message
        }
    }
    $audit = [ordered]@{
        status = if ($configValid -and $registryValid -and $catalogValid -and $runnerExists -and $appExecutable -and $authStatus.state -eq 'ready' -and $syncRunnerAuditReady -and $routerAuditReady) { 'ready' } else { 'needs_api_key_or_repair' }
        profile = 'SotaMultiVendor'
        target_root = $sotaRoot
        config_valid = $configValid
        registry_exists = $registryExists
        registry_valid = $registryValid
        registry_provider_count = @($registryProviders | Where-Object { $_.enabled -eq $true }).Count
        catalog_exists = $catalogExists
        catalog_valid = $catalogValid
        catalog_model_count = $catalogModels.Count
        catalog_registry_hash = $catalogRegistryHash
        auth_mode = $authMode
        auth_state = $authStatus.state
        api_key_present = $authStatus.key_present
        api_key_shape_valid = $authStatus.key_shape_valid
        app_exists = -not [string]::IsNullOrEmpty($appExecutable)
        model_provider = 'true_sota'
        default_model = $defaultModel
        selectable_models = $catalogModels
        upstreams = $upstreamAudit
        router_audit_ready = $routerAuditReady
        router_running = if ($routerAudit) { $routerAudit.running } else { $false }
        router_audit_error = $routerAuditError
        launch_policy = 'clear-all-inherited-api-environment'
        independent_from = @('Cockpit', 'Plus')
        three_way_sync_runner_exists = $runnerExists
        post_exit_watcher_exists = Test-Path -LiteralPath $postExitWatcherPath
        sync_runner_audit_ready = $syncRunnerAuditReady
        sync_runner_audit_error = $syncRunnerAuditError
    }
    Write-Output ($audit | ConvertTo-Json -Depth 5 -Compress)
    if ($audit.status -eq 'ready') {
        exit 0
    }
    exit 1
}

if ($PrepareOnly) {
    try {
        if (-not $configValid) {
            throw "SOTA preflight failed ($script:configValidationReason): $configPath"
        }
        if (-not $registryValid) {
            throw "SOTA provider registry is missing or invalid: $registryPath"
        }
        if (-not $catalogValid) {
            throw "Multi-vendor SOTA model catalog is missing or out of date: $catalogPath"
        }
        if ($authStatus.state -ne 'ready') {
            throw "True SOTA API key is not configured correctly (state: $($authStatus.state))."
        }
        $routerResult = Invoke-SotaRouterManager
        if ($routerResult.status -eq 'deferred') {
            Write-LaunchStatus -Status 'deferred' -Phase 'preparing_router' -Emit
            exit 0
        }
        if ($NoGui) {
            [ordered]@{
                status = 'ready'
                profile = 'Sota'
                router_started = [bool]$routerResult.started
            } | ConvertTo-Json -Compress
        }
        exit 0
    }
    catch {
        if ($NoGui) {
            [ordered]@{ status = 'error'; profile = 'Sota'; error = $_.Exception.Message } |
                ConvertTo-Json -Compress
        }
        else {
            Show-SotaMessage -Title 'Multi-vendor SOTA preparation failed' -Icon Error -Message $_.Exception.Message
        }
        exit 1
    }
}

$lifecycleLease = $null
$historyWriteLease = $null
try {
    Remove-StaleLaunchArtifacts
    if (-not $configValid) {
        throw "SOTA preflight failed ($script:configValidationReason): $configPath"
    }
    if (-not $registryValid) {
        throw "SOTA provider registry is missing or invalid: $registryPath"
    }
    if (-not $catalogValid) {
        throw "Multi-vendor SOTA model catalog is missing or out of date: $catalogPath"
    }
    if ($authStatus.state -ne 'ready') {
        throw "True SOTA API key is not configured correctly (state: $($authStatus.state)). Run codex-sota again and paste only the API key when prompted."
    }
    if (-not $appExecutable) {
        throw 'ChatGPT/Codex App was not found.'
    }

    Write-LaunchStatus -Status 'starting' -Phase 'preparing_router'
    # Validate/start the local router before closing a functioning desktop.
    $routerResult = Invoke-SotaRouterManager
    if ($routerResult.status -eq 'deferred') {
        Write-LaunchStatus -Status 'deferred' -Phase 'preparing_router' -Emit
        exit 0
    }
    Set-LaunchRequest
    Write-LaunchStatus -Status 'starting' -Phase 'waiting_for_history_sync'
    $lifecycleLease = Enter-AppLifecycleLock
    if (-not $lifecycleLease) {
        Write-LaunchStatus -Status 'deferred' -Phase 'waiting_for_history_sync' -Emit
        exit 0
    }
    # An already-running pre-upgrade sync may not know the lifecycle lock yet.
    # Protect the existing byte lock too; never open Codex over an active writer.
    $historyWriteLease = Enter-AppLifecycleLock -LockPath (Join-Path $PSScriptRoot 'sync.lock') -WaitSeconds 0
    if (-not $historyWriteLease) {
        Write-LaunchStatus -Status 'deferred' -Phase 'waiting_for_history_sync' -Emit
        exit 0
    }
    $existingApp = Get-ActiveSotaApp
    $sameRequest = $false
    try {
        $activeMarker = Get-Content -Raw -Encoding UTF8 -LiteralPath $activeProfilePath | ConvertFrom-Json
        $sameRequest = $activeMarker.creation_request_id -eq $LaunchRequestId -or $activeMarker.request_id -eq $LaunchRequestId
    } catch { }
    $requestedApp = Get-RequestedSotaApp
    if ($requestedApp) { $existingApp = $requestedApp; $sameRequest = $true }
    # Reuse a visible app, or a hidden app only when this exact request created
    # it and is being polled. A stale marker must never turn into an unbounded
    # waiting_for_window/deferred loop on the next launch.
    $existingWindow = $existingApp -and $existingApp.MainWindowHandle -ne 0
    $reusableExistingApp = $existingApp -and ($existingWindow -or $sameRequest)
    if ($reusableExistingApp -and (-not $Restart -or $sameRequest)) {
        # A pending launch polled with the same request id is idempotent, even
        # if the caller originally requested Restart.
        try { Set-ActiveSotaProfileMarker -AppProcessId $existingApp.Id -PreserveCreationRequest }
        catch { $launchWarnings.Add('Could not update the profile marker: ' + $_.Exception.Message) }
        Show-OwnedCodexWindow -App $existingApp
        try { Start-PostExitSyncWatcher -AppProcessId $existingApp.Id }
        catch { $launchWarnings.Add('Post-exit history sync could not be armed: ' + $_.Exception.Message) }
        $status = if ($existingApp.MainWindowHandle -ne 0) { 'ready' } else { 'deferred' }
        $phase = if ($status -eq 'ready') { 'window_ready' } else { 'waiting_for_window' }
        Clear-LaunchRequest
        Write-LaunchStatus -Status $status -Phase $phase -AppProcessId $existingApp.Id -Reused -Emit
        exit 0
    }
    if ($existingApp -and $Restart) { $launchWarnings.Add('Explicit restart requested; the existing SOTA desktop must close normally.') }
    if ($activeMarker -and $activeMarker.profile -ne 'Sota' -and -not $SyncBeforeLaunch) {
        $launchWarnings.Add('Opening existing local SOTA history now; changes from other profiles will be merged after a quiet exit or an explicit history sync.')
    }
    $staleHiddenApp = $existingApp -and -not $existingWindow -and -not $sameRequest
    if ($staleHiddenApp) {
        Write-LaunchStatus -Status 'starting' -Phase 'closing_previous_app'
        Stop-CodexApp -ForceHidden
    }
    elseif (-not $Restart -and @(Get-OwnedCodexAppProcesses).Count -gt 0) {
        throw 'An existing Codex window could not be verified as the SOTA profile. It was not closed. Close it yourself or explicitly choose Restart before switching profiles.'
    }
    else {
        Write-LaunchStatus -Status 'starting' -Phase 'closing_previous_app'
        Stop-CodexApp
    }
    if (@(Get-ActiveCodexAppBackends).Count -gt 0) {
        $launchWarnings.Add('A previous Codex app-server is still closing its history files; launch will resume after it exits.')
        Write-LaunchStatus -Status 'deferred' -Phase 'waiting_for_previous_app' -Emit
        exit 0
    }
    # Repair only while the App is fully closed.  This is a bounded, lightweight
    # projection cleanup; if Python is slow or unavailable, the watcher retries
    # after exit and the desktop still opens immediately.
    $archiveRepair = Invoke-ArchivedSidebarRepair
    if ($archiveRepair -and $archiveRepair.changed) {
        $launchWarnings.Add('Archived sidebar cache repaired before launch.')
    }
    if ($SyncBeforeLaunch -and -not $SkipSync) {
        # The manual sync owns its own lifecycle lease; never recursively lock it.
        $historyWriteLease.Unlock(0, 1); $historyWriteLease.Dispose(); $historyWriteLease = $null
        $lifecycleLease.Unlock(0, 1); $lifecycleLease.Dispose(); $lifecycleLease = $null
        Clear-LaunchRequest
        Write-LaunchStatus -Status 'starting' -Phase 'explicit_history_sync'
        $syncResult = Invoke-ThreeWayHistorySync
        Set-LaunchRequest
        $lifecycleLease = Enter-AppLifecycleLock
        if ($lifecycleLease) { $historyWriteLease = Enter-AppLifecycleLock -LockPath (Join-Path $PSScriptRoot 'sync.lock') -WaitSeconds 0 }
        if (-not $lifecycleLease -or -not $historyWriteLease -or ($syncResult -and $syncResult.status -eq 'deferred')) {
            Write-LaunchStatus -Status 'deferred' -Phase 'waiting_for_history_sync' -Emit
            exit 0
        }
    }
    # Existing local SOTA history opens immediately. Cross-profile changes are
    # merged after a quiet exit or via the explicit synchronization entrypoint.
    Write-LaunchStatus -Status 'starting' -Phase 'opening_desktop'
    $appProcessId = Start-SotaApp -ExecutablePath $appExecutable
    try { Set-ActiveSotaProfileMarker -AppProcessId $appProcessId }
    catch { $launchWarnings.Add('Could not update the profile marker: ' + $_.Exception.Message) }
    try {
        Start-PostExitSyncWatcher -AppProcessId $appProcessId
    }
    catch {
        # Optional background maintenance must never roll back a working app.
        $launchWarnings.Add('Post-exit history sync could not be armed: ' + $_.Exception.Message)
    }
    Clear-LaunchRequest
    if ($script:launchWindowPending) {
        Write-LaunchStatus -Status 'deferred' -Phase 'waiting_for_window' -AppProcessId $appProcessId -Emit
    }
    else { Write-LaunchStatus -Status 'ready' -Phase 'window_ready' -AppProcessId $appProcessId -Emit }
}
catch {
    Clear-LaunchRequest
    Write-LaunchStatus -Status 'error' -Phase $script:launchPhase -ErrorText $_.Exception.Message -Emit
    if (-not $NoGui) { Show-SotaMessage -Title 'Multi-vendor SOTA Codex launch failed' -Icon Error -Message $_.Exception.Message }
    exit 1
}
finally {
    if ($historyWriteLease) { $historyWriteLease.Unlock(0, 1); $historyWriteLease.Dispose() }
    if ($lifecycleLease) { $lifecycleLease.Unlock(0, 1); $lifecycleLease.Dispose() }
}

exit 0
