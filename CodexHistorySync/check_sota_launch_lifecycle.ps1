# Executes only the launch block extracted from the real script, with every
# effectful operation replaced by a stub in a fresh child PowerShell process.
# It never starts/stops Codex, the router, a watcher, or a real history sync.
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$sourcePath = Join-Path $PSScriptRoot 'Switch-CodexSota.ps1'
$ast = [Management.Automation.Language.Parser]::ParseFile($sourcePath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw $parseErrors[0].Message }
$flow = $ast.EndBlock.Statements | Where-Object {
    $_ -is [Management.Automation.Language.TryStatementAst] -and
    $_.Body.Extent.Text -match 'Start-SotaApp -ExecutablePath' -and
    $_.Body.Extent.Text -match 'Enter-AppLifecycleLock'
} | Select-Object -First 1
if (-not $flow) { throw 'Could not locate the real SOTA launch block' }
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$cases = @(
    @{ name = 'ordinary cold launch skips full sync but returns account deltas'; mode = 'cold'; status = 'ready'; starts = 1; stops = 1; returns = 1 },
    @{ name = 'ordinary repeat focuses existing'; mode = 'existing'; status = 'ready'; starts = 0; stops = 0; returns = 0 },
    @{ name = 'explicit restart restarts once'; mode = 'restart'; status = 'ready'; starts = 1; stops = 1 },
    @{ name = 'same restart request is idempotent'; mode = 'restart_same'; status = 'ready'; starts = 0; stops = 0 },
    @{ name = 'lifecycle busy is deferred'; mode = 'busy'; status = 'deferred'; starts = 0; stops = 0 },
    @{ name = 'legacy sync busy is deferred'; mode = 'legacy_busy'; status = 'deferred'; starts = 0; stops = 0 },
    @{ name = 'watcher error never rolls back app'; mode = 'watcher_error'; status = 'ready'; starts = 1; stops = 1 },
    @{ name = 'marker error never rolls back app'; mode = 'marker_error'; status = 'ready'; starts = 1; stops = 1 },
    @{ name = 'live app awaiting window is deferred'; mode = 'pending_window'; status = 'deferred'; starts = 1; stops = 1 },
    # Missing MainWindowHandle is not proof of a dead conversation. The old
    # expectation required a force-kill of a legitimate background/tray app.
    @{ name = 'owned hidden app is reused without stopping'; mode = 'pending_existing'; status = 'deferred'; starts = 0; stops = 0 },
    @{ name = 'unknown existing profile is never closed'; mode = 'unknown_profile'; status = 'error'; starts = 0; stops = 0 },
    @{ name = 'unknown profile requires explicit restart'; mode = 'restart_unknown'; status = 'ready'; starts = 1; stops = 1 },
    @{ name = 'lingering app-server defers new desktop'; mode = 'backend_closing'; status = 'deferred'; starts = 0; stops = 1 },
    @{ name = 'A restart B activation A retry is idempotent'; mode = 'restart_after_activation'; status = 'ready'; starts = 0; stops = 0 },
    @{ name = 'marker failure reuses pending per-request identity'; mode = 'request_identity_only'; status = 'deferred'; starts = 0; stops = 0 },
    @{ name = 'account return busy never opens a stale desktop'; mode = 'return_busy'; status = 'deferred'; starts = 0; stops = 1; returns = 1 },
    @{ name = 'account return failure stops before desktop'; mode = 'return_failure'; status = 'error'; starts = 0; stops = 1; returns = 1 },
    @{ name = 'history lock reacquisition is mandatory'; mode = 'return_lock_busy'; status = 'deferred'; starts = 0; stops = 1; returns = 1 },
    @{ name = 'app reappearance after account return defers'; mode = 'return_app_appears'; status = 'deferred'; starts = 0; stops = 1; returns = 1 }
)
$stubSource = @'
$ErrorActionPreference = 'Stop'
$configValid = $registryValid = $catalogValid = $true
$authStatus = [pscustomobject]@{state='ready'}
$appExecutable = 'C:\fixture\ChatGPT.exe'
$NoGui = $true
$LaunchRequestId = 'request-1'
$Restart = $mode -in @('restart','restart_same','restart_unknown','restart_after_activation','request_identity_only')
$SyncBeforeLaunch = $false
$SkipSync = $false
$lifecycleLease = $historyWriteLease = $null
$PSScriptRoot = 'C:\fixture'
$activeProfilePath = 'C:\fixture\active-profile.json'
$launchWarnings = [Collections.Generic.List[string]]::new()
$script:started = $script:stopped = $script:focused = $script:watched = 0
$script:returned = 0
function Get-Content { '{"profile":"Sota","creation_request_id":"' + $(if ($mode -eq 'restart_after_activation') {'request-1'} else {'other-origin'}) + '","request_id":"' + $(if ($mode -eq 'restart_same') {'request-1'} else {'old-request'}) + '"}' }
function Set-LaunchRequest { }
function Clear-LaunchRequest { }
function Invoke-SotaRouterManager { [pscustomobject]@{status='ready'} }
function Enter-AppLifecycleLock {
    param($LockPath, $WaitSeconds)
    if ($mode -eq 'busy' -or ($mode -eq 'legacy_busy' -and $LockPath -like '*sync.lock') -or
        ($mode -eq 'return_lock_busy' -and $script:returned -and $LockPath -like '*sync.lock')) { return $null }
    $lease = [pscustomobject]@{}
    $lease | Add-Member -MemberType ScriptMethod -Name Unlock -Value { param($a, $b) }
    $lease | Add-Member -MemberType ScriptMethod -Name Dispose -Value { }
    return $lease
}
function Get-ActiveSotaApp {
    if ($mode -in @('existing','restart','restart_same','pending_existing','restart_after_activation')) {
        [pscustomobject]@{ Id=321; MainWindowHandle=$(if ($mode -eq 'pending_existing') {0} else {100}) }
    }
}
function Get-RequestedSotaApp { if ($mode -eq 'request_identity_only') { [pscustomobject]@{Id=321;MainWindowHandle=0} } }
function Get-OwnedCodexAppProcesses {
    if (($mode -in @('unknown_profile','restart_unknown') -and -not $script:stopped) -or
        ($mode -eq 'return_app_appears' -and $script:returned)) { [pscustomobject]@{Id=222} }
}
function Get-ActiveCodexAppBackends { if ($mode -eq 'backend_closing') { [pscustomobject]@{ProcessId=444} } }
function Set-ActiveSotaProfileMarker { param($AppProcessId, [switch]$PreserveCreationRequest); if ($mode -eq 'marker_error') { throw 'fixture marker failure' } }
function Show-OwnedCodexWindow { param($App); $script:focused++ }
# `Stop-CodexApp -ForceHidden` is how the launch block clears a windowless, marker-matched
# stale process; the stub must accept the switch or the case dies in parameter binding.
function Stop-CodexApp { param([switch]$ForceHidden) $script:stopped++ }
# Both of these joined the launch block after this harness was written.  An unstubbed effectful
# call makes every case report the harness's own error instead of the flow's answer -- and
# `Invoke-ArchivedSidebarRepair` would otherwise spawn a real Python process from a regression run.
function Remove-StaleLaunchArtifacts { }
function Invoke-ArchivedSidebarRepair { }
function Invoke-AccountReturnHistorySync {
    if (-not $lifecycleLease -or $historyWriteLease -or -not $script:stopped) { throw 'Unsafe account-return lock handoff' }
    $script:returned++
    if ($mode -eq 'return_failure') { throw 'fixture account-return failure' }
    return $mode -ne 'return_busy'
}
function Start-SotaApp { param($ExecutablePath); $script:started++; if ($mode -eq 'pending_window') { $script:launchWindowPending=$true }; return 123 }
function Start-PostExitSyncWatcher { param($AppProcessId); $script:watched++; if ($mode -eq 'watcher_error') { throw 'fixture watcher failure' } }
function Invoke-ThreeWayHistorySync { throw 'REGRESSION: ordinary launch attempted a full history sync' }
function Stop-ProcessTreeById { throw 'REGRESSION: optional setup killed the working app' }
function Write-LaunchStatus {
    param($Status, $Phase, $AppProcessId, [switch]$Reused, $ErrorText, [switch]$Emit)
    $script:launchPhase = $Phase
    if ($Emit) { @{status=$Status;phase=$Phase;starts=$script:started;stops=$script:stopped;returns=$script:returned;warnings=$launchWarnings.Count;error=$ErrorText} | ConvertTo-Json -Compress }
}
function Show-SotaMessage { throw 'REGRESSION: NoGui launch opened a dialog' }
'@
$failures = 0
foreach ($case in $cases) {
    $program = '$mode = ''' + $case.mode + "'`n" + $stubSource + "`n" + $flow.Extent.Text
    # A complete launch flow can exceed Windows' command-line limit after
    # UTF-16/Base64 expansion. Use a uniquely owned fixture script instead.
    $flowScript = Join-Path ([IO.Path]::GetTempPath()) ('codex-sota-flow-' + [guid]::NewGuid().ToString('N') + '.ps1')
    try {
        [IO.File]::WriteAllText($flowScript, $program, [Text.UTF8Encoding]::new($true))
        $lines = @(& $powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $flowScript 2>&1)
    }
    finally { Remove-Item -LiteralPath $flowScript -Force -ErrorAction SilentlyContinue }
    $result = $null
    try { $result = ([string]$lines[-1]) | ConvertFrom-Json } catch { }
    if (-not $result -or $result.status -ne $case.status -or $result.starts -ne $case.starts -or $result.stops -ne $case.stops -or
        ($case.ContainsKey('returns') -and $result.returns -ne $case.returns)) {
        Write-Output ('FAIL ' + $case.name + ': ' + ($lines -join ' ')); $failures++
    }
    elseif ($case.mode -in @('watcher_error','marker_error') -and $result.warnings -ne 1) {
        Write-Output ('FAIL ' + $case.name + ': warning was lost'); $failures++
    }
    else { Write-Output ('PASS ' + $case.name) }
}
if ($failures) { throw "$failures SOTA launch lifecycle regression(s)" }
Write-Output ('All ' + $cases.Count + ' isolated SOTA launch flows passed.')
$stopFunction = $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Stop-CodexApp'}, $true) | Select-Object -First 1
# The confirmed defect was treating absence of a window as proof of staleness.
# Keep the old switch callable, but assert that both ordinary and explicit
# restart flows preserve a process that has not accepted a normal close.
$stopText = $stopFunction.Extent.Text
if ($stopText -match 'taskkill|/F') { throw 'SOTA shutdown may not tree-kill the app' }
if ($stopText -notmatch '\[switch\]\$ForceHidden') { throw 'legacy caller signature changed' }
if ($stopText -match 'Stop-Process\s|\.Kill\(') { throw 'windowless or unresponsive conversations must not be force-killed' }
if ($stopText -notmatch '\.CloseMainWindow\(' -or $stopText -notmatch 'throw') { throw 'normal close and actionable refusal must remain available' }
Write-Output 'PASS shutdown requests normal close and never force-kills a hidden conversation'

