#!/usr/bin/env zsh
# 停止监督者及其准确记录的 serve 子进程，不按进程名查杀。
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p data
touch data/.stop

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

_matches_role() {
  local pid="$1" role="$2" command
  command="$(_process_command "$pid")" || return 1
  if [ "$role" = "child" ]; then
    [ "$command" = "$ROOT/.venv/bin/vococo serve" ] \
      || [ "$command" = "/bin/sh $ROOT/.venv/bin/vococo serve" ] \
      || [[ "$command" == "$ROOT"/.venv/bin/python*\ "$ROOT"/.venv/bin/vococo\ serve ]]
    return
  fi
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

_stop_recorded() {
  local pid_file="$1" role="$2" expected_parent="${3:-}" pid
  pid="$(_read_pid "$pid_file")" || { rm -f "$pid_file"; return 1; }
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    return 1
  fi
  if ! _matches_role "$pid" "$role"; then
    echo "⚠️ PID $pid 不属于本仓库的 $role，未终止并清理陈旧记录" >&2
    rm -f "$pid_file"
    return 1
  fi
  if [ "$role" = "child" ] \
    && { [ -z "$expected_parent" ] || [ "$(_parent_pid "$pid")" != "$expected_parent" ]; }; then
    echo "⚠️ PID $pid 不属于当前监督者，未终止并清理陈旧记录" >&2
    rm -f "$pid_file"
    return 1
  fi
  kill "$pid" 2>/dev/null || return 1
  return 0
}

stopped=0
supervisor_pid="$(_read_pid data/supervisor.pid)" || supervisor_pid=""
if [ -n "$supervisor_pid" ] \
  && { ! kill -0 "$supervisor_pid" 2>/dev/null || ! _matches_role "$supervisor_pid" supervisor; }; then
  supervisor_pid=""
fi
_stop_recorded data/child.pid child "$supervisor_pid" && stopped=1

for attempt in {1..20}; do
  supervisor_pid="$(_read_pid data/supervisor.pid)" || break
  kill -0 "$supervisor_pid" 2>/dev/null || break
  sleep 0.05
done
_stop_recorded data/supervisor.pid supervisor && stopped=1

if [ "$stopped" -eq 1 ]; then
  echo "✅ 已停止"
else
  echo "(没在跑；已保留停止标记)"
fi
