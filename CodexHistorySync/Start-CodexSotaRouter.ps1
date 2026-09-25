param(
    [switch]$AuditOnly,
    [switch]$Stop,
    [ValidateSet('codex', 'claude')]
    [string]$Workspace = 'codex',
    [string]$SotaRootOverride,
    [int]$RouterPortOverride,
    [string]$RouterScriptOverride,
    [string]$PythonExecutableOverride
)

$ErrorActionPreference = 'Stop'
# Emit UTF-8 regardless of the host's console codepage: restart_router and the manager
# capture this script's output as UTF-8, and localized Windows error text (zh-CN process or
# file errors) otherwise goes out as GBK bytes and crashes the capturing reader.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
# One script serves both config roots so the start/stop/health logic cannot drift between
# them; Start-ClaudeSotaRouter.ps1 is a shim that passes -Workspace claude.
$workspaceSettings = @{
    codex  = @{ Root = '.codex-sota';  Port = 17895 }
    claude = @{ Root = '.claude-sota'; Port = 17994 }
}
$selected = $workspaceSettings[$Workspace]
$sotaRoot = if ($SotaRootOverride) {
    [System.IO.Path]::GetFullPath($SotaRootOverride)
} else {
    Join-Path $env:USERPROFILE $selected.Root
}
$routerPort = if ($RouterPortOverride -gt 0) { $RouterPortOverride } else { [int]$selected.Port }
$installRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$routerScript = if ($RouterScriptOverride) {
    [System.IO.Path]::GetFullPath($RouterScriptOverride)
} else {
    Join-Path $installRoot 'codex_sota_router.py'
}
$registryPath = Join-Path $sotaRoot 'providers.json'
$authPath = Join-Path $sotaRoot 'auth.json'
$pidPath = Join-Path $sotaRoot 'sota-router.pid'
$logPath = Join-Path $sotaRoot 'log\sota-router.jsonl'
$healthUri = 'http://127.0.0.1:' + $routerPort + '/healthz'

function Get-PythonExecutable {
    $candidates = @(
        $PythonExecutableOverride,
        $env:CODEX_PYTHON,
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe')
    )
    $candidates += @(Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName 'python.exe' })
    $candidates += Join-Path (Split-Path -Parent $PSScriptRoot) 'CodexSotaManager\.venv-build\Scripts\python.exe'
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    if ($PythonExecutableOverride) { $candidates = @($PythonExecutableOverride) }
    foreach ($candidate in @($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf) -or $candidate -match '\\WindowsApps\\') { continue }
        try {
            $probe = @(& $candidate -I -S -B -c 'import sys; print(193731 if sys.version_info >= (3,11) else 0)' 2>$null)
            if ($LASTEXITCODE -eq 0 -and $probe -contains '193731') { return [IO.Path]::GetFullPath($candidate) }
        }
        catch { continue }
    }
    return $null
}

function Get-ProcessById {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return $null
    }
    return Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
}

