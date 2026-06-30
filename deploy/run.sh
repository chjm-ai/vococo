#!/usr/bin/env zsh
# 启动 claude-hermes serve(后台,崩溃自动重启)。
#
# 为什么不用 launchd 直接拉起:某些 Mac 上 launchd 的会话上下文不完整,
# 会让 agent 派生的 `claude` 子进程同步卡死(冻住事件循环)。用登录 shell
# (nohup zsh -lc)启动可恢复完整会话上下文,稳定可用。
#
# 崩溃自启:serve 非正常退出后等 5s 重启;直到 stop.sh 写下 data/.stop 才停。
# 重启电脑后重新跑一次本脚本即可;想开机自启见 README「常驻」。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p data/logs
pkill -f "claude-hermes serve" 2>/dev/null || true
rm -f data/.stop          # 清除上次的停止标记,允许重启循环运行
sleep 1
nohup zsh -lc "
  cd '$ROOT'
  while [ ! -f data/.stop ]; do
    PYTHONUNBUFFERED=1 .venv/bin/claude-hermes serve
    ec=\$?
    [ -f data/.stop ] && break
    echo \"[run.sh] serve 退出码 \$ec —— 5s 后重启 \$(date '+%F %T')\"
    sleep 5
  done
" >> data/logs/hermes.out.log 2>&1 &
disown
sleep 3
if pgrep -f "claude-hermes serve" >/dev/null; then
  echo "✅ serve 已后台启动(崩溃自启;日志:data/logs/hermes.out.log)"
else
  echo "❌ 启动失败,看 data/logs/hermes.out.log"; exit 1
fi
