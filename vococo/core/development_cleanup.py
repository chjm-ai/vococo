"""开发工作树的保守巡检：首次见到的存量只列清单，新目录观察七天后才回收。"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, closing
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import secrets
import shutil
import time

from .. import config
from ..memory import session_store
from .repository_locks import lease, session_digest

_IDLE_SECONDS = 7 * 86400
_STATE_VERSION = 1


def _git(path: Path, *args: str) -> str:
    # 禁止 status 刷新索引；诊断和资格检查本身不应改动用户工作区。
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(path), *args], env=environment,
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        raise ValueError("Git 检查失败")
    return result.stdout.rstrip("\n")


def _git_path(path: Path, option: str) -> Path:
    return Path(_git(path, "rev-parse", "--path-format=absolute", option)).resolve()


def _roots() -> list[Path]:
    values = [config.ROOT_DIR, *(row["path"] for row in session_store.list_projects())]
    roots = {Path(value).expanduser().resolve() for value in values}
    return sorted(root for root in roots if (root / ".git").exists())


def _sessions() -> dict[str, list[dict]]:
    rows = session_store._conn().execute(
        "SELECT m.session_key, m.worktree_path, m.agent_id, "
        "MAX(COALESCE(m.bound_at,0), COALESCE(MAX(t.ts),0)) AS activity "
        "FROM session_meta m LEFT JOIN turns t ON t.session_key=m.session_key "
        "WHERE m.worktree_path IS NOT NULL AND m.worktree_path!='' GROUP BY m.session_key"
    )
    result: dict[str, list[dict]] = {}
    for key, path, owner, activity in rows:
        result.setdefault(str(Path(path).expanduser().absolute()), []).append(
            {"key": key, "owner": owner or _legacy_owner(key), "activity": float(activity)}
        )
    return result


def _legacy_owner(key: str) -> str | None:
    from ..memory import agents

    try:
        owner = agents.agent_for_session(key)
    except ValueError:
        return None
    return owner["id"] if owner else None


def _task_state(sessions: list[dict], path: Path) -> tuple[bool, float]:
    from . import tasks

    if not tasks._db_path().exists():
        return False, 0.0
    keys = {row["key"] for row in sessions}
    busy, activity = False, 0.0
    with tasks.read_connection():
        rows = tasks._conn().execute("SELECT id, session_key, cwd, status, updated_at FROM tasks")
        for row in rows:
            key = row["session_key"] or tasks.session_key(row["id"])
            cwd = row["cwd"]
            if key not in keys and (not cwd or not Path(cwd).expanduser().resolve().is_relative_to(path)):
                continue
            busy |= row["status"] in {"queued", "running"}
            activity = max(activity, float(row["updated_at"]))
    return busy, activity


def _runtime_busy(sessions: list[dict]) -> bool:
    from ..gateway import clarify, web_bridge
    from ..tools.sessions import claim_pending
    from ..cron import scheduler
    from . import task_runner, tasks

    running = {tasks.session_key(task_id) for task_id in task_runner._running}
    scripts = scheduler._script_running | set(scheduler._script_tasks) | set(scheduler._script_processes)
    # 不调用 script_session_busy：它会 load_jobs，可能迁移并回写旧配置。
    if any(not scheduler._script_sessions.get(job_id) for job_id in scripts):
        return True
    running.update(scheduler._script_sessions[job_id] for job_id in scripts)
    return any(row["key"] in clarify._active_sessions or clarify.has_pending(row["key"])
               or claim_pending(row["key"]) or row["key"] in running
               or web_bridge.session_busy(row["key"]) for row in sessions)


def _listing(root: Path) -> list[dict]:
    text = _git(root, "worktree", "list", "--porcelain", "-z")
    entries = []
    for block in text.split("\0\0"):
        fields = [field for field in block.split("\0") if field]
        if not fields:
            continue
        entry = dict(field.split(" ", 1) if " " in field else (field, "") for field in fields)
        entries.append(entry)
    return entries


def _candidates(roots: list[Path], sessions: dict) -> dict[Path, Path]:
    result = {}
    legacy_base = config.DATA_DIR.resolve() / "worktrees"
    for root in roots:
        if not (root / ".git").exists():
            continue
        for entry in _listing(root)[1:]:
            path = Path(entry["worktree"]).absolute()
            managed = path.is_relative_to(root / "data" / "worktrees") or path.is_relative_to(legacy_base)
            if managed or str(path) in sessions:
                result[path] = root
        for path in (root / "data" / "worktrees").glob("*/*"):
            if path.is_dir() or path.is_symlink():
                result.setdefault(path.absolute(), root)
    if roots:
        for path in legacy_base.glob("*/*"):
            if path.is_dir() or path.is_symlink():
                result.setdefault(path.absolute(), roots[0])
    return result


def _validate(root: Path, path: Path) -> tuple[str, Path]:
    if root == path or path.is_symlink() or path.resolve() != path or not (path / ".git").is_file():
        raise ValueError("不是受管的独立工作树")
    if (path / ".git").is_symlink() or Path(_git(path, "rev-parse", "--show-toplevel")) != path:
        raise ValueError("工作树路径异常")
    if _git_path(root, "--git-common-dir") != _git_path(path, "--git-common-dir"):
        raise ValueError("工作树来源不匹配")
    entries = _listing(root)
    entry = next((row for row in entries[1:] if row["worktree"] == str(path)), None)
    if entry is None or "locked" in entry or "prunable" in entry:
        raise ValueError("工作树登记异常或已锁定")
    branch = entry.get("branch", "")
    if not branch.startswith(("refs/heads/vococo/", "refs/heads/hermes/")):
        raise ValueError("不是开发会话分支")
    return branch, _git_path(path, "--git-dir")


def _preserve_reason(root: Path, path: Path, branch: str, git_dir: Path) -> str:
    common = _git_path(root, "--git-common-dir")
    if any(git_dir.glob("*.lock")) or any(common.glob("*.lock")):
        return "Git 操作未结束"
    if any((git_dir / name).exists() for name in ("config.worktree", "info")):
        return "有工作树专属配置或引用"
    if any((git_dir / "refs").rglob("*")):
        return "有工作树专属配置或引用"
    operations = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply")
    if any((git_dir / name).exists() for name in operations):
        return "合并或变基未结束"
    entries = _git(path, "ls-files", "--stage", "-z").split("\0")
    if any(entry.startswith("160000 ") for entry in entries):
        return "含子模块，需人工确认"
    flags = _git(path, "ls-files", "-v", "-z").split("\0")
    if any(flag[:1].islower() or flag.startswith("S ") for flag in flags if flag):
        return "索引隐藏了文件状态"
    if _git(path, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"):
        return "有未提交或未跟踪成果"
    # 不按目录名豁免依赖缓存，node_modules/.venv 内也可能放着本地配置和手工成果。
    if _git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"):
        return "有本地配置或其它忽略文件"
    return _history_reason(root, path, branch)


def _history_reason(root: Path, path: Path, branch: str) -> str:
    references = _git(root, "for-each-ref", "--format=%(refname)", "refs/heads/main", "refs/heads/master").splitlines()
    base = next((ref for ref in references if ref == "refs/heads/main"), None)
    base = base or next((ref for ref in references if ref == "refs/heads/master"), None)
    if not base:
        return "默认主分支无法确认"
    if (_git(root, "rev-parse", "--is-shallow-repository") != "false"
            or (_git_path(root, "--git-common-dir") / "info" / "grafts").exists()):
        return "浅克隆或改写历史，需人工确认"
    commits = {_git(path, "rev-parse", "HEAD"), _git(root, "rev-parse", branch)}
    commits.update(_private_commits(path))
    if _git(root, "rev-list", *sorted(commits), "--not", base):
        return "有未合并提交（含重置前历史）"
    return ""


def _reflogs(path: Path) -> list[bytes]:
    branch = _git(path, "symbolic-ref", "HEAD")
    files = [_git_path(path, "--git-dir") / "logs" / "HEAD",
             _git_path(path, "--git-common-dir") / "logs" / branch]
    if any(file.is_symlink() for file in files):
        raise ValueError("历史记录路径异常")
    return [file.read_bytes() for file in files if file.exists()]


def _private_commits(path: Path) -> set[str]:
    values = []
    for content in _reflogs(path):
        for line in content.splitlines():
            # reset 记录的旧 OID 也可能是唯一剩下的成果引用，%H 只会返回新 OID。
            old, new, *_ = line.split(b" ", 2)
            values.extend((old.decode("ascii"), new.decode("ascii")))
    for name in ("ORIG_HEAD", "FETCH_HEAD"):
        reference = _git_path(path, "--git-dir") / name
        if reference.is_symlink():
            raise ValueError("历史引用路径异常")
        if reference.exists():
            values.extend(line.split()[0] for line in reference.read_text().splitlines())
    if any(len(value) not in (40, 64) or set(value) - set("0123456789abcdef") for value in values):
        raise ValueError("历史引用损坏")
    return {value for value in values if set(value) != {"0"}}


def _identity(path: Path) -> str:
    info = path.stat()
    git_dir = _git_path(path, "--git-dir")
    return f"{info.st_dev}:{info.st_ino}:{git_dir}"


def _fingerprint(path: Path, sessions: list[dict], activity: float) -> str:
    # 目录 mtime 不能代表闲置；连干净文件被手工改后还原的情况也要重新计时。
    tracked = (path / name for name in _git(path, "ls-files", "-z").split("\0") if name)
    stamps = []
    for file in tracked:
        try:
            info = file.lstat()
            stamps.append((str(file.relative_to(path)), info.st_mtime_ns, info.st_ctime_ns))
        except FileNotFoundError:
            # 跟踪文件被删除是未提交成果，不是巡检故障；Git 状态检查仍会阻止回收。
            stamps.append((str(file.relative_to(path)), None, None))
    history = [hashlib.sha256(content).hexdigest() for content in _reflogs(path)]
    values = [_identity(path), _git(path, "rev-parse", "HEAD"), activity, stamps, history,
              sorted(_private_commits(path)), sorted((row["key"], row["activity"]) for row in sessions)]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _read_state(path: Path) -> dict:
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("巡检状态路径异常")
    if not path.exists():
        return {"version": _STATE_VERSION, "roots": [], "entries": {}}
    state = json.loads(path.read_text())
    if state["version"] != _STATE_VERSION or not isinstance(state["entries"], dict):
        raise ValueError("巡检状态损坏")
    if not isinstance(state["roots"], list):
        raise ValueError("巡检状态损坏")
    for entry in state["entries"].values():
        if not isinstance(entry["legacy"], bool) or not math.isfinite(entry["idle_since"]):
            raise ValueError("巡检记录损坏")
    return state


def _write_state(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    if temporary.is_symlink():
        raise ValueError("巡检临时文件路径异常")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(state, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def _observe(state: dict, root: Path, path: Path, fingerprint: str, now: float) -> dict:
    previous = state["entries"].get(str(path))
    identity = _identity(path)
    if not previous or previous.get("identity") != identity:
        legacy = bool(previous and previous["legacy"]) or str(root) not in state["roots"]
        previous = {"legacy": legacy, "idle_since": now,
                    "identity": identity, "fingerprint": fingerprint}
    if previous["fingerprint"] != fingerprint or previous["idle_since"] > now:
        previous.update(idle_since=now, fingerprint=fingerprint)
    state["entries"][str(path)] = previous
    return previous


def _inspect(root: Path, path: Path, sessions: list[dict], state: dict, now: float) -> dict:
    branch, git_dir = _validate(root, path)
    busy, activity = _task_state(sessions, path)
    fingerprint = _fingerprint(path, sessions, activity)
    observed = _observe(state, root, path, fingerprint, now)
    reason = _preserve_reason(root, path, branch, git_dir)
    if busy or _runtime_busy(sessions) or reason:
        observed["idle_since"] = now
    if observed["legacy"]:
        return {"reason": "旧存量仅列清单" + (f"；{reason}" if reason else "")}
    if path.parent != root / "data" / "worktrees" / session_store.project_hash(str(root)):
        return {"reason": "旧版或非标准路径，仅列清单"}
    if not sessions or any(not row["owner"] for row in sessions):
        return {"reason": "会话绑定或执行租约归属缺失，仅列清单"}
    if busy or _runtime_busy(sessions):
        return {"reason": "会话或任务正在执行/排队"}
    if reason:
        return {"reason": reason}
    last_activity = max([activity, *(row["activity"] for row in sessions)])
    if now - max(observed["idle_since"], last_activity) < _IDLE_SECONDS:
        return {"reason": "七天闲置观察期未满"}
    return {"reason": "", "branch": branch}


def _collect(root: Path, path: Path, sessions: list[dict], state: dict, now: float) -> dict:
    result = {"path": str(path), "removed": False, "reason": ""}
    try:
        with ExitStack() as stack:
            for row in sorted(sessions, key=lambda item: item["key"]):
                if row["owner"]:
                    stack.enter_context(lease(row["owner"], session_digest(row["key"]), exclusive=True))
            result.update(_inspect(root, path, sessions, state, now))
            if result["reason"]:
                return result
            # 删除前重新读绑定；会话归属、执行状态或文件检查失败均保留现场。
            with session_store._db.read_connection():
                fresh = _sessions().get(str(path), [])
            if fresh != sessions:
                result["reason"] = "会话绑定已变化"
                return result
            result.update(_inspect(root, path, fresh, state, time.time()))
            if not result["reason"]:
                _finish_collection(root, path, fresh, state, now, result)
    except BlockingIOError:
        result["reason"] = "正在执行或初始化"
        if str(path) in state["entries"]:
            state["entries"][str(path)]["idle_since"] = now
    except Exception as exc:
        result["reason"] = f"检查或回收失败：{type(exc).__name__}"
        if str(path) in state["entries"]:
            state["entries"][str(path)]["idle_since"] = now
    return result


def _finish_collection(root: Path, path: Path, sessions: list[dict], state: dict, now: float, result: dict) -> None:
    _write_state(config.DATA_DIR / "development-cleanup-state.json", state)
    result.update(_retire(root, path, sessions, state["entries"][str(path)]["fingerprint"]))
    if not result["removed"]:
        state["entries"][str(path)]["idle_since"] = now
        return
    _clear_bindings(path, sessions)


def _open_handles(path: Path) -> bool:
    executable = shutil.which("lsof")
    if not executable:
        raise ValueError("无法确认目录是否仍被其它进程使用")
    result = subprocess.run([executable, "-t", "+D", str(path)], capture_output=True, timeout=15)
    if result.stderr or result.returncode not in (0, 1):
        raise ValueError("打开文件检查失败")
    return bool(result.stdout.strip())


def _retirement_reason(root: Path, path: Path, sessions: list[dict], fingerprint: str) -> str:
    branch, git_dir = _validate(root, path)
    busy, activity = _task_state(sessions, path)
    if busy or _runtime_busy(sessions) or _open_handles(path):
        return "目录仍被执行任务或其它进程使用"
    if _fingerprint(path, sessions, activity) != fingerprint:
        return "检查期间文件或历史发生变化，已保留"
    return _preserve_reason(root, path, branch, git_dir)


def _restore_retired(root: Path, original: Path, retired: Path, result: dict) -> None:
    if not retired.exists():
        return
    try:
        if original.exists():
            raise ValueError("原路径已被占用")
        _git(root, "worktree", "move", str(retired), str(original))
    except Exception:
        result["recovery_path"] = str(retired)
        result["reason"] += "；未能恢复原路径，成果留在恢复目录，请人工核对"


def _retire(root: Path, path: Path, sessions: list[dict], fingerprint: str) -> dict:
    # 先原子搬离公开执行路径再检查，避免非强制 Git remove 仍会丢弃临时写入的忽略文件。
    retired = path.with_name(f".retiring-{path.name}-{secrets.token_hex(6)}")
    result = {"removed": False, "reason": ""}
    try:
        _git(root, "worktree", "move", str(path), str(retired))
        result["reason"] = _retirement_reason(root, retired, sessions, fingerprint)
        if not result["reason"]:
            _git(root, "worktree", "remove", str(retired))
            result["removed"] = True
    except Exception as exc:
        result["reason"] = f"回收检查失败：{type(exc).__name__}"
    finally:
        _restore_retired(root, path, retired, result)
    return result


def _clear_bindings(path: Path, sessions: list[dict]) -> None:
    # 条件更新避免删除后误清同时建立的新绑定；分支保留，不额外扩大自动删除范围。
    uri = (config.DATA_DIR / "state.db").resolve().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection, connection:
        for row in sessions:
            connection.execute("UPDATE session_meta SET worktree_path=NULL WHERE session_key=? AND worktree_path=?",
                               (row["key"], str(path)))


def _seed_legacy(state: dict, candidates: dict[Path, Path], now: float) -> None:
    # 即使首轮因执行锁或损坏目录无法检查，也要把当时的存量永远标成只列清单。
    for path, root in candidates.items():
        if str(root) not in state["roots"]:
            state["entries"].setdefault(str(path), {"legacy": True, "idle_since": now,
                                                   "identity": None, "fingerprint": None})


def _sweep_sync() -> list[dict]:
    state_path = config.DATA_DIR / "development-cleanup-state.json"
    lock_path = state_path.with_suffix(".lock")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink() or lock_path.resolve() != lock_path.absolute():
        raise ValueError("巡检锁路径异常")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = _read_state(state_path)
        with session_store._db.read_connection():
            roots, sessions = _roots(), _sessions()
        candidates = _candidates(roots, sessions)
        _seed_legacy(state, candidates, time.time())
        _write_state(state_path, state)
        results = [_collect(root, path, sessions.get(str(path), []), state, time.time())
                   for path, root in candidates.items()]
        state["roots"] = sorted(set(state["roots"]) | {str(root) for root in roots})
        try:
            _write_state(state_path, state)
        except Exception as exc:
            results.append({"path": "", "removed": False,
                            "reason": f"巡检状态保存失败：{type(exc).__name__}；请核对本轮真实回收结果"})
    return results


async def sweep() -> list[dict]:
    try:
        return await asyncio.to_thread(_sweep_sync)
    except BlockingIOError:
        return []
    except Exception as exc:
        return [{"path": "", "removed": False, "reason": f"开发巡检失败，已保留文件：{type(exc).__name__}"}]
