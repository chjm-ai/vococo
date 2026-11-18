"""仓库回收与模型执行共享租约，防止检查后又开始使用目录。"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import inspect
from contextlib import aclosing, asynccontextmanager, contextmanager
from functools import wraps
from pathlib import Path

from .. import config


def session_digest(session_key: str) -> str:
    return hashlib.sha256(session_key.encode()).hexdigest()[:20]


@contextmanager
def lease(agent_id: str, digest: str, *, exclusive: bool = False):
    root = config.AGENT_WORKSPACES_DIR.expanduser().resolve() / ".repository-locks" / agent_id
    root.mkdir(parents=True, exist_ok=True)
    path = root / digest
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("仓库执行锁路径无效")
    with path.open("a") as handle:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(handle, mode | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@asynccontextmanager
async def _wait_lease(key):
    lock = None
    while (entry := key()) is not None:
        candidate = lease(*entry)
        try:
            candidate.__enter__()
        except BlockingIOError:
            await asyncio.sleep(0.1)
        else:
            lock = candidate
            break
    try:
        yield
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)


def session_lease(session_key: str | None):
    from ..memory import agents, session_store

    def key():
        if not session_key or session_store.is_deleted_web_session(session_key):
            return None
        try:
            owner = agents.agent_for_session(session_key)
        except ValueError as exc:
            raise RuntimeError("Agent 已删除或会话未绑定，不能继续执行") from exc
        return (owner["id"], session_digest(session_key)) if owner else None

    return _wait_lease(key)


def _checkout_key(path):
    return ("checkout", session_digest(str(Path(path).resolve())))


def checkout_lease(path):
    return _wait_lease(lambda: _checkout_key(path) if path else None)


def checkout_retirement_lease(path):
    return lease(*_checkout_key(path), exclusive=True)


def protect_repositories(stream):
    signature = inspect.signature(stream)

    @wraps(stream)
    async def guarded(*args, **kwargs):
        key = signature.bind_partial(*args, **kwargs).arguments.get("session_key")
        async with session_lease(key), aclosing(stream(*args, **kwargs)) as events:
            async for event in events:
                yield event

    return guarded
