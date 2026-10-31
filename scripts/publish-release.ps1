# Publishes the draft release for the tag. Needs GH_REPO and GH_TOKEN.
param([Parameter(Mandatory = $true)][string]$Tag)
. (Join-Path $PSScriptRoot 'lib.ps1')

$release = Get-ReleaseByTag $Tag
if (-not $release) { Fail "no release for $Tag" }
if (-not $release.draft) { Fail "release $Tag is already published" }
& gh api -X PATCH "repos/$($env:GH_REPO)/releases/$($release.id)" -F draft=false | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "cannot publish $Tag" }
Write-Host "published $Tag"
