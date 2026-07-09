#!/usr/bin/env zsh
# 温和重启 claude-hermes serve:只杀 serve 子进程,run.sh 守护循环 5s 后自动用 main 最新代码拉起。
# 与 stop.sh 的区别:stop.sh 是彻底停机(touch data/.stop);本脚本用于「改完代码要生效」。
# AI 汇报重启结果时必须引用本脚本输出的 PID 证据,不许凭记忆口述(见记忆
# hermes-injection-hallucination-rootcause:7/6 曾虚构重启成功)。
#
# 重启是硬杀整个进程(kill),不区分会话:其他会话若正有轮次在跑,回复会被硬生生
# 打断、历史留空、用户毫无提示(2026-07-06 踩过连坐事故)。故杀之前先查
# data/active_sessions.json(gateway/clarify.py 维护的"在跑会话"登记表),
# 非空则提示确认;--force 跳过此提示(用于自动化场景)。
# --self=<session_key>:排除发起重启的会话自己(它必然在跑,不算"连坐"——
# 它自己重启完会靠 restart_self 的"遗书"机制自动还魂续聊,merge-main.sh --restart
# 会自动算出并传入)。排除后若已无其他人在跑,直接重启,不再询问。
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f data/.stop ]; then
  echo "❌ data/.stop 存在,守护循环已停;先 rm data/.stop 再 zsh deploy/run.sh" >&2
  exit 1
fi

FORCE=0
SELF_KEY=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --self=*) SELF_KEY="${arg#--self=}" ;;
  esac
done

ACTIVE_FILE="data/active_sessions.json"
if [ -f "$ACTIVE_FILE" ]; then
  active=$(SELF_KEY="$SELF_KEY" python3 -c "
import json, os
try:
    xs = json.load(open('$ACTIVE_FILE'))
    self_key = os.environ.get('SELF_KEY', '')
    xs = [x for x in xs if x != self_key]
    print('\n'.join(xs))
except Exception:
    pass
" 2>/dev/null)
  if [ -n "$active" ]; then
    echo "⚠️ 以下会话可能正在进行中(重启会把它们当前的回复硬生生打断,历史留空):"
    echo "$active" | sed 's/^/   - /'
    if [ "$FORCE" -eq 1 ]; then
      echo "→ 已指定 --force,继续重启。"
    elif read -q "?仍要强制重启吗? [y/N] "; then
      echo
    else
      echo
      echo "❌ 已取消。等它们结束后再重启,或加 --force 跳过此提示。" >&2
      exit 1
    fi
  fi
fi

# 模式注意 python3:写成 "bin/python .*"(带空格)匹配不到,踩过。
PAT="bin/python[0-9]* .*claude-hermes serve"
n=$(pgrep -f "$PAT" | wc -l | tr -d ' ')
old=$(pgrep -f "$PAT" | head -1)
if [ -z "$old" ]; then
  echo "❌ 没找到 serve 子进程。sandbox 里 pgrep 看不到进程 —— 换非沙箱终端跑本脚本" >&2
  exit 1
fi
[ "$n" -gt 1 ] && echo "⚠️ 发现 $n 个 serve 实例(孤儿进程会抢 TG 轮询),只杀最早的 $old,其余请手查"

echo "旧 serve PID: $old"
kill "$old"

for i in {1..30}; do
  sleep 1
  new=$(pgrep -f "$PAT" | head -1)
  if [ -n "$new" ] && [ "$new" != "$old" ]; then
    echo "新 serve PID: $new(等了 ${i}s)"
    echo "HEAD: $(git log --oneline -1 | cat)"
    echo "✅ 重启完成;验证:tail data/logs/hermes.out.log 应见「✅ Web/Telegram 已上线」"
    exit 0
  fi
done
echo "❌ 30s 没等到新进程 —— 查 data/logs/hermes.out.log" >&2
exit 1
