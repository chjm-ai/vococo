#!/usr/bin/env bash
# claude-hermes 常驻管理(macOS launchd)
# 用法:bash deploy/launchd.sh {install|uninstall|restart|status|logs}
set -euo pipefail

LABEL="com.wesley.claude-hermes"
ROOT="/Users/wesley/Desktop/Repos/claude-hermes"
PLIST_SRC="$ROOT/deploy/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

case "${1:-status}" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data/logs"
    cp "$PLIST_SRC" "$PLIST_DST"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST_DST"
    launchctl enable "$DOMAIN/$LABEL"
    echo "✅ 已安装并启动。开机自启 + 崩溃自愈已开。"
    echo "   看状态:bash deploy/launchd.sh status"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "✅ 已卸载并停止。"
    ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL"
    echo "✅ 已重启。"
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|program =" || echo "未运行(先 install)"
    ;;
  logs)
    echo "=== stdout(最近 30 行)==="; tail -n 30 "$ROOT/data/logs/hermes.out.log" 2>/dev/null || echo "(无)"
    echo "=== stderr(最近 30 行)==="; tail -n 30 "$ROOT/data/logs/hermes.err.log" 2>/dev/null || echo "(无)"
    ;;
  *)
    echo "用法:bash deploy/launchd.sh {install|uninstall|restart|status|logs}"; exit 1 ;;
esac
