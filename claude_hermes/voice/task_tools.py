"""P1 任务板 + 网页跨端续聊的 MCP 工具:voice_dispatch_task / voice_append_task /
voice_query_task / voice_list_tasks / voice_continue_web / voice_list_web_sessions。

只注入进语音前台会话(routes.py 调 session.run_turn 那次 stream_turn),后台任务
会话本身不挂这组工具——防止任务里的模型再派任务、无限套娃(见 00-overview.md §4.2)。

voice_continue_web / voice_list_web_sessions 是 2026-07-28 补的:voice_append_task
只认 voice-task: 前缀(它绑定的是 executor.py 自己维护的常驻 asyncio.Task,web 会话
没有这种东西),没法伸进网页端(web: 前缀)会话。网页端撞限流/出错后其实并不是
"卡在跑",而是那一轮已经收尾、锁已释放、只是空闲等下一条消息——所以"续上"就是
往那个会话里发一句话,走 gateway/web_bridge.py 桥到 WebAdapter,跟用户自己在
浏览器里发消息完全等价,不需要重建 voice-task 那套 cancel+resume 机制。
"""
from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server, tool

from .. import config
from ..gateway import clarify, web_bridge
from ..gateway.core import MODEL_CHOICES
from ..memory import session_store
from . import executor, tasks
from .task_words import status_word

_MODEL_EXAMPLES = "、".join(m for m, _ in MODEL_CHOICES)


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _describe(task: dict) -> str:
    parts = [f"任务「{task['title']}」(id={task['id']}):{status_word(task['status'])}"]
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
    "claude-hermes 自己的仓库(同样走 worktree 隔离)。model:用户明确指定要用哪个"
    f"模型跑这个任务时才传(如「用 opus 跑」),可选值:{_MODEL_EXAMPLES};"
    "没听到用户点名要哪个模型就不要传,默认跟当前对话同一个模型。",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "prompt": {"type": "string"},
            "cwd": {"type": "string"},
            "model": {"type": "string"},
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
    model = (args.get("model") or "").strip()
    if not (title and prompt):
        return _ok("voice_dispatch_task 需要 title 和 prompt 都非空。")
    # 捕获派发时的平台上下文:任务完成后需要知道通知该发给谁(见 notify.py)。
    # clarify.current() 由 run.py 在每轮对话开始前设置(含 adapter+chat_id);
    # 不在网关上下文里(如测试)返回 None,不阻塞任务派发。
    ctx = clarify.current()
    dispatch_platform = ctx.adapter.platform if (ctx and ctx.adapter) else None
    dispatch_chat_id = str(ctx.chat_id) if (ctx and ctx.chat_id is not None) else None
    task = executor.dispatch(
        title=title, prompt=prompt, cwd=cwd, model=model or None,
        dispatch_platform=dispatch_platform, dispatch_chat_id=dispatch_chat_id,
    )
    model_note = f",模型:{model}" if model else ""
    return _ok(f"已派发,task_id={task['id']},标题「{title}」{model_note},状态:{status_word(task['status'])}。")


@tool(
    "voice_append_task",
    "给一个已有的后台任务追加新的指令/需求——原地续在同一个任务上,自始至终只有"
    "一条会话,绝不会产生新任务。task_id:目标任务 id(必填,从 voice_list_tasks "
    "或 voice_dispatch_task 返回里拿);instruction:追加的指令内容。"
    "系统会自动判断怎么接:任务已经跑完了就直接把这句话当下一轮继续说;"
    "还在跑就自动打断当前这轮、带着已完成的进度接上新指令重新跑。"
    "本工具不等待新一轮跑完,可以用 voice_query_task 查后续状态。",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "instruction": {"type": "string"},
        },
        "required": ["task_id", "instruction"],
    },
)
async def voice_append_task(args: dict) -> dict:
    task_id = (args.get("task_id") or "").strip()
    instruction = (args.get("instruction") or "").strip()
    if not (task_id and instruction):
        return _ok("voice_append_task 需要 task_id 和 instruction 都非空。")
    result = await executor.append(task_id, instruction)
    if not result["ok"]:
        return _ok(f"追加失败:{result['message']}")
    parts = [f"✅ {result['message']}"]
    if result.get("task"):
        t = result["task"]
        parts.append(f"task_id={t['id']},标题「{t['title']}」,状态:{status_word(t['status'])}")
    return _ok("。".join(parts))


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


