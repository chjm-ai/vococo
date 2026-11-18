"""开发工作树巡检：仅回收归档满七天且通过复核的标准工作树。"""
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
import stat
import sys
import secrets
import shutil
import time

from .. import config
from ..memory import session_store
from .repository_locks import checkout_retirement_lease, lease, session_digest

_IDLE_SECONDS = 7 * 86400
_STATE_VERSION = 1


def _git(path: Path, *args: str) -> str:
    # 禁止 status 刷新索引；诊断和资格检查本身不应改动用户工作区。
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    result = subprocess.run(
        ["git", "--no-replace-objects", "-c", "core.fsmonitor=false", "-C", str(path), *args], env=environment,
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


def _sessions(path: Path | None = None):
    path_filter = "AND m.worktree_path=? " if path is not None else ""
    rows = session_store._conn().execute(
        "SELECT m.session_key, m.worktree_path, m.agent_id, a.archived_at, "
        "MAX(COALESCE(m.bound_at,0), COALESCE(MAX(t.ts),0)) AS activity "
        "FROM session_meta m LEFT JOIN turns t ON t.session_key=m.session_key "
        "LEFT JOIN session_archive_times a ON a.session_key=m.session_key AND m.archived=1 "
        "WHERE m.worktree_path IS NOT NULL AND m.worktree_path!='' "
        + path_filter + "GROUP BY m.session_key", (str(path),) if path is not None else (),
    )
    result: dict[str, list[dict]] = {}
    for key, bound_path, owner, archived_at, activity in rows:
        result.setdefault(str(Path(bound_path).expanduser().absolute()), []).append(
            {"key": key, "owner": owner or _legacy_owner(key), "activity": float(activity),
             "archived_at": archived_at}
        )
    return result if path is None else result.get(str(path), [])


def _legacy_owner(key: str) -> str | None:
    from ..memory import agents

    try:
        owner = agents.agent_for_session(key)
    except ValueError:
        return None
    return owner["id"] if owner else None


def _task_state(sessions: list[dict], path: Path) -> tuple[str, float]:
    from . import tasks

    if not tasks._db_path().exists():
        return "", 0.0
    keys = {row["key"] for row in sessions}
    busy, review, activity = False, False, 0.0
    with tasks.read_connection(), session_store._db.read_connection():
        rows = tasks._conn().execute("SELECT id, session_key, cwd, review_cwd, status, updated_at FROM tasks")
        for row in rows:
            key = row["session_key"] or tasks.session_key(row["id"])
            review_path, cwd = row["review_cwd"], row["cwd"]
            has_review = _under_checkout(review_path, path)
            if key not in keys and not has_review and not _under_checkout(cwd, path):
                continue
            busy |= row["status"] in {"queued", "running"}
            review |= has_review and not session_store.is_deleted_web_session(key)
            activity = max(activity, float(row["updated_at"]))
    if busy:
        return "会话或任务正在执行/排队", activity
    return ("仍有关联评审可续跑，保留原工作树" if review else ""), activity


def _under_checkout(value: str | None, path: Path) -> bool:
    return bool(value and Path(value).expanduser().resolve().is_relative_to(path))


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
    operations = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply")
    if any((git_dir / name).exists() for name in operations):
        return "合并或变基未结束"
    if _private_metadata(git_dir):
        return "有工作树专属配置或引用"
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


def _private_metadata(git_dir: Path) -> bool:
    standard = {"HEAD", "ORIG_HEAD", "FETCH_HEAD", "index", "gitdir", "commondir", "logs", "refs", "COMMIT_EDITMSG"}
    if any(entry.is_symlink() or entry.name not in standard for entry in git_dir.iterdir()):
        return True
    if any((git_dir / "refs").rglob("*")):
        return True
    return any(entry.is_symlink() or entry.name != "HEAD" for entry in (git_dir / "logs").glob("*"))


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
    task_reason, activity = _task_state(sessions, path)
    fingerprint = _fingerprint(path, sessions, activity)
    observed = _observe(state, root, path, fingerprint, now)
    reason = _preserve_reason(root, path, branch, git_dir)
    if task_reason or _runtime_busy(sessions) or reason:
        observed["idle_since"] = now
    if path.parent != root / "data" / "worktrees" / session_store.project_hash(str(root)):
        return {"reason": "旧版或非标准路径，仅列清单"}
    if len(sessions) != 1 or not sessions[0]["owner"]:
        return {"reason": "会话绑定或执行租约归属缺失，仅列清单"}
    if task_reason or _runtime_busy(sessions):
        return {"reason": task_reason or "会话或任务正在执行/排队"}
    if reason:
        return {"reason": reason}
    archived_at = sessions[0]["archived_at"]
    if archived_at is None:
        return {"reason": "旧存量仅列清单" if observed["legacy"] else "会话未归档，仅列清单"}
    if not math.isfinite(archived_at) or now - archived_at < _IDLE_SECONDS:
        return {"reason": "归档七天观察期未满"}
    from .worktree import _slug

    slug = _slug(sessions[0]["key"].split(":")[-1])
    if path.name != slug or branch != f"refs/heads/vococo/{slug}":
        return {"reason": "会话分支不可按原版本重建，仅列清单"}
    if now - max(observed["idle_since"], activity, sessions[0]["activity"]) < _IDLE_SECONDS:
        return {"reason": "七天闲置观察期未满"}
    return {"reason": "", "branch": branch}


def _collect(root: Path, path: Path, sessions: list[dict], state: dict, now: float) -> dict:
    result = {"path": str(path), "removed": False, "reason": ""}
    try:
        with ExitStack() as stack:
            for row in sorted(sessions, key=lambda item: item["key"]):
                if row["owner"]:
                    stack.enter_context(lease(row["owner"], session_digest(row["key"]), exclusive=True))
            if sessions != _current_sessions(path):
                result["reason"] = "会话绑定或状态已变化，稍后复查"
                return result
            result.update(_inspect(root, path, sessions, state, now))
            if result["reason"]:
                return result
            _finish_collection(root, path, sessions, state, now, result)
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
    fingerprint = state["entries"][str(path)]["fingerprint"]
    result.update(_retire(root, path, lambda retired: _retirement_reason(
        root, retired, sessions, fingerprint, original=path)))
    if not result["removed"]:
        state["entries"][str(path)]["idle_since"] = now
        return
    _clear_bindings(path, sessions)


def _open_handles(path: Path) -> bool:
    executable = shutil.which("lsof")
    if not executable:
        raise ValueError("无法确认目录是否仍被其它进程使用")
    result = subprocess.run([executable, "+D", str(path)], capture_output=True, timeout=15)
    if result.stderr or result.returncode not in (0, 1):
        raise ValueError("打开文件检查失败")
    return bool(result.stdout.strip())


def _current_sessions(path: Path) -> list[dict]:
    with session_store._db.read_connection():
        return _sessions(path)


def _retirement_reason(root: Path, path: Path, sessions: list[dict], fingerprint: str, *, original: Path) -> str:
    branch, git_dir = _validate(root, path)
    task_reason, activity = _task_state(sessions, original)
    if sessions != _current_sessions(original):
        return "会话绑定或状态已变化，已保留"
    if task_reason or _runtime_busy(sessions):
        return task_reason or "目录仍被执行任务使用"
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


def _check_filesystem(path: Path) -> None:
    device = path.stat().st_dev

    def raise_error(error):
        raise error

    for parent, directories, files in os.walk(path, onerror=raise_error, followlinks=False):
        for name in directories + files:
            entry = (Path(parent) / name).lstat()
            if entry.st_dev != device or not (stat.S_ISDIR(entry.st_mode)
                    or stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode)):
                raise ValueError("工作树包含挂载点或特殊文件")


