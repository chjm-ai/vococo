#!/usr/bin/env zsh
# 启动 vococo serve(后台,崩溃自动重启)。
#
# 为什么不用 launchd 直接拉起:某些 Mac 上 launchd 的会话上下文不完整,
# 会让 agent 派生的 `claude` 子进程同步卡死(冻住事件循环)。用登录 shell
# (nohup zsh -lc)启动可恢复完整会话上下文,稳定可用。
#
# 崩溃自启:serve 非正常退出后等 5s 重启;直到 stop.sh 写下 data/.stop 才停。
# 重启电脑后重新跑一次本脚本即可;想开机自启见 README「常驻」。
#
# 自我修改保险丝:agent 用 restart_self 改完自身代码重启(退出码 51)后,
# 若新代码【启动即崩】(活不过 20s)连续 3 次,按遗书 data/resume_task.json
# 里的 rollback_commit 执行 git reset --hard 回滚,并 touch data/.rollback_done
# 让还魂消息告知用户「已回滚」。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p data/logs
pkill -f "vococo serve" 2>/dev/null || true
rm -f data/.stop          # 清除上次的停止标记,允许重启循环运行
sleep 1
# 包改名/入口变动后自愈:入口缺失,或 shebang 指向的解释器已失效(改名后 venv 里
# python 旧路径残留,文件在但解释器没了)就重装 editable(uv sync 幂等,秒级)。
# 2026-08 改名 claude-hermes→vococo 后只查入口存在,漏掉了 bad interpreter,
# 曾连续 127 死循环 3 分钟(服务全挂)才被人手修好——故这里连解释器一起查。
_needs_venv=0
if [ ! -x .venv/bin/vococo ]; then
  _needs_venv=1
else
  _py="$(head -1 .venv/bin/vococo | sed 's/^#!//; s/ .*//')"
  # /usr/bin/env 形式的 shebang 视为有效(env 必在);绝对路径则要真实可执行
  if [ -n "$_py" ] && [ "$_py" != "/usr/bin/env" ] && [ ! -x "$_py" ]; then
    _needs_venv=1
  fi
fi
if [ "$_needs_venv" = 1 ]; then
  echo "[run.sh] .venv/bin/vococo 不可用(入口缺失或解释器失效),先 uv sync 重装 entry point"
  uv sync || { echo "❌ uv sync 失败" >&2; exit 1; }
fi
nohup zsh -lc "
  cd '$ROOT'
  fastfail=0
  while [ ! -f data/.stop ]; do
    # 保证线上始终跑 main:每轮(重)启动前若不在 main 就切回(切不动只警告,不阻塞启动)
    cur=\$(git symbolic-ref --short -q HEAD || echo '')
    if [ \"\$cur\" != 'main' ]; then
      if git checkout main 2>/dev/null; then
        echo \"[run.sh] 已切回 main(原:\$cur) \$(date '+%F %T')\"
      else
        echo \"[run.sh] 警告:当前在 \$cur 且切 main 失败(工作区可能有改动),本轮先用当前分支 \$(date '+%F %T')\"
      fi
    fi
    t0=\$(date +%s)
    PYTHONUNBUFFERED=1 .venv/bin/vococo serve
    ec=\$?
    [ -f data/.stop ] && break
    if [ \$(( \$(date +%s) - t0 )) -lt 20 ]; then
      fastfail=\$((fastfail + 1))
    else
      fastfail=0
    fi
    if [ \$fastfail -ge 3 ] && [ -f data/resume_task.json ]; then
      rb=\$(.venv/bin/python -c 'import json;print(json.load(open(\"data/resume_task.json\")).get(\"rollback_commit\") or \"\")' 2>/dev/null)
      if [ -n \"\$rb\" ]; then
        echo \"[run.sh] 启动连崩 \$fastfail 次 —— 回滚到 \$rb \$(date '+%F %T')\"
        git reset --hard \"\$rb\" && touch data/.rollback_done
        fastfail=0
      fi
    fi
    echo \"[run.sh] serve 退出码 \$ec —— 5s 后重启 \$(date '+%F %T')\"
    sleep 5
  done
" >> data/logs/vococo.out.log 2>&1 &
disown
sleep 3
if pgrep -f "vococo serve" >/dev/null; then
  echo "✅ serve 已后台启动(崩溃自启;日志:data/logs/vococo.out.log)"
else
  echo "❌ 启动失败,看 data/logs/vococo.out.log"; exit 1
fi
