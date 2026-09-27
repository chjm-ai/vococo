#!/usr/bin/env bash
# 国内中转隧道(macOS launchd):本机 Web 端口经 SSH 反向隧道挂到国内服务器的 127.0.0.1,
# 再由那台服务器的 nginx 反代出去——国内访问不必绕 Cloudflare 海外边缘(实测 2s+ → 0.2s)。
# 用法:bash deploy/cn-tunnel.sh {install <ssh主机别名> [远端端口]|uninstall|restart|status|logs}
# 断线靠两层自愈:ssh 的 ServerAlive 探测到死链就退出 → launchd KeepAlive 10 秒后重拉。
# 服务器侧 sshd 建议设 ClientAliveInterval,让死掉的旧转发尽快释放端口,否则新连接会因
# ExitOnForwardFailure 反复退出重试,直到旧转发超时。
set -euo pipefail

LABEL="com.vococo.cn-tunnel"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
LOG="$ROOT/data/logs/cn-tunnel.log"

# 本机 Web 端口跟 .env 走,没配就用 config.py 的默认值
_web_port() {
  local p
  p="$(grep -E '^WEB_PORT=' "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '"'"'"' ' || true)"
  echo "${p:-8848}"
}

_gen_plist() {
  local host="$1" rport="$2" lport
  lport="$(_web_port)"
  mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data/logs"
  cat > "$PLIST_DST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/ssh</string><string>-N</string>
    <string>-o</string><string>BatchMode=yes</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-o</string><string>ServerAliveInterval=15</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-R</string><string>127.0.0.1:$rport:127.0.0.1:$lport</string>
    <string>$host</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST
}

case "${1:-status}" in
  install)
    host="${2:?用法:bash deploy/cn-tunnel.sh install <ssh主机别名> [远端端口]}"
    _gen_plist "$host" "${3:-18849}"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST_DST"
    launchctl enable "$DOMAIN/$LABEL"
    echo "✅ 已安装:本机 $(_web_port) → $host 的 127.0.0.1:${3:-18849}。开机自启 + 断线自动重连。"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "✅ 已卸载并断开隧道。"
    ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL" && echo "✅ 已重连。" ;;
  status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|runs =|last exit code" || echo "未安装(先 install)" ;;
  logs)
    tail -n 30 "$LOG" 2>/dev/null || echo "(无)" ;;
  *)
    echo "用法:bash deploy/cn-tunnel.sh {install <ssh主机别名> [远端端口]|uninstall|restart|status|logs}"; exit 1 ;;
esac
