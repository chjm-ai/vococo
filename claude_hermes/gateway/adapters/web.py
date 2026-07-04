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
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import AsyncIterator, Callable

import aiohttp
from aiohttp import web

from ... import config, providers
from ...core.agent import AgentReply
from ...memory import session_store
from .. import settings_store
from ..core import MODEL_CHOICES, Choice, Sink
from .base import ImageAttachment, Incoming
from .web_push import PUSH

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

    async def tool_started(
        self, name: str, tool_id: str = "", parent_id: str | None = None
    ) -> None:
        # parent 非空 = 子代理(Task)内部的工具,前端嵌进对应 Task 卡片
        self.a._emit(
            {
                "conv": self.conv,
                "type": "tool_start",
                "name": name,
                "tool_id": tool_id,
                "parent": parent_id or "",
            }
        )

    async def tool_input(
        self,
        name: str,
        tool_id: str,
        tool_input: dict,
        parent_id: str | None = None,
    ) -> None:
        # 把工具入参推给前端 → 渲染 diff / todo 清单 / 计划卡 / 命令预览
        self.a._emit(
            {
                "conv": self.conv,
                "type": "tool_input",
                "name": name,
                "tool_id": tool_id,
                "input": tool_input,
                "parent": parent_id or "",
            }
        )

    async def tool_finished(
        self,
        name: str,
        ok: bool,
        preview: str,
        tool_id: str = "",
        detail: str = "",
        parent_id: str | None = None,
    ) -> None:
        self.a._emit(
            {
                "conv": self.conv,
                "type": "tool_end",
                "name": name,
                "tool_id": tool_id,
                "ok": ok,
                "preview": preview,
                "detail": detail,
                "parent": parent_id or "",
            }
        )

    async def done(self, reply: AgentReply) -> None:
        # converse() 已在此之前写好 token 计量,读回最新明细推给前端(实时刷新顶栏)
        key = config.resolve_session_key("web", self.conv)
        meta = session_store.session_summary(key)
        self.a._emit(
            {
                "conv": self.conv,
                "type": "done",
                "text": reply.text or "(空回复)",
                "ctx_tokens": meta.get("ctx_tokens", 0),
                "total_tokens": meta.get("total_tokens", 0),
                "ctx_window": meta.get("ctx_window", 0),
                "last_in": meta.get("last_in", 0),
                "last_cache": meta.get("last_cache", 0),
                "last_out": meta.get("last_out", 0),
                "model": meta.get("model", ""),
                "chosen_model": meta.get("chosen_model", ""),
            }
        )
        # 场景①「回复完成」:人不在页面时弹系统通知(前台由 SW 自行抑制)
        title = "主会话" if self.conv == "main" else (
            session_store.get_title(key) or "Hermes"
        )
        self.a._push_notify(
            title=title,
            body=reply.text or "回复完成",
            conv=self.conv,
            kind="done",
            enabled=config.PUSH_ON_DONE,
        )


