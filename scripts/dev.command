#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if [ ! -x ".venv/bin/python" ]; then
  echo "请先运行 install.command"
  exit 1
fi

.venv/bin/python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8503 &
BACKEND_PID=$!
trap 'kill "$BACKEND_PID" 2>/dev/null || true' EXIT
cd frontend
npm run dev

