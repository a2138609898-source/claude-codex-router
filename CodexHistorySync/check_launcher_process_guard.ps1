# Regression guard for the launch failure "Cannot bind argument to parameter 'Id'
# because it is an empty array."
#
# Get-Process -Id @() fails during PARAMETER BINDING. -ErrorAction SilentlyContinue only
# affects errors a cmdlet writes after binding succeeds, so it cannot suppress this one.
# Under $ErrorActionPreference = 'Stop' the binding failure is terminating, which aborted
# every cold start of the Codex App (Stop-CodexApp runs before the App is launched, so the
# owned-process list is legitimately empty at that moment).
#
# This check extracts Get-OwnedCodexAppProcesses from each launcher via the PowerShell AST
# and invokes it against a stubbed, empty process table. No real process is touched.

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$targets = @(
    'Switch-CodexSota.ps1'
    'Switch-CodexProfile.ps1'
    'Run-CodexHistorySync.ps1'
)

function Get-FunctionSource {
    param([string]$Path, [string]$Name)

    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
    if ($errors -and $errors.Count -gt 0) {
        throw "$([IO.Path]::GetFileName($Path)) does not parse: $($errors[0].Message)"
    }
    $match = $ast.FindAll(
        {
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $Name
        },
        $true
    ) | Select-Object -First 1
    if (-not $match) {
        throw "$([IO.Path]::GetFileName($Path)) has no function named $Name"
    }
    return $match.Extent.Text
}

function Test-EmptyProcessTable {
    param([string]$Source, [int]$FakeProcessCount)

    # Stubs shadow the real cmdlets: PowerShell resolves functions before cmdlets.
    # [CmdletBinding()] makes -ErrorAction bind as a common parameter instead of colliding.
    function Get-CodexAppExecutable { return (Join-Path $env:SystemRoot 'System32\cmd.exe') }
    function Get-CimInstance {
        [CmdletBinding()]
        param([Parameter(Position = 0)]$ClassName, $Filter)
        if ($FakeProcessCount -le 0) { return @() }
        return @(1..$FakeProcessCount | ForEach-Object {
            [pscustomobject]@{
                ProcessId      = $PID
                ExecutablePath = (Join-Path $env:SystemRoot 'System32\cmd.exe')
            }
        })
    }

    $ErrorActionPreference = 'Stop'
    . ([scriptblock]::Create($Source))
    return @(Get-OwnedCodexAppProcesses)
}

function Remove-EmptyIdGuard {
    # Negative control. Rather than hand-writing an approximation of the old code, this deletes
    # exactly the guard being tested out of the real current source, so a green result from the
    # check above cannot be a false positive. Derived from the file, never retyped.
    param([string]$Source)

    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($Source, [ref]$tokens, [ref]$errors)
    $guard = $ast.FindAll(
        {
            param($node)
            $node -is [System.Management.Automation.Language.IfStatementAst] -and
            $node.Clauses[0].Item1.Extent.Text -match '\$ownedIds\.Count\s*-eq\s*0'
        },
        $true
    ) | Select-Object -First 1
    if (-not $guard) {
        throw 'the $ownedIds emptiness guard is not present in the source at all'
    }
    $start = $guard.Extent.StartOffset
    $end = $guard.Extent.EndOffset
    return $Source.Substring(0, $start) + $Source.Substring($end)
}

$failures = 0
foreach ($name in $targets) {
    $path = Join-Path $here $name
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Host ("FAIL  {0,-28} file not found" -f $name)
        $failures++
        continue
    }

    try {
        $source = Get-FunctionSource -Path $path -Name 'Get-OwnedCodexAppProcesses'
    }
    catch {
        Write-Host ("FAIL  {0,-28} {1}" -f $name, $_.Exception.Message)
        $failures++
        continue
    }

    # Case 1: nothing running. This is the cold start that used to blow up.
    try {
        $empty = Test-EmptyProcessTable -Source $source -FakeProcessCount 0
        if ($empty.Count -ne 0) {
            Write-Host ("FAIL  {0,-28} empty table returned {1} processes" -f $name, $empty.Count)
            $failures++
            continue
        }
    }
    catch {
        Write-Host ("FAIL  {0,-28} empty table threw: {1}" -f $name, $_.Exception.Message)
        $failures++
        continue
    }

    # Case 2: one owned process. Proves the guard did not short-circuit the real lookup.
    try {
        $one = Test-EmptyProcessTable -Source $source -FakeProcessCount 1
        if ($one.Count -ne 1) {
            Write-Host ("FAIL  {0,-28} one owned process returned {1}" -f $name, $one.Count)
            $failures++
            continue
        }
    }
    catch {
        Write-Host ("FAIL  {0,-28} one owned process threw: {1}" -f $name, $_.Exception.Message)
        $failures++
        continue
    }

    # Case 3: negative control. With the guard deleted the same call must blow up, otherwise
    # cases 1 and 2 prove nothing.
    try {
        $unguarded = Remove-EmptyIdGuard -Source $source
        $leaked = Test-EmptyProcessTable -Source $unguarded -FakeProcessCount 0
        Write-Host ("FAIL  {0,-28} guard removed but empty table still returned {1} (check is toothless)" -f $name, $leaked.Count)
        $failures++
        continue
    }
    catch [System.Management.Automation.ParameterBindingException] {
        # Expected: this is the original crash.
    }
    catch {
        Write-Host ("FAIL  {0,-28} negative control could not run: {1}" -f $name, $_.Exception.Message)
        $failures++
        continue
    }

    Write-Host ("PASS  {0,-28} empty -> 0, one owned -> 1, unguarded -> throws" -f $name)
}

Write-Host ''
if ($failures -gt 0) {
    Write-Host ("{0} launcher(s) still crash on an empty process table." -f $failures)
    exit 1
}
Write-Host ("All {0} launchers survive an empty ChatGPT.exe process table." -f $targets.Count)
exit 0
