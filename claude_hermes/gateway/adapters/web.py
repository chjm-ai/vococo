"""Web 适配器 —— 手机浏览器直连的自建渠道。

一个进程内起 aiohttp:
- GET  /                 单页应用(聊天界面)
- GET  /events           SSE 长连接,服务器把流式事件推给浏览器
- POST /send             浏览器发消息(文本 + 可选图片)进 inbox
- GET  /conversations    会话列表(侧边栏)
- GET  /history?conv=    某会话最近历史(切换会话时加载)
- POST /conv/rename      重命名会话
- POST /conv/delete      删除会话

只负责 Web 的 I/O 和渲染;命令/会话/事件流都在 gateway.core，与 Telegram 共用内核。
Web 端不改写 Markdown 表格(浏览器能原生渲染),这正是"样式更好"的地方。
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from typing import AsyncIterator

from aiohttp import web

from ... import config
from ...core.agent import AgentReply
from ...memory import session_store
from ..core import Choice, Sink
from .base import ImageAttachment, Incoming

_STATIC = Path(__file__).resolve().parent / "web_static"


class _WebSink(Sink):
    """把一轮事件流以 SSE 增量推给浏览器(按 conv 打标)。

    像 Telegram 一样发【全文快照】而非增量:每个 thinking/text 事件都带当前累积
    的完整内容。这样断线补发时任何一帧都能把画面修正确,丢几帧也不会缺字。
    """

    def __init__(self, adapter: "WebAdapter", conv: str):
        super().__init__()
        self.a = adapter
        self.conv = conv
        self._text = ""  # 累积正文,每次发全文
        self._think = ""  # 累积思考
        self.a._emit({"conv": conv, "type": "start"})

    async def thinking(self, text: str) -> None:
        self._think += text
        self.a._emit({"conv": self.conv, "type": "thinking", "text": self._think})

    async def text(self, text: str) -> None:
        self._text += text
        self.a._emit({"conv": self.conv, "type": "text", "text": self._text})

    async def tool_started(self, name: str) -> None:
        self.a._emit({"conv": self.conv, "type": "tool_start", "name": name})

    async def tool_finished(self, name: str, ok: bool, preview: str) -> None:
        self.a._emit(
            {
                "conv": self.conv,
                "type": "tool_end",
                "name": name,
                "ok": ok,
                "preview": preview,
            }
        )

    async def done(self, reply: AgentReply) -> None:
        self.a._emit(
            {"conv": self.conv, "type": "done", "text": reply.text or "(空回复)"}
        )


class WebAdapter:
    platform = "web"

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[Incoming] = asyncio.Queue()
        # 每个浏览器 SSE 连接一个队列,元素是 (事件编号, JSON 串)
        self._clients: set[asyncio.Queue[tuple[int, str]]] = set()
        self._seq = 0  # 全局单调递增的事件编号
        self._buffer: deque[tuple[int, str]] = deque(maxlen=512)  # 断线补发用的环形缓冲
        self._runner: web.AppRunner | None = None
        try:
            self._index = (_STATIC / "index.html").read_text(encoding="utf-8")
        except OSError:
            self._index = "<h1>index.html 缺失</h1>"

    # ── 认证 ────────────────────────────────────────────────────────────
    def _ok_token(self, request: web.Request) -> bool:
        if not config.WEB_AUTH_TOKEN:
            return True  # 未设口令 = 不校验(仅本机调试)
        tok = request.headers.get("X-Auth-Token") or request.query.get("token") or ""
        return tok == config.WEB_AUTH_TOKEN

    def _guard(self, request: web.Request) -> web.Response | None:
        if not self._ok_token(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return None

    # ── SSE 广播 ─────────────────────────────────────────────────────────
    def _emit(self, payload: dict) -> None:
        """给事件编号、进环形缓冲,再塞进所有在线浏览器的 SSE 队列。

        编号(id)让前端能去重、让重连时按 Last-Event-ID 补发漏掉的事件——
        这就是 web 版的"中间仓库",丢一帧断一线都能补回来。
        """
        self._seq += 1
        seq = self._seq
        payload = {**payload, "id": seq}
        data = json.dumps(payload, ensure_ascii=False)
        self._buffer.append((seq, data))
        for q in list(self._clients):
            try:
                q.put_nowait((seq, data))
            except asyncio.QueueFull:
                pass

    # ── Adapter 协议 ─────────────────────────────────────────────────────
    async def send(self, chat_id: int | str, text: str) -> None:
        """一条完整消息(命令回复 / cron 主动推送)→ 作为一条 assistant 气泡推给前端。"""
        self._emit({"conv": str(chat_id), "type": "message", "text": text})

    def make_sink(self, chat_id: int | str) -> Sink:
        return _WebSink(self, str(chat_id))

    async def present_choice(self, chat_id: int | str, choice: Choice) -> None:
        self._emit(
            {
                "conv": str(chat_id),
                "type": "choice",
                "prompt": choice.prompt,
                "options": [[cmd, label] for cmd, label in choice.options],
            }
        )

    async def receive(self) -> AsyncIterator[Incoming]:
        await self._start_server()
        while True:
            yield await self._inbox.get()

    # ── HTTP 路由 ────────────────────────────────────────────────────────
    async def _handle_index(self, request: web.Request) -> web.Response:
        return web.Response(text=self._index, content_type="text/html")

    async def _handle_events(self, request: web.Request) -> web.StreamResponse:
        if not self._ok_token(request):
            return web.Response(status=401, text="unauthorized")
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # 关代理缓冲,保证流式即时
            }
        )
        await resp.prepare(request)
        # 重连时浏览器会自动带上 Last-Event-ID(上次收到的最后编号);首连则为空。
        # 首连不带 → 从当前最新编号起步,只接新事件(旧历史走 HTTP 拉,不重放缓冲);
        # 重连带了 → 从该编号补发漏掉的事件。
        raw_last = request.headers.get("Last-Event-ID") or request.query.get("last_id")
        try:
            last_id = int(raw_last) if raw_last else self._seq
        except ValueError:
            last_id = self._seq
        q: asyncio.Queue[tuple[int, str]] = asyncio.Queue(maxsize=1000)
        self._clients.add(q)  # 先挂上队列,再补发,漏网的靠前端按 id 去重
        try:
            await resp.write(b": connected\n\n")
            # 补发断线期间漏掉的事件(全文快照,直接重放即还原画面)
            for seq, data in list(self._buffer):
                if seq > last_id:
                    await resp.write(f"id: {seq}\ndata: {data}\n\n".encode("utf-8"))
            while True:
                try:
                    seq, data = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")  # 心跳保活,防代理掐断
                    continue
                await resp.write(f"id: {seq}\ndata: {data}\n\n".encode("utf-8"))
        except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
            pass
        finally:
            self._clients.discard(q)
        return resp

    async def _handle_send(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "bad json"}, status=400)
        conv = str(body.get("conv") or "main")
        text = (body.get("text") or "").strip()
        images = [
            ImageAttachment(
                data=i["data"], media_type=i.get("media_type", "image/jpeg")
            )
            for i in (body.get("images") or [])
            if i.get("data")
        ]
        if not text and not images:
            return web.json_response({"error": "empty"}, status=400)
        if not text:
            text = "(图片,无文字说明,看看图里是什么)"
        # 首条消息自动给会话起个名(命令 / 主会话除外)
        if not text.startswith("/") and conv != "main":
            session_store.ensure_title(config.resolve_session_key("web", conv), text)
        self._inbox.put_nowait(Incoming(self.platform, conv, text, images=images))
        return web.json_response({"ok": True})

    async def _handle_conversations(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        convs = session_store.list_sessions("web:")
        # 主会话(与 TG/CLI 共享的统一会话)固定置顶,conv id 用 "main"
        main = session_store.session_summary(config.resolve_session_key("web", "main"))
        main.update(key="main", title="主会话", pinned=True)
        # list_sessions 的 key 是 "web:xxx",前端只认 conv id(去掉前缀)
        for c in convs:
            c["conv"] = c["key"].split(":", 1)[1] if ":" in c["key"] else c["key"]
        main["conv"] = "main"
        return web.json_response({"main": main, "conversations": convs})

    async def _handle_history(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        conv = request.query.get("conv", "main")
        key = config.resolve_session_key("web", conv)
        turns = session_store.load_recent(key, limit=40)
        return web.json_response(
            {"turns": [{"user": t.user, "assistant": t.assistant} for t in turns]}
        )

    async def _handle_rename(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        body = await request.json()
        conv = str(body.get("conv") or "")
        title = (body.get("title") or "").strip()
        if conv and conv != "main" and title:
            session_store.set_title(config.resolve_session_key("web", conv), title)
        return web.json_response({"ok": True})

    async def _handle_delete(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        body = await request.json()
        conv = str(body.get("conv") or "")
        if conv and conv != "main":
            session_store.delete_session(config.resolve_session_key("web", conv))
        return web.json_response({"ok": True})

    async def _start_server(self) -> None:
        app = web.Application(client_max_size=32 * 1024 * 1024)  # 允许 32MB 图片上传
        app.add_routes(
            [
                web.get("/", self._handle_index),
                web.get("/events", self._handle_events),
                web.post("/send", self._handle_send),
                web.get("/conversations", self._handle_conversations),
                web.get("/history", self._handle_history),
                web.post("/conv/rename", self._handle_rename),
                web.post("/conv/delete", self._handle_delete),
            ]
        )
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, config.WEB_HOST, config.WEB_PORT)
        await site.start()
        lock = "🔒 已设访问口令" if config.WEB_AUTH_TOKEN else "⚠️ 未设口令(任何人可用)"
        print(
            f"✅ Web 已上线 http://{config.WEB_HOST}:{config.WEB_PORT} · "
            f"{lock} · 模型 {config.MODEL}"
        )