class WebAdapter:
    platform = "web"

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[Incoming] = asyncio.Queue()
        # 每个浏览器 SSE 连接一个队列,元素是 (事件编号, JSON 串)
        self._clients: set[asyncio.Queue[tuple[int, str]]] = set()
        self._seq = 0  # 全局单调递增的事件编号
        self._buffer: deque[tuple[int, str]] = deque(maxlen=512)  # 断线补发用的环形缓冲
        # 每会话「进行中那一轮」的活状态快照:conv -> {started, phase, frames:[(seq,payload)]}。
        # 刷新/首连时据此「状态先行、内容随后」地恢复——先秒推一条状态帧让用户知道
        # 「这轮还在跑、到哪一步」(避免空窗误发),再慢慢补回思考/正文/工具帧。
        self._live: dict[str, dict] = {}
        self._runner: web.AppRunner | None = None
        self._cancel_callback: Callable[[str], bool] | None = None

    def set_cancel_callback(self, cb: Callable[[str], bool]) -> None:
        self._cancel_callback = cb

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
        self._track_live(payload, seq)  # 维护「进行中那一轮」快照,供刷新恢复
        for q in list(self._clients):
            try:
                q.put_nowait((seq, data))
            except asyncio.QueueFull:
                pass

    def _track_live(self, payload: dict, seq: int) -> None:
        """把每帧折进 conv 的「进行中回合」快照;start 开一轮,done/message 收一轮。

        text/thinking 是全文快照,同类型只留最新一帧(防长回复撑爆);工具帧按序累积。
        这样首连时只需重放这一小撮帧,就能把画面恢复到当前进度,而非翻遍全局缓冲。
        """
        conv = payload.get("conv")
        if not conv:
            return
        t = payload.get("type")
        if t == "start":
            self._live[conv] = {
                "started": time.time(), "phase": "思考中", "frames": [(seq, payload)]
            }
            return
        if t in ("done", "message"):
            self._live.pop(conv, None)  # 一轮结束(choice 是审批暂停,不收轮)
            return
        st = self._live.get(conv)
        if st is None:
            return  # 没有进行中的回合,零散帧忽略
        if t in ("thinking", "text"):
            st["phase"] = "回复中" if t == "text" else "思考中"
            st["frames"] = [(s, p) for s, p in st["frames"] if p.get("type") != t]
            st["frames"].append((seq, payload))
        elif t in ("tool_start", "tool_input", "tool_end"):
            st["phase"] = f"执行 {payload.get('name') or '工具'}"
            st["frames"].append((seq, payload))

    # ── Web Push 系统通知 ────────────────────────────────────────────────
    def _push_notify(
        self,
        *,
        title: str,
        body: str,
        conv: str,
        kind: str,
        enabled: bool,
    ) -> None:
        """按场景发一条系统推送(非阻塞:丢进后台任务,绝不拖慢 SSE)。

        enabled 来自 config 里各场景开关;未配 VAPID 或没订阅设备则静默跳过。
        """
        if not enabled or not PUSH.is_configured():
            return
        try:
            asyncio.get_event_loop().create_task(
                PUSH.notify(title, body, conv=conv, kind=kind)
            )
        except RuntimeError:
            pass  # 没有运行中的事件循环(理论上不会走到),放弃这条通知

    # ── Adapter 协议 ─────────────────────────────────────────────────────
    async def send(self, chat_id: int | str, text: str) -> None:
        """一条完整消息(命令回复 / cron 主动推送 / 报错)→ 作为一条 assistant 气泡推给前端。"""
        self._emit({"conv": str(chat_id), "type": "message", "text": text})
        # 场景③「主动/cron」与 场景④「出错」共用这条出口,靠 ⚠️ 前缀区分
        is_err = text.lstrip().startswith("⚠️")
        self._push_notify(
            title="⚠️ 出错了" if is_err else "Hermes",
            body=text,
            conv=str(chat_id),
            kind="error" if is_err else "proactive",
            enabled=config.PUSH_ON_ERROR if is_err else config.PUSH_ON_PROACTIVE,
        )

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
        # 场景②「需要审批/确认」:高优先级,SW 前台也会弹,确保不漏
        self._push_notify(
            title="需要你确认",
            body=choice.prompt,
            conv=str(chat_id),
            kind="approval",
            enabled=config.PUSH_ON_APPROVAL,
        )

    async def receive(self) -> AsyncIterator[Incoming]:
        await self._start_server()
        while True:
            yield await self._inbox.get()

    # ── HTTP 路由 ────────────────────────────────────────────────────────
    async def _handle_index(self, request: web.Request) -> web.Response:
        # 每次请求实时读盘:改了 UI 刷新浏览器即可,不用重启 serve;no-cache 让浏览器也别缓存
        try:
            html = (_STATIC / "index.html").read_text(encoding="utf-8")
        except OSError:
            html = "<h1>index.html 缺失</h1>"
        return web.Response(
            text=html, content_type="text/html", headers={"Cache-Control": "no-cache"}
        )

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
            # 首连(刷新/新开)不带 Last-Event-ID:两阶段恢复「进行中那一轮」——
            # 先秒推状态帧(几十字节,让用户立刻看到"还在跑、到哪步",不会误发),
            # 内容帧随后慢补。旧历史仍走 /history 拉,不在此重放。
            if not raw_last and self._live:
                now = time.time()
                for conv, st in list(self._live.items()):  # 阶段①:状态帧先行
                    status = json.dumps(
                        {
                            "conv": conv, "type": "live_status",
                            "phase": st.get("phase", ""),
                            "elapsed": max(0, int(now - st.get("started", now))),
                        },
                        ensure_ascii=False,
                    )
                    # 不带 SSE id:纯状态提示,不去重、不污染重连游标
                    await resp.write(f"data: {status}\n\n".encode("utf-8"))
                for conv, st in list(self._live.items()):  # 阶段②:内容帧随后
                    # 按 seq 升序补发,保证前端按 id 去重时单调不丢帧
                    for seq, payload in sorted(st["frames"], key=lambda x: x[0]):
                        if payload.get("type") == "start":  # start 带真实已跑秒数
                            payload = {**payload,
                                       "elapsed": max(0, int(now - st.get("started", now)))}
                        data = json.dumps(payload, ensure_ascii=False)
                        await resp.write(f"id: {seq}\ndata: {data}\n\n".encode("utf-8"))
            # 重连:补发断线期间漏掉的事件(全文快照,直接重放即还原画面)
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
        # 项目会话(conv = p<hash>:<convid>)→ 刷新项目最近使用时间,好让侧边栏排序
        if conv.startswith("p") and ":" in conv:
            session_store.touch_project(conv[1:].split(":", 1)[0])
        # 广播用户消息让其他客户端(如桌面端)能实时渲染用户气泡
        if not text.startswith("/"):
            self._emit({"conv": conv, "type": "user", "text": text})
        self._inbox.put_nowait(Incoming(self.platform, conv, text, images=images))
        return web.json_response({"ok": True})

    async def _handle_abort(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "bad json"}, status=400)
        conv = str(body.get("conv") or "main")
        session_key = config.resolve_session_key("web", conv)
        stopped = bool(self._cancel_callback and self._cancel_callback(session_key))
        return web.json_response({"ok": True, "stopped": stopped})

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

    # ── 项目 ─────────────────────────────────────────────────────────────
    async def _handle_projects(self, request: web.Request) -> web.Response:
        """项目列表(侧边栏按项目分组用)。"""
        if (g := self._guard(request)) is not None:
            return g
        return web.json_response({"projects": session_store.list_projects()})

    async def _handle_browse(self, request: web.Request) -> web.Response:
        """服务端目录浏览器:列出某目录下的子文件夹(默认从用户主目录起)。

        只列目录、跳过隐藏(. 开头)项;返回可上翻的 parent。服务跑在本机,
        且本接口同样过 _ok_token —— 但一旦开了公网隧道又没设口令,等于把整块
        硬盘目录结构暴露出去,故务必设 WEB_AUTH_TOKEN。
        """
        if (g := self._guard(request)) is not None:
            return g
        raw = request.query.get("dir") or str(Path.home())
        try:
            base = Path(os.path.expanduser(raw)).resolve()
        except (OSError, ValueError):
            base = Path.home()
        if not base.is_dir():
            base = Path.home()
        entries: list[dict] = []
        try:
            for name in sorted(os.listdir(base), key=str.lower):
                if name.startswith("."):
                    continue
                p = base / name
                try:
                    if p.is_dir():
                        entries.append({"name": name, "path": str(p)})
                except OSError:
                    continue
        except (PermissionError, OSError):
            pass
        parent = str(base.parent) if base.parent != base else None
        return web.json_response({"dir": str(base), "parent": parent, "entries": entries})

    async def _handle_project_create(self, request: web.Request) -> web.Response:
        """新建/复活项目:校验路径是真实目录 → 入库,返回项目信息。"""
        if (g := self._guard(request)) is not None:
            return g
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "bad json"}, status=400)
        path = (body.get("path") or "").strip()
        if not path:
            return web.json_response({"error": "路径不能为空"}, status=400)
        norm = session_store.normalize_project_path(path)
        if not Path(norm).is_dir():
            return web.json_response({"error": f"不是有效目录:{norm}"}, status=400)
        return web.json_response({"project": session_store.upsert_project(norm)})

    async def _handle_project_remove(self, request: web.Request) -> web.Response:
        """软移除项目:仅从列表隐藏,文件夹与会话历史都不动。"""
        if (g := self._guard(request)) is not None:
            return g
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "bad json"}, status=400)
        h = (body.get("hash") or "").strip()
        if h:
            session_store.hide_project(h)
        return web.json_response({"ok": True})

    async def _handle_models(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        # 模型清单 = 官方档 + cc-switch 里配好的 DeepSeek/Kimi 等(available_models);
        # 只显示模型名(label=id,不带描述);default=当前激活的模型(跟随 cc-switch)。
        choices = providers.available_models(MODEL_CHOICES)
        # default = web 端上次选定的模型;没设过才回落到全局激活模型(cc-switch)
        active_model = settings_store.get_web_default_model() \
            or providers.resolve(None, config.MODEL)[0]
        return web.json_response(
            {
                "default": active_model,
                "choices": [[v, v] for v, _ in choices],
            }
        )

    # ── 语音转文字 ───────────────────────────────────────────────────────
    async def _handle_transcribe(self, request: web.Request) -> web.Response:
        """收手机录音 → 调 SenseVoice 转成文字回给前端(填进输入框)。"""
        if (g := self._guard(request)) is not None:
            return g
        if not config.STT_API_KEY:
            return web.json_response(
                {"error": "未配置语音转写:请在 .env 设 SILICONFLOW_API_KEY"},
                status=503,
            )
        t0 = time.monotonic()
        audio, filename, ctype = await self._read_audio(request)
        if not audio:
            return web.json_response({"error": "没收到音频"}, status=400)
        t1 = time.monotonic()
        resp = await self._transcribe(audio, filename, ctype)
        t2 = time.monotonic()
        # 诊断"转写慢"到底慢在哪:recv=接收上传耗时, stt=SenseVoice 往返耗时
        print(
            f"[transcribe] size={len(audio) / 1024:.1f}KB "
            f"recv={t1 - t0:.2f}s stt={t2 - t1:.2f}s total={t2 - t0:.2f}s",
            flush=True,
        )
        return resp

    async def _read_audio(
        self, request: web.Request
    ) -> tuple[bytes | None, str, str]:
        """从 multipart 里取出 audio 字段(字节、文件名、类型)。"""
        try:
            reader = await request.multipart()
        except (ValueError, AssertionError):
            return None, "", ""
        async for part in reader:
            if part.name == "audio":
                data = await part.read(decode=False)
                ctype = part.headers.get("Content-Type", "application/octet-stream")
                return data, (part.filename or "voice.webm"), ctype
        return None, "", ""

    async def _transcribe(
        self, audio: bytes, filename: str, ctype: str
    ) -> web.Response:
        """把音频转发给 SenseVoice(OpenAI 兼容 /audio/transcriptions),返回 {text}。"""
        form = aiohttp.FormData()
        form.add_field("model", config.STT_MODEL)
        form.add_field("file", audio, filename=filename, content_type=ctype)
        url = f"{config.STT_BASE_URL}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {config.STT_API_KEY}"}
        timeout = aiohttp.ClientTimeout(total=60)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(url, data=form, headers=headers) as resp:
                    body = await resp.text()
            if resp.status != 200:
                return web.json_response(
                    {"error": f"转写服务返回 {resp.status}"}, status=502
                )
            text = (json.loads(body).get("text") or "").strip()
            return web.json_response({"text": text})
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return web.json_response({"error": "转写服务连接失败"}, status=502)
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "转写返回解析失败"}, status=502)

    async def _handle_history(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        conv = request.query.get("conv", "main")
        key = config.resolve_session_key("web", conv)
        turns = session_store.load_history(key, limit=40)
        return web.json_response({"turns": turns})

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
            from ...core import worktree  # 懒加载

            key = config.resolve_session_key("web", conv)
            await worktree.remove_worktree(key)  # 先清 worktree(删库会抹掉绑定字段)
            session_store.delete_session(key)
        return web.json_response({"ok": True})

    # ── 项目 Git 状态 ────────────────────────────────────────────────────
    def _conv_cwd(self, conv: str) -> str | None:
        """会话对应的项目工作目录;非项目会话(main/普通 web)返回 None。"""
        key = config.resolve_session_key("web", conv)
        return config.project_cwd_for(key)

    async def _run_git(self, cwd: str, *args: str) -> tuple[int, str, str]:
        """在 cwd 里跑一条 git 命令,返回 (returncode, stdout, stderr)。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *args, cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
        except (OSError, ValueError) as e:
            return 127, "", str(e)
        return (
            proc.returncode or 0,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
        )

    async def _git_status(self, cwd: str) -> dict:
        """收集一份 git 状态:分支、领先/落后、改动文件清单。"""
        code, out, _ = await self._run_git(cwd, "rev-parse", "--is-inside-work-tree")
        if code != 0 or out.strip() != "true":
            return {"is_repo": False}
        _, raw, _ = await self._run_git(cwd, "status", "--porcelain=v1", "--branch")
        branch, ahead, behind = "", 0, 0
        files: list[dict] = []
        for line in raw.splitlines():
            if line.startswith("## "):
                # 形如 "main...origin/main [ahead 1, behind 2]" / "HEAD (no branch)"
                # / "No commits yet on main"
                head = line[3:]
                if head.startswith("No commits yet on "):
                    branch = head[len("No commits yet on "):].strip()
                    continue
                branch = head.split(" ", 1)[0].split("...", 1)[0]
                if m := re.search(r"ahead (\d+)", head):
                    ahead = int(m.group(1))
                if m := re.search(r"behind (\d+)", head):
                    behind = int(m.group(1))
            elif line:
                files.append({"x": line[:2], "path": line[3:]})  # XY 状态码 + 路径
        return {
            "is_repo": True,
            "branch": branch or "(游离 HEAD)",
            "ahead": ahead,
            "behind": behind,
            "dirty": len(files),
            "files": files[:60],  # 改动太多只回前 60 条,够看
        }

    async def _handle_conv_git(self, request: web.Request) -> web.Response:
        """会话对应项目的 git 状态;非项目会话回退到 serve 进程 cwd。"""
        if (g := self._guard(request)) is not None:
            return g
        key = config.resolve_session_key("web", request.query.get("conv", ""))
        cwd = config.project_cwd_for(key)
        bound = cwd is not None
        if not cwd:
            cwd = os.getcwd()
        info = await self._git_status(cwd)
        # 只有绑定项目的会话才强制显示;非项目会话仅在 cwd 真是 git 仓库时才暴露
        if not bound and not info.get("is_repo"):
            return web.json_response({"is_project": False})
        info.update(is_project=True, bound_project=bound, path=cwd, name=os.path.basename(cwd) or cwd)
        return web.json_response(info)

    async def _handle_conv_git_branch(self, request: web.Request) -> web.Response:
        """在项目工作目录建并切到新分支(当前改动随之带过去)。"""
        if (g := self._guard(request)) is not None:
            return g
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "bad json"}, status=400)
        name = (body.get("name") or "").strip()
        from ...core import worktree  # 懒加载

        # 先确保该会话有独立 worktree,再在它自己的目录里建分支 —— 只影响本会话,不动别人
        key = config.resolve_session_key("web", str(body.get("conv") or ""))
        await worktree.ensure_worktree(key)
        cwd = self._conv_cwd(str(body.get("conv") or ""))
        if not cwd:
            return web.json_response({"error": "该会话不是项目会话"}, status=400)
        if not name or " " in name or name.startswith("-"):
            return web.json_response({"error": "分支名非法"}, status=400)
        # 交给 git 兜底校验(拒绝 .. / ~ / 控制字符 / 已占用等)
        code, _, _ = await self._run_git(cwd, "check-ref-format", "--branch", name)
        if code != 0:
            return web.json_response({"error": f"分支名非法:{name}"}, status=400)
        code, _, err = await self._run_git(cwd, "checkout", "-b", name)
        if code != 0:
            return web.json_response({"error": err.strip() or "创建分支失败"}, status=400)
        info = await self._git_status(cwd)
        info.update(is_project=True, path=cwd, name=os.path.basename(cwd) or cwd)
        return web.json_response(info)

    # ── 设置:技能 / MCP ─────────────────────────────────────────────────
    async def _handle_conv_archive(self, request: web.Request) -> web.Response:
        """POST /conv/archive  {conv, archived: bool}  设置会话归档状态。"""
        if (g := self._guard(request)) is not None:
            return g
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "bad json"}, status=400)
        conv = (body.get("conv") or "").strip()
        if not conv:
            return web.json_response({"error": "缺少 conv"}, status=400)
        archived = bool(body.get("archived", True))
        session_key = config.resolve_session_key("web", conv)
        session_store.set_conv_archived(session_key, archived)
        return web.json_response({"ok": True})

    async def _handle_prefs_get(self, request: web.Request) -> web.Response:
        """GET /prefs  返回用户偏好 JSON。"""
        if (g := self._guard(request)) is not None:
            return g
        return web.json_response(session_store.get_prefs())

    async def _handle_prefs_set(self, request: web.Request) -> web.Response:
        """POST /prefs  {key: value, ...}  批量写入用户偏好。"""
        if (g := self._guard(request)) is not None:
            return g
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "bad json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be object"}, status=400)
        session_store.set_prefs(body)
        return web.json_response({"ok": True})

    async def _handle_settings(self, request: web.Request) -> web.Response:
        """设置页初始快照:技能清单 + MCP(内置 hermes + 外部)+ 记忆/AGENTS 文件列表。"""
        if (g := self._guard(request)) is not None:
            return g
        return web.json_response(
            {
                "skills": {
                    "mode": settings_store.skills_mode(),
                    "items": settings_store.list_skills(),
                },
                "mcp": {
                    "hermes_enabled": settings_store.hermes_enabled(),
                    "external": settings_store.list_external(),
                },
                "files": self._list_brain_files(),
                "brain_dir": str(config.AI_BRAIN_DIR),
            }
        )

    async def _handle_settings_skill(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "bad json"}, status=400)
        name = (body.get("name") or "").strip()
        if not name:
            return web.json_response({"error": "缺少 name"}, status=400)
        settings_store.set_skill(
            name,
            enabled=body.get("enabled") if "enabled" in body else None,
            hidden=body.get("hidden") if "hidden" in body else None,
        )
        return web.json_response({"ok": True, "mode": settings_store.skills_mode()})

    async def _handle_settings_skills_reset(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        settings_store.reset_skills()
        return web.json_response({"ok": True})

    async def _handle_settings_mcp_hermes(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "bad json"}, status=400)
        settings_store.set_hermes(bool(body.get("enabled")))
        return web.json_response({"ok": True})

    async def _handle_settings_mcp_external(self, request: web.Request) -> web.Response:
        """增删改 / 开关外部 MCP server。action: add|update|remove|toggle。"""
        if (g := self._guard(request)) is not None:
            return g
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "bad json"}, status=400)
        action = (body.get("action") or "").strip()
        name = (body.get("name") or "").strip()
        if not name:
            return web.json_response({"error": "缺少 name"}, status=400)
        if action == "remove":
            settings_store.remove_external(name)
            return web.json_response({"ok": True})
        if action == "toggle":
            settings_store.set_external_enabled(name, bool(body.get("enabled")))
            return web.json_response({"ok": True})
        # add / update:清洗成合法 SDK 配置
        cfg, err = self._clean_mcp_config(body)
        if err:
            return web.json_response({"error": err}, status=400)
        settings_store.upsert_external(name, cfg)
        return web.json_response({"ok": True})

    @staticmethod
    def _clean_mcp_config(body: dict) -> tuple[dict, str | None]:
        """把前端提交的字段清洗成 stdio/sse/http 配置;返回 (cfg, 错误)。"""
        typ = (body.get("type") or "stdio").strip().lower()
        enabled = bool(body.get("enabled", True))
        if typ == "stdio":
            command = (body.get("command") or "").strip()
            if not command:
                return {}, "stdio 类型需要 command"
            raw_args = body.get("args")
            if isinstance(raw_args, str):
                args = raw_args.split()
            elif isinstance(raw_args, list):
                args = [str(a) for a in raw_args]
            else:
                args = []
            env = body.get("env") if isinstance(body.get("env"), dict) else {}
            env = {str(k): str(v) for k, v in env.items()}
            return (
                {"type": "stdio", "command": command, "args": args,
                 "env": env, "enabled": enabled},
                None,
            )
        if typ in ("sse", "http"):
            url = (body.get("url") or "").strip()
            if not url:
                return {}, f"{typ} 类型需要 url"
            headers = body.get("headers") if isinstance(body.get("headers"), dict) else {}
            headers = {str(k): str(v) for k, v in headers.items()}
            return (
                {"type": typ, "url": url, "headers": headers, "enabled": enabled},
                None,
            )
        return {}, f"不支持的类型:{typ}"

    # ── 设置:记忆 / AGENTS.md 文件读写(限定在 AI_BRAIN 内)──────────────
    def _list_brain_files(self) -> list[dict]:
        """列出可编辑的长期记忆 / 人设文件(全部在 AI_BRAIN 下)。"""
        root = config.AI_BRAIN_DIR
        out: list[dict] = []
        # 固定文件:AGENTS.md(人设) + MEMORY.md(索引) + USER.md(画像)
        out.append({"rel": "AGENTS.md", "group": "agents"})
        out.append({"rel": "MEMORY.md", "group": "memory"})
        out.append({"rel": "USER.md", "group": "memory"})
        mem_dir = root / "memory"
        try:
            for p in sorted(mem_dir.glob("*.md"), key=lambda x: x.name.lower()):
                out.append({"rel": f"memory/{p.name}", "group": "memory"})
        except OSError:
            pass
        for f in out:
            f["exists"] = (root / f["rel"]).is_file()
        return out

    def _safe_brain_path(self, rel: str) -> Path | None:
        """把前端传的相对路径解析到 AI_BRAIN 内,越界 / 非 .md 一律拒绝。"""
        rel = (rel or "").strip().lstrip("/")
        if not rel or not rel.endswith(".md"):
            return None
        root = config.AI_BRAIN_DIR.resolve()
        try:
            target = (root / rel).resolve()
        except (OSError, ValueError):
            return None
        if target != root and root not in target.parents:
            return None  # 目录穿越,拒
        return target

    async def _handle_file_read(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        target = self._safe_brain_path(request.query.get("rel", ""))
        if target is None:
            return web.json_response({"error": "非法路径"}, status=400)
        try:
            content = target.read_text(encoding="utf-8") if target.is_file() else ""
        except OSError:
            return web.json_response({"error": "读取失败"}, status=500)
        return web.json_response({"rel": request.query.get("rel", ""), "content": content})

    async def _handle_file_save(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "bad json"}, status=400)
        rel = (body.get("rel") or "").strip()
        target = self._safe_brain_path(rel)
        if target is None:
            return web.json_response({"error": "非法路径"}, status=400)
        # 只允许改已存在文件,或在 memory/ 下新建;别的地方不给凭空造文件
        if not target.is_file() and not rel.lstrip("/").startswith("memory/"):
            return web.json_response({"error": "只能新建 memory/ 下的文件"}, status=400)
        content = body.get("content")
        if not isinstance(content, str):
            return web.json_response({"error": "content 必须是字符串"}, status=400)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError:
            return web.json_response({"error": "写入失败"}, status=500)
        return web.json_response({"ok": True})

    # ── PWA 静态资源 + 推送订阅 ──────────────────────────────────────────
    def _static_file(
        self, name: str, content_type: str, extra_headers: dict | None = None
    ) -> web.Response:
        """读 web_static 下的文件返回;缺失给 404。这些是公开资源(浏览器不带 token 取)。"""
        try:
            data = (_STATIC / name).read_bytes()
        except OSError:
            return web.Response(status=404, text="not found")
        headers = {"Cache-Control": "no-cache"}
        if extra_headers:
            headers.update(extra_headers)
        return web.Response(body=data, content_type=content_type, headers=headers)

    async def _handle_manifest(self, request: web.Request) -> web.Response:
        return self._static_file("manifest.json", "application/manifest+json")

    async def _handle_sw(self, request: web.Request) -> web.Response:
        # Service-Worker-Allowed: / 让 sw 能控整站;no-cache 保证改动能刷新
        return self._static_file(
            "sw.js", "text/javascript", {"Service-Worker-Allowed": "/"}
        )

    # 只放行这几个图标名,防目录穿越
    _ICONS = {"icon-192", "icon-512", "icon-maskable-512", "apple-touch-icon"}

    async def _handle_icon(self, request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        if name not in self._ICONS:
            return web.Response(status=404, text="not found")
        return self._static_file(
            f"{name}.png", "image/png", {"Cache-Control": "public, max-age=86400"}
        )

    async def _handle_push_config(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        return web.json_response(PUSH.public_config())

    async def _handle_push_subscribe(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        try:
            sub = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "bad json"}, status=400)
        ok = PUSH.add(sub if isinstance(sub, dict) else {})
        if not ok:
            return web.json_response({"error": "invalid subscription"}, status=400)
        return web.json_response({"ok": True})

    async def _handle_push_unsubscribe(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        PUSH.remove((body or {}).get("endpoint", ""))
        return web.json_response({"ok": True})

    async def _start_server(self) -> None:
        app = web.Application(client_max_size=32 * 1024 * 1024)  # 允许 32MB 图片上传
        app.add_routes(
            [
                web.get("/", self._handle_index),
                web.get("/manifest.json", self._handle_manifest),
                web.get("/sw.js", self._handle_sw),
                web.get(r"/{name}.png", self._handle_icon),
                web.get("/push/config", self._handle_push_config),
                web.post("/push/subscribe", self._handle_push_subscribe),
                web.post("/push/unsubscribe", self._handle_push_unsubscribe),
                web.get("/events", self._handle_events),
                web.post("/send", self._handle_send),
                web.post("/abort", self._handle_abort),
                web.get("/conversations", self._handle_conversations),
                web.get("/projects", self._handle_projects),
                web.get("/browse", self._handle_browse),
                web.post("/projects/create", self._handle_project_create),
                web.post("/projects/remove", self._handle_project_remove),
                web.get("/models", self._handle_models),
                web.get("/history", self._handle_history),
                web.post("/transcribe", self._handle_transcribe),
                web.post("/conv/rename", self._handle_rename),
                web.post("/conv/delete", self._handle_delete),
                web.post("/conv/archive", self._handle_conv_archive),
                web.get("/conv/git", self._handle_conv_git),
                web.post("/conv/git/branch", self._handle_conv_git_branch),
                # 用户偏好
                web.get("/prefs", self._handle_prefs_get),
                web.post("/prefs", self._handle_prefs_set),
                # 设置页
                web.get("/settings", self._handle_settings),
                web.post("/settings/skill", self._handle_settings_skill),
                web.post("/settings/skills/reset", self._handle_settings_skills_reset),
                web.post("/settings/mcp/hermes", self._handle_settings_mcp_hermes),
                web.post("/settings/mcp/external", self._handle_settings_mcp_external),
                web.get("/file/read", self._handle_file_read),
                web.post("/file/save", self._handle_file_save),
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
