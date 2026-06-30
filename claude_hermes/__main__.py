"""CLI 入口。

  claude-hermes            # 默认进 TUI(推荐)
  claude-hermes tui        # 同上,rich 界面
  claude-hermes chat       # 纯文本对话(无 rich,调试用)
  claude-hermes telegram   # 启动 Telegram bot
"""
from __future__ import annotations

import argparse
import sys

import anyio


def _cmd_tui() -> None:
    from .tui.app import run_tui

    anyio.run(run_tui)


def _cmd_chat() -> None:
    """纯文本多轮对话(无依赖渲染,留作调试 fallback)。"""
    from . import config
    from .core.agent import Turn, run_turn

    print(f"claude-hermes · 模型 {config.MODEL} · 走订阅")
    print("输入对话,/exit 退出。\n")
    history: list[Turn] = []

    async def loop() -> None:
        while True:
            try:
                user_text = input("我 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见。")
                return
            if not user_text:
                continue
            if user_text in ("/exit", "/quit"):
                print("再见。")
                return
            reply = await run_turn(history, user_text)
            if reply.is_error:
                print("⚠️  本轮出错(可能是认证/限额)。")
            tag = f"  [工具:{', '.join(reply.tool_calls)}]" if reply.tool_calls else ""
            print(f"\nHermes > {reply.text}{tag}\n")
            history.append(Turn(user=user_text, assistant=reply.text))

    anyio.run(loop)


def _cmd_telegram() -> None:
    from .gateway.telegram import run_telegram

    try:
        anyio.run(run_telegram)
    except KeyboardInterrupt:
        print("\nTelegram bot 已停止。")


def main() -> None:
    parser = argparse.ArgumentParser(prog="claude-hermes")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("tui", help="rich TUI 界面(默认)")
    sub.add_parser("chat", help="纯文本对话(调试用)")
    sub.add_parser("telegram", help="启动 Telegram bot")

    args = parser.parse_args()
    cmd = args.cmd or "tui"  # 无参默认 TUI
    {
        "tui": _cmd_tui,
        "chat": _cmd_chat,
        "telegram": _cmd_telegram,
    }[cmd]()


if __name__ == "__main__":
    main()
