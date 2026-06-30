"""Telegram 入口 —— 长轮询接收,消费 agent.stream_turn 事件流。

流式体验:
- 工具调用 → 发一条 "🔧 名字" 通知(过程可见)
- 正文 → 先发一条占位消息,再用 editMessageText 节流更新(流式感)

个人 bot:TELEGRAM_ALLOWED_CHAT_IDS 白名单只认自己;未配时回显 chat_id。
"""
from __future__ import annotations

import time

import httpx

from .. import config
from ..core.agent import Done, TextDelta, ToolStarted, Turn, stream_turn

WELCOME = "👋 我是 claude-hermes,你的私人助理。直接发消息即可。/clear 清空上下文。"
EDIT_MIN_INTERVAL = 1.1  # 秒,Telegram 编辑频率限制
EDIT_MIN_CHARS = 50
TG_LIMIT = 4000


class TelegramError(RuntimeError):
    pass


async def _api(client: httpx.AsyncClient, base: str, method: str, **payload):
    r = await client.post(f"{base}/{method}", json=payload)
    data = r.json()
    if not data.get("ok"):
        raise TelegramError(f"{method} 失败: {data}")
    return data["result"]


async def _send(client, base, chat_id: int, text: str) -> int | None:
    """发送(超长分段)。返回最后一条 message_id。"""
    last_id = None
    for i in range(0, len(text) or 1, TG_LIMIT):
        chunk = text[i : i + TG_LIMIT] or "(空回复)"
        res = await _api(client, base, "sendMessage", chat_id=chat_id, text=chunk)
        last_id = res.get("message_id")
    return last_id


async def _stream_reply(client, base, chat_id: int, history: list[Turn], text: str) -> None:
    """消费事件流,流式回 Telegram。"""
    answer = ""
    msg_id: int | None = None
    last_text = ""
    last_edit = 0.0
    reply = None

    async def flush(force: bool = False) -> None:
        nonlocal msg_id, last_text, last_edit
        if not answer or len(answer) > TG_LIMIT:  # 超长留给最后 _send 分段
            return
        now = time.monotonic()
        if not force and (now - last_edit < EDIT_MIN_INTERVAL
                          or len(answer) - len(last_text) < EDIT_MIN_CHARS):
            return
        if answer == last_text:
            return
        try:
            if msg_id is None:
                res = await _api(client, base, "sendMessage", chat_id=chat_id, text=answer)
                msg_id = res.get("message_id")
            else:
                await _api(client, base, "editMessageText",
                           chat_id=chat_id, message_id=msg_id, text=answer)
            last_text, last_edit = answer, now
        except TelegramError:
            pass  # 编辑频率/未改动等错误忽略

    async for ev in stream_turn(history, text):
        if isinstance(ev, ToolStarted):
            await _api(client, base, "sendMessage", chat_id=chat_id, text=f"🔧 {ev.name}")
        elif isinstance(ev, TextDelta):
            answer += ev.text
            await flush()
        elif isinstance(ev, Done):
            reply = ev.reply

    final = (reply.text if reply else answer) or "(空回复)"
    if len(final) > TG_LIMIT or msg_id is None:
        await _send(client, base, chat_id, final)
    else:
        answer = final
        await flush(force=True)
    history.append(Turn(user=text, assistant=final))


async def run_telegram() -> None:
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        raise TelegramError(
            "缺少 TELEGRAM_BOT_TOKEN。\n"
            "  解决:Telegram 找 @BotFather 发 /newbot 建 bot,\n"
            "  把 token 写进 .env 的 TELEGRAM_BOT_TOKEN。"
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
            print("    给 bot 发条消息,控制台会打印你的 chat_id,填进 .env 再重启锁定。")

        while True:
            updates = await _api(client, base, "getUpdates", timeout=50, offset=offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                text = (msg.get("text") or "").strip()
                if chat_id is None or not text:
                    continue
                print(f"[收] chat_id={chat_id}: {text[:60]}")

                if allowed and chat_id not in allowed:
                    await _send(client, base, chat_id,
                                f"未授权。你的 chat_id 是 {chat_id},让主人把它加进白名单。")
                    continue
                if text in ("/start", "/help"):
                    await _send(client, base, chat_id, WELCOME)
                    continue
                if text == "/clear":
                    histories.pop(chat_id, None)
                    await _send(client, base, chat_id, "🧹 上下文已清空。")
                    continue

                await _api(client, base, "sendChatAction", chat_id=chat_id, action="typing")
                hist = histories.setdefault(chat_id, [])
                try:
                    await _stream_reply(client, base, chat_id, hist, text)
                except Exception as e:
                    await _send(client, base, chat_id, f"⚠️ 出错了:{e}")
