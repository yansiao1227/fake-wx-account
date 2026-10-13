[CmdletBinding()]
param(
    [string]$BaseRef = "origin/master",
    [string]$Date = "",
    [string]$RepositoryPath = (Get-Location).Path
)

$ErrorActionPreference = "Stop"
$previousEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Invoke-Git {
    param([string[]]$Arguments)
    $output = @(& git -C $RepositoryPath @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed: $($output -join [Environment]::NewLine)"
    }
    return $output
}

function Assert-Date {
    param([string]$Value)
    $parsedDate = [datetime]::MinValue
    if ($Value -notmatch '^\d{4}-\d{2}-\d{2}$' -or -not [datetime]::TryParseExact(
        $Value, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::None, [ref]$parsedDate
    )) {
        throw "Invalid changelog date: $Value; expected a valid YYYY-MM-DD date."
    }
}

function Get-Entries {
    param([object[]]$Lines, [string]$Path)
    $text = $Lines -join "`n"
    $text = [regex]::Replace($text, '(?s)<!--.*?-->', '')
    $insideCode = $false
    $entries = @()
    foreach ($line in ($text -split "`n")) {
        if ($line -match '^\s*(```|~~~)') {
            $insideCode = -not $insideCode
            continue
        }
        if ($insideCode -or $line -notmatch '^\s*(?:[-*+]|\d+[.)])\s+(?<entry>\S.*)$') {
            continue
        }
        $entry = [regex]::Replace($Matches['entry'].Trim(), '\s+', ' ')
        if ($entry -notmatch '[\p{L}\p{N}]' -or $entry -match '^(?i:TODO|TBD|N/?A|none)[.!?]*$') {
            continue
        }
        $entries += $entry
    }
    return $entries
}

try {
    if (-not $Date) {
        $beijing = [TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')
        $Date = [TimeZoneInfo]::ConvertTimeFromUtc([datetime]::UtcNow, $beijing).ToString('yyyy-MM-dd')
    }
    Assert-Date $Date
    if (-not $BaseRef -or $BaseRef.StartsWith('-')) {
        throw 'BaseRef must be a Git commit or reference.'
    }
    $baseCommit = (Invoke-Git -Arguments @('rev-parse', '--verify', "$BaseRef^{commit}")) -join ''
    $headCommit = (Invoke-Git -Arguments @('rev-parse', '--verify', 'HEAD^{commit}')) -join ''
    $mergeBase = (Invoke-Git -Arguments @('merge-base', $baseCommit, $headCommit)) -join ''
    $comparison = "${baseCommit}...${headCommit}"
    $expectedPath = "changelogs/$Date.md"
    $changedPaths = @(Invoke-Git -Arguments @('-c', 'core.quotepath=false', 'diff', '--name-only', '--diff-filter=AM', $comparison, '--', 'changelogs'))
    if ($changedPaths -notcontains $expectedPath) {
        throw "The committed PR diff must add or update $expectedPath for the Beijing merge date."
    }
    foreach ($path in $changedPaths) {
        if ($path -eq 'changelogs/README.md') {
            continue
        }
        if ($path -notmatch '^changelogs/(?<date>\d{4}-\d{2}-\d{2})\.md$') {
            throw "Invalid changelog filename: $path; use changelogs/YYYY-MM-DD.md."
        }
        Assert-Date $Matches['date']
        $entries = @(Get-Entries -Lines (Invoke-Git -Arguments @('show', "${headCommit}:$path")) -Path $path)
        if ($entries.Count -eq 0) {
            throw "Changelog $path must contain at least one nonempty Markdown list entry."
        }
    }

    $headEntries = @(Get-Entries -Lines (Invoke-Git -Arguments @('show', "${headCommit}:$expectedPath")) -Path $expectedPath)
    $basePaths = @(Invoke-Git -Arguments @('ls-tree', '--name-only', $mergeBase, '--', $expectedPath))
    $existingEntries = @{}
    if ($basePaths -contains $expectedPath) {
        foreach ($entry in @(Get-Entries -Lines (Invoke-Git -Arguments @('show', "${mergeBase}:$expectedPath")) -Path $expectedPath)) {
            $existingEntries[$entry] = $true
        }
    }
    $newEntries = @($headEntries | Where-Object { -not $existingEntries.ContainsKey($_) } | Select-Object -Unique)
    if ($newEntries.Count -eq 0) {
        throw "Append at least one new effective list entry to $expectedPath; headings, whitespace and repeated entries do not count."
    }
    Write-Output "Changelog check passed: $expectedPath ($($newEntries.Count) new entries)."
    exit 0
} catch {
    Write-Output "Changelog check failed: $($_.Exception.Message)"
    exit 1
} finally {
    [Console]::OutputEncoding = $previousEncoding
}
