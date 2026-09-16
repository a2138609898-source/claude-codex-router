param(
    [switch]$AuditOnly,
    [switch]$NoGui,
    [switch]$SkipSync,
    [switch]$PrepareOnly
)

$ErrorActionPreference = 'Stop'
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
    'CODEX_PROVIDER_A_API_KEY',
    'CODEX_PROVIDER_B_API_KEY',
    'CODEX_PROVIDER_C_API_KEY',
    'CODEX_PROVIDER_D_API_KEY',
    'CODEX_PROVIDER_E_API_KEY',
    'CODEX_PROVIDER_F_API_KEY'
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
    return $null -ne $result -and $result.valid -eq $true
}

function Stop-ProcessTreeById {
    param([int]$ProcessId)

    $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
    if (-not (Test-Path -LiteralPath $taskkill)) {
        throw "taskkill.exe was not found: $taskkill"
    }

    $taskkillProcessNotFound = 128
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $taskkill /PID $ProcessId /T /F 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }

    if ($exitCode -eq 0 -or $exitCode -eq $taskkillProcessNotFound) {
        return $null
    }

    $detail = @($output | ForEach-Object { $_.ToString().Trim() } | Where-Object { $_ }) -join ' '
    if (-not $detail) {
        $detail = "taskkill exited with code $exitCode."
    }
    return "PID ${ProcessId}: $detail"
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

    $killErrors = [System.Collections.Generic.List[string]]::new()
    $mainProcesses = @($remaining | Where-Object { $_.MainWindowHandle -ne 0 })
    if ($mainProcesses.Count -eq 0) {
        $mainProcesses = @($remaining | Select-Object -First 1)
    }
    foreach ($process in $mainProcesses) {
        $killError = Stop-ProcessTreeById -ProcessId $process.Id
        if ($killError) {
            $killErrors.Add($killError)
        }
    }

    $forceDeadline = [DateTime]::UtcNow.AddSeconds(8)
    do {
        $remaining = @(Get-OwnedCodexAppProcesses)
        if ($remaining.Count -eq 0) {
            return
        }
        foreach ($process in $remaining) {
            $killError = Stop-ProcessTreeById -ProcessId $process.Id
            if ($killError) {
                $killErrors.Add($killError)
            }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $forceDeadline)

    $shutdownMessage = 'ChatGPT.exe processes remained after Codex App shutdown.'
    if ($killErrors.Count -gt 0) {
        $details = @($killErrors | Select-Object -Unique)
        $shutdownMessage = (@($shutdownMessage) + $details) -join [Environment]::NewLine
    }
    throw $shutdownMessage
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
    $deadline = [DateTime]::UtcNow.AddSeconds(12)
    $launcherStableSince = $null
    do {
        Start-Sleep -Milliseconds 250
        $childProcess = Get-CimInstance Win32_Process -Filter "Name = 'ChatGPT.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ParentProcessId -eq $launcherProcessId -or $_.ParentProcessId -eq $PID } |
            Select-Object -First 1
        if ($childProcess) {
            return [int]$childProcess.ProcessId
        }
        $running = Get-Process -Id $launcherProcessId -ErrorAction SilentlyContinue
        if ($running) {
            if ($null -eq $launcherStableSince) {
                $launcherStableSince = [DateTime]::UtcNow
            }
            elseif (([DateTime]::UtcNow - $launcherStableSince).TotalSeconds -ge 2) {
                return $launcherProcessId
            }
        }
        else {
            $launcherStableSince = $null
        }
    } while ([DateTime]::UtcNow -lt $deadline)

    throw 'ChatGPT.exe did not remain running with the isolated multi-vendor SOTA environment.'
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
    $candidates = @(
        [Environment]::GetEnvironmentVariable('CODEX_PYTHON'),
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    )
    $python = $candidates |
        Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
        Select-Object -First 1
    if ($python) {
        return $python
    }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    return $null
}

