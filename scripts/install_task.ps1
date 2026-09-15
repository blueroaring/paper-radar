# Register a Windows Scheduled Task that runs the Paper Radar daily digest.
#
# NOTE FOR MAINTAINERS: keep this file PURE ASCII (no Chinese, no BOM).
# Windows PowerShell 5.1 decodes BOM-less .ps1 files using the ANSI/GBK code page,
# so non-ASCII comments corrupt the whole script. See agent-experience/lessons/02.
#
# Usage (from the repository root):
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Time 07:45 -Send
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove
#
# The task and the built-in scheduler (python -m paper_radar serve / daemon) may
# both be enabled: the digest only mails papers it has never mailed before, so a
# second runner finds nothing new and stays quiet.

param(
  [string]$Time = "08:30",
  [switch]$Send,
  [switch]$Remove,
  [string]$TaskName = "PaperRadar-Digest"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

if ($Remove) {
  schtasks /Delete /TN $TaskName /F
  Write-Host "Removed scheduled task: $TaskName"
  exit 0
}

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "python not found on PATH. Install Python 3.10+ first." }

$cliArgs = "-m paper_radar digest"
if ($Send) { $cliArgs += " --send" }

$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "digest.log"

# Wrap in cmd so stdout/stderr can be appended to a log file.
$inner = 'cd /d "' + $repo + '" && set PYTHONIOENCODING=utf-8 && "' + $python + '" ' + $cliArgs + ' >> "' + $log + '" 2>&1'
$command = 'cmd /c ' + $inner

schtasks /Create /TN $TaskName /TR $command /SC DAILY /ST $Time /F | Out-Null
Write-Host "Registered scheduled task: $TaskName"
Write-Host "  time     : daily at $Time"
Write-Host "  command  : python $cliArgs"
Write-Host "  workdir  : $repo"
Write-Host "  log file : $log"
if (-not $Send) {
  Write-Host ""
  Write-Host "[i] Report-only mode. Re-run with -Send to actually mail the digest."
}
Write-Host ""
Write-Host "Inspect : schtasks /Query /TN $TaskName /V /FO LIST"
Write-Host "Run now : schtasks /Run /TN $TaskName"
