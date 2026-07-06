"""rich + prompt_toolkit 流式 TUI。

复用 gateway.core 的命令注册表 + converse;TUI 只提供 RichSink(rich.Live 渲染)
和输入循环。和 Telegram/飞书共用同一套命令与会话逻辑。
"""
from __future__ import annotations

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.shortcuts import radiolist_dialog
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from .. import config
from ..core.agent import AgentReply
from ..gateway import core
from ..memory import session_store

SLASH = ["/new", "/clear", "/model", "/history", "/status", "/help", "/exit"]


def _header(console: Console, model: str) -> None:
    console.print(
        Panel(
            Text.from_markup(
                "[bold]claude-hermes[/bold] · 你的私人 AI 助理\n"
                f"模型 [cyan]{model}[/cyan] · 走订阅 · 输入 [yellow]/help[/yellow] 看命令",
            ),
            border_style="cyan",
            expand=False,
        )
    )


class RichSink(core.Sink):
    """把事件流渲染成 rich.Live:💭思考 + 🔧工具 + 流式 Markdown 正文。"""

    def __init__(self, console: Console):
        super().__init__()  # thinking_buf / answer / tools 由基类聚合
        self.console = console
        self.reply: AgentReply | None = None
        self.live = Live(self._render(), console=console, refresh_per_second=12)

    def __enter__(self):
        self.live.__enter__()
        return self

    def __exit__(self, *a):
        self.live.__exit__(*a)

    def _render(self):
        parts = []
        if self.thinking_buf and not self.answer:
            parts.append(
                Panel(
                    Text(self.thinking_buf.strip()[-300:], style="italic"),
                    title="💭 思考中", border_style="grey37", title_align="left",
                )
            )
        for t in self.tools:
            if not t["done"]:
                parts.append(Text(f"🔧 {t['name']} …", style="yellow"))
            else:
                icon, style = ("✓", "green") if t["ok"] else ("✗", "red")
                parts.append(
                    Text.assemble((f"{icon} {t['name']}  ", style), (t["preview"], "dim"))
                )
        if self.answer:
            parts.append(
                Panel(Markdown(self.answer), title="Wazir", border_style="green",
                      title_align="left")
            )
        elif not parts:
            parts.append(Text("Wazir 思考中…", style="cyan"))
        return Group(*parts)

    async def render(self) -> None:
        # 思考/正文/工具状态全由基类聚合,这里只负责刷新 rich.Live
        self.live.update(self._render())

    async def done(self, reply: AgentReply) -> None:
        self.reply = reply
        await super().done(reply)  # 写入 answer 并 render


async def run_tui() -> None:
    console = Console()
    current_model = config.MODEL
    session_key = config.resolve_session_key("cli", "local")

    _header(console, current_model)
    n = len(session_store.load_recent(session_key))
    if n:
        console.print(f"[dim]已载入 {n} 轮历史(/new 开新会话)[/dim]")

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    session: PromptSession = PromptSession(
        history=FileHistory(str(config.DATA_DIR / "tui_history")),
        completer=WordCompleter(SLASH, sentence=True),
        style=Style.from_dict({"prompt": "bold ansicyan"}),
    )

    while True:
        try:
            text = (await session.prompt_async([("class:prompt", "› ")])).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]再见。[/dim]")
            return
        if not text:
            continue

        if core.is_command(text):
            outcome = core.handle_command(text, session_key, current_model)
            # 交互选项:弹选择框(方向键 + 鼠标点),选中=执行那条命令
            if outcome.choice is not None:
                picked = await radiolist_dialog(
                    title="claude-hermes",
                    text=outcome.choice.prompt,
                    values=outcome.choice.options,
                ).run_async()
                if not picked:
                    continue
                outcome = core.handle_command(picked, session_key, current_model)
            if outcome.exit:
                console.print("[dim]再见。[/dim]")
                return
            if outcome.new_model:
                current_model = outcome.new_model
            if outcome.clear_screen:
                console.clear()
                _header(console, current_model)
            if outcome.reply:
                console.print(f"[dim]{outcome.reply}[/dim]")
            continue

        with RichSink(console) as sink:
            await core.converse(session_key, text, current_model, sink)
        if sink.reply and sink.reply.cost_usd is not None:
            console.print(f"[dim]≈${sink.reply.cost_usd:.4f}(订阅理论值)[/dim]")
