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
    @{ name = 'ordinary cold launch skips sync'; mode = 'cold'; status = 'ready'; starts = 1; stops = 1 },
    @{ name = 'ordinary repeat focuses existing'; mode = 'existing'; status = 'ready'; starts = 0; stops = 0 },
    @{ name = 'explicit restart restarts once'; mode = 'restart'; status = 'ready'; starts = 1; stops = 1 },
    @{ name = 'same restart request is idempotent'; mode = 'restart_same'; status = 'ready'; starts = 0; stops = 0 },
    @{ name = 'lifecycle busy is deferred'; mode = 'busy'; status = 'deferred'; starts = 0; stops = 0 },
    @{ name = 'legacy sync busy is deferred'; mode = 'legacy_busy'; status = 'deferred'; starts = 0; stops = 0 },
    @{ name = 'watcher error never rolls back app'; mode = 'watcher_error'; status = 'ready'; starts = 1; stops = 1 },
    @{ name = 'marker error never rolls back app'; mode = 'marker_error'; status = 'ready'; starts = 1; stops = 1 },
    @{ name = 'live app awaiting window is deferred'; mode = 'pending_window'; status = 'deferred'; starts = 1; stops = 1 },
    @{ name = 'pending existing app is not restarted'; mode = 'pending_existing'; status = 'deferred'; starts = 0; stops = 0 },
    @{ name = 'unknown existing profile is never closed'; mode = 'unknown_profile'; status = 'error'; starts = 0; stops = 0 },
    @{ name = 'unknown profile requires explicit restart'; mode = 'restart_unknown'; status = 'ready'; starts = 1; stops = 1 },
    @{ name = 'lingering app-server defers new desktop'; mode = 'backend_closing'; status = 'deferred'; starts = 0; stops = 1 },
    @{ name = 'A restart B activation A retry is idempotent'; mode = 'restart_after_activation'; status = 'ready'; starts = 0; stops = 0 },
    @{ name = 'marker failure reuses pending per-request identity'; mode = 'request_identity_only'; status = 'deferred'; starts = 0; stops = 0 }
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
function Get-Content { '{"profile":"Sota","creation_request_id":"' + $(if ($mode -eq 'restart_after_activation') {'request-1'} else {'other-origin'}) + '","request_id":"' + $(if ($mode -eq 'restart_same') {'request-1'} else {'old-request'}) + '"}' }
function Set-LaunchRequest { }
function Clear-LaunchRequest { }
function Invoke-SotaRouterManager { [pscustomobject]@{status='ready'} }
function Enter-AppLifecycleLock {
    param($LockPath, $WaitSeconds)
    if ($mode -eq 'busy' -or ($mode -eq 'legacy_busy' -and $LockPath -like '*sync.lock')) { return $null }
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
function Get-OwnedCodexAppProcesses { if ($mode -in @('unknown_profile','restart_unknown')) { [pscustomobject]@{Id=222} } }
function Get-ActiveCodexAppBackends { if ($mode -eq 'backend_closing') { [pscustomobject]@{ProcessId=444} } }
function Set-ActiveSotaProfileMarker { param($AppProcessId, [switch]$PreserveCreationRequest); if ($mode -eq 'marker_error') { throw 'fixture marker failure' } }
function Show-OwnedCodexWindow { param($App); $script:focused++ }
function Stop-CodexApp { $script:stopped++ }
function Start-SotaApp { param($ExecutablePath); $script:started++; if ($mode -eq 'pending_window') { $script:launchWindowPending=$true }; return 123 }
function Start-PostExitSyncWatcher { param($AppProcessId); $script:watched++; if ($mode -eq 'watcher_error') { throw 'fixture watcher failure' } }
function Invoke-ThreeWayHistorySync { throw 'REGRESSION: ordinary launch attempted a full history sync' }
function Stop-ProcessTreeById { throw 'REGRESSION: optional setup killed the working app' }
function Write-LaunchStatus {
    param($Status, $Phase, $AppProcessId, [switch]$Reused, $ErrorText, [switch]$Emit)
    $script:launchPhase = $Phase
    if ($Emit) { @{status=$Status;phase=$Phase;starts=$script:started;stops=$script:stopped;warnings=$launchWarnings.Count;error=$ErrorText} | ConvertTo-Json -Compress }
}
function Show-SotaMessage { throw 'REGRESSION: NoGui launch opened a dialog' }
'@
$failures = 0
foreach ($case in $cases) {
    $program = '$mode = ''' + $case.mode + "'`n" + $stubSource + "`n" + $flow.Extent.Text
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($program))
    $lines = @(& $powershell -NoLogo -NoProfile -NonInteractive -EncodedCommand $encoded 2>&1)
    $result = $null
    try { $result = ([string]$lines[-1]) | ConvertFrom-Json } catch { }
    if (-not $result -or $result.status -ne $case.status -or $result.starts -ne $case.starts -or $result.stops -ne $case.stops) {
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
if ($stopFunction.Extent.Text -match 'Stop-Process|taskkill|/F') { throw 'SOTA shutdown may not force-kill conversations' }
Write-Output 'PASS explicit restart uses normal window close without force-kill'

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
        foreach ($functionName in @('Set-ActiveSotaProfileMarker','Get-RequestedSotaApp','Write-LaunchStatus')) {
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
