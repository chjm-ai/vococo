"""仓库回收与模型执行共享租约，防止检查后又开始使用目录。"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import inspect
from contextlib import aclosing, contextmanager
from functools import wraps

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


def protect_repositories(stream):
    signature = inspect.signature(stream)

    @wraps(stream)
    async def guarded(*args, **kwargs):
        from ..memory import agents

        key = signature.bind_partial(*args, **kwargs).arguments.get("session_key")
        owner = agents.agent_for_session(key) if key else None
        lock = None
        if owner:
            while lock is None:
                candidate = lease(owner["id"], session_digest(key))
                try:
                    candidate.__enter__()
                    lock = candidate
                except BlockingIOError:
                    await asyncio.sleep(0.1)
        try:
            async with aclosing(stream(*args, **kwargs)) as events:
                async for event in events:
                    yield event
        finally:
            if lock is not None:
                lock.__exit__(None, None, None)

    return guarded