function Test-RealMarkerAndRequestFunctions {
    # Exercise the REAL marker/status implementations on an owned temp fixture,
    # not just control-flow stubs. No production marker is read or written.
    $temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $fixtureName = 'codex-launch-marker-test-' + [guid]::NewGuid().ToString('N')
    $fixtureDirectory = Join-Path $temporaryRoot $fixtureName
    New-Item -ItemType Directory -Path $fixtureDirectory | Out-Null
    try {
        $fakeApp = [pscustomobject]@{Id=421;Path='C:\fixture\ChatGPT.exe';StartTime=[datetime]'2026-09-23T10:00:00Z';MainWindowHandle=0}
        function Get-Process { param($Id); if ($Id -eq 421) { return $fakeApp } }
        function Get-OwnedCodexAppProcesses { return $fakeApp }
        function Get-CodexAppExecutable { return $fakeApp.Path }
        # `Get-ProcessCreationTicks` is a dependency of the real `Write-LaunchStatus`; without it
        # in this list the fixture died on "not recognized" the moment the case table above
        # stopped throwing early -- i.e. this whole section had not actually run for a while.
        foreach ($functionName in @('Set-ActiveSotaProfileMarker','Get-RequestedSotaApp','Write-LaunchStatus','Get-ProcessCreationTicks')) {
            $definition = $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName},$true) | Select-Object -First 1
            . ([scriptblock]::Create($definition.Extent.Text))
        }
        $activeProfilePath = Join-Path $fixtureDirectory 'marker.json'
        $LaunchRequestId = 'A'
        $script:previousRequestStatus = $null
        Set-ActiveSotaProfileMarker -AppProcessId 421
        $LaunchRequestId = 'B'
        Set-ActiveSotaProfileMarker -AppProcessId 421 -PreserveCreationRequest
        $marker = Get-Content -Raw -LiteralPath $activeProfilePath | ConvertFrom-Json
        if ($marker.creation_request_id -ne 'A' -or $marker.request_id -ne 'A' -or $marker.last_activation_request_id -ne 'B') {
            throw 'Activation B changed the actual creation marker owned by A'
        }
        Write-Output 'PASS real marker preserves A creation while B activates'
        $LaunchRequestId = 'A'
        $script:previousRequestStatus = [pscustomobject]@{request_id='A';app_process_id=421;app_creation_ticks=$fakeApp.StartTime.ToUniversalTime().ToFileTimeUtc().ToString()}
        if ((Get-RequestedSotaApp).Id -ne 421) { throw 'Per-request identity did not recover the pending app' }
        $launchClock = [Diagnostics.Stopwatch]::StartNew()
        $launchWarnings = [Collections.Generic.List[string]]::new()
        $SyncBeforeLaunch = $false
        $lifecycleWorkDir = $fixtureDirectory
        $launchStatusPath = Join-Path $fixtureDirectory 'launch-status-A.json'
        Write-LaunchStatus -Status 'starting' -Phase 'preparing_router'
        $status = Get-Content -Raw -LiteralPath $launchStatusPath | ConvertFrom-Json
        if ($status.app_process_id -ne 421 -or $status.app_creation_ticks -ne $marker.app_creation_ticks) {
            throw 'A new preflight phase erased the durable pending identity'
        }
        Write-Output 'PASS real preflight status retains identity when marker cannot be used'
        $beforeAudit = [IO.File]::ReadAllText($launchStatusPath)
        $AuditOnly = $true
        Write-LaunchStatus -Status 'error' -Phase 'preflight' -ErrorText 'fixture audit error'
        if ([IO.File]::ReadAllText($launchStatusPath) -ne $beforeAudit) { throw 'AuditOnly wrote launch status' }
        $AuditOnly = $false
        Write-Output 'PASS audit error leaves durable launch status unchanged'
        $script:previousRequestStatus.app_creation_ticks = '1'
        if ($null -ne (Get-RequestedSotaApp)) { throw 'Per-request fallback accepted a recycled PID' }
        Write-Output 'PASS real per-request fallback rejects a recycled PID'
    }
    finally {
        $resolvedFixture = [IO.Path]::GetFullPath($fixtureDirectory)
        if (-not $resolvedFixture.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase) -or
            [IO.Path]::GetFileName($resolvedFixture) -ne $fixtureName) { throw 'Refusing unsafe test fixture cleanup' }
        Remove-Item -LiteralPath $resolvedFixture -Recurse -Force
        $script:previousRequestStatus = $null
    }
}
Test-RealMarkerAndRequestFunctions

