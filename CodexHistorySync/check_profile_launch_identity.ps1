# Native file identity plus real production functions with synthetic processes.
# Never launch/stop a real app, load user profiles, or run history maintenance.
param([string]$BaselineSourcePath)
$ErrorActionPreference = 'Stop'
$profileSourcePath = Join-Path $PSScriptRoot 'Switch-CodexProfile.ps1'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($profileSourcePath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw $parseErrors[0].Message }
$functions = @($ast.EndBlock.Statements | Where-Object { $_ -is [Management.Automation.Language.FunctionDefinitionAst] })
$functionSource = ($functions | ForEach-Object { $_.Extent.Text }) -join "`n"
$script:checksPassed = 0
function Assert-Check {
    param([bool]$Condition, [string]$Name)
    if (-not $Condition) { throw ('FAIL ' + $Name) }
    $script:checksPassed++
    Write-Output ('PASS ' + $Name)
}
function Test-Rejected {
    param([scriptblock]$Action)
    try { $null = & $Action; return $false } catch { $script:fixtureLastRejection = $_.Exception.Message; return $true }
}

$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$fixtureRoot = Join-Path $temporaryRoot ('codex-profile-identity-' + [guid]::NewGuid().ToString('N'))
$realDirectory = Join-Path $fixtureRoot 'real'
$aliasDirectory = Join-Path $fixtureRoot 'alias'
$otherDirectory = Join-Path $fixtureRoot 'other'
$fixtureExe = Join-Path $realDirectory 'ChatGPT.exe'
$aliasExe = Join-Path $aliasDirectory 'ChatGPT.exe'
$otherExe = Join-Path $otherDirectory 'ChatGPT.exe'
$null = New-Item -ItemType Directory -Path $realDirectory,$otherDirectory
# Non-executable fixture contents: same bytes in different files are not identity.
[IO.File]::WriteAllText($fixtureExe, 'not an executable; identity fixture')
[IO.File]::WriteAllText($otherExe, 'not an executable; identity fixture')
$null = New-Item -ItemType Junction -Path $aliasDirectory -Target $realDirectory
try {
    & {
        . ([scriptblock]::Create($functionSource))
        Assert-Check (Test-CodexExecutableIdentity $fixtureExe $aliasExe) 'junction and target are the same Windows file'
        Assert-Check (Test-CodexExecutableIdentity $aliasExe $fixtureExe) 'junction identity is symmetric'
        Assert-Check (Test-CodexExecutableIdentity $fixtureExe $fixtureExe.ToUpperInvariant()) 'case differences retain identity'
        Assert-Check (-not (Test-CodexExecutableIdentity $fixtureExe $otherExe)) 'same name and bytes in another file are rejected'
        Assert-Check (-not (Test-CodexExecutableIdentity $fixtureExe (Join-Path $fixtureRoot 'missing.exe'))) 'missing alias is rejected'
        Assert-Check (-not (Test-CodexExecutableIdentity $fixtureExe '')) 'unreadable process image is rejected'

        function Get-CodexAppExecutable { $fixtureExe }
        function Get-CimInstance { param($ClassName,$Filter,$ErrorAction); $script:fixtureRows }
        function Get-Process { param($Id,$ErrorAction); foreach ($value in $Id) { [pscustomobject]@{Id=$value} } }
        $script:fixtureRows = @()
        Assert-Check (@(Get-OwnedCodexAppProcesses).Count -eq 0) 'empty process table remains a safe cold start'
        $script:fixtureRows = @([pscustomobject]@{ProcessId=701;ExecutablePath=$aliasExe})
        Assert-Check (@(Get-OwnedCodexAppProcesses)[0].Id -eq 701) 'process discovery accepts the real alias'
        $script:fixtureRows = @([pscustomobject]@{ProcessId=701;ExecutablePath=$fixtureExe},[pscustomobject]@{ProcessId=702;ExecutablePath=$aliasExe})
        Assert-Check (@(Get-OwnedCodexAppProcesses).Count -eq 2) 'main and helper through different aliases remain owned'
        $script:fixtureRows = @([pscustomobject]@{ProcessId=701;ExecutablePath=$fixtureExe},[pscustomobject]@{ProcessId=703;ExecutablePath=$otherExe})
        Assert-Check (Test-Rejected { Get-OwnedCodexAppProcesses }) 'unknown same-name program still blocks destructive switching'
        Assert-Check (Test-Rejected { Stop-CodexApp }) 'normal close cannot reach an unverified process'
        $script:fixtureRows = @([pscustomobject]@{ProcessId=704;ExecutablePath=$null})
        Assert-Check (Test-Rejected { Get-OwnedCodexAppProcesses }) 'unreadable process image remains fail-closed'

        if ($BaselineSourcePath) {
            $baselineAst = [Management.Automation.Language.Parser]::ParseFile($BaselineSourcePath,[ref]$tokens,[ref]$parseErrors)
            $oldGuard = $baselineAst.Find({param($node)
                $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-OwnedCodexAppProcesses'
            }, $false)
            . ([scriptblock]::Create($oldGuard.Extent.Text))
            $script:fixtureRows = @([pscustomobject]@{ProcessId=701;ExecutablePath=$aliasExe})
            Assert-Check (Test-Rejected { Get-OwnedCodexAppProcesses }) 'negative control: original discovery rejects the same real alias'
        }
    }

    $startCases = @(
        @{mode='ready-with-foreign-helper';pending=$false;error=$false},
        @{mode='alias-image';pending=$false;error=$false},
        @{mode='marker-fails';pending=$false;error=$false;warning=$true},
        @{mode='pending-window';pending=$true;error=$false},
        @{mode='inspection-fails';pending=$true;error=$false;warning=$true},
        @{mode='window-inspection-fails';pending=$true;error=$false;warning=$true},
        @{mode='pid-recycled';pending=$false;error=$true},
        @{mode='exited-with-foreign-window';pending=$false;error=$true},
        @{mode='image-changed';pending=$false;error=$true},
        @{mode='wrong-spawn-image';pending=$false;error=$true}
    )
    foreach ($case in $startCases) {
        & {
            . ([scriptblock]::Create($functionSource))
            $Profile = 'Plus'; $ChatGptDirect = $true; $LaunchRequestId = 'fixture-start'
            $profileRoots = @{Plus=$fixtureRoot}
            $apiEnvironmentNames = @()
            $WindowReadyTimeoutSeconds = 0
            $script:profileLaunchWarnings = @(); $script:profileLaunchIdentity = $null
            $script:launchWindowPending = $false
            $script:fixtureStarts = $script:fixtureMarkerAttempts = $script:fixtureReceipts = 0
            $script:fixtureWindowReads = 0
            $script:fixtureMode = $case.mode
            $script:fixtureBirth = [datetime]'2026-01-01T00:00:00Z'
            $startPath = if ($case.mode -eq 'alias-image') { $aliasExe } elseif ($case.mode -eq 'wrong-spawn-image') { $otherExe } else { $fixtureExe }
            $script:fixtureApp = [pscustomobject]@{Id=701;StartTime=$script:fixtureBirth;Path=$startPath}
            $script:fixtureApp | Add-Member ScriptProperty MainWindowHandle {
                $script:fixtureWindowReads++
                if ($script:fixtureMode -eq 'window-inspection-fails') { throw 'fixture window probe unavailable' }
                if ($script:fixtureMode -eq 'pending-window') { return 0 }
                return 123
            }
            function Test-Path { param($LiteralPath,$PathType); $true }
            function Get-ProfileAuthMode { param($Root); 'chatgpt' }
            function Test-ProfileConfig { param($Root,$SelectedProfile); $true }
            function Invoke-ProfileStoreLaunch { param($StartInfo); $script:fixtureStarts++; $script:fixtureApp }
            function Get-CimInstance { throw 'REGRESSION: global process enumeration after spawning the app' }
            function Get-OwnedCodexAppProcesses { throw 'REGRESSION: unrelated helper invalidated a successful spawn' }
            function Get-Process {
                param($Id,$ErrorAction)
                if ($Id -ne 701) { throw 'REGRESSION: adopted a different process' }
                if ($script:fixtureMode -eq 'inspection-fails') { throw 'fixture process probe unavailable' }
                if ($script:fixtureMode -eq 'exited-with-foreign-window') { return $null }
                if ($script:fixtureMode -eq 'pid-recycled') { return [pscustomobject]@{Id=701;StartTime=$script:fixtureBirth.AddSeconds(1);Path=$fixtureExe;MainWindowHandle=999} }
                if ($script:fixtureMode -eq 'image-changed') { return [pscustomobject]@{Id=701;StartTime=$script:fixtureBirth;Path=$otherExe;MainWindowHandle=999} }
                $script:fixtureApp
            }
            function Set-ActiveProfileMarker {
                param($SelectedProfile,$AppProcessId,$ExpectedCreationTicks)
                if ($script:fixtureWindowReads -ne 0 -or $AppProcessId -ne 701 -or -not $ExpectedCreationTicks) { throw 'REGRESSION: marker not captured before window polling' }
                $script:fixtureMarkerAttempts++
                if ($script:fixtureMode -eq 'marker-fails') { throw 'fixture marker write denied' }
            }
            function Write-ProfileLaunchResult {
                param($Status,$Phase,$AppProcessId,[switch]$Quiet)
                if ($script:fixtureMarkerAttempts -ne 1 -or $AppProcessId -ne 701 -or -not $Quiet -or $Phase -ne 'waiting_for_window') { throw 'REGRESSION: missing quiet post-spawn receipt' }
                $script:fixtureReceipts++
            }
            function Start-Sleep { param($Milliseconds) }
            function Stop-CodexApp { throw 'REGRESSION: post-spawn cleanup closed a live app' }
            function Stop-Process { throw 'REGRESSION: post-spawn cleanup killed a process' }
            $caught = $false; $result = $null
            try { $result = @(Start-CodexAppForProfile -ExecutablePath $fixtureExe -SelectedProfile Plus) }
            catch { $caught = $true }
            $valid = $caught -eq $case.error -and $script:fixtureStarts -eq 1
            if (-not $case.error) {
                $valid = $valid -and $result.Count -eq 1 -and $result[0] -eq 701 -and
                    $script:launchWindowPending -eq $case.pending -and $script:fixtureReceipts -eq 1 -and $script:fixtureMarkerAttempts -eq 1
            }
            if ($case.warning) { $valid = $valid -and $script:profileLaunchWarnings.Count -gt 0 }
            if ($case.mode -eq 'wrong-spawn-image') { $valid = $valid -and $script:fixtureReceipts -eq 0 -and $script:fixtureMarkerAttempts -eq 0 }
            Assert-Check $valid ('post-spawn ' + $case.mode + ' keeps one identity and never stops/adopts another app')

            if ($BaselineSourcePath -and $case.mode -eq 'ready-with-foreign-helper') {
                $baselineAst = [Management.Automation.Language.Parser]::ParseFile($BaselineSourcePath,[ref]$tokens,[ref]$parseErrors)
                $oldStart = $baselineAst.Find({param($node)
                    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Start-CodexAppForProfile'
                }, $false)
                # The public baseline also supports an explicit portable override.
                # Keep its external process dependency isolated in this negative control.
                function Test-ExplicitPortableCodexApp { param($ExecutablePath); return $false }
                $oldText = $oldStart.Extent.Text
                $oldCall = '$startedProcess = & $storeLauncher -StartInfo $startInfo'
                if (-not $oldText.Contains($oldCall)) { throw 'Original external Store call not found; cannot isolate negative control' }
                $oldText = $oldText.Replace('$storeLauncher = Join-Path $PSScriptRoot ''Start-CodexStoreProcess.ps1''', '$storeLauncher = $null')
                # Substitute only the external process-creation dependency. The
                # original post-spawn polling and error behavior run unchanged.
                . ([scriptblock]::Create($oldText.Replace($oldCall, '$startedProcess = Invoke-ProfileStoreLaunch -StartInfo $startInfo')))
                $script:fixtureStarts = $script:fixtureMarkerAttempts = $script:fixtureReceipts = 0
                $rejected = Test-Rejected { Start-CodexAppForProfile -ExecutablePath $fixtureExe -SelectedProfile Plus }
                if (-not $rejected -or $script:fixtureStarts -ne 1) { throw "Negative control setup failed: rejected=$rejected starts=$script:fixtureStarts error=$script:fixtureLastRejection" }
                Assert-Check ($rejected -and $script:fixtureStarts -eq 1 -and $script:fixtureMarkerAttempts -eq 0 -and $script:fixtureReceipts -eq 0) 'negative control: original startup fails after spawn and loses its marker/receipt'
            }
        }
    }

    & {
        . ([scriptblock]::Create($functionSource))
        $Profile='Plus'; $ChatGptDirect=$true; $LaunchRequestId='fixture-receipt'
        $AuditOnly=$PrepareOnly=$false
        $lifecycleWorkDir = Join-Path $fixtureRoot 'work'
        $launchStatusPath = Join-Path $lifecycleWorkDir 'launch-status-fixture.json'
        $activeProfilePath = Join-Path $fixtureRoot 'active-profile.json'
        $script:profileLaunchWarnings=@(); $script:profileLaunchIdentity=$null
        $script:accountHistorySyncResult=$null
        $script:fixtureBirth=[datetime]'2026-01-01T00:00:00Z'
        $script:fixtureApp=[pscustomobject]@{Id=701;StartTime=$script:fixtureBirth;Path=$aliasExe;MainWindowHandle=0}
        $script:fixtureProbe='ok'
        function Get-CodexAppExecutable { $fixtureExe }
        function Get-CimInstance { throw 'REGRESSION: receipt recovery enumerated unrelated processes' }
        function Get-Process {
            param($Id,$ErrorAction)
            if ($Id -eq $PID) { return [pscustomobject]@{Id=$PID;StartTime=$script:fixtureBirth} }
            if ($Id -ne 701) { throw 'Unexpected fixture PID' }
            if ($script:fixtureProbe -eq 'unavailable') { throw 'fixture targeted query unavailable' }
            if ($script:fixtureProbe -eq 'gone') { return $null }
            $script:fixtureApp
        }
        Set-ProfileLaunchIdentity -App $script:fixtureApp
        $birthTicks = $script:profileLaunchIdentity.app_creation_ticks
        Set-ActiveProfileMarker -SelectedProfile Plus -AppProcessId 701 -ExpectedCreationTicks $birthTicks
        $marker = Get-Content -Raw -LiteralPath $activeProfilePath | ConvertFrom-Json
        Assert-Check ($marker.app_creation_ticks -eq $birthTicks -and $marker.app_process_id -eq 701 -and $marker.app_executable -eq $aliasExe) 'real marker stores process birth and executable identity'
        $script:fixtureApp.StartTime=$script:fixtureBirth.AddSeconds(1)
        $markerBefore=Get-Content -Raw -LiteralPath $activeProfilePath
        $rejected=Test-Rejected { Set-ActiveProfileMarker -SelectedProfile Plus -AppProcessId 701 -ExpectedCreationTicks $birthTicks }
        Assert-Check ($rejected -and (Get-Content -Raw -LiteralPath $activeProfilePath) -ceq $markerBefore) 'recycled PID cannot overwrite a saved marker'
        $script:fixtureApp.StartTime=$script:fixtureBirth
        $quietOutput = @(Write-ProfileLaunchResult -Status starting -Phase waiting_for_window -AppProcessId 701 -Quiet)
        $receipt = Get-Content -Raw -LiteralPath $launchStatusPath | ConvertFrom-Json
        Assert-Check ($quietOutput.Count -eq 0 -and $receipt.app_process_id -eq 701 -and $receipt.app_creation_ticks -eq $birthTicks -and -not $receipt.window_owned) 'quiet real receipt persists pending identity without polluting the return value'
        $activeProfilePath = Join-Path $fixtureRoot 'missing-marker.json'
        $recovered = Get-RequestedProfileApp
        Assert-Check ($recovered.Id -eq 701) 'same request recovers an owned GUI from the independent receipt'
        $script:fixtureApp.StartTime=$script:fixtureBirth.AddSeconds(1)
        Assert-Check (-not (Get-RequestedProfileApp)) 'receipt recovery rejects a recycled PID'
        $script:fixtureApp.StartTime=$script:fixtureBirth
        $Profile='Sota'
        Assert-Check (-not (Get-RequestedProfileApp)) 'account receipt cannot be adopted as SOTA'
        $Profile='Plus'
        $script:fixtureProbe='unavailable'
        Write-ProfileLaunchResult -Status deferred -Phase waiting_for_window -AppProcessId 701 -Quiet
        $receipt=Get-Content -Raw -LiteralPath $launchStatusPath | ConvertFrom-Json
        Assert-Check ($receipt.app_process_id -eq 701 -and $receipt.app_creation_ticks -eq $birthTicks -and -not $receipt.window_owned -and $receipt.warnings.Count -gt 0) 'inspection failure preserves identity but never claims a verified window'
        $script:fixtureProbe='gone'
        Write-ProfileLaunchResult -Status error -Phase opening_desktop -AppProcessId 701 -ErrorText 'fixture exit' -Quiet
        $receipt=Get-Content -Raw -LiteralPath $launchStatusPath | ConvertFrom-Json
        Assert-Check ($receipt.status -eq 'error' -and $receipt.app_creation_ticks -eq $birthTicks -and -not $receipt.window_owned) 'real exit retains diagnostic receipt without adopting a replacement'
    }
    $stopSource = ($functions | Where-Object Name -eq 'Stop-CodexApp').Extent.Text
    Assert-Check ($stopSource -match '\.CloseMainWindow\(' -and $stopSource -notmatch 'Stop-Process\s|\.Kill\(|taskkill') 'shutdown remains normal-close only'
    Write-Output ('All ' + $script:checksPassed + ' isolated profile identity checks passed.')
}
finally {
    # Delete only our unique fixture; unlink the junction without traversing it.
    $resolvedFixture = [IO.Path]::GetFullPath($fixtureRoot)
    if (-not $resolvedFixture.StartsWith($temporaryRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($resolvedFixture) -notlike 'codex-profile-identity-*') { throw 'Unsafe fixture cleanup path' }
    if ([IO.Directory]::Exists($aliasDirectory)) { [IO.Directory]::Delete($aliasDirectory) }
    Remove-Item -LiteralPath $resolvedFixture -Recurse -Force
}
