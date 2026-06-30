#!/usr/bin/env zsh
# 停止 claude-hermes serve(连带崩溃自启循环一起停)。
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p data
touch data/.stop          # 先立停止标记,让重启循环不再拉起
if pkill -f "claude-hermes serve" 2>/dev/null; then
  echo "✅ 已停止"
else
  echo "(没在跑)"
fi
