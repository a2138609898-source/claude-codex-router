# Shared, side-effect-free bundle checks. Dot-sourcing this file only defines functions;
# only an explicit New-ArtifactManifest call writes anything.

function Assert-ScopedArtifactPath([string]$Path, [string]$ScopeRoot) {
    $scope = [IO.Path]::GetFullPath($ScopeRoot).TrimEnd('\', '/')
    $absolute = [IO.Path]::GetFullPath($Path)
    $prefix = $scope + [IO.Path]::DirectorySeparatorChar
    if (-not $absolute.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Artifact path is outside its owned directory: $absolute"
    }
    $cursor = $absolute
    while ($cursor) {
        # Get-Item -Force also sees a dangling link that Test-Path may report as absent.
        $ancestor = Get-Item -LiteralPath $cursor -Force -ErrorAction SilentlyContinue
        if ($null -ne $ancestor -and ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Linked artifact paths are not supported: $cursor"
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    return $absolute
}

function Get-ArtifactSha256([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
        finally { $stream.Dispose() }
    }
    finally { $sha.Dispose() }
}

function ConvertTo-ArtifactHashMap($Value) {
    if ($null -eq $Value) { throw 'Artifact file hashes are missing.' }
    $result = @{}
    $entries = if ($Value -is [System.Collections.IDictionary]) {
        @($Value.GetEnumerator() | ForEach-Object { [pscustomobject]@{ Name = $_.Key; Value = $_.Value } })
    } else { @($Value.PSObject.Properties) }
    foreach ($entry in $entries) {
        $name = [string]$entry.Name
        $hash = [string]$entry.Value
        if (-not $name -or $name.Contains('\') -or $name.Contains(':') -or $name.StartsWith('/') -or
            @($name.Split('/') | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' }).Count -gt 0) {
            throw "Invalid relative artifact filename: $name"
        }
        if ($hash -notmatch '^[0-9a-fA-F]{64}$' -or $result.ContainsKey($name)) {
            throw "Invalid or duplicate artifact hash: $name"
        }
        $result[$name] = $hash.ToLowerInvariant()
    }
    if ($result.Count -eq 0) { throw 'Artifact file hashes are empty.' }
    return $result
}

function Assert-ArtifactHashSet($Actual, $Expected) {
    $expectedMap = ConvertTo-ArtifactHashMap $Expected
    if ($Actual.Count -ne $expectedMap.Count) { throw 'Artifact file inventory changed.' }
    foreach ($name in $expectedMap.Keys) {
        if (-not $Actual.ContainsKey($name) -or $Actual[$name] -ne $expectedMap[$name]) {
            throw "Artifact file is missing or changed: $name"
        }
    }
}

function Assert-ArtifactRuntime([string]$ArtifactRoot, $Hashes) {
    # Match the Python verifier's required runtime roles. Hashing the whole tree below
    # protects additional DLLs/data too; these checks stop an incomplete build being signed
    # off merely because all files that happen to remain have valid hashes.
    $required = @(
        'codex-sota.exe', '_internal/base_library.zip', '_internal/_tkinter.pyd',
        '_internal/_ctypes.pyd', '_internal/_ssl.pyd', '_internal/_socket.pyd',
        '_internal/_sqlite3.pyd', '_internal/sqlite3.dll', '_internal/vcruntime140.dll'
    )
    foreach ($name in $required) {
        if (-not $Hashes.ContainsKey($name)) { throw "Required artifact runtime file is missing: $name" }
    }
    $roles = @(
        '^_internal/python3(1[1-9]|[2-9][0-9])t?(_d)?\.dll$',
        '^_internal/tcl[89][0-9a-z]*\.dll$', '^_internal/tk[89][0-9a-z]*\.dll$',
        '^_internal/libssl[^/]*\.dll$', '^_internal/libcrypto[^/]*\.dll$',
        '^_internal/(_tcl_data|tcl[89][^/]*)/init\.tcl$',
        '^_internal/(_tk_data|tk[89][^/]*)/tk\.tcl$'
    )
    foreach ($role in $roles) {
        $roleFiles = @($Hashes.Keys | Where-Object { $_ -match $role })
        if ($roleFiles.Count -eq 0) { throw "Required artifact runtime role is missing: $role" }
        $required += $roleFiles
    }
    foreach ($name in @($required | Select-Object -Unique)) {
        $path = Assert-ScopedArtifactPath (Join-Path $ArtifactRoot $name) $ArtifactRoot
        if ((Get-Item -LiteralPath $path).Length -eq 0) { throw "Required artifact runtime file is empty: $name" }
        if ($name -match '\.(exe|dll|pyd)$') {
            $stream = [IO.File]::OpenRead($path)
            try {
                if ($stream.ReadByte() -ne 77 -or $stream.ReadByte() -ne 90) {
                    throw "Required artifact binary has no PE signature: $name"
                }
            }
            finally { $stream.Dispose() }
        }
    }
}

function Get-ArtifactPayloadHashes([string]$ArtifactRoot) {
    $artifact = [IO.Path]::GetFullPath($ArtifactRoot).TrimEnd('\', '/')
    $null = Assert-ScopedArtifactPath $artifact ([IO.Path]::GetDirectoryName($artifact))
    if (-not (Test-Path -LiteralPath $artifact -PathType Container)) { throw "Artifact directory is missing: $artifact" }
    if ((Get-Item -LiteralPath $artifact -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Linked artifact directories are not supported: $artifact"
    }
    $prefix = $artifact + [IO.Path]::DirectorySeparatorChar
    $pending = [System.Collections.Generic.Stack[string]]::new()
    $pending.Push($artifact)
    $hashes = @{}
    while ($pending.Count -gt 0) {
        foreach ($item in @(Get-ChildItem -LiteralPath ($pending.Pop()) -Force -ErrorAction Stop)) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Linked artifact entries are not supported: $($item.FullName)"
            }
            if ($item.PSIsContainer) { $pending.Push($item.FullName); continue }
            $relative = $item.FullName.Substring($prefix.Length).Replace('\', '/')
            if ($relative -eq 'codex-sota-artifact-manifest.json') { continue }
            if ($hashes.ContainsKey($relative)) { throw "Duplicate artifact filename: $relative" }
            $hashes[$relative] = Get-ArtifactSha256 $item.FullName
        }
    }
    Assert-ArtifactRuntime $artifact $hashes
    return $hashes
}

function New-ArtifactManifest([string]$ArtifactRoot) {
    $artifact = [IO.Path]::GetFullPath($ArtifactRoot)
    $hashes = Get-ArtifactPayloadHashes $artifact
    $manifest = Assert-ScopedArtifactPath (Join-Path $artifact 'codex-sota-artifact-manifest.json') $artifact
    $temporary = Assert-ScopedArtifactPath (Join-Path $artifact ('.artifact-' + [guid]::NewGuid().ToString('N') + '.tmp')) $artifact
    $content = [ordered]@{ schema_version = 1; files = $hashes } | ConvertTo-Json -Depth 5
    try {
        [IO.File]::WriteAllText($temporary, $content + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $manifest -Force -ErrorAction Stop
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force -ErrorAction Stop }
    }
    return $manifest
}

function Assert-ArtifactBundle([string]$ArtifactRoot, $ExpectedFiles = $null) {
    $artifact = [IO.Path]::GetFullPath($ArtifactRoot)
    $manifestPath = Assert-ScopedArtifactPath (Join-Path $artifact 'codex-sota-artifact-manifest.json') $artifact
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'Artifact manifest is missing; rebuild or rerun full artifact verification before installing.'
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema_version -ne 1) { throw 'Unsupported artifact manifest version.' }
    $hashes = Get-ArtifactPayloadHashes $artifact
    Assert-ArtifactHashSet $hashes $manifest.files
    # Include the manifest itself when binding a verification report or a transaction's
    # first snapshot, so a replacement manifest cannot bless replacement DLLs later.
    $hashes['codex-sota-artifact-manifest.json'] = Get-ArtifactSha256 $manifestPath
    if ($null -ne $ExpectedFiles) { Assert-ArtifactHashSet $hashes $ExpectedFiles }
    return $hashes
}

function Read-ArtifactVerification([string]$ReportPath, [string]$ArtifactRoot, [string]$SourceRoot) {
    $report = Get-Content -LiteralPath $ReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($report.verification_schema -ne 2 -or $report.status -ne 'passed' -or
        $report.validation_scope -ne 'full-bundle-and-regressions' -or
        -not ($report.sources_unchanged_during_tests -is [bool]) -or -not $report.sources_unchanged_during_tests -or
        -not ($report.artifact_unchanged_during_tests -is [bool]) -or -not $report.artifact_unchanged_during_tests) {
        throw 'A successful full-bundle verification report is required; rebuild or rerun Verify-StartupFix.py.'
    }
    if ([IO.Path]::GetFullPath([string]$report.artifact_root).TrimEnd('\', '/') -ne
        [IO.Path]::GetFullPath($ArtifactRoot).TrimEnd('\', '/')) {
        throw 'The verification report belongs to a different artifact directory.'
    }
    $expectedFiles = ConvertTo-ArtifactHashMap $report.artifact_files_sha256
    if (-not $expectedFiles.ContainsKey('codex-sota.exe') -or
        -not $expectedFiles.ContainsKey('codex-sota-artifact-manifest.json') -or
        $expectedFiles['codex-sota.exe'] -ne ([string]$report.artifact_sha256).ToLowerInvariant()) {
        throw 'The verification report does not bind both the executable and the full-bundle manifest.'
    }
    $probe = $report.runtime_probe
    if ($null -eq $probe -or $probe.probe_protocol -ne 1 -or $probe.status -ne 'ready' -or
        -not ($probe.network_used -is [bool]) -or $probe.network_used -or
        -not ($probe.profiles_loaded -is [bool]) -or $probe.profiles_loaded -or
        $probe.executable_sha256 -ne $expectedFiles['codex-sota.exe']) {
        throw 'A successful, offline runtime self-test bound to this executable is required.'
    }
    foreach ($name in @('python', 'ctypes', 'ssl', 'sqlite', 'tcl', 'socket')) {
        if ($null -eq $probe.checks -or $probe.checks.$name -ne 'passed') {
            throw "Frozen runtime check is missing or failed: $name"
        }
    }
    if (-not $report.source_sha256 -or @($report.source_sha256.PSObject.Properties).Count -eq 0) {
        throw 'Reviewed source hashes are missing from the verification report.'
    }
    foreach ($source in $report.source_sha256.PSObject.Properties) {
        $sourcePath = Assert-ScopedArtifactPath $source.Name $SourceRoot
        if ([string]$source.Value -notmatch '^[0-9a-fA-F]{64}$' -or
            (Get-ArtifactSha256 $sourcePath) -ne ([string]$source.Value).ToLowerInvariant()) {
            throw "Reviewed source changed after verification: $($source.Name)"
        }
    }
    return $expectedFiles
}
