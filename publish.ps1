param(
    [string]$Token = "",
    [string]$DisplayName = "Ellya, Your Virtual Companion",
    [string]$Slug = "ellya",
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = Join-Path $root ".publish-dist"
$distSkill = Join-Path $dist "Ellya"
$npmCache = Join-Path $root ".npm-cache"
$semverRegex = '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z\-\.]+)?(?:\+[0-9A-Za-z\-\.]+)?$'

function Get-VersionFromGitHead {
    # Optional best-effort refresh for local tags.
    try { & git fetch --tags --quiet origin 2>$null | Out-Null } catch {}

    $headTags = @()
    try {
        $headTags = (& git tag --points-at HEAD) 2>$null
    } catch {
        $headTags = @()
    }

    foreach ($tag in $headTags) {
        $normalized = $tag.Trim()
        if ($normalized.StartsWith("v")) {
            $normalized = $normalized.Substring(1)
        }
        if ($normalized -match $semverRegex) {
            return $normalized
        }
    }

    return ""
}

$required = @(
    "SKILL.md",
    "README.md",
    "ANALYSIS_PROMPT.md",
    "scripts/genai_media.py",
    "templates/SOUL.md"
)

if (Test-Path $dist) {
    Remove-Item -LiteralPath $dist -Recurse -Force
}
New-Item -ItemType Directory -Path $distSkill -Force | Out-Null

foreach ($rel in $required) {
    $src = Join-Path $root $rel
    if (-not (Test-Path $src)) {
        throw "Missing required publish file: $rel"
    }

    $target = Join-Path $distSkill $rel
    $targetDir = Split-Path -Parent $target
    if (-not (Test-Path $targetDir)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    }
    Copy-Item -LiteralPath $src -Destination $target -Force
}

if (-not $Token) {
    $Token = $env:CLAWHUB_TOKEN
}
if (-not $Token) {
    $Token = Read-Host "Enter CLAWHUB token (input hidden not supported in this script)"
}
if (-not $Token) {
    throw "No token provided. Set CLAWHUB_TOKEN or pass -Token."
}

Write-Host "Prepared minimal publish bundle at: $distSkill"
$env:npm_config_cache = $npmCache

if (-not $Version) {
    $Version = Get-VersionFromGitHead
    if (-not $Version) {
        throw "Version not provided and no semver git tag found on HEAD. Publish your GitHub release tag first (e.g. v1.2.3), then rerun."
    }
    Write-Host "Version selected from git tag on HEAD: $Version"
}

# simple semver validator: 1.2.3 / 1.2.3-alpha.1 / 1.2.3+build
if ($Version -notmatch $semverRegex) {
    throw "Version must be valid semver, e.g. 1.0.0"
}

Push-Location $distSkill
try {
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    & npx.cmd -y clawhub login --token $Token --no-input
    $loginExit = $LASTEXITCODE
    if ($loginExit -ne 0) {
        $ErrorActionPreference = $oldEap
        throw "clawhub login failed (exit code $loginExit)."
    }

    $publishArgs = @("-y", "clawhub", "publish", ".", "--name", $DisplayName, "--slug", $Slug, "--version", $Version, "--no-input")
    Write-Host "Publishing with: slug=$Slug, name=$DisplayName, version=$Version"

    & npx.cmd @publishArgs
    $publishExit = $LASTEXITCODE
    $ErrorActionPreference = $oldEap

    if ($publishExit -ne 0) {
        Write-Host ""
        Write-Host "Common causes:"
        Write-Host "1) Slug already exists and you do not own it."
        Write-Host "2) Version already exists for this slug."
        Write-Host "3) Invalid token/session (login expired)."
        Write-Host "4) Name/slug/version policy mismatch on server."
        throw "clawhub publish failed (exit code $publishExit)."
    }
}
finally {
    Pop-Location
}

Write-Host "Publish completed."
