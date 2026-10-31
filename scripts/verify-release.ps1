# Downloads every asset of the (draft or published) release for the tag and compares the
# hashes with the local dist\ files and with the release's own SHA256SUMS. Exit 1 on any
# difference. Needs GH_REPO and GH_TOKEN.
param([Parameter(Mandatory = $true)][string]$Tag)
. (Join-Path $PSScriptRoot 'lib.ps1')

$release = Get-ReleaseByTag $Tag
if (-not $release) { Fail "no release for $Tag" }
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("stintmate-ipc-verify-" + [Guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    foreach ($a in $release.assets) {
        $dest = Join-Path $tmp $a.name
        & gh api -H 'Accept: application/octet-stream' "repos/$($env:GH_REPO)/releases/assets/$($a.id)" > $dest
        if ($LASTEXITCODE -ne 0) { Fail "cannot download $($a.name)" }
    }
    $sumsPath = Join-Path $tmp 'SHA256SUMS'
    if (-not (Test-Path -LiteralPath $sumsPath)) { Fail 'the release has no SHA256SUMS' }
    $sums = Read-Sums $sumsPath
    $local = Get-ChildItem -LiteralPath (Join-Path $RepoRoot 'dist') -File
    foreach ($f in $local) {
        $remote = Join-Path $tmp $f.Name
        if (-not (Test-Path -LiteralPath $remote)) { Fail "the release has no $($f.Name)" }
        if ((Get-Sha256 $remote) -ne (Get-Sha256 $f.FullName)) { Fail "downloaded $($f.Name) differs from the built file" }
        if ($f.Name -ne 'SHA256SUMS') {
            if (-not $sums.ContainsKey($f.Name)) { Fail "SHA256SUMS has no line for $($f.Name)" }
            if ($sums[$f.Name] -ne (Get-Sha256 $remote)) { Fail "SHA256SUMS does not match $($f.Name)" }
        }
        Write-Host "verified $($f.Name)"
    }
} finally {
    Remove-Item -Recurse -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
}
