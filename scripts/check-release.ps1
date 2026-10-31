# Fails (exit 1) unless the tag can be released from this checkout:
#   -Tag v<version>   no '+', an ancestor of origin/main, equal to v + pyproject's version,
#                     a '## <version>' section in CHANGELOG.md, no published release for it.
# Needs a full clone (fetch-depth 0) and, for the last check, GH_REPO and GH_TOKEN.
param([Parameter(Mandatory = $true)][string]$Tag)
. (Join-Path $PSScriptRoot 'lib.ps1')

if ($Tag.Contains('+')) { Fail "tag $Tag contains '+'" }
if ($Tag -notmatch '^v\d+\.\d+\.\d+$') { Fail "tag $Tag is not v<major>.<minor>.<patch>" }
& git -C $RepoRoot rev-parse --verify --quiet "refs/tags/$Tag^{commit}" | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "tag $Tag does not exist" }
& git -C $RepoRoot merge-base --is-ancestor "refs/tags/$Tag" origin/main
if ($LASTEXITCODE -ne 0) { Fail "tag $Tag is not an ancestor of origin/main" }
$version = Get-ProjectVersion
if ($Tag -ne "v$version") { Fail "tag $Tag differs from v$version (pyproject.toml)" }
$section = & (Join-Path $PSScriptRoot 'changelog-section.ps1') -Version $version
if ($LASTEXITCODE -ne 0 -or -not $section) { Fail "CHANGELOG.md has no '## $version' section" }
$existing = Get-ReleaseByTag $Tag
if ($existing -and -not $existing.draft) { Fail "release $Tag is already published" }
Write-Host "ok: $Tag"
