# Shared helpers of the scripts in this folder (dot-sourced). PowerShell 5 compatible.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$RepoRoot = Split-Path -Parent $PSScriptRoot

function Fail([string]$Message) {
    [Console]::Error.WriteLine("error: $Message")
    exit 1
}

# The version in the [project] table of pyproject.toml.
function Get-ProjectVersion {
    $m = Select-String -Path (Join-Path $RepoRoot 'pyproject.toml') -Pattern '^version\s*=\s*"([^"]+)"' |
        Select-Object -First 1
    if (-not $m) { Fail 'no version found in pyproject.toml' }
    return $m.Matches[0].Groups[1].Value
}

# Parses a SHA256SUMS file ("<hash>  <name>" per line) into a name -> lowercase hash table.
function Read-Sums([string]$Path) {
    $sums = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*([0-9A-Fa-f]{64})\s+\*?(\S.*?)\s*$') {
            $sums[$Matches[2]] = $Matches[1].ToLower()
        }
    }
    return $sums
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
}

# The release of this repository with the given tag, draft or published ($null when none).
# GH_REPO (owner/name) selects the repository; `gh` reads GH_TOKEN.
function Get-ReleaseByTag([string]$Tag) {
    $repo = $env:GH_REPO
    if (-not $repo) { Fail 'GH_REPO (owner/name) is not set' }
    $json = & gh api --paginate "repos/$repo/releases?per_page=100"
    if ($LASTEXITCODE -ne 0) { Fail 'cannot list the releases' }
    $all = ($json -join "`n") | ConvertFrom-Json
    return @($all) | Where-Object { $_.tag_name -eq $Tag } | Select-Object -First 1
}
