"""把 SDK 内置 Task* 待办工具投影到网页状态条。

SDK 没有公开的任务清单查询 API;这里以官方 PostToolUse hook 收到的工具输入/结果为
唯一事实来源。它只写 core.tasks 的 sdk_task_views,绝不创建可被 task_runner 执行的任务。
"""
from __future__ import annotations

import re

from ..core import tasks

_NUMBER_RE = re.compile(r"(?:Task\s*#?|#)(\d+)|^(\d+)$", re.IGNORECASE)
_TASK_GET_RE = re.compile(
    r"Task\s*#(\d+)\s*:\s*(.*?)\s+Status:\s*([^\s]+)(?:\s+Description:\s*(.*))?",
    re.IGNORECASE | re.DOTALL,
)


def _chat_id() -> str | None:
    try:
        from ..gateway import clarify

        ctx = clarify.current()
        return str(ctx.chat_id) if ctx and ctx.chat_id is not None else None
    except Exception:
        return None


def _number(*values: object) -> int | None:
    for value in values:
        match = _NUMBER_RE.search(str(value or "").strip())
        if match:
            return int(match.group(1) or match.group(2))
    return None


def _emit(task: dict | None) -> None:
    if task is None:
        return
    from ..voice import notify

    notify.on_sdk_task_activity(task)


def _upsert(chat_id: str, number: int, data: dict, response: object = "") -> None:
    old = tasks.get_sdk_task(chat_id, number) or {}
    title = str(data.get("subject") or data.get("title") or old.get("title") or "任务 #" + str(number)).strip()
    description = str(
        data.get("description") or data.get("activeForm") or old.get("description") or ""
    ).strip()
    status = str(data.get("status") or old.get("status") or "pending").strip()
    _emit(tasks.upsert_sdk_task(
        chat_id=chat_id, number=number, title=title, description=description, status=status
    ))


def _sync_list_response(chat_id: str, response: object) -> None:
    """TaskGet/TaskList 的人类可读结果是目前唯一的全量校准输入。"""
    for match in _TASK_GET_RE.finditer(str(response or "")):
        _emit(tasks.upsert_sdk_task(
            chat_id=chat_id,
            number=int(match.group(1)),
            title=match.group(2).strip(),
            status=match.group(3).strip(),
            description=(match.group(4) or "").strip(),
        ))


async def posttool_sdk_task_sync_hook(input_data, tool_use_id, context):
    """Task* 完成后同步状态;失败或未知格式静默跳过,不干扰 Agent 本身。"""
    try:
        name = str(input_data.get("tool_name") or "")
        if not tasks.is_sdk_task_tool(name):
            return {}
        chat_id = _chat_id()
        if not chat_id:
            return {}
        data = input_data.get("tool_input") or {}
        response = input_data.get("tool_response") or ""
        if name == "TaskCreate":
            number = _number(response, data.get("task_id"))
            if number is not None:
                _upsert(chat_id, number, data, response)
        elif name == "TaskUpdate":
            number = _number(data.get("task_id"), data.get("id"), response)
            if number is not None:
                if str(data.get("status") or "").lower() == "deleted":
                    _emit(tasks.delete_sdk_task(chat_id, number))
                else:
                    _upsert(chat_id, number, data, response)
        elif name == "TaskDelete":
            number = _number(data.get("task_id"), data.get("id"), response)
            if number is not None:
                _emit(tasks.delete_sdk_task(chat_id, number))
        elif name in {"TaskGet", "TaskList"}:
            _sync_list_response(chat_id, response)
    except Exception:
        pass
    return {}