function Invoke-SotaConfigValidator {
    if (-not (Test-Path -LiteralPath $configValidatorPath)) {
        return [pscustomobject]@{ valid = $false; reason = 'validator_missing' }
    }
    $python = Get-PythonExecutable
    if (-not $python) {
        return [pscustomobject]@{ valid = $false; reason = 'python_missing' }
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(
            & $python $configValidatorPath --profile Sota --root $sotaRoot --catalog $catalogPath 2>&1
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
    $payload = [ordered]@{
        profile = 'Sota'
        updated_utc = [DateTime]::UtcNow.ToString('o')
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
        throw 'pythonw.exe was not found for post-exit history sync.'
    }
    Start-Process -FilePath $pythonw -ArgumentList @(
        $postExitWatcherPath,
        '--pid',
        $AppProcessId.ToString()
    ) -WorkingDirectory (Split-Path -Parent $runnerPath) -WindowStyle Hidden | Out-Null
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

function Test-RecentSuccessfulSync {
    param([int]$WithinMinutes = 10)

    # The post-exit watcher and a previous launch both leave a fully successful three-way
    # sync in the log.  When one just finished, the histories are already merged: running
    # the same 3-5 minute sync again before every launch only adds dead wait time.
    if (-not (Test-Path -LiteralPath $syncLogPath)) {
        return $false
    }
    try {
        $tail = @(Get-Content -Tail 10 -LiteralPath $syncLogPath -ErrorAction Stop)
    }
    catch {
        return $false
    }
    for ($index = $tail.Count - 1; $index -ge 0; $index--) {
        $line = [string]$tail[$index]
        if ($line -notmatch 'INFO Three-way sync completed' -or $line -notmatch "'status': 'ok'") {
            continue
        }
        $stamp = $null
        if ($line -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
            $stamp = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
        }
        if (-not $stamp) {
            return $true
        }
        return ((Get-Date) - $stamp).TotalMinutes -le $WithinMinutes
    }
    return $false
}

function Wait-HistorySyncIdle {
    param([int]$TimeoutSeconds = 900)

    # The msvcrt byte-range lock is process-lifetime, so "already running" clears when the
    # holding python process exits.  Wait for that process instead of spinning the whole
    # runner every 500 ms: a real three-way sync takes minutes, and the old 15-second retry
    # budget burned out and failed the launch while the first sync was still doing its job.
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $holders = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
            Where-Object { ([string]$_.CommandLine) -match 'sync_codex_histories' })
        if ($holders.Count -eq 0) {
            return $true
        }
        Start-Sleep -Seconds 2
    }
    return $false
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

    if (-not $AuditOnly -and (Test-RecentSuccessfulSync -WithinMinutes 10)) {
        return
    }

    $lastExitCode = 1
    $lastDetail = ''
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        $runnerArguments = @(
            '-NoLogo',
            '-NoProfile',
            '-File', $runnerPath,
            '-NoGui',
            '-RestartProfile', 'None',
            '-WaitForExisting'
        )
        if ($AuditOnly) {
            $runnerArguments += '-AuditOnly'
        }
        $runnerInvocation = Invoke-NativeCapture -FilePath $powershell -ArgumentList $runnerArguments
        $raw = $runnerInvocation.Output
        $lastExitCode = $runnerInvocation.ExitCode
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
        # Another sync (usually the post-exit watcher of the previous session) owns the
        # lock.  Wait for it to finish; if it completed cleanly its work counts and the
        # launch proceeds instead of punishing the user with a failure dialog.
        Wait-HistorySyncIdle -TimeoutSeconds 900 | Out-Null
        if (-not $AuditOnly -and (Test-RecentSuccessfulSync -WithinMinutes 10)) {
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Three-way history sync failed with exit code $lastExitCode.`n$lastDetail"
}

$appExecutable = Get-CodexAppExecutable
$authStatus = Get-SotaAuthStatus
$authMode = $authStatus.mode
$configValid = Test-SotaConfig
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

if ($AuditOnly) {
    $syncRunnerAuditReady = $false
    $syncRunnerAuditError = $null
    try {
        Invoke-ThreeWayHistorySync -AuditOnly
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
        model_provider = 'tango_relay'
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
            throw "Multi-vendor SOTA configuration is missing or invalid: $configPath"
        }
        if (-not $registryValid) {
            throw "SOTA provider registry is missing or invalid: $registryPath"
        }
        if (-not $catalogValid) {
            throw "Multi-vendor SOTA model catalog is missing or out of date: $catalogPath"
        }
        if ($authStatus.state -ne 'ready') {
            throw "Tango Relay API key is not configured correctly (state: $($authStatus.state))."
        }
        $routerResult = Invoke-SotaRouterManager
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

try {
    if (-not $configValid) {
        throw "Multi-vendor SOTA configuration is missing or invalid: $configPath"
    }
    if (-not $registryValid) {
        throw "SOTA provider registry is missing or invalid: $registryPath"
    }
    if (-not $catalogValid) {
        throw "Multi-vendor SOTA model catalog is missing or out of date: $catalogPath"
    }
    if ($authStatus.state -ne 'ready') {
        throw "Tango Relay API key is not configured correctly (state: $($authStatus.state)). Run codex-sota again and paste only the API key when prompted."
    }
    if (-not $appExecutable) {
        throw 'ChatGPT/Codex App was not found.'
    }

    Stop-CodexApp
    if (-not $SkipSync) {
        Invoke-ThreeWayHistorySync
    }
    $routerResult = Invoke-SotaRouterManager
    $appProcessId = Start-SotaApp -ExecutablePath $appExecutable
    try {
        Start-PostExitSyncWatcher -AppProcessId $appProcessId
        Set-ActiveSotaProfileMarker
    }
    catch {
        $launchCompletionError = $_.Exception.Message
        $cleanupError = Stop-ProcessTreeById -ProcessId $appProcessId
        if ($cleanupError) {
            $launchCompletionError += "`nCleanup failed: $cleanupError"
        }
        throw "Post-exit synchronization could not be armed, so this App launch was rolled back.`n$launchCompletionError"
    }
}
catch {
    Show-SotaMessage -Title 'Multi-vendor SOTA Codex launch failed' -Icon Error -Message $_.Exception.Message
    exit 1
}

exit 0
