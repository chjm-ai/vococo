"""回收已收尾会话的可重建仓库；未知内容和检查失败一律保留。"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

from .. import config
from ..memory import session_store
from . import business_repositories as repositories
from .repository_locks import lease, session_digest

logger = logging.getLogger(__name__)
_DAY = 86400
# 只豁免明确可重建的依赖缓存；其它忽略文件（包括配置）同样算成果。
_CACHES = {"node_modules", ".venv", ".pytest_cache", "__pycache__"}


def _sessions() -> dict:
    rows = session_store._conn().execute("SELECT session_key, archived FROM session_meta").fetchall()
    result = {session_digest(row[0]): bool(row[1]) for row in rows}
    for row in session_store._conn().execute("SELECT DISTINCT session_key FROM turns"):
        result.setdefault(session_digest(row[0]), False)
    from . import tasks

    if tasks._db_path().exists():
        with tasks.read_connection():
            for task in tasks._conn().execute("SELECT id, session_key FROM tasks WHERE status IN ('running', 'queued')"):
                key = task["session_key"] or tasks.session_key(task["id"])
                result[session_digest(key)] = False
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
    if any(Path(name).parts[0] not in _CACHES for name in ignored.split("\0") if name):
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


def _remove(binding: repositories.RepositoryCopy) -> None:
    path = binding.checkout
    if (path / ".git").is_dir():
        shutil.rmtree(path)
        return
    # 未跟踪和忽略文件已逐项检查；不强制移除被Git标记为锁定的工作树。
    repositories._git(binding.source, "worktree", "remove", "--force", str(path))
    try:
        repositories._git(binding.source, "branch", "-d", binding.branch)
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.warning("工作目录已回收，会话分支暂时保留：%s", binding.branch)


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
        return states[digest] is True and (not path.name.startswith(".initializing-") or old_enough)
    # 没有删除时间的旧会话从首次观察到失联起算，不能拿目录年龄冒充宽限期。
    try:
        with marker.open("x") as handle:
            handle.write(str(time.time()))
    except FileExistsError:
        pass
    return time.time() - float(marker.read_text()) >= 7 * _DAY


def _collect_path(path: Path, *, closing: bool = False) -> dict:
    result = {"path": str(path), "removed": False, "reason": ""}
    try:
        with lease(path.parent.parent.name, path.parent.name, exclusive=True):
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
                result["reason"] = _preserve_reason(binding)
                if not result["reason"]:
                    _remove(binding)
                    result["removed"] = True
    except BlockingIOError:
        result["reason"] = "正在执行或初始化"
    except (OSError, ValueError, RuntimeError) as exc:
        result["reason"] = f"检查或回收失败：{type(exc).__name__}"
    except Exception as exc:
        # Git超时等意外也不能变成“没有成果”。不输出可能含凭据的错误原文。
        result["reason"] = f"检查或回收失败：{type(exc).__name__}"
    return result


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
        if _eligible(path, states):
            yield path


async def collect_session(session_key: str) -> list[dict]:
    states = _sessions()
    digest = session_digest(session_key)
    # 归档/删除调用属于明确收尾，但不能回收仍在运行或排队的任务。
    from . import tasks

    if tasks._db_path().exists():
        with tasks.read_connection():
            task = tasks.task_for_session(session_key)
            if task and task["status"] in {"running", "queued"}:
                return []
    results = []
    for path in list(_candidates(states, digest)):
        results.append(await asyncio.to_thread(_collect_path, path, closing=True))
    return results


async def sweep() -> list[dict]:
    states = _sessions()
    candidates = await asyncio.to_thread(lambda: list(_candidates(states)))
    results = []
    for path in candidates:
        results.append(await asyncio.to_thread(_collect_path, path))
    return results


async def sweep_loop() -> None:
    # 不阻塞启动；服务自带每日纯代码巡检，不派模型、不产生新工作副本。
    await asyncio.sleep(60)
    while True:
        try:
            results = await sweep()
            if results:
                report = config.DATA_DIR / "repository-cleanup.json"
                report.write_text(json.dumps({"at": time.time(), "results": results}, ensure_ascii=False, indent=2))
                logger.info("仓库回收：移除%s个，保留%s个", sum(r["removed"] for r in results),
                            sum(not r["removed"] for r in results))
        except Exception:
            logger.exception("仓库巡检失败，已保留文件")
        await asyncio.sleep(_DAY)