def _blob_hash(path: Path, mode: str, algorithm: str) -> str:
    info = path.lstat()
    if mode == "120000" and stat.S_ISLNK(info.st_mode):
        target = os.fsencode(os.readlink(path))
        return hashlib.new(algorithm, f"blob {len(target)}\0".encode() + target).hexdigest()
    if not stat.S_ISREG(info.st_mode) or mode != ("100755" if info.st_mode & stat.S_IXUSR else "100644"):
        return ""
    digest = hashlib.new(algorithm, f"blob {info.st_size}\0".encode())
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if os.fstat(source.fileno()) != info:
            return ""
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.lstat()
    if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        return ""
    return digest.hexdigest()


def _rebuild_reason(path: Path) -> str:
    # Git status 可能经过过滤器或换行转换。只有原始文件字节与对象一致才允许删除。
    algorithm = _git(path, "rev-parse", "--show-object-format")
    entries = _git(path, "ls-files", "--stage", "-z")
    tracked = {path / ".git"}
    for entry in filter(None, entries.split("\0")):
        meta, name = entry.split("\t", 1)
        mode, revision, stage = meta.split()
        file = path / name
        tracked.add(file)
        if stage != "0" or revision != _blob_hash(file, mode, algorithm):
            return "跟踪文件与 Git 原始内容不同，无法保证按原样恢复"
    return "有未登记的本地文件" if _has_extra_files(path, tracked) else ""


