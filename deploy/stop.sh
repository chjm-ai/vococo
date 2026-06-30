#!/usr/bin/env zsh
# 停止 claude-hermes serve
if pkill -f "claude-hermes serve" 2>/dev/null; then
  echo "✅ 已停止"
else
  echo "(没在跑)"
fi
