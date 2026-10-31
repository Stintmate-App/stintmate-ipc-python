# Writes dist\SHA256SUMS for every file in dist\ (the wheel).
. (Join-Path $PSScriptRoot 'lib.ps1')

$dist = Join-Path $RepoRoot 'dist'
$files = @(Get-ChildItem -LiteralPath $dist -File | Where-Object { $_.Name -ne 'SHA256SUMS' })
if ($files.Count -eq 0) { Fail 'dist\ has no files' }
$text = ($files | ForEach-Object { (Get-Sha256 $_.FullName) + '  ' + $_.Name }) -join "`n"
[IO.File]::WriteAllText((Join-Path $dist 'SHA256SUMS'), $text + "`n", (New-Object Text.UTF8Encoding($false)))
Write-Host $text