def _has_extra_files(path: Path, tracked: set[Path]) -> bool:
    def raise_error(error):
        raise error

    for parent, directories, files in os.walk(path, onerror=raise_error, followlinks=False):
        links = (name for name in directories if (Path(parent) / name).is_symlink())
        if any(Path(parent) / name not in tracked for name in (*files, *links)):
            return True
    return False


def _retire(root: Path, path: Path, check) -> dict:
    with checkout_retirement_lease(path):
        return _retire_locked(root, path, check)


def _retire_locked(root: Path, path: Path, check) -> dict:
    # 先原子搬离公开执行路径再检查，避免非强制 Git remove 仍会丢弃临时写入的忽略文件。
    from .repository_recovery import retirement_record
    from .storage_maintenance import _measure

    retired = path.with_name(f".retiring-{path.name}-{secrets.token_hex(6)}")
    result = {"removed": False, "reason": ""}
    try:
        if _open_handles(path):
            result["reason"] = "目录仍被其它进程使用"
            return result
        _check_filesystem(path)
        with retirement_record(path, retired):
            try:
                _git(root, "worktree", "move", str(path), str(retired))
                _finish_retirement(root, retired, check, result, _measure)
            except Exception as exc:
                result["reason"] = f"回收检查失败：{type(exc).__name__}"
            finally:
                _restore_retired(root, path, retired, result)
    except Exception as exc:
        result["reason"] = f"回收检查失败：{type(exc).__name__}"
    return result


def _extended_attributes_reason(path: Path) -> str:
    if sys.platform == "darwin":
        result = subprocess.run(["/usr/bin/xattr", "-rs", str(path)], capture_output=True, timeout=15, check=True)
        # provenance 是系统生成的标记；其它扩展属性可能是用户标签或资源分支。
        if any(line.rsplit(b": ", 1)[-1] != b"com.apple.provenance" for line in result.stdout.splitlines()):
            return "有 Git 不记录的文件扩展属性"
    return ""


def _finish_retirement(root, retired, check, result, measure) -> None:
    _check_filesystem(retired)
    size = measure([retired])
    if _open_handles(retired):
        result["reason"] = "目录仍被其它进程使用"
        return
    result["reason"] = _extended_attributes_reason(retired) or _rebuild_reason(retired) or check(retired)
    if not result["reason"]:
        _git(root, "worktree", "remove", str(retired))
        result.update(removed=True, bytes=size)


def _clear_bindings(path: Path, sessions: list[dict]) -> None:
    # 条件更新避免删除后误清同时建立的新绑定；分支保留，不额外扩大自动删除范围。
    uri = (config.DATA_DIR / "state.db").resolve().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection, connection:
        for row in sessions:
            connection.execute("UPDATE session_meta SET worktree_path=NULL WHERE session_key=? AND worktree_path=?",
                               (row["key"], str(path)))


def _seed_legacy(state: dict, candidates: dict[Path, Path], now: float) -> None:
    # 标记旧存量用于回执；没有可靠归档和闲置时间前仅列清单。
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
