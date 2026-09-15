#!/usr/bin/env bash
# Paper Radar 一键启动（Linux / macOS）
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8

if [ ! -f config.json ]; then
  echo "[i] 未找到 config.json，正在从 config.example.json 复制..."
  cp config.example.json config.json
fi

exec python3 -m paper_radar serve "$@"
