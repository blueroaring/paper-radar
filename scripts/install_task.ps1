# 注册 Windows 计划任务：每天定时执行 Paper Radar 每日简报
#
# 用法（在仓库根目录执行）：
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1                 # 默认 08:30，只生成报告不发信
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Time 07:45 -Send
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove
#
# 注意：它与程序内置的定时器（python -m paper_radar serve / daemon）二选一，同时开会重复发信。

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
  Write-Host "已删除计划任务 $TaskName"
  exit 0
}

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "未找到 python，请确认已安装 Python 3.10+ 并加入 PATH。" }

$args = "-m paper_radar digest"
if ($Send) { $args += " --send" }

$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "digest.log"

# 用 cmd 包一层，方便把 stdout/stderr 追加到日志文件
$command = "cmd /c cd /d `"$repo`" && set PYTHONIOENCODING=utf-8 && `"$python`" $args >> `"$log`" 2>&1"

schtasks /Create /TN $TaskName /TR $command /SC DAILY /ST $Time /F | Out-Null
Write-Host "已注册计划任务：$TaskName"
Write-Host "  执行时间 : 每天 $Time"
Write-Host "  执行命令 : python $args"
Write-Host "  工作目录 : $repo"
Write-Host "  日志     : $log"
if (-not $Send) {
  Write-Host ""
  Write-Host "[i] 当前为“只生成报告不发信”模式。要真的发邮件，请加 -Send 重新注册。" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "查看：schtasks /Query /TN $TaskName /V /FO LIST"
Write-Host "立即测试：schtasks /Run /TN $TaskName"
