#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

echo ""
echo "=========================================="
echo "  瞬游光影 · 首次安装"
echo "=========================================="

if ! command -v python3.11 >/dev/null 2>&1; then
  echo "没有找到 Python 3.11。"
  if command -v brew >/dev/null 2>&1; then
    brew install python@3.11
  else
    echo "请先安装 Homebrew：https://brew.sh"
    read -r -p "按回车键退出..."
    exit 1
  fi
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "正在安装 FFmpeg..."
    brew install ffmpeg
  else
    echo "没有找到 FFmpeg，请先安装 Homebrew。"
    read -r -p "按回车键退出..."
    exit 1
  fi
fi

if ! command -v npm >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "正在安装 Node.js（构建网页需要）..."
    brew install node
  else
    echo "没有找到 Node.js/npm，请先安装 Homebrew：https://brew.sh"
    read -r -p "按回车键退出..."
    exit 1
  fi
fi

if [ ! -d ".venv" ]; then
  python3.11 -m venv .venv
fi

echo "正在安装 Python 依赖..."
.venv/bin/python -m pip install --upgrade pip wheel setuptools
.venv/bin/python -m pip install -r backend/requirements.txt

echo "正在构建网页..."
cd frontend
npm install
npm run build
cd ..

echo "正在检查环境..."
.venv/bin/python - <<'PY'
from openai import OpenAI
import fastapi, numpy, uvicorn, whisper
print("Python 依赖正常")
PY
ffmpeg -version | head -1

echo ""
echo "✅ 安装完成。以后双击 start.command 即可启动。"
read -r -p "按回车键关闭窗口..."
