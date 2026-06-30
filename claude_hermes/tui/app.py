"""rich + prompt_toolkit 的 TUI(参考 Hermes 的 Python CLI 体验)。

特性:slash 命令补全 + 输入历史 + 思考 spinner + Markdown 渲染回复。
比 `chat` 子命令好看,但底层同一个 agent loop。
"""
from __future__ import annotations

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from .. import config
from ..core.agent import Turn, run_turn

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


def _render_reply(console: Console, reply) -> None:
    if reply.is_error:
        console.print("[red]⚠️  本轮出错(可能是认证/限额)。[/red]")
    body = Markdown(reply.text or "_(空回复)_")
    console.print(Panel(body, title="Hermes", border_style="green", title_align="left"))
    foot_bits = []
    if reply.tool_calls:
        foot_bits.append(f"工具: {', '.join(reply.tool_calls)}")
    if reply.cost_usd is not None:
        foot_bits.append(f"≈${reply.cost_usd:.4f}(订阅理论值)")
    if foot_bits:
        console.print(f"[dim]{'  ·  '.join(foot_bits)}[/dim]")


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

        # --- slash 命令 ---
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

        # --- 正常对话 ---
        with console.status("[cyan]Hermes 思考中…[/cyan]", spinner="dots"):
            reply = await run_turn(history, text, model=current_model)
        _render_reply(console, reply)
        history.append(Turn(user=text, assistant=reply.text))
