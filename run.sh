#!/usr/bin/env bash
# 单命令启动体检报告解读预约演示系统。
#
#   ./run.sh            初始化（如需）并启动服务，默认 http://127.0.0.1:8000
#   ./run.sh --reset    清空数据库与 PDF，重新生成演示数据（含更正报告样例）后启动
#   ./run.sh --test     运行全部测试
#
# 可选环境变量：HC_HOST / HC_PORT / HC_DATA_DIR / HC_DB_PATH / HC_PDF_DIR
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if [[ "${1:-}" == "--test" ]]; then
  exec "$PYTHON" -m unittest discover -s tests -v
fi

if [[ "${1:-}" == "--reset" ]]; then
  echo ">> 重置演示数据…"
  "$PYTHON" -m app.seed --reset
fi

echo ">> 启动服务（首次运行会自动生成演示数据）…"
exec "$PYTHON" -m app.main
