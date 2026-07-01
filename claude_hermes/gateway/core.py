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
    ImageAttachment,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolStarted,
    stream_turn,
)
from ..memory import session_store


class Sink:
    """渲染层接口 + 状态聚合(平台无关)。

    基类把事件流聚合成可显示状态:思考缓冲、工具进度、正文增量,
    并生成单行状态串(💭 思考中 / 🔧 Read✓ · Bash⏳)。
    子类通常只需实现 render()(把当前状态显示出去),
    各端(TG editMessageText / TUI rich.Live / 将来飞书卡片)复用同一套聚合。
    """

    def __init__(self) -> None:
        self.thinking_buf = ""
        self.answer = ""
        # 每项 {name, done, ok, preview};done=False 即进行中
        self.tools: list[dict] = []

    # --- 事件入口(子类一般不覆盖,只覆盖 render)---
    async def thinking(self, text: str) -> None:
        self.thinking_buf += text
        await self.render()

    async def text(self, text: str) -> None:
        self.answer += text
        await self.render()

    async def tool_started(self, name: str) -> None:
        self.tools.append({"name": name, "done": False, "ok": True, "preview": ""})
        await self.render()

    async def tool_finished(self, name: str, ok: bool, preview: str) -> None:
        for t in self.tools:  # 标记最近一个同名未完成项
            if t["name"] == name and not t["done"]:
                t.update(done=True, ok=ok, preview=preview)
                break
        else:
            self.tools.append({"name": name, "done": True, "ok": ok, "preview": preview})
        await self.render()

    async def done(self, reply: AgentReply) -> None:
        if reply.text:
            self.answer = reply.text
        await self.render()

    # --- 渲染(子类实现:把当前聚合状态显示出去)---
    async def render(self) -> None: ...

    # --- 状态串 helper(供子类拼装显示)---
    def tools_summary(self) -> str:
        marks = {"pending": "⏳", "ok": "✓", "err": "✗"}
        bits = []
        for t in self.tools:
            key = "pending" if not t["done"] else ("ok" if t["ok"] else "err")
            bits.append(f"{t['name']}{marks[key]}")
        return " · ".join(bits)

    def status_line(self) -> str:
        """单行状态。有工具→显示工具进度;否则正文未开始时→💭 思考中。"""
        if self.tools:
            return "🔧 " + self.tools_summary()
        if self.thinking_buf and not self.answer:
            return "💭 思考中…"
        return ""


async def converse(
    session_key: str,
    user_text: str,
    model: str | None,
    sink: Sink,
    images: list[ImageAttachment] | None = None,
) -> AgentReply | None:
    """跑一轮:载入历史 → 流式 → 喂 sink → 落库。"""
    history = session_store.load_recent(session_key)
    reply: AgentReply | None = None
    async for ev in stream_turn(history, user_text, model=model, images=images):
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


# === 命令注册表(单一来源:TG 菜单 setMyCommands 和 /help 都从这生成)===
COMMAND_LIST: list[tuple[str, str]] = [
    ("new", "开新会话(旧历史保留)"),
    ("clear", "清屏并开新会话"),
    ("model", "查看或切换模型,如 /model claude-opus-4-8"),
    ("history", "看最近历史"),
    ("status", "会话信息"),
    ("help", "显示帮助"),
]
HELP_TEXT = "可用命令:\n" + "\n".join(f"/{n} — {d}" for n, d in COMMAND_LIST)

# 可选模型(/model 无参时弹这些供选择)
MODEL_CHOICES: list[tuple[str, str]] = [
    ("claude-opus-4-8", "Opus 4.8 · 最强(吃周限额)"),
    ("claude-sonnet-5", "Sonnet 5 · 日常均衡(新)"),
    ("claude-sonnet-4-6", "Sonnet 4.6 · 上一代均衡"),
    ("claude-haiku-4-5", "Haiku 4.5 · 最快最省"),
]


@dataclass
class Choice:
    """通用交互选项。每个 option 是 (选中后执行的命令, 显示文字)。

    各端各自渲染(TUI 选择框 / TG inline 按钮 / 飞书卡片),选中=执行那条命令。
    """

    prompt: str
    options: list[tuple[str, str]]


@dataclass
class CommandOutcome:
    handled: bool = True
    reply: str | None = None
    exit: bool = False
    clear_screen: bool = False
    reset_history: bool = False
    new_model: str | None = None
    choice: Choice | None = None


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
        opts = [
            (f"/model {v}", f"{label}{' ✓ 当前' if v == current_model else ''}")
            for v, label in MODEL_CHOICES
        ]
        return CommandOutcome(choice=Choice(prompt="选择模型:", options=opts))
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
