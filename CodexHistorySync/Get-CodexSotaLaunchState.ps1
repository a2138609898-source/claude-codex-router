# Read-only preflight: import exactly three process-discovery functions from the
# launcher AST. Never evaluate its top-level validation, restart or sync code.
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot 'Switch-CodexSota.ps1'), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw $parseErrors[0].Message }
$names = @('Get-CodexAppExecutable', 'Get-OwnedCodexAppProcesses', 'Get-ActiveSotaApp')
foreach ($name in $names) {
    $function = $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}, $true) | Select-Object -First 1
    if (-not $function) { throw "Missing process discovery function: $name" }
    . ([scriptblock]::Create($function.Extent.Text))
}
$activeProfilePath = Join-Path $PSScriptRoot 'active-profile.json'
$timer = [Diagnostics.Stopwatch]::StartNew()
$executable = Get-CodexAppExecutable
$owned = @(Get-OwnedCodexAppProcesses)
$app = Get-ActiveSotaApp
[ordered]@{
    status = if ($app -and $app.MainWindowHandle -ne 0) { 'existing_sota_window' } elseif ($owned.Count) { 'existing_unverified_profile' } else { 'not_running' }
    app_executable = $executable
    owned_process_count = $owned.Count
    app_process_id = if ($app) { $app.Id } else { $null }
    app_creation_ticks = if ($app) { $app.StartTime.ToUniversalTime().ToFileTimeUtc().ToString() } else { $null }
    window_owned = [bool]($app -and $app.MainWindowHandle -ne 0)
    elapsed_seconds = [Math]::Round($timer.Elapsed.TotalSeconds, 3)
} | ConvertTo-Json -Compress
