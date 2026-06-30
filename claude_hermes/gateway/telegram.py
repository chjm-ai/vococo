"""Telegram 入口 —— 长轮询(getUpdates)接收,复用同一个 agent loop 回复。

个人 bot:用 TELEGRAM_ALLOWED_CHAT_IDS 白名单只认自己。
首次未配白名单时,bot 会把对方 chat_id 回给他,方便你拿到自己的 id 再填进 .env。
"""
from __future__ import annotations

import httpx

from .. import config
from ..core.agent import Turn, run_turn

WELCOME = "👋 我是 claude-hermes,你的私人助理。直接发消息即可。/clear 清空上下文。"


class TelegramError(RuntimeError):
    pass


async def _api(client: httpx.AsyncClient, base: str, method: str, **payload):
    r = await client.post(f"{base}/{method}", json=payload)
    data = r.json()
    if not data.get("ok"):
        raise TelegramError(f"{method} 失败: {data}")
    return data["result"]


async def _send(client: httpx.AsyncClient, base: str, chat_id: int, text: str) -> None:
    # Telegram 单条上限 4096,长回复分段发(纯文本,不用 Markdown 以免转义踩坑)
    for i in range(0, len(text) or 1, 4000):
        chunk = text[i : i + 4000] or "(空回复)"
        await _api(client, base, "sendMessage", chat_id=chat_id, text=chunk)


async def run_telegram() -> None:
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        raise TelegramError(
            "缺少 TELEGRAM_BOT_TOKEN。\n"
            "  解决:在 Telegram 找 @BotFather 发 /newbot 建一个 bot,\n"
            "  把拿到的 token 写进 .env 的 TELEGRAM_BOT_TOKEN。"
        )
    base = f"https://api.telegram.org/bot{token}"
    allowed = config.TELEGRAM_ALLOWED_CHAT_IDS
    histories: dict[int, list[Turn]] = {}
    offset: int | None = None

    async with httpx.AsyncClient(timeout=httpx.Timeout(70.0)) as client:
        me = await _api(client, base, "getMe")
        print(f"✅ Telegram bot 已上线:@{me.get('username')} · 模型 {config.MODEL}")
        if not allowed:
            print("⚠️  未配 TELEGRAM_ALLOWED_CHAT_IDS:任何人都能跟它聊。")
            print("    给 bot 发条消息,控制台会打印你的 chat_id,填进 .env 再重启即可锁定。")

        while True:
            updates = await _api(
                client, base, "getUpdates", timeout=50, offset=offset
            )
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                text = (msg.get("text") or "").strip()
                if chat_id is None or not text:
                    continue

                print(f"[收] chat_id={chat_id}: {text[:60]}")

                if allowed and chat_id not in allowed:
                    await _send(
                        client, base, chat_id,
                        f"未授权。你的 chat_id 是 {chat_id},"
                        f"让主人把它加进白名单。",
                    )
                    continue

                if text in ("/start", "/help"):
                    await _send(client, base, chat_id, WELCOME)
                    continue
                if text == "/clear":
                    histories.pop(chat_id, None)
                    await _send(client, base, chat_id, "🧹 上下文已清空。")
                    continue

                await _api(
                    client, base, "sendChatAction", chat_id=chat_id, action="typing"
                )
                hist = histories.setdefault(chat_id, [])
                try:
                    reply = await run_turn(hist, text)
                except Exception as e:  # 单条消息失败不该让整个 bot 崩
                    await _send(client, base, chat_id, f"⚠️ 出错了:{e}")
                    continue
                hist.append(Turn(user=text, assistant=reply.text))
                await _send(client, base, chat_id, reply.text)
