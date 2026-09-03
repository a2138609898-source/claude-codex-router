param(
    [string]$CodexExecutable = '',
    [switch]$Force,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
$sotaRoot = Join-Path $env:USERPROFILE '.codex-sota'
$authPath = Join-Path $sotaRoot 'auth.json'
$apiEnvironmentNames = @(
    'OPENAI_API_KEY',
    'CODEX_API_KEY',
    'OPENAI_API_BASE',
    'OPENAI_BASE_URL',
    'OPENAI_ORG_ID',
    'OPENAI_PROJECT_ID',
    'CODEX_ACCESS_TOKEN'
)

function Test-ApiKeyShape {
    param([object]$Value)

    if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace($Value)) {
        return $false
    }
    return $Value -notmatch '(?i)Get-Clipboard|codex-sota|--with-api-key|[|\r\n]'
}

function Test-ExistingSotaAuth {
    if (-not (Test-Path -LiteralPath $authPath)) {
        return $false
    }
    try {
        $auth = Get-Content -Raw -Encoding UTF8 -LiteralPath $authPath | ConvertFrom-Json
        return $auth.auth_mode -eq 'apikey' -and (Test-ApiKeyShape -Value $auth.OPENAI_API_KEY)
    }
    catch {
        return $false
    }
}

if ((Test-ExistingSotaAuth) -and -not $Force) {
    exit 0
}

if ($NonInteractive) {
    throw 'True SOTA API key is missing or invalid, and this run cannot prompt for it. Open PowerShell, run codex-sota, and paste the key when prompted.'
}

if (-not $CodexExecutable) {
    $codexCommand = Get-Command 'codex.exe' -ErrorAction SilentlyContinue
    if ($codexCommand) {
        $CodexExecutable = $codexCommand.Source
    }
}
if (-not $CodexExecutable -or -not (Test-Path -LiteralPath $CodexExecutable)) {
    throw "Codex CLI executable was not found: $CodexExecutable"
}

$env:CODEX_HOME = $sotaRoot
foreach ($name in $apiEnvironmentNames) {
    Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
}

Write-Host 'True SOTA needs its own API key.'
Write-Host 'Paste only the key itself. Do not paste a PowerShell command, quotes, or the word Bearer.'
$secureKey = Read-Host -Prompt 'True SOTA API Key' -AsSecureString
$keyPointer = [IntPtr]::Zero
$plainKey = $null
try {
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    if (-not (Test-ApiKeyShape -Value $plainKey)) {
        throw 'The input looks like a command or an empty value, not an API key. Nothing was saved.'
    }

    $plainKey | & $CodexExecutable login --with-api-key
    $loginExitCode = $LASTEXITCODE
    if ($loginExitCode -ne 0) {
        throw "Codex API-key login failed with exit code $loginExitCode."
    }
}
finally {
    $plainKey = $null
    $secureKey = $null
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
}

if (-not (Test-ExistingSotaAuth)) {
    throw 'Codex did not persist a structurally valid True SOTA API-key login.'
}

Write-Host 'True SOTA API key saved to the isolated profile.'
exit 0
