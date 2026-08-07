#!/usr/bin/env zsh
# 温和重启：只终止 PID 文件记录且身份验证通过的 serve 子进程。
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RESTART_ATTEMPTS="${VOCOCO_RESTART_ATTEMPTS:-300}"
cd "$ROOT"

_read_pid() {
  local pid_file="$1" value
  [ -f "$pid_file" ] || return 1
  value="$(<"$pid_file")"
  [[ "$value" == <-> ]] || return 1
  print -r -- "$value"
}

_process_command() {
  ps -p "$1" -o command= 2>/dev/null
}

_is_child() {
  local command
  command="$(_process_command "$1")" || return 1
  [ "$command" = "$ROOT/.venv/bin/vococo serve" ] \
    || [ "$command" = "/bin/sh $ROOT/.venv/bin/vococo serve" ] \
    || [[ "$command" == "$ROOT"/.venv/bin/python*\ "$ROOT"/.venv/bin/vococo\ serve ]]
}

_is_supervisor() {
  local command
  command="$(_process_command "$1")" || return 1
  [ "$command" = "zsh $ROOT/deploy/run.sh --foreground" ] \
    || [ "$command" = "/bin/zsh $ROOT/deploy/run.sh --foreground" ]
}

_parent_pid() {
  local parent
  parent="$(ps -p "$1" -o ppid= 2>/dev/null)" || return 1
  parent="${parent//[[:space:]]/}"
  [[ "$parent" == <-> ]] || return 1
  print -r -- "$parent"
}

_health_field() {
  local field="$1" payload
  command -v curl >/dev/null 2>&1 || return 1
  payload="$(curl -fsS --max-time 3 "${VOCOCO_HEALTH_URL:-http://127.0.0.1:${WEB_PORT:-8848}/healthz}")" || return 1
  HEALTH_PAYLOAD="$payload" HEALTH_FIELD="$field" python3 -c '
import json, os
data = json.loads(os.environ["HEALTH_PAYLOAD"])
if data.get("ok") is not True:
    raise SystemExit(1)
value = data.get(os.environ["HEALTH_FIELD"])
if value is None:
    raise SystemExit(1)
print(value)
' 2>/dev/null
}

if [ -f data/.stop ]; then
  echo "❌ data/.stop 存在，监督者已停止；请运行 zsh deploy/run.sh" >&2
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
    print('\\n'.join(x for x in xs if x != self_key))
except Exception:
    pass
" 2>/dev/null)
  if [ -n "$active" ] && [ "$FORCE" -ne 1 ]; then
    echo "⚠️ 以下会话可能正在进行中："
    echo "$active" | sed 's/^/   - /'
    if ! read -q "?仍要强制重启吗? [y/N] "; then
      echo
      echo "❌ 已取消。可加 --force 跳过提示。" >&2
      exit 1
    fi
    echo
  fi
fi

old_pid="$(_read_pid data/child.pid)" || {
  rm -f data/child.pid
  echo "❌ serve 子进程 PID 文件缺失或无效" >&2
  exit 1
}
if ! kill -0 "$old_pid" 2>/dev/null; then
  rm -f data/child.pid
  echo "❌ serve 子进程 PID 已失效，已清理陈旧记录" >&2
  exit 1
fi
if ! _is_child "$old_pid"; then
  rm -f data/child.pid
  echo "❌ PID $old_pid 不属于本仓库的 serve 子进程，拒绝终止" >&2
  exit 1
fi

supervisor_pid="$(_read_pid data/supervisor.pid)" || {
  rm -f data/supervisor.pid
  echo "❌ 监督者 PID 文件缺失或无效，拒绝重启" >&2
  exit 1
}
if ! kill -0 "$supervisor_pid" 2>/dev/null || ! _is_supervisor "$supervisor_pid"; then
  rm -f data/supervisor.pid
  echo "❌ 监督者 PID 已失效或不属于本仓库，拒绝重启" >&2
  exit 1
fi
if [ "$(_parent_pid "$old_pid")" != "$supervisor_pid" ]; then
  rm -f data/child.pid
  echo "❌ PID $old_pid 不属于当前监督者，拒绝终止" >&2
  exit 1
fi

old_boot=""
if [ "${VOCOCO_SKIP_HEALTH_CHECK:-0}" != "1" ]; then
  old_boot="$(_health_field boot_id)" || {
    echo "❌ /healthz 不可用，拒绝把当前服务当作可验证的重启目标" >&2
    exit 1
  }
fi

echo "旧 serve PID: $old_pid"
kill "$old_pid"
for (( attempt = 1; attempt <= RESTART_ATTEMPTS; attempt++ )); do
  sleep 0.1
  new_pid="$(_read_pid data/child.pid)" || continue
  if [ "$new_pid" != "$old_pid" ] && kill -0 "$new_pid" 2>/dev/null && _is_child "$new_pid"; then
    if [ -n "$old_boot" ]; then
      new_boot="$(_health_field boot_id)" || continue
      health_pid="$(_health_field pid)" || continue
      [ "$new_boot" != "$old_boot" ] && [ "$health_pid" = "$new_pid" ] || continue
    fi
    echo "新 serve PID: $new_pid"
    echo "HEAD: $(git log --oneline -1 2>/dev/null || echo unknown)"
    echo "✅ 重启完成"
    exit 0
  fi
done
echo "❌ 30s 没等到经过身份验证的新 serve 子进程" >&2
exit 1
