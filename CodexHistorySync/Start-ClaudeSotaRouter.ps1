param(
    [switch]$AuditOnly,
    [switch]$Stop,
    [string]$SotaRootOverride,
    [int]$RouterPortOverride,
    [string]$RouterScriptOverride,
    [string]$PythonExecutableOverride
)

# Thin shim: the Claude router is the same script against the .claude-sota root on 17994.
$starter = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'Start-CodexSotaRouter.ps1'
$arguments = @{ Workspace = 'claude' }
if ($AuditOnly) { $arguments['AuditOnly'] = $true }
if ($Stop) { $arguments['Stop'] = $true }
if ($SotaRootOverride) { $arguments['SotaRootOverride'] = $SotaRootOverride }
if ($RouterPortOverride -gt 0) { $arguments['RouterPortOverride'] = $RouterPortOverride }
if ($RouterScriptOverride) { $arguments['RouterScriptOverride'] = $RouterScriptOverride }
if ($PythonExecutableOverride) { $arguments['PythonExecutableOverride'] = $PythonExecutableOverride }
& $starter @arguments
exit $LASTEXITCODE
