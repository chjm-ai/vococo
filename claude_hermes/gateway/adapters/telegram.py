"""Telegram 适配器 —— 长轮询收发 + 流式 Sink(节流 editMessageText)。

只负责 Telegram 的 I/O 和渲染;命令/会话/事件流都在 gateway.core。
"""
from __future__ import annotations

import time
from typing import AsyncIterator

import anyio
import httpx

from ... import config
from ...core.agent import AgentReply
from ..core import COMMAND_LIST, Sink
from .base import Incoming

TG_LIMIT = 4000
EDIT_MIN_INTERVAL = 1.1
EDIT_MIN_CHARS = 50


class TelegramError(RuntimeError):
    pass


class _TelegramSink(Sink):
    """把事件流渲染到 Telegram:工具发通知,正文节流 editMessageText。"""

    def __init__(self, adapter: "TelegramAdapter", chat_id: int | str):
        self.a = adapter
        self.chat_id = chat_id
        self.answer = ""
        self.msg_id: int | None = None
        self.last_text = ""
        self.last_edit = 0.0

    async def tool_started(self, name: str) -> None:
        await self.a._call("sendMessage", chat_id=self.chat_id, text=f"🔧 {name}")

    async def text(self, delta: str) -> None:
        self.answer += delta
        await self._flush(force=False)

    async def done(self, reply: AgentReply) -> None:
        self.answer = reply.text or "(空回复)"
        if len(self.answer) > TG_LIMIT or self.msg_id is None:
            await self.a.send(self.chat_id, self.answer)
        else:
            await self._flush(force=True)

    async def _flush(self, force: bool) -> None:
        if not self.answer or len(self.answer) > TG_LIMIT:
            return
        now = time.monotonic()
        if not force and (
            now - self.last_edit < EDIT_MIN_INTERVAL
            or len(self.answer) - len(self.last_text) < EDIT_MIN_CHARS
        ):
            return
        if self.answer == self.last_text:
            return
        try:
            if self.msg_id is None:
                res = await self.a._call(
                    "sendMessage", chat_id=self.chat_id, text=self.answer
                )
                self.msg_id = res.get("message_id")
            else:
                await self.a._call(
                    "editMessageText",
                    chat_id=self.chat_id,
                    message_id=self.msg_id,
                    text=self.answer,
                )
            self.last_text, self.last_edit = self.answer, now
        except TelegramError:
            pass


class TelegramAdapter:
    platform = "telegram"

    def __init__(self) -> None:
        token = config.TELEGRAM_BOT_TOKEN
        if not token:
            raise TelegramError(
                "缺少 TELEGRAM_BOT_TOKEN。Telegram 找 @BotFather /newbot 建 bot,token 写进 .env。"
            )
        self.base = f"https://api.telegram.org/bot{token}"
        self.allowed = config.TELEGRAM_ALLOWED_CHAT_IDS
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(70.0))

    async def _call(self, method: str, **payload):
        r = await self.client.post(f"{self.base}/{method}", json=payload)
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(f"{method} 失败: {data}")
        return data["result"]

    async def send(self, chat_id: int | str, text: str) -> None:
        for i in range(0, len(text) or 1, TG_LIMIT):
            await self._call(
                "sendMessage", chat_id=chat_id, text=text[i : i + TG_LIMIT] or "(空)"
            )

    def make_sink(self, chat_id: int | str) -> Sink:
        return _TelegramSink(self, chat_id)

    async def _register_commands(self) -> None:
        """注册命令菜单 → TG 客户端打 / 就能弹出选择。"""
        cmds = [{"command": n, "description": d} for n, d in COMMAND_LIST]
        await self._call("setMyCommands", commands=cmds)

    async def receive(self) -> AsyncIterator[Incoming]:
        # 连上为止(网络没就绪时不崩,重试)
        while True:
            try:
                me = await self._call("getMe")
                break
            except (httpx.HTTPError, TelegramError) as e:
                print(f"[tg] getMe 失败,5s 后重试: {e}")
                await anyio.sleep(5)
        try:
            await self._register_commands()
        except (httpx.HTTPError, TelegramError):
            pass
        print(f"✅ Telegram @{me.get('username')} 已上线 · 模型 {config.MODEL}")
        if not self.allowed:
            print("⚠️  未配白名单:任何人都能聊。发条消息看控制台 chat_id,填 .env 再重启。")
        offset: int | None = None
        while True:
            try:
                updates = await self._call("getUpdates", timeout=50, offset=offset)
            except (httpx.HTTPError, TelegramError) as e:
                print(f"[tg] getUpdates 出错,3s 后重试: {e}")
                await anyio.sleep(3)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                text = (msg.get("text") or "").strip()
                if chat_id is None or not text:
                    continue
                print(f"[收] tg chat_id={chat_id}: {text[:60]}")
                try:
                    if self.allowed and chat_id not in self.allowed:
                        await self.send(
                            chat_id, f"未授权。你的 chat_id 是 {chat_id},让主人加进白名单。"
                        )
                        continue
                    await self._call("sendChatAction", chat_id=chat_id, action="typing")
                except (httpx.HTTPError, TelegramError):
                    pass
                yield Incoming(self.platform, chat_id, text)
