"""CLI 入口:`python -m claude_hermes chat`(M0 唯一子命令)。"""
from __future__ import annotations

import argparse
import sys

import anyio


def _cmd_chat() -> None:
    """命令行多轮对话。"""
    # 延迟导入:让 config 的报错(如缺令牌)只在真正进 chat 时触发
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="claude-hermes")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("chat", help="命令行多轮对话(M0)")

    args = parser.parse_args()
    if args.cmd == "chat":
        _cmd_chat()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
