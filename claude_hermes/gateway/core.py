"""Gateway 核心 —— 平台无关。

所有入口(Telegram / 飞书 / TUI)共享:
- 命令注册表(handle_command):/new /clear /model /history /status /help …
- converse():消费 agent 事件流,喂给平台各自的 Sink(渲染层)
- 会话持久化

各平台只需实现 Sink(怎么把事件渲染出去)+ 收发 I/O。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.agent import (
    AgentReply,
    Done,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolStarted,
    stream_turn,
)
from ..memory import session_store


class Sink:
    """渲染层接口。各平台子类化,默认全部 no-op(只实现关心的)。"""

    async def thinking(self, text: str) -> None: ...
    async def text(self, text: str) -> None: ...
    async def tool_started(self, name: str) -> None: ...
    async def tool_finished(self, name: str, ok: bool, preview: str) -> None: ...
    async def done(self, reply: AgentReply) -> None: ...


async def converse(
    session_key: str, user_text: str, model: str | None, sink: Sink
) -> AgentReply | None:
    """跑一轮:载入历史 → 流式 → 喂 sink → 落库。"""
    history = session_store.load_recent(session_key)
    reply: AgentReply | None = None
    async for ev in stream_turn(history, user_text, model=model):
        if isinstance(ev, TextDelta):
            await sink.text(ev.text)
        elif isinstance(ev, ThinkingDelta):
            await sink.thinking(ev.text)
        elif isinstance(ev, ToolStarted):
            await sink.tool_started(ev.name)
        elif isinstance(ev, ToolFinished):
            await sink.tool_finished(ev.name, ev.ok, ev.preview)
        elif isinstance(ev, Done):
            reply = ev.reply
    if reply is not None:
        session_store.append(session_key, user_text, reply.text)
        await sink.done(reply)
    return reply


# === 命令注册表 ===
HELP_TEXT = (
    "可用命令:\n"
    "/new(/reset) 开新会话(旧历史保留)\n"
    "/clear 清屏并开新会话\n"
    "/model [名称] 查看或切换模型\n"
    "/history 看最近历史\n"
    "/status 会话信息\n"
    "/help 帮助\n"
    "/exit 退出(仅 CLI)"
)


@dataclass
class CommandOutcome:
    handled: bool = True
    reply: str | None = None
    exit: bool = False
    clear_screen: bool = False
    reset_history: bool = False
    new_model: str | None = None


def is_command(text: str) -> bool:
    return text.startswith("/")


def handle_command(text: str, session_key: str, current_model: str) -> CommandOutcome:
    """处理斜杠命令。平台无关的部分在这;UI 副作用(清屏等)由前端按 outcome 标志执行。"""
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.lower(), arg.strip()

    if cmd in ("/exit", "/quit"):
        return CommandOutcome(reply="再见。", exit=True)
    if cmd in ("/new", "/reset"):
        session_store.new_session(session_key)
        return CommandOutcome(reply="🆕 已开新会话(旧历史已保留)。", reset_history=True)
    if cmd == "/clear":
        session_store.new_session(session_key)
        return CommandOutcome(
            reply="🧹 已清空当前上下文(旧历史保留)。", reset_history=True, clear_screen=True
        )
    if cmd in ("/start", "/help"):
        return CommandOutcome(reply=HELP_TEXT)
    if cmd == "/model":
        if arg:
            return CommandOutcome(reply=f"已切换模型 → {arg}", new_model=arg)
        return CommandOutcome(reply=f"当前模型:{current_model}")
    if cmd == "/history":
        h = session_store.load_recent(session_key, limit=10)
        if not h:
            return CommandOutcome(reply="(当前会话还没有历史)")
        lines = [f"最近 {len(h)} 轮:"]
        for t in h:
            lines.append(f"· 我:{t.user[:50]}")
        return CommandOutcome(reply="\n".join(lines))
    if cmd == "/status":
        n = len(session_store.load_recent(session_key))
        return CommandOutcome(
            reply=f"会话:{session_key}\n模型:{current_model}\n本会话轮数:{n}"
        )
    return CommandOutcome(handled=False, reply=f"未知命令 {cmd} · /help 看命令")
