"""CLI 入口。

  vococo            # 默认进 TUI
  vococo tui        # rich TUI
  vococo chat       # 纯文本对话(调试 fallback)
  vococo serve      # 常驻:Web 收发 + 调度器(heartbeat/主动推送)
  vococo cron       # 列出定时任务
  vococo doctor     # 自检:配置/认证/DB/AI_BRAIN/进程
"""
from __future__ import annotations

import argparse
import sys

import anyio


def _cmd_tui() -> None:
    from .core import client_pool
    from .tui.app import run_tui

    async def _run() -> None:
        try:
            await run_tui()
        finally:
            await client_pool.close_all()  # 退出时收掉保温的 CLI 子进程,不留孤儿

    anyio.run(_run)


def _cmd_chat() -> None:
    from . import config
    from .core.agent import run_turn
    from .memory import session_store

    print(f"vococo · 模型 {config.MODEL} · 走订阅\n输入对话,/exit 退出。\n")
    key = config.resolve_session_key("cli", "local")

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
            history = session_store.load_recent(key)
            reply = await run_turn(history, user_text)
            tag = f"  [工具:{', '.join(reply.tool_calls)}]" if reply.tool_calls else ""
            print(f"\n{config.PERSONA_NAME} > {reply.text}{tag}\n")
            session_store.append(key, user_text, reply.text)

    anyio.run(loop)


def _cmd_serve() -> None:
    from .gateway.run import run_serve

    try:
        anyio.run(run_serve)
    except KeyboardInterrupt:
        print("\n已停止。")


def _cmd_doctor() -> None:
    """自检:配置 / 认证 / DB / AI_BRAIN / 进程。有 ❌ 则退出码 1。"""
    import os
    import subprocess

    oks: list[str] = []
    warns: list[str] = []
    errs: list[str] = []

    # 1) 订阅认证(import config 即校验,缺 token 会抛 ConfigError)
    try:
        from . import config
    except Exception as e:  # noqa: BLE001 —— 配置问题原样报给用户
        print(f"❌ 配置加载失败:{e}")
        sys.exit(1)
    from . import providers

    active = providers.load_active()
    if config.OAUTH_TOKEN:
        # 只判断"非空"等于没检查:令牌被吊销时照样非空,官方模型却已全线不可用
        # (2026-08-16 踩过,静默了五天)。这里实打实发一个请求验活,见
        # providers.probe_subscription_token 的注释。
        state, detail = providers.probe_subscription_token(config.OAUTH_TOKEN)
        if state == "ok":
            oks.append("CLAUDE_CODE_OAUTH_TOKEN 探活通过(走订阅)")
        elif state == "bad":
            errs.append(
                f"CLAUDE_CODE_OAUTH_TOKEN 已失效({detail})—— 官方模型全线不可用。"
                "重新跑 `claude setup-token` 换新令牌并更新 .env,"
                "见 OPERATIONS.md「订阅令牌(CLAUDE_CODE_OAUTH_TOKEN)」"
            )
        else:
            warns.append(
                f"CLAUDE_CODE_OAUTH_TOKEN 探活没结论({detail})—— "
                "多半是网络/限流,不代表令牌失效,联网后重跑 doctor 确认"
            )
    elif active and not active.is_official:
        oks.append(f"无订阅 token,但设置页已激活第三方供应商 {active.name}(可用)")
    else:
        warns.append("无 CLAUDE_CODE_OAUTH_TOKEN 且未配置第三方供应商 —— 官方模型将不可用")
    if os.environ.get("ANTHROPIC_API_KEY"):
        warns.append("ANTHROPIC_API_KEY 仍在环境里 —— 会走 API 按量计费,应移除")
    else:
        oks.append("无 ANTHROPIC_API_KEY 干扰(不会误走按量计费)")

    # 1b) 第三方供应商配置:从设置页读取,cc-switch 配置文件仅作为迁移来源保留
    web_providers = providers.load_active()
    if web_providers and not web_providers.is_official:
        oks.append(f"设置页供应商:检测到 {web_providers.name} · 模型 {web_providers.model}")
    else:
        oks.append("走 .env 的 AGENT_MODEL + 订阅")

    # 2) 数据目录可写 + 会话库
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = config.DATA_DIR / ".doctor_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        oks.append(f"数据目录可写:{config.DATA_DIR}")
    except OSError as e:
        errs.append(f"数据目录不可写:{e}")
    try:
        from .memory import session_store

        n = len(session_store.search("", limit=1))  # 触发连库
        oks.append(f"会话库可读(state.db),近期检索可用")
    except Exception as e:  # noqa: BLE001
        errs.append(f"会话库异常:{e}")

    # 3) AI_BRAIN 记忆
    brain = config.AI_BRAIN_DIR
    if (brain / "USER.md").exists():
        oks.append(f"AI_BRAIN 画像可读:{brain}/USER.md")
    else:
        warns.append(f"AI_BRAIN/USER.md 不存在({brain})—— 启动不会注入画像")
    if brain.exists():
        try:
            (brain / "memory").mkdir(parents=True, exist_ok=True)
            oks.append("AI_BRAIN/memory 可写(save_memory 能落盘)")
        except OSError as e:
            errs.append(f"AI_BRAIN/memory 不可写:{e}")
    else:
        warns.append(f"AI_BRAIN 目录不存在:{brain}")

    # 4) serve 健康（不再以 pgrep 命中某段命令行为准）
    try:
        import json
        from urllib.request import urlopen

        with urlopen(f"http://{config.WEB_HOST}:{config.WEB_PORT}/healthz", timeout=3) as response:
            health = json.loads(response.read())
        running = response.status == 200 and health.get("ok") is True and bool(health.get("boot_id"))
    except Exception:  # noqa: BLE001 — 服务不通仅作为提醒，不影响其它诊断
        running = False
    (oks if running else warns).append(
        "serve 常驻进程在跑" if running else "serve 未在跑(bash deploy/run.sh 启动)"
    )

    for s in oks:
        print(f"✅ {s}")
    for s in warns:
        print(f"⚠️  {s}")
    for s in errs:
        print(f"❌ {s}")
    print(f"\n{len(oks)} 项正常 · {len(warns)} 项提醒 · {len(errs)} 项错误")
    if errs:
        sys.exit(1)


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
    parser = argparse.ArgumentParser(prog="vococo")
    sub = parser.add_subparsers(dest="cmd")
    for name, help_ in [
        ("tui", "rich TUI(默认)"),
        ("chat", "纯文本对话(调试)"),
        ("serve", "常驻:Web + 调度器"),
        ("cron", "列出定时任务"),
        ("doctor", "自检:配置/认证/DB/AI_BRAIN/进程"),
    ]:
        sub.add_parser(name, help=help_)

    args = parser.parse_args()
    cmd = args.cmd or "tui"
    handlers = {
        "tui": _cmd_tui,
        "chat": _cmd_chat,
        "serve": _cmd_serve,
        "cron": _cmd_cron,
        "doctor": _cmd_doctor,
    }
    if cmd not in handlers:
        parser.print_help()
        sys.exit(1)
    handlers[cmd]()


if __name__ == "__main__":
    main()
