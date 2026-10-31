# Prints the body of the '## <version>' section of CHANGELOG.md (up to the next '## ').
# Exit 1 when there is none.
param([Parameter(Mandatory = $true)][string]$Version)
. (Join-Path $PSScriptRoot 'lib.ps1')

$lines = Get-Content -LiteralPath (Join-Path $RepoRoot 'CHANGELOG.md')
$body = New-Object System.Collections.Generic.List[string]
$inside = $false
foreach ($line in $lines) {
    if ($line -match '^## ') {
        if ($inside) { break }
        $inside = ($line -match ('^##\s+' + [regex]::Escape($Version) + '(\s|$)'))
        continue
    }
    if ($inside) { $body.Add($line) }
}
if (-not $inside) { Fail "CHANGELOG.md has no '## $Version' section" }
($body -join "`n").Trim()
