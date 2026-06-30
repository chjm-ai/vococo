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
from ..core import COMMAND_LIST, Choice, Sink
from .base import Incoming

TG_LIMIT = 4000
EDIT_MIN_INTERVAL = 0.8  # editMessageText 节流(TG 上限约每秒 1 次)
EDIT_MIN_CHARS = 24      # 正文增量满这么多字才编辑(攒批省请求)


class TelegramError(RuntimeError):
    pass


class _TelegramSink(Sink):
    """把事件流渲染到 Telegram:单条消息滚动更新。

    顶部一行实时状态(💭 思考中 / 🔧 Read✓ · Bash⏳),下面流式正文,
    都进同一条消息(editMessageText)。不再为每个工具刷屏。
    首条消息立即发出(首字延迟最小),之后按节流编辑。
    """

    def __init__(self, adapter: "TelegramAdapter", chat_id: int | str):
        super().__init__()
        self.a = adapter
        self.chat_id = chat_id
        self.msg_id: int | None = None
        self.last_sent = ""
        self.last_edit = 0.0

    def _compose(self) -> str:
        """状态行 + 正文。两者都有则状态行在上(正文出来后状态行只剩工具进度)。"""
        head = self.status_line()
        body = self.answer
        if head and body:
            return f"{head}\n\n{body}"
        return head or body

    async def render(self) -> None:
        await self._flush(force=False)

    async def done(self, reply: AgentReply) -> None:
        # 最终只留正文(去掉状态行);超长则分片重发
        self.answer = reply.text or "(空回复)"
        self.tools.clear()
        if len(self.answer) > TG_LIMIT or self.msg_id is None:
            await self.a.send(self.chat_id, self.answer)
        else:
            await self._flush(force=True)

    async def _flush(self, force: bool) -> None:
        content = self._compose()
        if not content or len(content) > TG_LIMIT:
            return
        if content == self.last_sent:
            return
        now = time.monotonic()
        # 首条消息立即发(首字延迟最小);后续编辑才节流。
        # 工具进度变化也值得即时反馈,放宽字数门槛。
        if self.msg_id is not None and not force:
            if now - self.last_edit < EDIT_MIN_INTERVAL:
                return
            tool_active = self.status_line().startswith("🔧")
            if not tool_active and len(content) - len(self.last_sent) < EDIT_MIN_CHARS:
                return
        try:
            if self.msg_id is None:
                res = await self.a._call(
                    "sendMessage", chat_id=self.chat_id, text=content
                )
                self.msg_id = res.get("message_id")
            else:
                await self.a._call(
                    "editMessageText",
                    chat_id=self.chat_id,
                    message_id=self.msg_id,
                    text=content,
                )
            self.last_sent, self.last_edit = content, now
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

    async def present_choice(self, chat_id: int | str, choice: Choice) -> None:
        """用 inline keyboard 渲染选项,callback_data=选中后要执行的命令。"""
        keyboard = [[{"text": label, "callback_data": cmd}] for cmd, label in choice.options]
        await self._call(
            "sendMessage",
            chat_id=chat_id,
            text=choice.prompt,
            reply_markup={"inline_keyboard": keyboard},
        )

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

                # 按钮点击:callback_data 就是要执行的命令
                cq = upd.get("callback_query")
                if cq:
                    data = (cq.get("data") or "").strip()
                    cq_chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
                    try:
                        await self._call("answerCallbackQuery", callback_query_id=cq["id"])
                    except (httpx.HTTPError, TelegramError):
                        pass
                    if cq_chat is None or not data:
                        continue
                    if self.allowed and cq_chat not in self.allowed:
                        continue
                    print(f"[收] tg 点击 chat_id={cq_chat}: {data}")
                    yield Incoming(self.platform, cq_chat, data)
                    continue

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
