"""rich + prompt_toolkit 的流式 TUI(参考 Hermes 体验)。

- slash 命令补全 + 输入历史
- 实时渲染:💭 思考块(流式)+ 🔧 工具调用过程 + Markdown 正文(流式)
底层消费 agent.stream_turn 的事件流。
"""
from __future__ import annotations

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from .. import config
from ..core.agent import (
    Done,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolStarted,
    Turn,
    stream_turn,
)

SLASH_COMMANDS = {
    "/help": "显示帮助",
    "/clear": "清空本次会话上下文 + 屏幕",
    "/model": "查看或切换模型(/model claude-opus-4-8)",
    "/exit": "退出",
    "/quit": "退出",
}


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


def _render(thinking: str, tools: list[dict], answer: str):
    """根据当前状态拼一个可渲染对象。"""
    parts = []
    if thinking and not answer:
        parts.append(
            Panel(
                Text(thinking.strip()[-300:], style="italic"),
                title="💭 思考中",
                border_style="grey37",
                title_align="left",
            )
        )
    for t in tools:
        if not t["done"]:
            parts.append(Text(f"🔧 {t['name']} …", style="yellow"))
        else:
            icon, style = ("✓", "green") if t["ok"] else ("✗", "red")
            parts.append(
                Text.assemble(
                    (f"{icon} {t['name']}  ", style), (t["preview"], "dim")
                )
            )
    if answer:
        parts.append(
            Panel(Markdown(answer), title="Hermes", border_style="green", title_align="left")
        )
    elif not parts:
        parts.append(Text("Hermes 思考中…", style="cyan"))
    return Group(*parts)


async def _dialogue(console: Console, history: list[Turn], text: str, model: str) -> None:
    thinking = ""
    answer = ""
    tools: list[dict] = []
    reply = None

    with Live(_render(thinking, tools, answer), console=console, refresh_per_second=12) as live:
        async for ev in stream_turn(history, text, model=model):
            if isinstance(ev, TextDelta):
                answer += ev.text
            elif isinstance(ev, ThinkingDelta):
                thinking += ev.text
            elif isinstance(ev, ToolStarted):
                tools.append({"name": ev.name, "done": False, "ok": True, "preview": ""})
            elif isinstance(ev, ToolFinished):
                for t in tools:
                    if t["name"] == ev.name and not t["done"]:
                        t.update(done=True, ok=ev.ok, preview=ev.preview)
                        break
            elif isinstance(ev, Done):
                reply = ev.reply
            live.update(_render(thinking, tools, answer))

    if reply is not None:
        if reply.is_error:
            console.print("[red]⚠️  本轮出错(可能是认证/限额)。[/red]")
        if reply.cost_usd is not None:
            console.print(f"[dim]≈${reply.cost_usd:.4f}(订阅理论值)[/dim]")
        history.append(Turn(user=text, assistant=reply.text))


async def run_tui() -> None:
    console = Console()
    current_model = config.MODEL
    history: list[Turn] = []

    _header(console, current_model)

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    session: PromptSession = PromptSession(
        history=FileHistory(str(config.DATA_DIR / "tui_history")),
        completer=WordCompleter(list(SLASH_COMMANDS), sentence=True),
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

        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            if cmd in ("/exit", "/quit"):
                console.print("[dim]再见。[/dim]")
                return
            if cmd == "/help":
                for c, desc in SLASH_COMMANDS.items():
                    console.print(f"  [yellow]{c}[/yellow]  {desc}")
                continue
            if cmd == "/clear":
                history.clear()
                console.clear()
                _header(console, current_model)
                console.print("[dim]上下文已清空。[/dim]")
                continue
            if cmd == "/model":
                if arg.strip():
                    current_model = arg.strip()
                    console.print(f"[dim]已切换模型 → [cyan]{current_model}[/cyan][/dim]")
                else:
                    console.print(f"当前模型:[cyan]{current_model}[/cyan]")
                continue
            console.print(f"[red]未知命令 {cmd}[/red] · /help 看可用命令")
            continue

        await _dialogue(console, history, text, current_model)
