#!/usr/bin/env bash
# vococo 常驻管理(macOS launchd)
# 用法:bash deploy/launchd.sh {install|uninstall|restart|status|logs}
# plist 在 install 时按本机路径自动生成,仓库不存硬编码路径。
set -euo pipefail

LABEL="com.vococo"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/.venv/bin/vococo"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

_gen_plist() {
  mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data/logs"
  cat > "$PLIST_DST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <!-- 经登录 shell 启动:给进程完整会话上下文(env/keychain),
       否则 launchd 直接 spawn 在某些 Mac 上会卡死(子进程/路径解析挂起) -->
  <key>ProgramArguments</key>
  <array><string>/bin/zsh</string><string>-lc</string><string>"$BIN" serve</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <!-- launchd 无 shell PATH,SDK 靠它找 claude 命令 -->
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$ROOT/data/logs/vococo.out.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/logs/vococo.err.log</string>
</dict>
</plist>
PLIST
}

case "${1:-status}" in
  install)
    _gen_plist
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST_DST"
    launchctl enable "$DOMAIN/$LABEL"
    echo "✅ 已安装并启动($BIN serve)。开机自启 + 崩溃自愈已开。"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "✅ 已卸载并停止。"
    ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL" && echo "✅ 已重启。" ;;
  status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =" || echo "未运行(先 install)" ;;
  logs)
    echo "=== stdout ==="; tail -n 30 "$ROOT/data/logs/vococo.out.log" 2>/dev/null || echo "(无)"
    echo "=== stderr ==="; tail -n 30 "$ROOT/data/logs/vococo.err.log" 2>/dev/null || echo "(无)" ;;
  *)
    echo "用法:bash deploy/launchd.sh {install|uninstall|restart|status|logs}"; exit 1 ;;
esac
