#!/usr/bin/env zsh
# 温和重启 claude-hermes serve:只杀 serve 子进程,run.sh 守护循环 5s 后自动用 main 最新代码拉起。
# 与 stop.sh 的区别:stop.sh 是彻底停机(touch data/.stop);本脚本用于「改完代码要生效」。
# AI 汇报重启结果时必须引用本脚本输出的 PID 证据,不许凭记忆口述(见记忆
# hermes-injection-hallucination-rootcause:7/6 曾虚构重启成功)。
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f data/.stop ]; then
  echo "❌ data/.stop 存在,守护循环已停;先 rm data/.stop 再 zsh deploy/run.sh" >&2
  exit 1
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
