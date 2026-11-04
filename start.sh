#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  exec python3 dashboard.py "$@"
elif command -v python >/dev/null 2>&1; then
  exec python dashboard.py "$@"
else
  echo "未找到 Python，请先安装 Python 3.8 或更高版本。"
  exit 1
fi
