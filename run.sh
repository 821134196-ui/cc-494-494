#!/usr/bin/env bash
# 体检报告解读预约系统（演示）—— 单命令启动。
# 仅依赖 Python 3.10+ 标准库；首次运行自动建库、生成 PDF 与演示数据。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

# --reseed / --reset 可重置演示数据
ARGS=()
for a in "$@"; do ARGS+=("$a"); done

echo "启动体检报告解读预约系统（演示）..."
exec python3 main.py --host "$HOST" --port "$PORT" "${ARGS[@]}"
