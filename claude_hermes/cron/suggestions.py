"""建议(suggestion)—— 提给用户一键接受的待命 cron 任务。

移植自原版 hermes 的 cron/suggestions.py,精简掉上游专属依赖。核心哲学:
**人在环内**——Hermes 只"提议"自动化,用户接受(才真正建 cron 任务)或忽略
(按 dedup_key 记住,永不再提)。绝不自动建任务,消费前必征得同意。

来源(source):
- catalog     —— 内置起步自动化(晨间简报、每周复盘…)
- usage       —— 复盘发现你反复问的事,值得排成定时任务
- blueprint   —— (预留)技能自带的自动化蓝图
- integration —— (预留)接入某账号后提供的自动化

接受 = 调用 scheduler.create_job(**job_spec),不搞第二套引擎。
存储:data/suggestions.json,原子写 + 进程内锁 + 0600 权限。
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
import threading
import uuid
from typing import Any, Optional

from .. import config

_lock = threading.Lock()

MAX_PENDING = 5  # 待定上限,防止变成"提醒墙";满了新建议直接丢弃
VALID_SOURCES = frozenset({"catalog", "usage", "blueprint", "integration"})
_PENDING, _ACCEPTED, _DISMISSED = "pending", "accepted", "dismissed"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load_raw() -> list[dict]:
    try:
        data = json.loads(config.SUGGESTIONS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if isinstance(data, dict) and isinstance(data.get("suggestions"), list):
        return data["suggestions"]
    return data if isinstance(data, list) else []


def _save_raw(suggestions: list[dict]) -> None:
    config.SUGGESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(config.SUGGESTIONS_PATH.parent), prefix=".sugg_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {"suggestions": suggestions, "updated_at": _now()},
                f, ensure_ascii=False, indent=2,
            )
        os.replace(tmp, config.SUGGESTIONS_PATH)
        try:
            os.chmod(config.SUGGESTIONS_PATH, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_suggestions() -> list[dict]:
    return _load_raw()


def list_pending() -> list[dict]:
    """待定建议,创建顺序(最早在前)。"""
    return [s for s in _load_raw() if s.get("status") == _PENDING]


def add_suggestion(
    *, title: str, description: str, source: str, job_spec: dict, dedup_key: str
) -> Optional[dict]:
    """登记一条待定建议。返回记录,或 None(被跳过)。

    跳过条件:source 非法、同 dedup_key 已被接受/忽略(永不再提)、已有相同
    待定建议、待定已满(MAX_PENDING)。job_spec 是给 scheduler.create_job 的
    kwargs,接受时原样透传。
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"未知建议来源: {source!r}")
    if not title.strip() or not dedup_key.strip():
        raise ValueError("title 和 dedup_key 必填")

    with _lock:
        suggestions = _load_raw()
        for existing in suggestions:  # 同 dedup_key 决定过或还挂着 → 不重复提
            if existing.get("dedup_key") == dedup_key and existing.get("status") in (
                _DISMISSED, _ACCEPTED, _PENDING
            ):
                return None
        if sum(1 for s in suggestions if s.get("status") == _PENDING) >= MAX_PENDING:
            return None
        record = {
            "id": uuid.uuid4().hex[:8],
            "title": title.strip(),
            "description": description.strip(),
            "source": source,
            "job_spec": job_spec,
            "dedup_key": dedup_key.strip(),
            "status": _PENDING,
            "created_at": _now(),
        }
        suggestions.append(record)
        _save_raw(suggestions)
        return record


def get_suggestion(ref: str) -> Optional[dict]:
    """按 id / 1-based 待定序号 / 标题(不分大小写)解析一条建议。"""
    suggestions = _load_raw()
    for s in suggestions:
        if s.get("id") == ref:
            return s
    if ref.isdigit():
        pending = [s for s in suggestions if s.get("status") == _PENDING]
        idx = int(ref) - 1
        if 0 <= idx < len(pending):
            return pending[idx]
    for s in suggestions:
        if s.get("title", "").lower() == ref.lower():
            return s
    return None


def _set_status(sid: str, status: str) -> bool:
    with _lock:
        suggestions = _load_raw()
        for s in suggestions:
            if s.get("id") == sid:
                s["status"] = status
                s["resolved_at"] = _now()
                _save_raw(suggestions)
                return True
        return False


def dismiss_suggestion(ref: str) -> bool:
    """忽略一条建议(latch —— 其 dedup_key 永不再提)。"""
    s = get_suggestion(ref)
    return bool(s) and _set_status(s["id"], _DISMISSED)


def accept_suggestion(ref: str, *, origin: Optional[dict] = None) -> Optional[dict]:
    """接受建议:用其 job_spec 建真正的 cron 任务。返回该任务,或 None。

    origin({"platform","chat_id"})并入 job 的 target,让任务结果推回用户
    接受时所在的聊天。
    """
    s = get_suggestion(ref)
    if not s or s.get("status") != _PENDING:
        return None

    from .scheduler import create_job  # 延迟导入避免循环

    spec: dict[str, Any] = dict(s.get("job_spec") or {})
    if origin is not None and not spec.get("target"):
        spec["target"] = origin
    job = create_job(**spec)
    _set_status(s["id"], _ACCEPTED)
    return job


def clear_resolved() -> int:
    """清掉已接受的记录(已忽略的必须留着占 dedup 记忆)。返回清除条数。"""
    with _lock:
        suggestions = _load_raw()
        kept = [s for s in suggestions if s.get("status") != _ACCEPTED]
        removed = len(suggestions) - len(kept)
        if removed:
            _save_raw(kept)
        return removed