def _web_conv(session_key: str) -> str:
    """web: 前缀的 session_key → 纯 conv id(去掉前缀)。"""
    prefix = "web:"
    return session_key[len(prefix):] if session_key.startswith(prefix) else session_key


@tool(
    "voice_list_web_sessions",
    "列出最近的网页端对话(浏览器打开的 Web UI 里的对话,不是 voice_dispatch_task 派的"
    "后台任务——两者是完全不同的东西,id 格式也不一样)。用于找出用户说的「网页那个xx"
    "对话/网页端卡住的对话」具体是哪一个,以及有没有对话因为限流/报错停在原地等续聊。"
    "返回每个对话的 conv(给 voice_continue_web 用)、标题、是否最后一轮报错。",
    {"type": "object", "properties": {}},
)
async def voice_list_web_sessions(args: dict) -> dict:
    rows = session_store.list_sessions("web:")[:10]
    if not rows:
        return _ok("当前没有任何网页端对话。")
    lines = []
    for r in rows:
        flag = "⚠️最后一轮报错/卡住,等续聊" if r.get("last_error") else "正常"
        lines.append(f"conv={_web_conv(r['key'])},标题「{r['title']}」,{flag}")
    return _ok("\n".join(lines))


@tool(
    "voice_continue_web",
    "让网页端(浏览器打开的 Web UI)一个对话继续跑下一轮——用于那边因为限流/网络"
    "错误等停在原地等续聊、或者你要替用户往网页对话里追加一句话的场景。"
    "conv:网页对话 id(从 voice_list_web_sessions 拿,不是 voice_dispatch_task 那种"
    "task_id,两套 id 不通用,别传错);instruction:要发给这个网页对话的下一句话。"
    "系统会自动判断:该对话正在跑就先打断,再把这句话当下一轮发过去;空闲(撞限流后"
    "通常是这种)就直接当下一轮发过去。本工具不等这一轮跑完,过一会儿可以用"
    "voice_list_web_sessions 或 voice_query_task 类的方式确认是不是恢复正常了。",
    {
        "type": "object",
        "properties": {
            "conv": {"type": "string"},
            "instruction": {"type": "string"},
        },
        "required": ["conv", "instruction"],
    },
)
async def voice_continue_web(args: dict) -> dict:
    conv = (args.get("conv") or "").strip()
    instruction = (args.get("instruction") or "").strip()
    if not (conv and instruction):
        return _ok("voice_continue_web 需要 conv 和 instruction 都非空。")
    if not web_bridge.available():
        return _ok("当前不是常驻服务模式(没有开启网页入口),没法操作网页端对话。")
    session_key = config.resolve_session_key("web", conv)
    interrupted = web_bridge.cancel_if_running(session_key)
    await web_bridge.continue_session(conv, instruction)
    note = "(先打断了正在跑的那一轮)" if interrupted else "(原本是空闲的,直接续上)"
    return _ok(f"已经把这句话发进网页对话 conv={conv}{note},它会自动接着跑。")


def build_server() -> dict:
    """返回可直接塞进 stream_turn(extra_mcp_servers=...) 的 server 表。"""
    return {
        "voice_tasks": create_sdk_mcp_server(
            "voice_tasks",
            tools=[
                voice_dispatch_task, voice_append_task, voice_query_task, voice_list_tasks,
                voice_continue_web, voice_list_web_sessions,
            ],
        )
    }