function Test-DeferredRestartContract {
    # Extract only the restart function and replace its single subprocess effect.
    # No real launcher, filesystem mutation, or sleeping occurs in this fixture.
    $runner = Join-Path $PSScriptRoot 'Run-CodexHistorySync.ps1'
    $runnerTokens = $null
    $runnerErrors = $null
    $runnerAst = [Management.Automation.Language.Parser]::ParseFile($runner, [ref]$runnerTokens, [ref]$runnerErrors)
    if ($runnerErrors.Count) { throw $runnerErrors[0].Message }
    $restart = $runnerAst.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Start-CodexApp'}, $true) | Select-Object -First 1
    $fixtureSource = $restart.Extent.Text.Replace('& $powershell @arguments 2>&1', 'Invoke-FixtureSwitcher @arguments')
    . ([scriptblock]::Create($fixtureSource))
    function Set-RestartLaunchRequest { param($RequestId) }
    function Clear-RestartLaunchRequest { param($RequestId) }
    function Test-Path { return $true }
    function Start-Sleep { }
    function Invoke-FixtureSwitcher {
        $script:fixtureCalls++
        $script:fixtureArguments.Add(($args -join '|'))
        $script:LASTEXITCODE = 0
        if ($script:fixtureAlwaysDeferred -or $script:fixtureCalls -lt 3) { '{"status":"deferred","phase":"waiting_for_history_sync"}' }
        else { '{"status":"ready"}' }
    }
    $sotaSwitcherPath = 'C:\fixture\Switch-CodexSota.ps1'
    $script:fixtureCalls = 0
    $script:fixtureAlwaysDeferred = $false
    $script:fixtureArguments = [Collections.Generic.List[string]]::new()
    Start-CodexApp -ExecutablePath 'C:\fixture\Codex.exe' -Profile Sota -MaxAttempts 3 -RetryDelayMilliseconds 0
    if ($script:fixtureCalls -ne 3 -or @($script:fixtureArguments | Select-Object -Unique).Count -ne 1) { throw 'Deferred restart did not retry one stable request until ready' }
    $script:fixtureCalls = 0
    $script:fixtureAlwaysDeferred = $true
    $failed = $false
    try { Start-CodexApp -ExecutablePath 'C:\fixture\Codex.exe' -Profile Sota -MaxAttempts 2 -RetryDelayMilliseconds 0 }
    catch { $failed = $_.Exception.Message -like '*still deferred*' }
    if (-not $failed -or $script:fixtureCalls -ne 2) { throw 'A permanently deferred restart was falsely reported as successful' }
    Write-Output 'PASS deferred restart retries the same request and reports exhaustion'
}
Test-DeferredRestartContract
