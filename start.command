#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if [ ! -x ".venv/bin/python" ] || [ ! -f "frontend/dist/index.html" ]; then
  echo "尚未完成安装，请先双击 install.command。"
  read -r -p "按回车键关闭窗口..."
  exit 1
fi

echo "=========================================="
echo "  瞬游光影正在启动"
echo "  网页地址：http://127.0.0.1:8503"
echo "  使用期间请不要关闭这个窗口"
echo "=========================================="

(sleep 2 && open "http://127.0.0.1:8503") &
exec .venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8503

