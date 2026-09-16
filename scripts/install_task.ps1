# Register a Windows Scheduled Task that runs the Paper Radar daily digest.
#
# NOTE FOR MAINTAINERS: keep this file PURE ASCII (no Chinese, no BOM).
# Windows PowerShell 5.1 decodes BOM-less .ps1 files using the ANSI/GBK code page,
# so non-ASCII comments corrupt the whole script. See agent-experience/lessons/02.
#
# WHY THIS SCRIPT USES THE ScheduledTasks MODULE INSTEAD OF schtasks.exe:
#   `schtasks /Create` cannot set StartWhenAvailable or the battery options.
#   Its defaults are exactly the ones that make a laptop silently skip the digest:
#       StartWhenAvailable         = false   -> a missed run is never caught up
#       DisallowStartIfOnBatteries = true    -> does not run on battery
#       StopIfGoingOnBatteries     = true    -> killed if the charger is unplugged
#   A user who shut their laptop down overnight gets NO email and no explanation.
#   Register-ScheduledTask with explicit settings is the only way to set them.
#
# Usage (from the repository root):
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Time 07:45 -Send
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Send -Wake
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Test
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove
#
# The task and the built-in scheduler (python -m paper_radar serve / daemon) may
# both be enabled: the digest only mails papers it has never mailed before, so a
# second runner finds nothing new and stays quiet.

param(
  [string]$Time = "08:30",
  [switch]$Send,
  [switch]$Wake,
  [switch]$Test,
  [switch]$Remove,
  [string]$TaskName = "PaperRadar-Digest"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

if ($Remove) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Write-Host "Removed scheduled task: $TaskName"
  exit 0
}

if ($Test) {
  Write-Host "Starting the task now: $TaskName"
  Start-ScheduledTask -TaskName $TaskName
  Write-Host "Triggered. Check the log file, and the console's digest history."
  exit 0
}

# --- locate python ---------------------------------------------------------
$pythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $pythonCmd) { throw "python.exe not found on PATH. Install Python 3.10+ first." }
$python = $pythonCmd.Source

$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "digest.log"

# --- build the command line ------------------------------------------------
# cmd.exe is used only to get the ">>" redirection; the quotes are deliberate
# so a repository path containing spaces still works.
$cliArgs = "-m paper_radar digest"
if ($Send) { $cliArgs += " --send" }
$inner = 'cd /d "' + $repo + '" && set PYTHONIOENCODING=utf-8 && "' + $python + '" ' + $cliArgs + ' >> "' + $log + '" 2>&1'

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument ('/c ' + $inner)
$trigger = New-ScheduledTaskTrigger -Daily -At $Time

# --- the settings that actually matter -------------------------------------
$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Hours 1)
if ($Wake) {
  # Wakes the machine from sleep at the trigger time. Off by default: on a
  # laptop running on battery this is a surprise, and Modern Standby devices
  # may ignore wake timers anyway.
  $settings.WakeToRun = $true
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

# --- report what is actually on disk --------------------------------------
$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
$s = $task.Settings

Write-Host "Registered scheduled task: $TaskName"
Write-Host "  time      : daily at $Time"
Write-Host "  command   : python $cliArgs"
Write-Host "  workdir   : $repo"
Write-Host "  log file  : $log"
Write-Host "  catch up  : StartWhenAvailable = $($s.StartWhenAvailable)   (missed runs start as soon as the PC is available)"
Write-Host "  on battery: DisallowStartIfOnBatteries = $($s.DisallowStartIfOnBatteries), StopIfGoingOnBatteries = $($s.StopIfGoingOnBatteries)"
Write-Host "  wake PC   : WakeToRun = $($s.WakeToRun)"
Write-Host "  next run  : $($info.NextRunTime)"
if (-not $Send) {
  Write-Host ""
  Write-Host "[i] Report-only mode. Re-run with -Send to actually mail the digest."
}
Write-Host ""
Write-Host "Inspect : Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "Run now : powershell -File scripts\install_task.ps1 -Test"
Write-Host "Remove  : powershell -File scripts\install_task.ps1 -Remove"
