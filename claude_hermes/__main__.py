"""CLI 入口。

  claude-hermes            # 默认进 TUI
  claude-hermes tui        # rich TUI
  claude-hermes chat       # 纯文本对话(调试 fallback)
  claude-hermes serve      # 常驻:Telegram 收发 + 调度器(heartbeat/主动推送)
  claude-hermes telegram   # serve 的别名
  claude-hermes cron       # 列出定时任务
"""
from __future__ import annotations

import argparse
import sys

import anyio


def _cmd_tui() -> None:
    from .tui.app import run_tui

    anyio.run(run_tui)


def _cmd_chat() -> None:
    from . import config
    from .core.agent import run_turn
    from .memory import session_store

    print(f"claude-hermes · 模型 {config.MODEL} · 走订阅\n输入对话,/exit 退出。\n")

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
            history = session_store.load_recent("cli")
            reply = await run_turn(history, user_text)
            tag = f"  [工具:{', '.join(reply.tool_calls)}]" if reply.tool_calls else ""
            print(f"\nHermes > {reply.text}{tag}\n")
            session_store.append("cli", user_text, reply.text)

    anyio.run(loop)


def _cmd_serve() -> None:
    from .gateway.run import run_serve

    try:
        anyio.run(run_serve)
    except KeyboardInterrupt:
        print("\n已停止。")


def _cmd_cron() -> None:
    from .cron import scheduler

    jobs = scheduler.load_jobs()
    if not jobs:
        print("(没有定时任务。编辑 data/cron_jobs.json 添加。)")
        return
    for j in jobs:
        flag = "✅" if j.get("enabled") else "⏸"
        print(
            f"{flag} {j.get('id')} · {j.get('name')} · "
            f"{j.get('schedule', {}).get('kind')} · 上次={j.get('last_status')}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="claude-hermes")
    sub = parser.add_subparsers(dest="cmd")
    for name, help_ in [
        ("tui", "rich TUI(默认)"),
        ("chat", "纯文本对话(调试)"),
        ("serve", "常驻:Telegram + 调度器"),
        ("telegram", "serve 的别名"),
        ("cron", "列出定时任务"),
    ]:
        sub.add_parser(name, help=help_)

    args = parser.parse_args()
    cmd = args.cmd or "tui"
    handlers = {
        "tui": _cmd_tui,
        "chat": _cmd_chat,
        "serve": _cmd_serve,
        "telegram": _cmd_serve,  # 别名
        "cron": _cmd_cron,
    }
    if cmd not in handlers:
        parser.print_help()
        sys.exit(1)
    handlers[cmd]()


if __name__ == "__main__":
    main()
