# Extract production functions/flow by AST. All profiles and processes below
# are fixtures: never execute a real launcher, router, watcher, login, or sync.
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot 'Switch-CodexProfile.ps1'), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw $parseErrors[0].Message }
function Get-ProductionFunction {
    param([string]$Name)
    $definition = $ast.FindAll({param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $Name
    }, $true) | Select-Object -First 1
    if (-not $definition) { throw "Missing production function: $Name" }
    $definition.Extent.Text
}

& {
    . ([scriptblock]::Create((Get-ProductionFunction 'Set-ProfileStartEnvironment')))
    $ChatGptDirect = $true
    $apiEnvironmentNames = @('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'CODEX_ACCESS_TOKEN')
    $parentHome = [Environment]::GetEnvironmentVariable('CODEX_HOME')
    $parentCli = [Environment]::GetEnvironmentVariable('CODEX_CLI_PATH')
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.UseShellExecute = $false
    $child = $startInfo.EnvironmentVariables
    foreach ($name in @('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_API_KEY',
            'CODEX_ACCESS_TOKEN', 'CODEX_CLI_PATH', 'CODEX_SOTA_API_KEY', 'OPENAI_ORG_ID')) {
        $child[$name] = 'fixture-value-not-a-real-secret'
    }
    $child['CODEX_HOME'] = 'C:\fixture\sota'
    $child['HTTPS_PROXY'] = 'http://fixture.invalid:1234'
    Set-ProfileStartEnvironment -StartInfo $startInfo -Root 'C:\fixture\plus'
    if ($child['CODEX_HOME'] -ne 'C:\fixture\plus') { throw 'Account child did not use isolated root' }
    foreach ($name in @('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_API_KEY',
            'CODEX_ACCESS_TOKEN', 'CODEX_CLI_PATH', 'CODEX_SOTA_API_KEY', 'OPENAI_ORG_ID')) {
        if ($child.ContainsKey($name)) { throw "Child retained incompatible environment: $name" }
    }
    if ($child['HTTPS_PROXY'] -ne 'http://fixture.invalid:1234') { throw 'Network proxy was unexpectedly removed' }
    if ([Environment]::GetEnvironmentVariable('CODEX_HOME') -cne $parentHome -or
        [Environment]::GetEnvironmentVariable('CODEX_CLI_PATH') -cne $parentCli) {
        throw 'Parent environment was changed'
    }
    $ChatGptDirect = $false
    $legacy = [Diagnostics.ProcessStartInfo]::new()
    $legacy.UseShellExecute = $false
    $legacy.EnvironmentVariables['CODEX_CLI_PATH'] = 'fixture-legacy-pin'
    Set-ProfileStartEnvironment -StartInfo $legacy -Root 'C:\fixture\legacy'
    if ($legacy.EnvironmentVariables['CODEX_CLI_PATH'] -ne 'fixture-legacy-pin') {
        throw 'Opt-in account behavior leaked into a legacy launcher'
    }
    Write-Output 'PASS only the account child loses API routing/credentials and incompatible CLI pin'
}

& {
    . ([scriptblock]::Create((Get-ProductionFunction 'Assert-ChatGptDirectProfile')))
    $Profile = 'Plus'
    $canonicalPlusRoot = 'C:\fixture\plus'
    function Test-Path { param($LiteralPath, $PathType); return $true }
    function Get-ProfileAuthMode { param($Root); return $fixtureAuthMode }
    function Invoke-ProfileConfigValidator { param($Root, $SelectedProfile); @{valid=$true;reason='ok'} }
    foreach ($fixtureAuthMode in @('chatgpt', 'missing')) {
        Assert-ChatGptDirectProfile
    }
    foreach ($fixtureAuthMode in @('apikey', 'unknown', 'invalid')) {
        $rejected = $false
        try { Assert-ChatGptDirectProfile } catch { $rejected = $true }
        if (-not $rejected) { throw 'Incompatible account credentials were accepted' }
    }
    $fixtureAuthMode = 'chatgpt'
    $Profile = 'Cockpit'
    $rejected = $false
    try { Assert-ChatGptDirectProfile } catch { $rejected = $true }
    if (-not $rejected) { throw 'Account entry accepted a non-account profile' }
    Write-Output 'PASS signed-out account can open login; incompatible credentials are not cleared'
}

$profileFlow = $ast.EndBlock.Statements | Where-Object {
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
$ChatGptDirect = $true
$Restart = $mode -ne 'other-profile-unconfirmed'
$SkipSync = $true
$LaunchRequestId = 'fixture-account-request'
$script:starts = $script:stops = $script:syncs = $script:validations = $script:accountSyncs = 0
$script:lifecycleHeld = $script:historyHeld = $script:pending = $false
$lifecycleLease = $historyWriteLease = $null
function Set-LaunchRequest { $script:pending = $true }
function Clear-LaunchRequest { $script:pending = $false }
function Get-CodexAppExecutable { 'C:\fixture\ChatGPT.exe' }
function Assert-ChatGptDirectProfile {
    if ($script:stops) { throw 'Preflight happened after closing a working app' }
    $script:validations++
    if ($mode -eq 'invalid-profile') { throw 'Fixture invalid account profile' }
}
function Enter-AppLifecycleLock {
    param($LockPath, $WaitSeconds)
    $kind = if ($LockPath -like '*sync.lock') {'history'} else {'lifecycle'}
    if (($mode -eq 'sync-busy' -and $kind -eq 'history') -or
        ($mode -eq 'lifecycle-busy' -and $kind -eq 'lifecycle')) { return $null }
    if ($kind -eq 'history' -and -not $script:lifecycleHeld) { throw 'Lock order reversed' }
    if ($kind -eq 'history') { $script:historyHeld = $true } else { $script:lifecycleHeld = $true }
    $lease = [pscustomobject]@{kind=$kind}
    $lease | Add-Member ScriptMethod Unlock {
        param($offset,$length)
        if ($this.kind -eq 'history') { $script:historyHeld=$false } else { $script:lifecycleHeld=$false }
    }
    $lease | Add-Member ScriptMethod Dispose { }
    $lease
}
function Get-RequestedProfileApp {
    if ($mode -eq 'already-account') { [pscustomobject]@{Id=432;MainWindowHandle=123} }
    if ($mode -eq 'account-window-pending') { [pscustomobject]@{Id=432;MainWindowHandle=0} }
}
function Get-OwnedCodexAppProcesses {
    if ($mode -eq 'other-profile-unconfirmed') { [pscustomobject]@{Id=431;MainWindowHandle=123} }
}
function Stop-CodexApp {
    if (-not $script:validations -or -not $script:lifecycleHeld -or
        -not $script:historyHeld -or -not $script:pending) { throw 'Unsafe close before validation/locks' }
    $script:stops++
    if ($mode -eq 'app-refuses-close') { throw 'Fixture app is still running; no forced close' }
}
function Stop-CockpitTools { throw 'Account switching must not stop unrelated services' }
function Invoke-CanonicalHistorySync { $script:syncs++; throw 'Account switching must not use bidirectional history sync' }
function Invoke-ChatGptAccountHistorySync {
    if (-not $script:lifecycleHeld -or $script:historyHeld -or -not $script:stops) {
        throw 'Account history sync did not receive the safe lock handoff after normal app exit'
    }
    $script:accountSyncs++
    if ($mode -eq 'account-sync-fails') { throw 'Fixture account-only sync failed' }
    return $mode -ne 'account-sync-busy'
}
function Get-ActiveCodexAppBackends {
    if ($mode -eq 'backend-closing') { [pscustomobject]@{ProcessId=999} }
}
function Start-CodexAppForProfile {
    param($ExecutablePath,$SelectedProfile)
    if (-not $script:validations -or -not $script:lifecycleHeld -or -not $script:historyHeld) {
        throw 'Unsafe account launch'
    }
    $script:starts++
    432
}
function Set-ActiveProfileMarker { param($SelectedProfile,$AppProcessId) }
function Start-PostExitSyncWatcher { param($AppProcessId); throw 'Direct account path must not start a cross-profile history writer' }
function Show-ProfileMessage { param($Title,$Icon,$Message) }
function Write-ProfileLaunchResult {
    param($Status,$Phase,$AppProcessId,$ErrorText)
    @{status=$Status;phase=$Phase;starts=$script:starts;stops=$script:stops;
      syncs=$script:syncs;accountSyncs=$script:accountSyncs;validations=$script:validations} | ConvertTo-Json -Compress
}
'@
$cases = @(
    @{mode='cold';status='ready';starts=1;stops=1;accountSyncs=1},
    @{mode='account-sync-fails';status='error';starts=0;stops=1;accountSyncs=1},
    @{mode='account-sync-busy';status='deferred';starts=0;stops=1;accountSyncs=1},
    @{mode='invalid-profile';status='error';starts=0;stops=0},
    @{mode='other-profile-unconfirmed';status='error';starts=0;stops=0},
    @{mode='app-refuses-close';status='error';starts=0;stops=1},
    @{mode='already-account';status='ready';starts=0;stops=0},
    @{mode='account-window-pending';status='deferred';starts=0;stops=0},
    @{mode='backend-closing';status='deferred';starts=0;stops=1},
    @{mode='sync-busy';status='deferred';starts=0;stops=0},
    @{mode='lifecycle-busy';status='deferred';starts=0;stops=0}
)
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
foreach ($case in $cases) {
    $program = '$mode = ''' + $case.mode + "'`n" + $stubSource + "`n" + $profileFlow.Extent.Text
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($program))
    $lines = @(& $powershell -NoLogo -NoProfile -NonInteractive -EncodedCommand $encoded 2>&1)
    $result = $null
    try { $result = ([string]$lines[-1]) | ConvertFrom-Json } catch { }
    $expectedAccountSyncs = if ($case.accountSyncs) { $case.accountSyncs } else { 0 }
    if (-not $result -or $result.status -ne $case.status -or $result.starts -ne $case.starts -or
        $result.stops -ne $case.stops -or $result.syncs -ne 0 -or $result.validations -ne 1 -or
        $result.accountSyncs -ne $expectedAccountSyncs) {
        throw ('Account ' + $case.mode + ' fixture failed: ' + ($lines -join ' '))
    }
    Write-Output ('PASS account ' + $case.mode + ' preserves config, other services, and process safety')
}

& {
    . ([scriptblock]::Create((Get-ProductionFunction 'Get-RequestedProfileApp')))
    $ChatGptDirect = $true
    $Profile = 'Plus'
    $LaunchRequestId = 'new-click'
    $activeProfilePath = 'C:\fixture\active-profile.json'
    $launchStatusPath = 'C:\fixture\launch-status.json'
    $script:fixtureBirth = [datetime]'2026-01-01T00:00:00Z'
    $script:fixtureMarker = @{
        profile='Plus';app_process_id=432;app_creation_ticks=$script:fixtureBirth.ToUniversalTime().ToFileTimeUtc().ToString()
        request_id='previous-click';creation_request_id='previous-click'
    }
    function Get-Content { param([switch]$Raw,$Encoding,$LiteralPath,$ErrorAction); $script:fixtureMarker | ConvertTo-Json -Compress }
    function Get-OwnedCodexAppProcesses { [pscustomobject]@{Id=432;StartTime=$script:fixtureBirth;MainWindowHandle=123} }
    if (-not (Get-RequestedProfileApp)) { throw 'Existing verified account profile was not reused' }
    $ChatGptDirect = $false
    if (Get-RequestedProfileApp) { throw 'Legacy request identity guard changed' }
    $ChatGptDirect = $true
    $script:fixtureBirth = $script:fixtureBirth.AddMinutes(1)
    if (Get-RequestedProfileApp) { throw 'Recycled PID was accepted as the account profile' }
    $script:fixtureBirth = $script:fixtureBirth.AddMinutes(-1)
    $script:fixtureMarker.profile = 'Sota'
    if (Get-RequestedProfileApp) { throw 'SOTA instance was reused as account mode' }
    Write-Output 'PASS existing account reuse still requires profile, PID, and birth identity'
}
Write-Output 'All account-switch fixtures passed.'
