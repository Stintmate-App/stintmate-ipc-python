# Downloads the native files this binding wraps from the release `v<version>` of the
# stintmate-ipc repository (version = the one in pyproject.toml), verifies them against the
# release's SHA256SUMS (the header is for the tests of the ctypes function prototypes) and writes them into .native\ (use it as STINTMATE_IPC_PREBUILT).
#
#   scripts\fetch-native.ps1
#
# STINTMATE_IPC_READ_TOKEN  a GitHub token that can read the release (needed only while the
#                           repository is private); it is sent to api.github.com only.
# STINTMATE_IPC_RELEASE     a local folder or a base URL holding the four files, used instead
#                           of the GitHub release.
. (Join-Path $PSScriptRoot 'lib.ps1')

$names = @('stintmate_ipc.dll', 'stintmate-ipc-check.exe', 'stintmate_ipc.h', 'SHA256SUMS')
$version = Get-ProjectVersion
$tag = "v$version"
$out = Join-Path $RepoRoot '.native'
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("stintmate-ipc-native-" + [Guid]::NewGuid().ToString('N'))

try {
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $source = $env:STINTMATE_IPC_RELEASE
    $token = $env:STINTMATE_IPC_READ_TOKEN
    $base = "https://github.com/Stintmate-App/stintmate-ipc/releases/download/$tag"
    $assets = $null
    if (-not $source -and $token) {
        $headers = @{ Authorization = "Bearer $token"; Accept = 'application/vnd.github+json'; 'X-GitHub-Api-Version' = '2022-11-28' }
        $release = Invoke-RestMethod -UseBasicParsing -Headers $headers `
            -Uri "https://api.github.com/repos/Stintmate-App/stintmate-ipc/releases/tags/$tag"
        $assets = @{}
        foreach ($a in $release.assets) { $assets[$a.name] = $a.url }
    }
    foreach ($name in $names) {
        $dest = Join-Path $tmp $name
        Write-Host "fetching $name ($tag)"
        if ($source -and (Test-Path -LiteralPath $source -PathType Container)) {
            Copy-Item -LiteralPath (Join-Path $source $name) -Destination $dest
        } elseif ($source) {
            Invoke-WebRequest -UseBasicParsing -Uri ($source.TrimEnd('/') + "/$name") -OutFile $dest
        } elseif ($assets) {
            if (-not $assets.ContainsKey($name)) { Fail "release $tag has no asset $name" }
            $h = @{ Authorization = "Bearer $token"; Accept = 'application/octet-stream' }
            Invoke-WebRequest -UseBasicParsing -Headers $h -Uri $assets[$name] -OutFile $dest
        } else {
            Invoke-WebRequest -UseBasicParsing -Uri "$base/$name" -OutFile $dest
        }
    }
    $sums = Read-Sums (Join-Path $tmp 'SHA256SUMS')
    foreach ($name in $names[0..2]) {
        if (-not $sums.ContainsKey($name)) { Fail "SHA256SUMS has no line for $name" }
        $actual = Get-Sha256 (Join-Path $tmp $name)
        if ($actual -ne $sums[$name]) { Fail "hash mismatch for $name (expected $($sums[$name]), got $actual)" }
    }
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    foreach ($name in $names) { Copy-Item -Force -LiteralPath (Join-Path $tmp $name) -Destination (Join-Path $out $name) }
    Write-Host "ok: $out"
} catch {
    Fail $_.Exception.Message
} finally {
    Remove-Item -Recurse -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
}
