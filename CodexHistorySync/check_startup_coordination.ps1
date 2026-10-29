# Extracted production functions and launch flows, synthetic markers/processes
# only. Never execute a production launcher, app, watcher, or history sync.
$ErrorActionPreference = 'Stop'
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function Read-LauncherAst {
    param([string]$Name)
    $tokens = $null
    $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $Name), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ($Name + ': ' + $errors[0].Message) }
    return $ast
}

function Find-Function {
    param($Ast, [string]$Name)
    $definition = $Ast.FindAll({param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $Name
    }, $true) | Select-Object -First 1
    if (-not $definition) { throw "Missing production function: $Name" }
    return $definition.Extent.Text
}

function Test-ActualShellRequests {
    param($Ast, [string]$Name)
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $fixtureName = 'codex-request-fixture-' + [guid]::NewGuid().ToString('N')
    $fixture = Join-Path $tempRoot $fixtureName
    New-Item -ItemType Directory -Path $fixture | Out-Null
    try {
        $lifecycleWorkDir = $fixture
        $lifecycleLockPath = Join-Path $fixture 'lifecycle.lock'
        $launchRequestPath = Join-Path $fixture 'legacy.json'
        $launchRequestDir = Join-Path $fixture 'requests'
        $launchRequestGuardPath = Join-Path $fixture 'requests.lock'
        $LifecycleWaitSeconds = 0
        foreach ($functionName in @('Enter-AppLifecycleLock','Set-LaunchRequest','Clear-LaunchRequest')) {
            . ([scriptblock]::Create((Find-Function $Ast $functionName)))
        }
        if ($Name -eq 'Sota') { . ([scriptblock]::Create((Find-Function $Ast 'Get-ProcessCreationTicks'))) }
        $LaunchRequestId = 'fixture-A'
        $shellLaunchRequestPath = Join-Path $launchRequestDir ('shell-' + $LaunchRequestId + '-' + $PID + '.json')
        $pathA = $shellLaunchRequestPath
        Set-LaunchRequest
        $first = Get-Content -Raw -LiteralPath $pathA | ConvertFrom-Json
        $expectedTicks = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
        $unixNow = ([DateTime]::UtcNow - [DateTime]'1970-01-01Z').TotalSeconds
        if ($first.launcher_pid -ne $PID -or $first.launcher_creation_ticks -ne $expectedTicks -or
            $first.request_id -ne $LaunchRequestId -or $first.expires_at -lt ($unixNow + 1200)) {
            throw "$Name request did not retain a verifiable shell owner and sufficient lifetime"
        }
        $LaunchRequestId = 'fixture-B'
        $shellLaunchRequestPath = Join-Path $launchRequestDir ('shell-' + $LaunchRequestId + '-' + $PID + '.json')
        $pathB = $shellLaunchRequestPath
        Set-LaunchRequest
        $LaunchRequestId = 'fixture-A'; $shellLaunchRequestPath = $pathA
        Clear-LaunchRequest
        if ((Test-Path -LiteralPath $pathA) -or -not (Test-Path -LiteralPath $pathB)) {
            throw "$Name request A retirement affected a different live request B"
        }
        if ($Name -eq 'Sota') {
            $legacy = Get-Content -Raw -LiteralPath $launchRequestPath | ConvertFrom-Json
            if ($legacy.request_id -ne 'fixture-B') { throw 'SOTA A erased B legacy intent' }
        }
        $LaunchRequestId = 'fixture-B'; $shellLaunchRequestPath = $pathB
        Clear-LaunchRequest
        if (Test-Path -LiteralPath $pathB) { throw "$Name shell request did not clear" }
        Write-Output "PASS $Name real per-shell markers preserve PID birth identity and concurrent launch intent"
    }
    finally {
        $resolved = [IO.Path]::GetFullPath($fixture)
        if (-not $resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
            [IO.Path]::GetFileName($resolved) -ne $fixtureName) { throw 'Unsafe fixture cleanup target' }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

$sotaAst = Read-LauncherAst 'Switch-CodexSota.ps1'
$profileAst = Read-LauncherAst 'Switch-CodexProfile.ps1'
Test-ActualShellRequests $sotaAst 'Sota'
Test-ActualShellRequests $profileAst 'Profile'

$profileFlow = $profileAst.EndBlock.Statements | Where-Object {
    $_ -is [Management.Automation.Language.TryStatementAst] -and
    $_.Body.Extent.Text -match 'Start-CodexAppForProfile -ExecutablePath' -and
    $_.Body.Extent.Text -match 'Enter-AppLifecycleLock'
} | Select-Object -First 1
if (-not $profileFlow) { throw 'Missing production profile launch flow' }
$stubSource = @'
$ErrorActionPreference = 'Stop'
$installDir = 'C:\fixture'
$NoGui = $true
$Profile = 'Plus'
$LaunchRequestId = 'fixture-request'
$SkipSync = $mode -ne 'explicit-sync'
$lifecycleLease = $historyWriteLease = $null
$script:lifecycleHeld = $script:historyHeld = $script:pending = $false
$script:starts = $script:stops = $script:syncs = 0
function Set-LaunchRequest { $script:pending = $true }
function Clear-LaunchRequest { $script:pending = $false }
function Get-CodexAppExecutable {
    if (-not $script:pending) { throw 'Profile preflight ran before launch intent' }
    'C:\fixture\ChatGPT.exe'
}
function Enter-AppLifecycleLock {
    param($LockPath, $WaitSeconds)
    $kind = if ($LockPath -like '*sync.lock') {'history'} else {'lifecycle'}
    if (($mode -eq 'lifecycle-busy' -and $kind -eq 'lifecycle') -or
        ($mode -eq 'sync-busy' -and $kind -eq 'history')) { return $null }
    if ($kind -eq 'history' -and -not $script:lifecycleHeld) { throw 'Lock order reversed' }
    if ($kind -eq 'history') { $script:historyHeld = $true } else { $script:lifecycleHeld = $true }
    $lease = [pscustomobject]@{kind=$kind}
    $lease | Add-Member ScriptMethod Unlock {
        param($offset, $length)
        if ($this.kind -eq 'history') { $script:historyHeld = $false } else { $script:lifecycleHeld = $false }
    }
    $lease | Add-Member ScriptMethod Dispose { }
    $lease
}
function Get-RequestedProfileApp {
    if ($mode -eq 'same-request-window-pending') { [pscustomobject]@{Id=432;MainWindowHandle=0} }
}
function Stop-CodexApp {
    if (-not $script:lifecycleHeld -or -not $script:historyHeld -or -not $script:pending) {
        throw 'Closing an app without write exclusion and pending launch intent'
    }
    $script:stops++
}
function Stop-CockpitTools { }
function Invoke-CanonicalHistorySync {
    if ($script:lifecycleHeld -or $script:historyHeld -or $script:pending) {
        throw 'Manual synchronization kept a recursive lock or self-cancelling intent'
    }
    $script:syncs++
}
function Get-ActiveCodexAppBackends { if ($mode -eq 'backend-closing') { [pscustomobject]@{ProcessId=444} } }
function Start-CodexAppForProfile {
    param($ExecutablePath, $SelectedProfile)
    if (-not $script:lifecycleHeld -or -not $script:historyHeld -or -not $script:pending) {
        throw 'Desktop launched over a live writer or without announced intent'
    }
    $script:starts++
    432
}
function Start-PostExitSyncWatcher { param($AppProcessId); if ($mode -eq 'watcher-error') { throw 'fixture watcher failure' } }
function Set-ActiveProfileMarker { param($SelectedProfile, $AppProcessId); if ($mode -eq 'marker-error') { throw 'fixture marker failure' } }
function Show-ProfileMessage { throw 'Fixture should not open a profile dialog' }
function Write-ProfileLaunchResult {
    param($Status, $Phase, $AppProcessId, $ErrorText)
    @{status=$Status;phase=$Phase;starts=$script:starts;stops=$script:stops;syncs=$script:syncs} | ConvertTo-Json -Compress
}
'@
$cases = @(
    @{mode='cold';status='ready';starts=1;stops=1;syncs=0},
    @{mode='lifecycle-busy';status='deferred';starts=0;stops=0;syncs=0},
    @{mode='sync-busy';status='deferred';starts=0;stops=0;syncs=0},
    @{mode='backend-closing';status='deferred';starts=0;stops=1;syncs=0},
    @{mode='same-request-window-pending';status='deferred';starts=0;stops=0;syncs=0},
    @{mode='watcher-error';status='ready';starts=1;stops=1;syncs=0},
    @{mode='marker-error';status='ready';starts=1;stops=1;syncs=0},
    @{mode='explicit-sync';status='ready';starts=1;stops=1;syncs=1}
)
foreach ($case in $cases) {
    $program = '$mode = ''' + $case.mode + "'`n" + $stubSource + "`n" + $profileFlow.Extent.Text
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($program))
    $lines = @(& $powershell -NoLogo -NoProfile -NonInteractive -EncodedCommand $encoded 2>&1)
    $result = $null
    try { $result = ([string]$lines[-1]) | ConvertFrom-Json } catch { }
    if (-not $result -or $result.status -ne $case.status -or $result.starts -ne $case.starts -or
        $result.stops -ne $case.stops -or $result.syncs -ne $case.syncs) {
        throw ('Profile ' + $case.mode + ' fixture failed: ' + ($lines -join ' '))
    }
    Write-Output ('PASS Profile ' + $case.mode + ' preserves lock order and startup intent')
}

$sotaPreflight = $sotaAst.EndBlock.Statements | Where-Object {
    $_ -is [Management.Automation.Language.TryStatementAst] -and
    $_.Body.Extent.Text -match '\$appExecutable = Get-CodexAppExecutable' -and
    $_.Body.Extent.Text -match 'Set-LaunchRequest'
} | Select-Object -First 1
if (-not $sotaPreflight -or $sotaPreflight.Body.Extent.Text.IndexOf('Set-LaunchRequest') -gt
    $sotaPreflight.Body.Extent.Text.IndexOf('Get-CodexAppExecutable')) { throw 'SOTA preflight can precede announced intent' }
Write-Output 'PASS SOTA announces launch intent before slow Appx/config probes'

function Test-ActualRestartParentRequest {
    $runnerAst = Read-LauncherAst 'Run-CodexHistorySync.ps1'
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $fixtureName = 'codex-restart-parent-fixture-' + [guid]::NewGuid().ToString('N')
    $fixture = Join-Path $tempRoot $fixtureName
    New-Item -ItemType Directory -Path $fixture | Out-Null
    try {
        $installDir = $fixture
        foreach ($functionName in @('Set-RestartLaunchRequest','Clear-RestartLaunchRequest')) {
            . ([scriptblock]::Create((Find-Function $runnerAst $functionName)))
        }
        Set-RestartLaunchRequest -RequestId 'parent-A'
        Set-RestartLaunchRequest -RequestId 'parent-B'
        $pathA = Join-Path $fixture ('work\codex-launch-requests\restart-parent-A-' + $PID + '.json')
        $pathB = Join-Path $fixture ('work\codex-launch-requests\restart-parent-B-' + $PID + '.json')
        $request = Get-Content -Raw -LiteralPath $pathA | ConvertFrom-Json
        if ($request.launcher_pid -ne $PID -or $request.launcher_creation_ticks -ne
            (Get-Process -Id $PID).StartTime.ToUniversalTime().ToFileTimeUtc().ToString()) {
            throw 'Manual restart parent request did not retain exact owner identity'
        }
        Clear-RestartLaunchRequest -RequestId 'parent-A'
        if ((Test-Path -LiteralPath $pathA) -or -not (Test-Path -LiteralPath $pathB)) {
            throw 'Manual restart parent A cleanup affected B'
        }
        Clear-RestartLaunchRequest -RequestId 'parent-B'
        Write-Output 'PASS manual restart parent request retains its identity and clears only its own request'
    }
    finally {
        $resolved = [IO.Path]::GetFullPath($fixture)
        if (-not $resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
            [IO.Path]::GetFileName($resolved) -ne $fixtureName) { throw 'Unsafe fixture cleanup target' }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
Test-ActualRestartParentRequest

function Test-RestartParentFlow {
    $runnerAst = Read-LauncherAst 'Run-CodexHistorySync.ps1'
    $source = (Find-Function $runnerAst 'Start-CodexApp').Replace('& $powershell @arguments 2>&1', 'Invoke-FixtureSwitcher @arguments')
    . ([scriptblock]::Create($source))
    function Test-Path { return $true }
    function Start-Sleep { }
    function Set-RestartLaunchRequest {
        param($RequestId)
        $script:parentIntent = $RequestId
    }
    function Clear-RestartLaunchRequest {
        param($RequestId)
        if ($script:parentIntent -ne $RequestId) { throw 'Parent cleared another request identity' }
        $script:parentIntent = $null
    }
    function Invoke-FixtureSwitcher {
        if (-not $script:parentIntent) { throw 'Retry ran without parent launch intent' }
        if ($args[-1] -ne $script:parentIntent) { throw 'Child retry changed launch identity' }
        $script:calls++
        $script:LASTEXITCODE = 0
        if ($script:forceLegacyZeroSuccess) { return 'historical wrapper returned zero without a confirmed app' }
        if ($script:calls -eq 1) { return '{"status":"deferred","phase":"waiting_for_history_sync"}' }
        '{"status":"ready"}'
    }
    $profileSwitcherPath = 'C:\fixture\Switch-CodexProfile.ps1'
    $sotaSwitcherPath = 'C:\fixture\Switch-CodexSota.ps1'
    foreach ($profileName in @('Plus','Cockpit','Sota')) {
        $script:calls = 0
        $script:parentIntent = $null
        $script:forceLegacyZeroSuccess = $false
        Start-CodexApp -ExecutablePath 'C:\fixture\Codex.exe' -Profile $profileName -MaxAttempts 2 -RetryDelayMilliseconds 0
        if ($script:calls -ne 2 -or $script:parentIntent) { throw "$profileName restart did not preserve and then retire the parent request" }
        $script:calls = 0
        $script:forceLegacyZeroSuccess = $true
        $failed = $false
        try { Start-CodexApp -ExecutablePath 'C:\fixture\Codex.exe' -Profile $profileName -MaxAttempts 2 -RetryDelayMilliseconds 0 }
        catch { $failed = $_.Exception.Message -like '*did not confirm restart*' }
        if (-not $failed -or $script:parentIntent) { throw "$profileName accepted unconfirmed success or leaked its parent intent" }
        Write-Output ("PASS $profileName restart preserves one parent request and rejects unconfirmed exit-zero success")
    }
}
Test-RestartParentFlow

function Test-ProfilePendingWindowIdentity {
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $fixtureName = 'codex-profile-window-fixture-' + [guid]::NewGuid().ToString('N')
    $fixture = Join-Path $tempRoot $fixtureName
    New-Item -ItemType Directory -Path $fixture | Out-Null
    try {
        $lifecycleWorkDir = $fixture
        $activeProfilePath = Join-Path $fixture 'marker.json'
        $launchStatusPath = Join-Path $fixture 'launch-status.json'
        $LaunchRequestId = 'profile-window-A'
        $Profile = 'Plus'
        $AuditOnly = $PrepareOnly = $false
        $fakeApp = [pscustomobject]@{Id=431;StartTime=[datetime]'2026-10-14T10:00:00Z';MainWindowHandle=0}
        function Get-Process {
            param($Id)
            if ($Id -eq $fakeApp.Id) { $fakeApp }
            elseif ($Id -eq $PID) { [pscustomobject]@{StartTime=[datetime]'2026-10-14T09:00:00Z'} }
        }
        function Get-OwnedCodexAppProcesses { $fakeApp }
        foreach ($functionName in @('Set-ActiveProfileMarker','Write-ProfileLaunchResult','Get-RequestedProfileApp')) {
            . ([scriptblock]::Create((Find-Function $profileAst $functionName)))
        }
        Set-ActiveProfileMarker -SelectedProfile Plus -AppProcessId $fakeApp.Id
        Write-ProfileLaunchResult -Status deferred -Phase waiting_for_window -AppProcessId $fakeApp.Id | Out-Null
        if ((Get-RequestedProfileApp).Id -ne $fakeApp.Id) { throw 'Profile marker did not recover its pending app' }
        Remove-Item -LiteralPath $activeProfilePath
        if ((Get-RequestedProfileApp).Id -ne $fakeApp.Id) { throw 'Profile request status did not recover pending app after marker failure' }
        $fakeApp.StartTime = $fakeApp.StartTime.AddSeconds(1)
        if ($null -ne (Get-RequestedProfileApp)) { throw 'Profile window recovery accepted a recycled process identity' }
        Write-Output 'PASS Profile pending-window retries recover durable identity and reject recycled PIDs'
    }
    finally {
        $resolved = [IO.Path]::GetFullPath($fixture)
        if (-not $resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
            [IO.Path]::GetFileName($resolved) -ne $fixtureName) { throw 'Unsafe fixture cleanup target' }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
Test-ProfilePendingWindowIdentity

function Test-ManualSyncNormalClose {
    $runnerAst = Read-LauncherAst 'Run-CodexHistorySync.ps1'
    $stopText = Find-Function $runnerAst 'Stop-CodexApp'
    if ($stopText -match 'taskkill|Stop-Process|\.Kill\(') { throw 'Manual history sync may not force-kill conversations.' }
    . ([scriptblock]::Create($stopText))
    $script:closeRequests = 0
    $script:ownedReads = 0
    $script:closeScenario = 'normal'
    $fakeApp = [pscustomobject]@{
        Id=941;Path='C:\synthetic\ChatGPT.exe';StartTime=[datetime]'2026-10-14T10:00:00Z';MainWindowHandle=123
    }
    $fakeApp | Add-Member -MemberType ScriptMethod -Name CloseMainWindow -Value { $script:closeRequests++; return $true }
    function Get-OwnedCodexAppProcesses {
        $script:ownedReads++
        if ($script:closeScenario -eq 'absent') { return }
        if ($script:closeScenario -eq 'normal' -and $script:ownedReads -gt 1) { return }
        if ($script:closeScenario -eq 'busy' -and $script:ownedReads -gt 1) {
            throw 'synthetic app still closing'
        }
        $fakeApp
    }
    function Get-Process {
        param($Id)
        if ($Id -ne $fakeApp.Id) { throw 'Unexpected synthetic process lookup.' }
        if ($script:closeScenario -eq 'recycled') {
            return [pscustomobject]@{Id=$Id;Path=$fakeApp.Path;StartTime=$fakeApp.StartTime.AddSeconds(1);MainWindowHandle=123}
        }
        $fakeApp
    }
    function Start-Sleep { param($Milliseconds) }
    if ((Stop-CodexApp) -ne $true -or $script:closeRequests -ne 1) { throw 'Manual sync did not request a normal window close.' }
    $script:closeScenario = 'absent'; $script:ownedReads = 0
    if ((Stop-CodexApp) -ne $false) { throw 'No-app manual sync close must be a no-op.' }
    $script:closeScenario = 'busy'; $script:ownedReads = 0
    $busyFailed = $false
    try { Stop-CodexApp | Out-Null } catch { $busyFailed = $_.Exception.Message -eq 'synthetic app still closing' }
    if (-not $busyFailed -or $script:closeRequests -ne 2) { throw 'Manual sync did not retain a busy conversation.' }
    $script:closeScenario = 'recycled'; $script:ownedReads = 0
    $recycledFailed = $false
    try { Stop-CodexApp | Out-Null } catch { $recycledFailed = $_.Exception.Message -like '*identity changed*' }
    if (-not $recycledFailed -or $script:closeRequests -ne 2) { throw 'Manual sync closed a recycled process identity.' }
    Write-Output 'PASS Manual sync normally closes an owned window, preserves busy apps and rejects recycled PIDs'
}
Test-ManualSyncNormalClose
