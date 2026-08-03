#Requires -Version 5.1
<#
.SYNOPSIS
  Simple local service control for the WeChat-only CowAgent workspace.
.EXAMPLE
  .\cow.ps1 start
  .\cow.ps1 stop
  .\cow.ps1 restart
  .\cow.ps1 status
#>

param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Command = "start"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$EnvironmentName = "cowagent-wechat"
$Port = 9899
$AppPath = Join-Path $ProjectRoot "app.py"

function Get-EnvironmentPython {
    $conda = Get-Command conda -ErrorAction SilentlyContinue
    if (-not $conda) {
        throw "conda was not found in PATH"
    }
    $environmentData = (& $conda.Source env list --json | ConvertFrom-Json)
    $environmentPath = $environmentData.envs | Where-Object {
        (Split-Path $_ -Leaf) -eq $EnvironmentName
    } | Select-Object -First 1
    if (-not $environmentPath) {
        throw "Conda environment '$EnvironmentName' was not found"
    }
    $python = Join-Path $environmentPath "python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        throw "Python was not found at '$python'"
    }
    return (Resolve-Path -LiteralPath $python).Path
}

function Get-CowAgentProcess {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $listener) { return $null }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if (-not $process) { return $null }
    return $process
}

function Assert-ProjectProcess {
    param($Process, [string]$Python)
    $expectedPython = [System.IO.Path]::GetFullPath($Python)
    $actualPython = [System.IO.Path]::GetFullPath([string]$Process.ExecutablePath)
    $isPython = $actualPython.Equals(
        $expectedPython,
        [System.StringComparison]::OrdinalIgnoreCase
    )
    $isApp = [string]$Process.CommandLine -match '(^|[\\/\s\"])(app\.py)([\s\"]|$)'
    if (-not ($isPython -and $isApp)) {
        throw "Port $Port belongs to another process (PID $($Process.ProcessId)); refusing to stop it"
    }
}

function Start-CowAgent {
    $python = Get-EnvironmentPython
    $existing = Get-CowAgentProcess
    if ($existing) {
        Assert-ProjectProcess $existing $python
        Write-Host "CowAgent is already running on port $Port (PID $($existing.ProcessId))"
        return
    }
    Write-Host "Starting CowAgent in the current terminal (foreground mode)"
    Write-Host "Press Ctrl+C to stop. Browser auto-open is disabled."
    $previousOpenBrowser = $env:COW_OPEN_BROWSER
    $env:COW_OPEN_BROWSER = "0"
    Push-Location $ProjectRoot
    try {
        & $python $AppPath
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
        $env:COW_OPEN_BROWSER = $previousOpenBrowser
    }
    if ($exitCode -ne 0) {
        throw "CowAgent exited with code $exitCode"
    }
}

function Stop-CowAgent {
    $python = Get-EnvironmentPython
    $process = Get-CowAgentProcess
    if (-not $process) {
        Write-Host "CowAgent is not running"
        return
    }
    Assert-ProjectProcess $process $python
    Stop-Process -Id $process.ProcessId
    Wait-Process -Id $process.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    Write-Host "CowAgent stopped (PID $($process.ProcessId))"
}

switch ($Command) {
    "start" { Start-CowAgent }
    "stop" { Stop-CowAgent }
    "restart" {
        Stop-CowAgent
        Start-CowAgent
    }
    "status" {
        $process = Get-CowAgentProcess
        if ($process) {
            Write-Host "CowAgent is running on port $Port (PID $($process.ProcessId))"
        } else {
            Write-Host "CowAgent is stopped"
        }
    }
}
