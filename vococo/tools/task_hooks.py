"""PostToolUse hook:SDK 任务工具(TaskCreate/TaskUpdate)同步到 vococo 任务表。

背景(2026-08-07 实测定案):TaskCreate 是 Claude Code 内置的「任务清单」工具,
创建的待办只存在于 SDK/CLI 会话内部——SDK 消息流里【不广播任何任务生命周期系统
消息】(实测 subtypes 只有 init/status/thinking_tokens),所以基于
TaskStartedMessage 的同步方案永远不触发,前端任务状态条看不到这些任务。
真正的抓手是官方 PostToolUse hook:工具调用完成时能拿到 tool_name/tool_input/
tool_response,在这里落库 + 推 SSE 最干净。

同步规则:
- TaskCreate → 落库一条 queued 任务(origin="task",绑定当前会话),推 SSE;
  从工具响应文本里解析 SDK 任务编号(Task #N)存进 progress_note 作关联锚。
- TaskUpdate → 按 SDK 任务编号(Task #N)反查 vococo 任务,更新状态
  (in_progress→running, completed→done),推 SSE。

hook 出错一律静默(不阻断工具调用本身)。
"""
from __future__ import annotations

import re

_TASK_ID_RE = re.compile(r"Task #(\d+)", re.IGNORECASE)

# SDK 任务状态 → vococo 状态;只认这两个会动的,其余(pending 等)不折腾
_STATUS_MAP = {"in_progress": "running", "completed": "done"}


def _chat_id() -> str | None:
    """当前会话 chat_id(与 dispatch_session 落库口径一致);拿不到返回 None。"""
    try:
        from ..gateway import clarify

        ctx = clarify.current()
        return str(ctx.chat_id) if (ctx and ctx.chat_id is not None) else None
    except Exception:
        return None


def _sync_task_create(input_data: dict) -> None:
    from ..core import tasks
    from ..voice import notify

    ti = input_data.get("tool_input") or {}
    title = (ti.get("subject") or ti.get("description") or "").strip()
    if not title:
        return
    task = tasks.create(title=title, prompt="", dispatch_chat_id=_chat_id(), origin="task")
    # 从工具响应里解析 SDK 任务编号(Task #N)存 progress_note,TaskUpdate 反查用
    m = _TASK_ID_RE.search(str(input_data.get("tool_response") or ""))
    if m:
        tasks.set_progress(task["id"], f"SDK任务 #{m.group(1)}")
    notify.on_task_activity(task)


def _sync_task_update(input_data: dict) -> None:
    from ..core import tasks
    from ..voice import notify

    ti = input_data.get("tool_input") or {}
    ref = str(ti.get("task_id") or ti.get("task") or ti.get("id") or "")
    m = _TASK_ID_RE.search(ref)
    target = _STATUS_MAP.get(str(ti.get("status") or "").strip())
    if not m or target is None:
        return
    anchor = f"SDK任务 #{m.group(1)}"
    for row in tasks.list_recent(origins=("task",), limit=50):
        if row.get("progress_note") != anchor:
            continue
        if target == "running":
            tasks.set_status(row["id"], "running")  # queued → running
        else:  # completed → done;任务可能还在 queued(跳过 in_progress 直接完成),兜底先迁 running
            if not tasks.finish(row["id"], "done", result_full="", result_summary=""):
                tasks.set_status(row["id"], "running")
                tasks.finish(row["id"], "done", result_full="", result_summary="")
        notify.on_task_activity(tasks.get(row["id"]))
        return


async def posttool_task_sync_hook(input_data, tool_use_id, context):
    """PostToolUse hook:TaskCreate/TaskUpdate 调用 → 同步 vococo 任务表。

    其余工具一律不管。hook 自身异常静默吞掉,绝不阻断工具调用。
    """
    try:
        tool_name = input_data.get("tool_name", "") or ""
        if tool_name == "TaskCreate":
            _sync_task_create(input_data)
        elif tool_name == "TaskUpdate":
            _sync_task_update(input_data)
    except Exception:
        pass  # 同步失败不影响模型正常工具调用
    return {}
