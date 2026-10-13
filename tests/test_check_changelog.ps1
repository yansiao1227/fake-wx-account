[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$checker = Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts/check_changelog.ps1'
$powershellExe = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ('cowagent-changelog-check-' + [guid]::NewGuid().ToString('N'))
$testDate = '2026-10-13'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$passed = 0

function Run-Git {
    param([string]$Repo, [string[]]$Arguments)
    $output = @(& git -C $Repo @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Test setup git command failed: $($output -join ' ')"
    }
}

function Write-File {
    param([string]$Repo, [string]$RelativePath, [string]$Content)
    $target = Join-Path $Repo $RelativePath
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
    [IO.File]::WriteAllText($target, $Content, $utf8)
}

function Assert-Case {
    param(
        [string]$Name, [bool]$ExpectedSuccess,
        [string]$Baseline = '', [string]$ChangePath = '', [string]$Content = '',
        [string]$Date = $testDate, [string]$ExtraPath = '', [string]$ExtraContent = ''
    )
    $repo = Join-Path $temporaryRoot $Name
    [IO.Directory]::CreateDirectory($repo) | Out-Null
    Run-Git $repo @('init', '--quiet')
    Run-Git $repo @('config', 'user.name', 'Changelog Test')
    Run-Git $repo @('config', 'user.email', 'changelog-test@example.invalid')
    Run-Git $repo @('config', 'core.autocrlf', 'false')
    Write-File $repo 'README.md' 'fixture'
    if ($Baseline) { Write-File $repo "changelogs/$testDate.md" $Baseline }
    Run-Git $repo @('add', '.')
    Run-Git $repo @('commit', '--quiet', '-m', 'baseline')
    Run-Git $repo @('tag', 'base')
    Write-File $repo 'implementation.txt' 'change'
    if ($ChangePath) { Write-File $repo $ChangePath $Content }
    if ($ExtraPath) { Write-File $repo $ExtraPath $ExtraContent }
    Run-Git $repo @('add', '.')
    Run-Git $repo @('commit', '--quiet', '-m', 'test change')
    $output = @(& $powershellExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $checker -BaseRef base -Date $Date -RepositoryPath $repo 2>&1)
    $success = $LASTEXITCODE -eq 0
    if ($success -ne $ExpectedSuccess) {
        throw "${Name}: expected success=$ExpectedSuccess, got success=$success. $($output -join ' ')"
    }
    $script:passed += 1
    Write-Output "PASS $Name"
}

try {
    [IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null
    Assert-Case -Name 'new-file' -ExpectedSuccess $true -ChangePath "changelogs/$testDate.md" -Content "# $testDate`n`n- Add daily changelog notifications.`n"
    $chineseItem = [string][char]0x65B0 + [char]0x589E + [char]0x66F4 + [char]0x65B0 + [char]0x901A + [char]0x77E5
    Assert-Case -Name 'chinese-entry' -ExpectedSuccess $true -ChangePath "changelogs/$testDate.md" -Content "# $testDate`n- $chineseItem`n"
    Assert-Case -Name 'same-day-append' -ExpectedSuccess $true -Baseline "# $testDate`n- Existing item.`n" -ChangePath "changelogs/$testDate.md" -Content "# $testDate`n- Existing item.`n- Add another change.`n"
    Assert-Case -Name 'no-changelog' -ExpectedSuccess $false
    Assert-Case -Name 'heading-only' -ExpectedSuccess $false -ChangePath "changelogs/$testDate.md" -Content "# $testDate`n`n"
    Assert-Case -Name 'old-date' -ExpectedSuccess $false -ChangePath 'changelogs/2026-10-12.md' -Content "# 2026-10-12`n- Add something.`n"
    Assert-Case -Name 'heading-edit' -ExpectedSuccess $false -Baseline "# Original title`n- Existing item.`n" -ChangePath "changelogs/$testDate.md" -Content "# Changed title`n`n- Existing item.`n"
    Assert-Case -Name 'repeat-existing-item' -ExpectedSuccess $false -Baseline "# $testDate`n- Existing item.`n" -ChangePath "changelogs/$testDate.md" -Content "# $testDate`n- Existing item.`n- Existing item.`n"
    Assert-Case -Name 'comment-only' -ExpectedSuccess $false -ChangePath "changelogs/$testDate.md" -Content "# $testDate`n- <!-- Added change -->`n"
    Assert-Case -Name 'invalid-calendar-date' -ExpectedSuccess $false -Date '2026-02-30'
    Assert-Case -Name 'invalid-filename-date' -ExpectedSuccess $false -ChangePath "changelogs/$testDate.md" -Content "- Valid change.`n" -ExtraPath 'changelogs/2026-02-30.md' -ExtraContent "- Invalid date.`n"
    Assert-Case -Name 'date-path-traversal' -ExpectedSuccess $false -Date '../../README'
    Assert-Case -Name 'code-block-only' -ExpectedSuccess $false -ChangePath "changelogs/$testDate.md" -Content "# $testDate`n~~~text`n- Sample only.`n~~~`n"
    Write-Output "All $passed changelog checks passed under Windows PowerShell 5.1."
} finally {
    $resolvedRoot = [IO.Path]::GetFullPath($temporaryRoot)
    $tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (-not $resolvedRoot.StartsWith($tempParent, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($resolvedRoot) -notmatch '^cowagent-changelog-check-[a-f0-9]{32}$') {
        throw 'Refusing to remove a temporary directory outside the test workspace.'
    }
    if (Test-Path -LiteralPath $resolvedRoot) {
        Remove-Item -LiteralPath $resolvedRoot -Recurse -Force
    }
}
