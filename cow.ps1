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
$RunDir = Join-Path $ProjectRoot "tmp\run"
$PidFile = Join-Path $RunDir "cowagent.pid"
$StdoutLog = Join-Path $RunDir "cowagent.stdout.log"
$StderrLog = Join-Path $RunDir "cowagent.stderr.log"
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

function Get-EmergencyHotkey {
    $configPath = Join-Path $ProjectRoot "config.json"
    if (-not (Test-Path -LiteralPath $configPath)) { return "Ctrl+Alt+Shift+Q" }
    try {
        $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
        if ($config.emergency_stop_hotkey_enabled -eq $false) { return "disabled" }
        if ($config.emergency_stop_hotkey) { return [string]$config.emergency_stop_hotkey }
    } catch {}
    return "Ctrl+Alt+Shift+Q"
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
        Write-Host "CowAgent is already running: http://127.0.0.1:$Port (PID $($existing.ProcessId))"
        Write-Host "Emergency stop: $(Get-EmergencyHotkey)"
        return
    }
    New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
    $startOptions = @{
        FilePath = $python
        ArgumentList = @($AppPath)
        WorkingDirectory = $ProjectRoot
        WindowStyle = "Hidden"
        RedirectStandardOutput = $StdoutLog
        RedirectStandardError = $StderrLog
        PassThru = $true
    }
    $process = Start-Process @startOptions
    Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ascii
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
            Write-Host "CowAgent started: http://127.0.0.1:$Port (PID $($process.Id))"
            Write-Host "Emergency stop: $(Get-EmergencyHotkey)"
            Write-Host "Logs: $StdoutLog"
            return
        }
        if ($process.HasExited) {
            throw "CowAgent exited during startup; inspect '$StderrLog'"
        }
    }
    throw "CowAgent did not listen on port $Port within 15 seconds"
}

function Stop-CowAgent {
    $python = Get-EnvironmentPython
    $process = Get-CowAgentProcess
    if (-not $process) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        Write-Host "CowAgent is not running"
        return
    }
    Assert-ProjectProcess $process $python
    Stop-Process -Id $process.ProcessId
    Wait-Process -Id $process.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
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
            Write-Host "CowAgent is running: http://127.0.0.1:$Port (PID $($process.ProcessId))"
            Write-Host "Emergency stop: $(Get-EmergencyHotkey)"
        } else {
            Write-Host "CowAgent is stopped"
        }
    }
}
