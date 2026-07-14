"""P1 任务板的三个 MCP 工具:voice_dispatch_task / voice_query_task / voice_list_tasks。

只注入进语音前台会话(routes.py 调 session.run_turn 那次 stream_turn),后台任务
会话本身不挂这组工具——防止任务里的模型再派任务、无限套娃(见 00-overview.md §4.2)。
"""
from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server, tool

from .. import config
from ..gateway import clarify
from . import executor, tasks

_STATUS_WORD = {
    "queued": "排队中",
    "running": "进行中",
    "done": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _describe(task: dict) -> str:
    parts = [f"任务「{task['title']}」(id={task['id']}):{_STATUS_WORD.get(task['status'], task['status'])}"]
    if task["status"] == "running" and task["progress_note"]:
        parts.append(f"当前进展:{task['progress_note']}")
    if task["status"] in tasks.TERMINAL_STATUSES and task["result_summary"]:
        parts.append(f"结果:{task['result_summary']}")
    return "。".join(parts)


@tool(
    "voice_dispatch_task",
    "把一件重活派给后台独立会话去干:要写改代码/文件、要查多处资料、要拆成多步才能"
    "做完、或要跑命令/脚本的事都算(按工作量信号判断,不要靠猜耗时),立即返回不等它跑完;你应该同时口头告诉用户「好,我去办,好了叫你」"
    "这类话。title:6 字以内短名(会出现在播报/任务卡片里);prompt:完整任务描述(后台会话"
    "看不到当前对话上下文,必须把要做的事说完整);cwd:任务要在哪个项目目录下干活——"
    "涉及改代码/改仓库文件/查项目代码的任务【必须】传该项目根目录的绝对路径,"
    "是 git 仓库会自动开独立 worktree+分支,绝不会动主目录;不传则默认落到"
    "claude-hermes 自己的仓库(同样走 worktree 隔离)。",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "prompt": {"type": "string"},
            "cwd": {"type": "string"},
        },
        "required": ["title", "prompt"],
    },
)
async def voice_dispatch_task(args: dict) -> dict:
    title = (args.get("title") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    # 不传 cwd 就默认本项目根:2026-07-12 事故里模型从没传过 cwd,worktree 隔离
    # 形同虚设,子代理直接在 serve 的主检出目录上改代码拉分支——代码层兜底,
    # 让"忘了传"也走 worktree,而不是落到主目录。
    cwd = (args.get("cwd") or "").strip() or str(config.ROOT_DIR)
    if not (title and prompt):
        return _ok("voice_dispatch_task 需要 title 和 prompt 都非空。")
    # 捕获派发时的平台上下文:任务完成后需要知道通知该发给谁(见 notify.py)。
    # clarify.current() 由 run.py 在每轮对话开始前设置(含 adapter+chat_id);
    # 不在网关上下文里(如测试)返回 None,不阻塞任务派发。
    ctx = clarify.current()
    dispatch_platform = ctx.adapter.platform if (ctx and ctx.adapter) else None
    dispatch_chat_id = str(ctx.chat_id) if (ctx and ctx.chat_id is not None) else None
    task = executor.dispatch(
        title=title, prompt=prompt, cwd=cwd,
        dispatch_platform=dispatch_platform, dispatch_chat_id=dispatch_chat_id,
    )
    return _ok(f"已派发,task_id={task['id']},标题「{title}」,状态:{_STATUS_WORD[task['status']]}。")


@tool(
    "voice_query_task",
    "查一个后台任务当前进展。task_id 省略则查最近一次派发的那个。"
    "返回的是原始字段拼的一句话,你要把它压成更口语的转述再讲给用户,不要念「状态/进展」这类字段名。",
    {"type": "object", "properties": {"task_id": {"type": "string"}}},
)
async def voice_query_task(args: dict) -> dict:
    task_id = (args.get("task_id") or "").strip()
    task = tasks.get(task_id) if task_id else tasks.get_latest()
    if task is None:
        return _ok("没有找到任务(task_id 不对,或者还从没派发过任务)。")
    return _ok(_describe(task))


@tool(
    "voice_list_tasks",
    "列出最近的后台任务,看有哪些任务、各自什么状态。",
    {"type": "object", "properties": {}},
)
async def voice_list_tasks(args: dict) -> dict:
    rows = tasks.list_recent(limit=10)
    if not rows:
        return _ok("当前没有任何任务。")
    return _ok("\n".join(_describe(t) for t in rows))


def build_server() -> dict:
    """返回可直接塞进 stream_turn(extra_mcp_servers=...) 的 server 表。"""
    return {
        "voice_tasks": create_sdk_mcp_server(
            "voice_tasks",
            tools=[voice_dispatch_task, voice_query_task, voice_list_tasks],
        )
    }
