# Create a desktop shortcut that opens the Paper Radar web console.
#
# NOTE FOR MAINTAINERS: keep this file PURE ASCII (no BOM, no Chinese).
# Windows PowerShell 5.1 decodes BOM-less .ps1 files with the ANSI/GBK code page,
# so non-ASCII comments corrupt the whole script.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1 -Name "My Radar"
#   powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1 -Remove
#
# The shortcut runs scripts\open-console.ps1, which starts the console if it is not
# running yet and then opens the default browser. It needs no arguments, so a plain
# double-click is enough.

param(
  [string]$Name = "Paper Radar",
  [switch]$Remove
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop ($Name + ".lnk")

if ($Remove) {
  if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "Removed shortcut: $lnk" }
  else { Write-Host "No such shortcut: $lnk" }
  exit 0
}

$target = Join-Path $PSScriptRoot "open-console.ps1"
if (-not (Test-Path $target)) { throw "Launcher not found: $target" }

$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$icon = Join-Path $repo "paper_radar\web\favicon.ico"
if (-not (Test-Path $icon)) {
  Write-Host "Icon missing - generating it with scripts\make_icon.py ..."
  $python = (Get-Command python -ErrorAction SilentlyContinue).Source
  if ($python) {
    Push-Location $repo
    try { & $python "scripts\make_icon.py" | Out-Null } finally { Pop-Location }
  }
}
if (-not (Test-Path $icon)) { $icon = "$env:SystemRoot\System32\shell32.dll" }

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnk)
$sc.TargetPath = $powershell
$sc.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $target + '"'
$sc.WorkingDirectory = $repo
$sc.IconLocation = "$icon,0"
$sc.Description = "Paper Radar literature radar - open the local web console"
$sc.WindowStyle = 7   # minimized: avoids a flashing console window
$sc.Save()

# Read the shortcut back so the report reflects what is actually on disk.
$check = $shell.CreateShortcut($lnk)
Write-Host "Created shortcut: $lnk"
Write-Host "  target : $($check.TargetPath)"
Write-Host "  args   : $($check.Arguments)"
Write-Host "  workdir: $($check.WorkingDirectory)"
Write-Host "  icon   : $($check.IconLocation)"
Write-Host ""
Write-Host "Double-click it to open the console; it starts the server when needed."
Write-Host "To stop the server: powershell -File scripts\open-console.ps1 -Stop"