function Test-WorkspaceRouterProcess {
    param([object]$Process)

    if (-not $Process) {
        return $false
    }
    $imageName = [System.IO.Path]::GetFileName([string]$Process.ExecutablePath)
    if (-not $imageName) {
        $imageName = [string]$Process.Name
    }
    if ($imageName -notmatch '^pythonw?(\.exe)?$') {
        return $false
    }
    $commandLine = [string]$Process.CommandLine
    if (-not $commandLine) {
        return $false
    }
    foreach ($literal in @($routerScript, $registryPath)) {
        if ($commandLine.IndexOf($literal, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
            return $false
        }
    }
    $portPattern = '(?i)(?:^|\s)--port(?:\s+|=)["'']?' +
        [regex]::Escape([string]$routerPort) + '["'']?(?=\s|$)'
    return $commandLine -match $portPattern
}

function Get-ListeningProcessIds {
    $ids = @()
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        try {
            $ids = @(
                Get-NetTCPConnection -State Listen -LocalPort $routerPort -ErrorAction Stop |
                    Select-Object -ExpandProperty OwningProcess
            )
        }
        catch {
            $ids = @()
        }
    }
    if ($ids.Count -eq 0) {
        try {
            foreach ($line in @(& netstat.exe -ano -p tcp 2>$null)) {
                if ($line -notmatch '^\s*TCP\s+(\S+)\s+\S+\s+LISTENING\s+(\d+)\s*$') {
                    continue
                }
                $localEndpoint = $matches[1]
                $ownerText = $matches[2]
                $owner = 0
                if (
                    $localEndpoint -match (':'+[regex]::Escape([string]$routerPort)+'$') -and
                    [int]::TryParse($ownerText, [ref]$owner)
                ) {
                    $ids += $owner
                }
            }
        }
        catch {
            $ids = @()
        }
    }
    return @($ids | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
}

function Stop-OwnedRouterProcess {
    param([int]$ProcessId)

    $process = Get-ProcessById -ProcessId $ProcessId
    if (-not (Test-WorkspaceRouterProcess -Process $process)) {
        return $false
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        Wait-Process -Id $ProcessId -Timeout 5 -ErrorAction SilentlyContinue
    }
    return -not [bool](Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Stop-WorkspaceRouters {
    $stopped = [System.Collections.Generic.List[int]]::new()
    $conflicts = [System.Collections.Generic.List[int]]::new()
    $recordedPid = 0
    if (Test-Path -LiteralPath $pidPath) {
        [void][int]::TryParse((Get-Content -Raw -LiteralPath $pidPath).Trim(), [ref]$recordedPid)
        if ($recordedPid -gt 0 -and (Stop-OwnedRouterProcess -ProcessId $recordedPid)) {
            $stopped.Add($recordedPid)
        }
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    }

    foreach ($owner in @(Get-ListeningProcessIds)) {
        if ($stopped.Contains([int]$owner)) {
            continue
        }
        if (Stop-OwnedRouterProcess -ProcessId ([int]$owner)) {
            $stopped.Add([int]$owner)
        } else {
            $conflicts.Add([int]$owner)
        }
    }
    return [pscustomobject]@{
        Stopped = @($stopped)
        Conflicts = @($conflicts | Sort-Object -Unique)
    }
}

function Repair-RouterPidFile {
    if (Test-Path -LiteralPath $pidPath) {
        $recordedPid = 0
        if ([int]::TryParse((Get-Content -Raw -LiteralPath $pidPath).Trim(), [ref]$recordedPid) -and
            (Test-WorkspaceRouterProcess -Process (Get-ProcessById -ProcessId $recordedPid))) { return }
    }
    foreach ($owner in @(Get-ListeningProcessIds)) {
        $process = Get-ProcessById -ProcessId ([int]$owner)
        if (Test-WorkspaceRouterProcess -Process $process) {
            Set-Content -LiteralPath $pidPath -Value ([string]$owner) -Encoding Ascii
            return
        }
    }
}

function Get-RouterHealth {
    param(
        [string]$ExpectedVersion,
        [string]$ExpectedHash,
        [string[]]$ExpectedProviders
    )

    try {
        $health = Invoke-RestMethod -UseBasicParsing -Uri $healthUri -TimeoutSec 2
        $actualProviders = @($health.upstreams | Sort-Object)
        $expectedSorted = @($ExpectedProviders | Sort-Object)
        if (
            $health.status -eq 'ok' -and
            [string]$health.version -eq $ExpectedVersion -and
            [string]$health.registry_hash -eq $ExpectedHash -and
            ($actualProviders -join '|') -eq ($expectedSorted -join '|')
        ) {
            return $health
        }
    }
    catch {
        return $null
    }
    return $null
}

$routerMutex = $null
$routerMutexOwned = $false
try {
if (-not $AuditOnly) {
    $routerMutex = [Threading.Mutex]::new($false, ('Local\CodexSotaRouter-' + $Workspace + '-' + $routerPort))
    try { $routerMutexOwned = $routerMutex.WaitOne(15000) }
    catch [Threading.AbandonedMutexException] { $routerMutexOwned = $true }
    if (-not $routerMutexOwned) {
        @{ status = 'deferred'; reason = 'router_operation_in_progress'; workspace = $Workspace } | ConvertTo-Json -Compress
        exit 0
    }
}

if ($Stop) {
    $stopResult = Stop-WorkspaceRouters
    [ordered]@{
        status = if ($stopResult.Conflicts.Count -gt 0) { 'blocked' } else { 'stopped' }
        workspace = $Workspace
        stopped_process_ids = @($stopResult.Stopped)
        unrelated_listeners = @($stopResult.Conflicts)
    } | ConvertTo-Json -Compress
    exit $(if ($stopResult.Conflicts.Count -gt 0) { 2 } else { 0 })
}

$python = Get-PythonExecutable
if (-not $python) {
    throw 'Python runtime was not found for the local SOTA router.'
}
foreach ($path in @($routerScript, $registryPath, $authPath)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "SOTA router dependency is missing: $path"
    }
}

$checkOutput = @(
    & $python $routerScript --check --registry $registryPath --auth $authPath 2>&1
)
$checkExitCode = $LASTEXITCODE
if ($checkExitCode -ne 0) {
    throw "SOTA router credential/registry check failed with exit code $checkExitCode.`n$($checkOutput -join [Environment]::NewLine)"
}
$check = $checkOutput[-1] | ConvertFrom-Json
if ($check.status -ne 'ready' -or -not $check.registry_hash) {
    throw 'SOTA router credential/registry check did not return ready.'
}

$expectedProviders = @($check.providers)
$health = Get-RouterHealth -ExpectedVersion ([string]$check.version) -ExpectedHash $check.registry_hash -ExpectedProviders $expectedProviders
if ($AuditOnly) {
    [ordered]@{
        status = 'ready'
        config_and_credentials_ready = $true
        running = $null -ne $health
        health_uri = $healthUri
        registry_hash = $check.registry_hash
        providers = $expectedProviders
        models = @($check.models)
        secret_storage = 'windows-dpapi-current-user'
    } | ConvertTo-Json -Depth 5 -Compress
    exit 0
}

if ($health) {
    Repair-RouterPidFile
    [ordered]@{
        status = 'ready'
        started = $false
        registry_hash = $check.registry_hash
        process_id = if (Test-Path $pidPath) { [string](Get-Content -Raw $pidPath) } else { $null }
    } | ConvertTo-Json -Compress
    exit 0
}

$stopResult = Stop-WorkspaceRouters
if ($stopResult.Conflicts.Count -gt 0) {
    throw "Port $routerPort is occupied by an unrelated process (PID $($stopResult.Conflicts -join ', ')); it was not stopped."
}

$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) {
    $pythonw = $python
}
$argumentList = @(
    ('"' + $routerScript + '"'),
    '--host', '127.0.0.1',
    '--port', $routerPort,
    '--registry', ('"' + $registryPath + '"'),
    '--auth', ('"' + $authPath + '"'),
    '--pid-file', ('"' + $pidPath + '"'),
    '--log', ('"' + $logPath + '"')
)
$startedProcess = Start-Process -FilePath $pythonw -ArgumentList $argumentList -WorkingDirectory $installRoot -WindowStyle Hidden -PassThru

$deadline = [DateTime]::UtcNow.AddSeconds(12)
do {
    Start-Sleep -Milliseconds 250
    $health = Get-RouterHealth -ExpectedVersion ([string]$check.version) -ExpectedHash $check.registry_hash -ExpectedProviders $expectedProviders
    if ($health) {
        [ordered]@{
            status = 'ready'
            started = $true
            registry_hash = $check.registry_hash
            process_id = if (Test-Path $pidPath) { [string](Get-Content -Raw $pidPath) } else { $null }
        } | ConvertTo-Json -Compress
        exit 0
    }
} while ([DateTime]::UtcNow -lt $deadline)

$owners = @(Get-ListeningProcessIds)
$ownerDetail = if ($owners.Count -gt 0) { " Listening PID(s): $($owners -join ', ')." } else { '' }
$exitDetail = if ($startedProcess.HasExited) { " Router process exited with code $($startedProcess.ExitCode)." } else { '' }
throw "Local SOTA router did not become healthy on 127.0.0.1:${routerPort}.$exitDetail$ownerDetail"
}
finally {
    if ($routerMutexOwned) { $routerMutex.ReleaseMutex() }
    if ($routerMutex) { $routerMutex.Dispose() }
}
