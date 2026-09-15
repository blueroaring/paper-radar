# Paper Radar launcher -- one double-click gets you to the web console.
#
# Behavior:
#   1. figure out the port from config.json (no hardcoded port)
#   2. if the console is not answering, start it detached (hidden window)
#   3. wait until it actually answers, then open the default browser
#   4. on failure, show a message box explaining what to do
#
# NOTE FOR MAINTAINERS: keep this file PURE ASCII (no BOM, no Chinese).
# Windows PowerShell 5.1 decodes BOM-less .ps1 files with the ANSI/GBK code page,
# so non-ASCII comments corrupt the whole script.
# See agent-experience/lessons/02-windows-powershell51.md
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\open-console.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\open-console.ps1 -NoBrowser
#   powershell -ExecutionPolicy Bypass -File scripts\open-console.ps1 -Stop

param(
  [switch]$NoBrowser,
  [switch]$Stop,
  [int]$TimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

function Show-Dialog([string]$text, [string]$title = "Paper Radar") {
  # WScript.Shell.Popup needs no type literal, so it is safe on PowerShell 5.1.
  try {
    $shell = New-Object -ComObject WScript.Shell
    $shell.Popup($text, 0, $title, 0x40) | Out-Null
  } catch {
    Write-Host $text
  }
}

function Get-ConfigPort {
  foreach ($name in @("config.json", "config.example.json")) {
    $path = Join-Path $repo $name
    if (-not (Test-Path $path)) { continue }
    try {
      $cfg = Get-Content -Raw -Path $path | ConvertFrom-Json
      if ($cfg.app -and $cfg.app.port) { return [int]$cfg.app.port }
    } catch { }
  }
  return 8848
}

function Test-Console([int]$port) {
  # A listening port does not prove it is *our* service, so check the API payload.
  try {
    $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/state" -TimeoutSec 5
    return ($resp.ok -eq $true)
  } catch {
    return $false
  }
}

function Find-Python {
  foreach ($candidate in @("pythonw.exe", "python.exe")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
  }
  return $null
}

$port = Get-ConfigPort
$url = "http://127.0.0.1:$port"

if ($Stop) {
  $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  if (-not $conn) {
    Show-Dialog "Nothing is listening on port $port - the console is not running."
    exit 0
  }
  foreach ($ownerPid in ($conn | Select-Object -ExpandProperty OwningProcess -Unique)) {
    $proc = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -like "python*") {
      Stop-Process -Id $ownerPid -Force
      Write-Host "Stopped Paper Radar console (pid $ownerPid)."
    } else {
      Write-Host "Port $port is owned by $($proc.ProcessName) (pid $ownerPid) - not touched."
    }
  }
  exit 0
}

if (-not (Test-Console $port)) {
  if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    Show-Dialog "Port $port is already in use by another program, so Paper Radar cannot start.`n`nChange app.port in config.json, or close the other program."
    exit 1
  }

  $python = Find-Python
  if (-not $python) {
    Show-Dialog "Python was not found on PATH.`n`nInstall Python 3.10+ from python.org and tick 'Add python.exe to PATH'."
    exit 1
  }

  Write-Host "Starting Paper Radar console on port $port ..."
  # pythonw.exe gives no console window; the process is detached from this
  # script, so it keeps running after the shortcut's PowerShell exits.
  Start-Process -FilePath $python `
    -ArgumentList "-m", "paper_radar", "serve" `
    -WorkingDirectory $repo `
    -WindowStyle Hidden | Out-Null

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  $ready = $false
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 700
    if (Test-Console $port) { $ready = $true; break }
  }
  if (-not $ready) {
    Show-Dialog "Paper Radar did not answer on $url within $TimeoutSeconds seconds.`n`nTry running this once in a terminal to see the error:`n  cd $repo`n  python -m paper_radar serve"
    exit 1
  }
  Write-Host "Console is up: $url"
}

if ($NoBrowser) {
  Write-Host "Console ready at $url (browser not opened)."
} else {
  Start-Process $url
  Write-Host "Opened $url in the default browser."
}
