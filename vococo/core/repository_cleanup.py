"""按归档期限回收会话仓库；归属无法确认或检查失败则保留。"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace
import fcntl
import hashlib
import json
import logging
import math
import os
import time
from pathlib import Path

from .. import config
from ..memory import session_store
from . import business_repositories as repositories, development_cleanup
from .repository_locks import lease, session_digest

logger = logging.getLogger(__name__)
_DAY = 86400
_ARCHIVE_GRACE = 7 * _DAY
PREVIEW_REASON = "初步检查通过，仍需复核进程和文件，暂不回收"


def _sessions() -> dict:
    rows = session_store._conn().execute(
        "SELECT m.session_key, m.archived, a.archived_at FROM session_meta m "
        "LEFT JOIN session_archive_times a ON m.session_key=a.session_key"
    )
    result = {session_digest(key): (key, archived_at) if archived else None
              for key, archived, archived_at in rows}
    for row in session_store._conn().execute("SELECT DISTINCT session_key FROM turns"):
        result.setdefault(session_digest(row[0]), None)
    from . import tasks

    if tasks._db_path().exists():
        with tasks.read_connection():
            for task in tasks._conn().execute("SELECT id, session_key FROM tasks WHERE status IN ('running', 'queued')"):
                key = task["session_key"] or tasks.session_key(task["id"])
                result[session_digest(key)] = None
    return result


def _binding(path: Path) -> repositories.RepositoryCopy:
    if path.is_symlink() or path.resolve() != path or (path / ".git").is_symlink():
        raise ValueError("路径异常")
    if (path / ".git").is_dir():
        source = Path(repositories._git(path, "config", "--get", "vococo.source"))
    else:
        listing = repositories._git(path, "worktree", "list", "--porcelain", "-z")
        source = Path(listing.split("\0")[0].removeprefix("worktree "))
    source = source.expanduser().resolve()
    digest = hashlib.sha256(str(source).encode()).hexdigest()[:12]
    if path.name != digest and not path.name.startswith(".initializing-"):
        raise ValueError("来源目录不匹配")
    if source.is_relative_to(config.AGENT_WORKSPACES_DIR.expanduser().resolve() / ".repositories"):
        raise ValueError("来源不是原仓库")
    binding = repositories.RepositoryCopy(source, path, f"vococo/business-{path.parent.name}-{digest}")
    repositories._verify_copy(binding)
    return binding


def _preserve_reason(binding: repositories.RepositoryCopy) -> str:
    path = binding.checkout
    git_dir = repositories._git_path(path, "--git-dir")
    if any(git_dir.rglob("*.lock")):
        return "Git操作未结束"
    entries = repositories._git(path, "ls-files", "--stage", "-z")
    if any(entry.startswith("160000 ") for entry in entries.split("\0")):
        return "含子模块，需人工确认成果"
    flags = repositories._git(path, "ls-files", "-v", "-z")
    if any(name[:1].islower() or name.startswith("S ") for name in flags.split("\0") if name):
        return "索引隐藏了部分文件状态"
    if repositories._git(path, "status", "--porcelain", "--untracked-files=all"):
        return "有未提交或未跟踪成果"
    ignored = repositories._git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    if any(ignored.split("\0")):
        return "有本地配置或其它忽略文件"
    if (path / ".git").is_dir():
        own = set(repositories._git(path, "rev-list", "--all", "--reflog").splitlines())
        source = set(repositories._git(binding.source, "rev-list", "--all", "--reflog").splitlines())
        if own - source:
            return "有原仓库未保存的提交"
    else:
        saved = set(repositories._git(binding.source, "rev-list", "HEAD").splitlines())
        history = {repositories._git(path, "rev-parse", "HEAD")}
        for reference in ("HEAD", f"refs/heads/{binding.branch}"):
            history.update(repositories._git(path, "reflog", "show", "--format=%H", reference).splitlines())
        if history - saved:
            return "有未合并提交"
    return ""


def _archive_reason(path: Path, state: tuple[str, float | None] | None) -> str:
    from ..memory import agents

    if state is None:
        return "找不到已归档会话，仅列清单"
    key, archived_at = state
    owner = agents.agent_for_session(key)
    if not owner or owner["id"] != path.parent.parent.name:
        return "会话与副本归属不匹配，仅列清单"
    if archived_at is None or not math.isfinite(archived_at) or time.time() - archived_at < _ARCHIVE_GRACE:
        return "归档七天观察期未满"
    if path.name.startswith(".initializing-") or (path / ".git").is_dir():
        return "初始化残留或旧式完整克隆，仅列清单"
    sessions = [{"key": key}]
    task_reason, _ = development_cleanup._task_state(sessions, path)
    if task_reason:
        return task_reason
    if development_cleanup._runtime_busy(sessions):
        return "会话或任务正在执行/排队"
    return ""


def _retirement_reason(binding: repositories.RepositoryCopy, retired: Path) -> str:
    with session_store._db.read_connection():
        state = _sessions().get(binding.checkout.parent.name)
        reason = _archive_reason(binding.checkout, state)
    if reason:
        return reason
    repositories._verify_copy(replace(binding, checkout=retired))
    branch, git_dir = development_cleanup._validate(binding.source, retired)
    return development_cleanup._preserve_reason(binding.source, retired, branch, git_dir)


def _eligible(path: Path, states: dict) -> bool:
    if path.resolve() != path.absolute():
        return False
    digest = path.parent.name
    marker = path.parent / ".orphaned-at"
    if marker.is_symlink():
        return False
    if digest in states:
        marker.unlink(missing_ok=True)
        old_enough = time.time() - path.stat().st_mtime >= _DAY
        return states[digest] is not None and (not path.name.startswith(".initializing-") or old_enough)
    # 没有删除时间的旧会话从首次观察到失联起算，不能拿目录年龄冒充宽限期。
    try:
        with marker.open("x") as handle:
            handle.write(str(time.time()))
    except FileExistsError:
        pass
    return time.time() - float(marker.read_text()) >= _ARCHIVE_GRACE


def _collect_path(path: Path, *, closing: bool = False, preview: bool = False,
                  locked_owner: str | None = None) -> dict:
    result = {"path": str(path), "removed": False, "reason": ""}
    try:
        owner = path.parent.parent.name
        lock = nullcontext() if owner == locked_owner else lease(owner, path.parent.name, exclusive=True)
        with lock:
            with session_store._db.read_connection():
                states = _sessions()
            if not closing and not _eligible(path, states):
                result["reason"] = "会话已恢复活跃"
                return result
            binding = _binding(path)
            digest = hashlib.sha256(str(binding.source).encode()).hexdigest()[:12]
            with (path.parent / f".{digest}.lock").open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                repositories._verify_copy(binding)
                state = states.get(path.parent.name)
                if not (closing or preview) and state and development_cleanup.archive_expired(state[1], time.time()):
                    _finish_expired_collection(binding, state, result)
                else:
                    result["reason"] = _preserve_reason(binding)
                    if not result["reason"]:
                        _finish_collection(binding, states, result, preview=closing or preview)
    except BlockingIOError:
        result["reason"] = "正在执行或初始化"
    except Exception as exc:
        # Git超时等意外也不能变成“没有成果”。不输出可能含凭据的错误原文。
        result["reason"] = f"检查或回收失败：{type(exc).__name__}"
    return result


def _finish_collection(binding: repositories.RepositoryCopy, states: dict, result: dict, *, preview: bool) -> None:
    path = binding.checkout
    if preview:
        result["reason"] = "旧式完整克隆，仅列清单" if (path / ".git").is_dir() else PREVIEW_REASON
        return
    with session_store._db.read_connection():
        result["reason"] = _archive_reason(path, states.get(path.parent.name))
    if not result["reason"]:
        result.update(development_cleanup._retire(binding.source, path,
                                                 lambda retired: _retirement_reason(binding, retired)))


def _finish_expired_collection(binding: repositories.RepositoryCopy, state: tuple, result: dict) -> None:
    path = binding.checkout

    def check(candidate: Path) -> str:
        with session_store._db.read_connection():
            latest = _sessions().get(path.parent.name)
            if latest != state or not development_cleanup.archive_expired(state[1], time.time()):
                return "会话绑定或归档状态已变化"
            if reason := _archive_reason(path, latest):
                return reason
        repositories._verify_copy(replace(binding, checkout=candidate))
        development_cleanup._validate(binding.source, candidate)
        return ""

    if reason := check(path):
        result["reason"] = reason
        return
    result.update(development_cleanup._retire(binding.source, path, check, expired=True))


def _candidates(states: dict, only: str | None = None):
    root = config.AGENT_WORKSPACES_DIR.expanduser().resolve() / ".repositories"
    for path in root.glob("*/*/*"):
        if not path.is_dir() or (path.name.startswith(".") and not path.name.startswith(".initializing-")):
            continue
        # 来源不明的临时文件仍会被严格来源校验挡住，不按年龄强删。
        digest = path.parent.name
        if only is not None:
            if digest == only:
                yield path
            continue
        try:
            listed = _eligible(path, states)
        except (OSError, ValueError):
            listed = True  # 交由逐项巡检记录失败，不影响其它副本。
        if listed:
            yield path


async def collect_session(session_key: str, *, preview: bool = False,
                          locked_owner: str | None = None) -> list[dict]:
    states = _sessions()
    digest = session_digest(session_key)
    # 归档/删除调用属于明确收尾，但不能回收仍在运行或排队的任务。
    from . import tasks

    if tasks._db_path().exists():
        with tasks.read_connection():
            task = tasks.task_for_session(session_key)
            if task and task["status"] in {"running", "queued"}:
                return [{"path": str(path), "removed": False, "reason": "任务仍在执行或排队"}
                        for path in _candidates(states, digest)]
    results = []
    for path in list(_candidates(states, digest)):
        results.append(await asyncio.to_thread(_collect_path, path, closing=True, preview=preview,
                                               locked_owner=locked_owner))
    return results


def record_retained_session(session_key: str, results: list[dict]) -> None:
    """删除会话前落盘保留位置，写入失败则调用方不得删除绑定。"""
    if not results:
        return
    path = config.DATA_DIR / "retained-session-copies.jsonl"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps({"at": time.time(), "session": session_key, "results": results},
                                ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


async def sweep() -> list[dict]:
    states = _sessions()
    candidates = await asyncio.to_thread(lambda: list(_candidates(states)))
    results = []
    for path in candidates:
        results.append(await asyncio.to_thread(_collect_path, path))
    return results


def _storage_worktrees(results: list[dict]) -> list[Path]:
    paths = []
    for row in results:
        value = row.get("path")
        if not isinstance(value, str) or not value:
            continue
        path = Path(value)
        if path.is_absolute() and any(parent.name == "worktrees" and parent.parent.name == "data"
                                      for parent in path.parents):
            paths.append(path)
    return paths


async def sweep_loop() -> None:
    # 不阻塞启动；服务自带每日纯代码巡检，不派模型、不产生新工作副本。
    await asyncio.sleep(60)
    while True:
        results = []
        try:
            results = await sweep()
            results.extend(await development_cleanup.sweep())
            from .repository_recovery import pending_retirements

            known = {row["recovery_path"] for row in results if row.get("recovery_path")}
            results.extend(row for row in await asyncio.to_thread(pending_retirements)
                           if row.get("recovery_path") not in known)
            report = config.DATA_DIR / "repository-cleanup.json"
            report.write_text(json.dumps({"at": time.time(), "results": results}, ensure_ascii=False, indent=2))
            for row in results:
                if row["removed"]:
                    logger.info("已回收会话副本：%s（目录占用约 %s 字节）", row["path"], row["bytes"])
                if row.get("recovery_path"):
                    logger.warning("副本未能移回原路径，成果保留在：%s", row["recovery_path"])
            if results:
                logger.info("仓库回收：移除%s个，保留%s个", sum(r["removed"] for r in results),
                            sum(not r["removed"] for r in results))
        except Exception:
            logger.exception("仓库巡检失败，已保留文件")
        try:
            from . import storage_maintenance

            await asyncio.to_thread(
                storage_maintenance.maintain, config.DATA_DIR, worktrees=_storage_worktrees(results),
                repositories=config.AGENT_WORKSPACES_DIR.expanduser().absolute() / ".repositories",
            )
        except Exception:
            logger.exception("存储养护失败，已保留文件")
        await asyncio.sleep(_DAY)
