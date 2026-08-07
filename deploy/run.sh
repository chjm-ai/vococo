#!/usr/bin/env zsh
# 启动 vococo serve。默认后台启动；--foreground 由 launchd 前台监督。
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$ROOT/data"
LOG_DIR="$DATA_DIR/logs"
LOCK_DIR="$DATA_DIR/supervisor.lock"
SUPERVISOR_PID_FILE="$DATA_DIR/supervisor.pid"
CHILD_PID_FILE="$DATA_DIR/child.pid"
RESTART_DELAY="${VOCOCO_RESTART_DELAY:-5}"
CURRENT_CHILD_PID=""
mkdir -p "$LOG_DIR"

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

_is_supervisor() {
  local command
  command="$(_process_command "$1")" || return 1
  [ "$command" = "zsh $ROOT/deploy/run.sh --foreground" ] \
    || [ "$command" = "/bin/zsh $ROOT/deploy/run.sh --foreground" ]
}

_write_pid() {
  local pid_file="$1" pid="$2" temporary="$1.$$"
  print -r -- "$pid" > "$temporary"
  mv -f "$temporary" "$pid_file"
}

_remove_own_pid() {
  local pid_file="$1" expected="$2" recorded
  recorded="$(_read_pid "$pid_file")" || return 0
  [ "$recorded" = "$expected" ] && rm -f "$pid_file"
}

_acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    return 0
  fi

  local recorded
  recorded="$(_read_pid "$SUPERVISOR_PID_FILE")" || {
    echo "❌ 监督者锁已存在但 PID 尚未就绪；拒绝重复启动" >&2
    return 1
  }
  if kill -0 "$recorded" 2>/dev/null && _is_supervisor "$recorded"; then
    echo "❌ 监督者已在运行(PID: $recorded)" >&2
    return 1
  fi

  rm -f "$SUPERVISOR_PID_FILE" "$CHILD_PID_FILE"
  rmdir "$LOCK_DIR" 2>/dev/null || {
    echo "❌ 无法清理陈旧监督者锁" >&2
    return 1
  }
  mkdir "$LOCK_DIR" 2>/dev/null || {
    echo "❌ 另一监督者正在启动" >&2
    return 1
  }
}

_ensure_venv() {
  local python_path needs_venv=0
  if [ ! -x "$ROOT/.venv/bin/vococo" ]; then
    needs_venv=1
  else
    python_path="$(head -1 "$ROOT/.venv/bin/vococo" | sed 's/^#!//; s/ .*//')"
    [ -n "$python_path" ] && [ "$python_path" != "/usr/bin/env" ] \
      && [ ! -x "$python_path" ] && needs_venv=1
  fi
  [ "$needs_venv" -eq 0 ] && return 0
  echo "[run.sh] .venv/bin/vococo 不可用，先 uv sync 重装 entry point"
  (cd "$ROOT" && uv sync) || { echo "❌ uv sync 失败" >&2; return 1; }
}

_transaction_field() {
  local field="$1"
  [ -x "$ROOT/.venv/bin/python" ] || return 1
  FIELD="$field" "$ROOT/.venv/bin/python" -c \
    'import json,os; print(json.load(open("data/restart_transaction.json")).get(os.environ["FIELD"], ""))' \
    2>/dev/null
}

_maybe_rollback() {
  [ "$fastfail" -ge 3 ] || return 0
  [ -f "$DATA_DIR/restart_transaction.json" ] || return 0
  local stable candidate head dirty
  stable="$(_transaction_field stable_revision)" || return 0
  candidate="$(_transaction_field candidate_revision)" || return 0
  head="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null)" || return 0
  dirty="$(git -C "$ROOT" status --porcelain --untracked-files=normal 2>/dev/null)" || return 0
  [ -n "$stable" ] && [ "$head" = "$candidate" ] && [ -z "$dirty" ] || return 0
  echo "[run.sh] 启动连崩 $fastfail 次 —— 回滚到 $stable $(date '+%F %T')"
  git -C "$ROOT" reset --hard "$stable" && touch "$DATA_DIR/.rollback_done"
  fastfail=0
}

_cleanup() {
  if [ -n "$CURRENT_CHILD_PID" ] && kill -0 "$CURRENT_CHILD_PID" 2>/dev/null; then
    kill "$CURRENT_CHILD_PID" 2>/dev/null || true
    wait "$CURRENT_CHILD_PID" 2>/dev/null || true
  fi
  _remove_own_pid "$CHILD_PID_FILE" "${CURRENT_CHILD_PID:-0}"
  _remove_own_pid "$SUPERVISOR_PID_FILE" "$$"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

_run_foreground() {
  [ -f "$DATA_DIR/.stop" ] && return 0
  _acquire_lock || return 1
  _write_pid "$SUPERVISOR_PID_FILE" "$$"
  trap '_cleanup' EXIT
  trap 'exit 0' TERM INT HUP
  _ensure_venv || return 1

  local started_at elapsed exit_code fastfail=0
  while [ ! -f "$DATA_DIR/.stop" ]; do
    started_at="$(date +%s)"
    PYTHONUNBUFFERED=1 "$ROOT/.venv/bin/vococo" serve &
    CURRENT_CHILD_PID=$!
    _write_pid "$CHILD_PID_FILE" "$CURRENT_CHILD_PID"
    wait "$CURRENT_CHILD_PID"
    exit_code=$?
    _remove_own_pid "$CHILD_PID_FILE" "$CURRENT_CHILD_PID"
    CURRENT_CHILD_PID=""
    [ -f "$DATA_DIR/.stop" ] && break

    elapsed=$(( $(date +%s) - started_at ))
    if [ "$elapsed" -lt 20 ]; then
      fastfail=$((fastfail + 1))
    else
      fastfail=0
    fi
    _maybe_rollback
    echo "[run.sh] serve 退出码 $exit_code —— ${RESTART_DELAY}s 后重启 $(date '+%F %T')"
    sleep "$RESTART_DELAY"
  done
}

_start_background() {
  rm -f "$DATA_DIR/.stop"
  nohup zsh "$ROOT/deploy/run.sh" --foreground >> "$LOG_DIR/vococo.out.log" 2>&1 &
  local launched_pid=$!
  disown
  local attempt recorded
  for attempt in {1..30}; do
    sleep 0.1
    recorded="$(_read_pid "$SUPERVISOR_PID_FILE")" || continue
    if [ "$recorded" = "$launched_pid" ] && _is_supervisor "$recorded"; then
      echo "✅ serve 监督者已后台启动(PID: $recorded；日志:data/logs/vococo.out.log)"
      return 0
    fi
  done
  echo "❌ 启动失败，请查看 data/logs/vococo.out.log" >&2
  return 1
}

cd "$ROOT"
if [ "${1:-}" = "--foreground" ]; then
  _run_foreground
else
  _start_background
fi
