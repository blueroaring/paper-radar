@echo off
REM Paper Radar 一键启动（Windows）
REM 双击运行：启动本地控制台 + 内置每日定时器
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
chcp 65001 >nul

if not exist config.json (
  echo [i] 未找到 config.json，正在从 config.example.json 复制...
  copy /y config.example.json config.json >nul
)

python -m paper_radar serve %*
if errorlevel 1 (
  echo.
  echo [!] 启动失败。请确认已安装 Python 3.10+ 并加入 PATH。
  pause
)
endlocal
