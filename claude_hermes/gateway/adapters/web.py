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
import hashlib
import hmac
import json
import mimetypes
import os
import re
import time
import uuid
from collections import deque
from pathlib import Path
from typing import AsyncIterator, Callable

from aiohttp import web

from ... import config, providers
from ...core import title
from ...core.agent import AgentReply, get_rate_limits
from ...memory import session_store
from .. import git_status, settings_store
from ..core import COMMAND_LIST, MODEL_CHOICES, Choice, Sink
from .base import ImageAttachment, Incoming
from .usage_local import get_local_claude_usage
from .web_push import PUSH

_STATIC = Path(__file__).resolve().parent / "web_static"
_DOC_PREVIEW_MAX = 3 * 1024 * 1024  # 文档预览分屏读文件上限;超过就不读,前端提示下载/自己开
# 文档预览模糊兜底搜索用:直接拼接找不到时,按路径尾部扫一遍——AI 提到文件时经常掉了包名
# 前缀(比如把 claude_hermes/memory/images.py 说成 memory/images.py)。跳过这些目录纯粹是
# 图快、避免误判(里面几乎不会是用户真正想看的文档),不是安全边界(边界仍是越界即拒)。
_DOC_SEARCH_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", ".cache", "target", ".pytest_cache", ".mypy_cache", ".obsidian",
}
_DOC_SEARCH_MAX_SCAN = 20000  # 扫描文件数上限,避免大仓库/大 vault 卡住请求

# 纵深防御(审计 web#5/#9 · 2-9)。CSP 里 script/style 仍留 'unsafe-inline'——前端是单文件
# 内联脚本,去掉会整页崩;但收紧 connect/img、禁 frame 嵌入/base/object/表单外发,给「未来
# 新增 innerHTML 分支忘了转义」兜底。frame-ancestors 'none' 顺带防点击劫持。
# frame-src/img-src 额外放 blob:/https: 是给文档预览分屏用(openDocPreview)——本地文件
# 走 /doc/preview 读成 blob 再塞 iframe/img,外部文档链接(比如已发布页面)直接 iframe 真
# URL。这条不影响 frame-ancestors 'none':那个管"别人能不能嵌我们",这个管"我们能不能嵌别人"。
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; "
    "frame-src 'self' blob: https:; "
    "connect-src 'self'; "
    "base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
)

# 发布页(/pub/*)沙箱化 CSP:在站点默认策略上加 `sandbox`——强制把响应文档放进「不透明源」,
# 读不到聊天主站的 localStorage/Cookie(哪怕发布的页面被投毒也偷不走 X-Auth-Token),状态
# 变更请求 fetch 出来的 Origin 也会变成 "null",照样撞上 `_security_mw` 的同源校验被拦。
# allow-scripts/allow-popups 留给自包含 demo 页正常跑 JS、点外链跳转。
# frame-ancestors 从 'none' 改成 'self':文档预览分屏要把 /pub/ 页面直接塞进本站的 iframe
# 里(见 openDocPreview),继承 _CSP 原样的 'none' 会连自己都嵌不进去,导致预览面板一直空白
# (2026-07-30 踩过)。放宽到 'self' 只是"允许本站嵌自己发布的页面",不影响"防第三方站点
# 盗嵌"这条防线;真正防"页面被投毒偷 token"的是前面那句 sandbox 没给 allow-same-origin,
# 跟 frame-ancestors 无关,放宽这条不削弱那道防护。
_PUBLISH_CSP = (
    "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; "
    + _CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
)


def _same_origin(origin: str, host: str) -> bool:
    """Origin 的 host:port 是否与请求的 Host 一致(同源)。用于挡跨站写 / DNS rebinding。"""
    from urllib.parse import urlsplit

    return bool(host) and urlsplit(origin).netloc.lower() == host.strip().lower()


@web.middleware
async def _security_mw(request: web.Request, handler):
    """① 状态变更请求带跨源 Origin → 拒绝(挡 DNS rebinding / 跨站 POST);
    ② 给非流式响应补 CSP / nosniff / DENY 等安全头。"""
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("Origin")
        if origin and not _same_origin(origin, request.headers.get("Host", "")):
            return web.json_response({"error": "cross-origin forbidden"}, status=403)
    resp = await handler(request)
    # SSE 等已 prepare 的流式响应不能再改头,跳过
    if not getattr(resp, "prepared", False):
        resp.headers.setdefault("Content-Security-Policy", _CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


class _WebSink(Sink):
    """把一轮事件流以 SSE 增量推给浏览器(按 conv 打标)。

    像 Telegram 一样发【全文快照】而非增量:每个 thinking/text 事件都带当前累积
    的完整内容。这样断线补发时任何一帧都能把画面修正确,丢几帧也不会缺字。

    正文按【段】发:顶层工具一启动就切新段(seg 自增),text 帧带 seg 序号和
    该段的全文快照。前端按段建独立文字块,与工具卡按到达顺序交错排列 ——
    这才是 Claude Code 那种"说一句、跑个工具、接着说"的画面。
    """

    def __init__(self, adapter: "WebAdapter", conv: str):
        super().__init__()
        self.a = adapter
        self.conv = conv
        self._seg = 0  # 当前文字段序号
        self._text = ""  # 当前段累积正文,每次发该段全文
        self._think = ""  # 累积思考
        # SSE 限流:150ms 内 text/thinking 合并为一次发出,大幅减少跨境小包数
        self._last_think_ts = 0.0
        self._last_text_ts: dict[int, float] = {}
        self.a._emit({"conv": conv, "type": "start"})

    async def thinking(self, text: str) -> None:
        self._think += text
        now = time.time()
        if now - self._last_think_ts < 0.15:
            return
        self._last_think_ts = now
        self.a._emit({"conv": self.conv, "type": "thinking", "text": self._think})

    async def text(self, text: str) -> None:
        self._text += text
        now = time.time()
        last = self._last_text_ts.get(self._seg, 0.0)
        if now - last < 0.15:
            return
        self._last_text_ts[self._seg] = now
        self.a._emit(
            {"conv": self.conv, "type": "text", "seg": self._seg, "text": self._text}
        )

    async def tool_started(
        self, name: str, tool_id: str = "", parent_id: str | None = None
    ) -> None:
        # 顶层工具启动且当前段已有文字 → 切段,之后的正文进新文字块(交错排布)
        if not parent_id and self._text:
            self._seg += 1
            self._text = ""
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

    async def compacted(self, trigger: str = "") -> None:
        # CLI 自动压缩了上下文:压缩点之后正文进新段(旧段已定格),前端插系统条
        if self._text:
            self._seg += 1
            self._text = ""
        self.a._emit({"conv": self.conv, "type": "compact", "trigger": trigger})

    async def cancelled(self) -> None:
        # 用户手动取消:清理「进行中」快照,避免刷新页面后重放旧内容。
        self.a._live.pop(self.conv, None)
        self.a._emit({"conv": self.conv, "type": "cancelled"})

    async def done(self, reply: AgentReply) -> None:
        # 发 done 前先输出最终全文快照,补全限流可能漏掉的尾巴,确保重连缓冲里有完整版
        if self._think:
            self.a._emit({"conv": self.conv, "type": "thinking", "text": self._think})
        if self._text:
            self.a._emit({"conv": self.conv, "type": "text", "seg": self._seg, "text": self._text})
        # converse() 已在此之前写好 token 计量,读回最新明细推给前端(实时刷新顶栏)
        key = config.resolve_session_key("web", self.conv)
        meta = session_store.session_summary(key)
        self.a._emit(
            {
                "conv": self.conv,
                "type": "done",
                "text": reply.text or ("⚠️ 出了点问题,请重试" if reply.is_error else "(空回复)"),
                "is_error": reply.is_error,
                "error": reply.error or "",
                "api_error_status": reply.api_error_status,
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
            session_store.get_title(key) or "Wazir"
        )
        self.a._push_notify(
            title=title,
            body=reply.text or "回复完成",
            conv=self.conv,
            kind="done",
            enabled=config.PUSH_ON_DONE,
        )
        # 该会话已有新完成内容,标记为待查看(用户打开后会清零)。
        session_store.set_pending_review(key, True)


def _conv_id_for_key(key: str) -> str:
    """session_key → 前端认的 conv id(前端只认 conv,不认完整 session_key)。

    规则统一收口在这一处:主会话(config.SESSION_KEY,与 TG/CLI 共享)固定叫
    "main";web: 前缀剥掉前缀;其余(后台任务 task:xxx 等非项目 key)原样用完整
    key——resolve_session_key 的透传分支要吃完整字符串。这条规则以前在
    _handle_conversations/_handle_conv_search/_handle_voice_sidebar 三处分别
    手写,新增一种前缀时容易漏改一处(2026-07-29 复盘)。
    """
    if key == config.SESSION_KEY:
        return "main"
    if key.startswith("web:"):
        return key[len("web:"):]
    return key


def _rows_with_conv(prefix: str) -> list[dict]:
    """session_store.list_sessions(prefix) 的结果统一装上 conv 字段。

    普通会话/语音任务两个侧栏分组都从这里取数据——它们和 pending_review/
    last_error/archived 这套"完成态信号"字段全部来自同一个 list_sessions()
    调用,以后这套信号加新字段会自动对两个分组同时生效,不会有第三处忘了接
    (cron-task 分组数据源是 scheduler.load_jobs() 而非 list_sessions() 直接
    返回的行,结构不同,不套用本函数,但仍可用 list_sessions() 查同一份字段,
    见 _handle_cron_sidebar 的 _pending_map 用法)。
    """
    rows = session_store.list_sessions(prefix)
    for r in rows:
        r["conv"] = _conv_id_for_key(r["key"])
    return rows


def _pending_map(prefix: str) -> dict[str, bool]:
    """key → pending_review 的映射,给数据源不是 list_sessions() 直接返回行的
    场景(如 cron-task,行来自 scheduler.load_jobs())按 key 单独查这个字段用。
    """
    return {r["key"]: r.get("pending_review", False) for r in session_store.list_sessions(prefix)}


class WebAdapter:
    platform = "web"

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[Incoming] = asyncio.Queue()
        # 每个浏览器 SSE 连接一个队列,元素是 (事件编号, JSON 串)
        self._clients: set[asyncio.Queue[tuple[int, str]]] = set()
        self._seq = 0  # 全局单调递增的事件编号
        self._buffer: deque[tuple[int, str]] = deque(maxlen=2000)  # 断线补发用的环形缓冲
        # 每会话「进行中那一轮」的活状态快照:conv -> {started, phase, frames:[(seq,payload)]}。
        # 刷新/首连时据此「状态先行、内容随后」地恢复——先秒推一条状态帧让用户知道
        # 「这轮还在跑、到哪一步」(避免空窗误发),再慢慢补回思考/正文/工具帧。
        self._live: dict[str, dict] = {}
        self._runner: web.AppRunner | None = None
        self._cancel_callback: Callable[[str], bool] | None = None
        self._model_switch_callback: Callable[[str, str], None] | None = None
        # 进程本次启动的标识:重启后这个值必变。前端拿它跟上次记住的值比对——
        # 一旦不一样就说明断线期间进程重启过,上面的环形缓冲/_live 全被清空了,
        # 断线补发这条路救不回来,前端得主动整体核对一次(侧栏 + 当前会话历史)。
        self._boot_id = f"{int(time.time() * 1000)}-{os.getpid()}"
        # 注册后台任务桥接:task: 状态变化经主 SSE 推给前端,让侧栏小红点闪烁。
        # 懒加载避免非语音场景循环依赖;bridge 本身只要 _emit,不依赖 voice 包的其他模块。
        from ...voice import notify as _voice_notify
        _voice_notify.register_main_event_bridge(self._emit)
        # 注册跨入口事件桥接:Telegram 那边发生的会话更新(自己不经过 _emit)靠这条
        # 路推进来,见 gateway/event_bridge.py。
        from .. import event_bridge
        event_bridge.register(self._emit)

    def set_cancel_callback(self, cb: Callable[[str], bool]) -> None:
        self._cancel_callback = cb

    def set_model_switch_callback(self, cb: Callable[[str, str], None]) -> None:
        """注册模型切换回调:UI 切模型时同步更新 GatewayRunner 的内存缓存。"""
        self._model_switch_callback = cb

    # ── 认证 ────────────────────────────────────────────────────────────
    def _ok_token(self, request: web.Request) -> bool:
        if not config.WEB_AUTH_TOKEN:
            return True  # 未设口令 = 不校验(仅本机调试;非本机绑定时启动已 fail-closed)
        # 只认请求头,不再收 ?token= query:query 会进 cloudflared 访问日志 / 浏览器历史 /
        # Referer,一旦泄露等于交出控制权。前端(含 SSE 的 FetchSSE)本就走 X-Auth-Token 头。
        # 用 hmac.compare_digest 常量时间比较,堵住按字节时序爆破口令(审计 #3 / 2-3)。
        tok = request.headers.get("X-Auth-Token") or ""
        return hmac.compare_digest(tok, config.WEB_AUTH_TOKEN)

    def _guard(self, request: web.Request) -> web.Response | None:
        if not self._ok_token(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return None

    @staticmethod
    async def _read_json(request: web.Request) -> tuple[dict, web.Response | None]:
        """解析 POST body 成 dict;跟 _guard 同一个用法 —— 非 None 就直接 return。

        以前这四行 try/except 在本文件里原样重复了近 20 次,收口成这一个 helper。
        """
        try:
            return await request.json(), None
        except (json.JSONDecodeError, ValueError):
            return {}, web.json_response({"error": "bad json"}, status=400)

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
        # send_image 工具中途发的图(mid_turn=True):这一轮还没结束,只按普通帧追加,
        # 不能当"message"收轮——否则重连的客户端拿不到这条之后还会继续的正文/工具帧。
        if t == "message" and payload.get("mid_turn"):
            st = self._live.get(conv)
            if st is not None:
                st["frames"].append((seq, payload))
            return
        if t in ("done", "message", "cancelled"):
            self._live.pop(conv, None)  # 一轮结束(choice 是审批暂停,不收轮)
            return
        st = self._live.get(conv)
        if st is None:
            return  # 没有进行中的回合,零散帧忽略
        if t in ("thinking", "text"):
            st["phase"] = "回复中" if t == "text" else "思考中"
            # text 是分段全文快照:同段只留最新一帧(不同段各留各的,保住交错顺序);
            # thinking 全局只留最新一帧。
            seg = payload.get("seg")
            st["frames"] = [
                (s, p) for s, p in st["frames"]
                if p.get("type") != t or (t == "text" and p.get("seg") != seg)
            ]
            st["frames"].append((seq, payload))
        elif t in ("tool_start", "tool_input", "tool_end"):
            st["phase"] = f"执行 {payload.get('name') or '工具'}"
            st["frames"].append((seq, payload))
        elif t == "compact":  # 压缩标记按序保留,刷新重放时系统条不丢
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
        # 该会话收到一条完整消息,标记为待查看。
        session_store.set_pending_review(config.resolve_session_key("web", str(chat_id)), True)
        # 场景③「主动/cron」与 场景④「出错」共用这条出口,靠 ⚠️ 前缀区分
        is_err = text.lstrip().startswith("⚠️")
        self._push_notify(
            title="⚠️ 出错了" if is_err else "Wazir",
            body=text,
            conv=str(chat_id),
            kind="error" if is_err else "proactive",
            enabled=config.PUSH_ON_ERROR if is_err else config.PUSH_ON_PROACTIVE,
        )

    async def send_image(self, chat_id: int | str, src_path: Path, caption: str = "") -> str | None:
        """把本地图片文件复制进 IMAGES_DIR 并作为一条 assistant 气泡推给前端;返回错误信息(None=成功)。

        供 send_image 工具用 —— 模型生图/截图后主动把本地文件发出去,复用 send() 的
        已读标记/推送逻辑,只是多带一份 images 字段。带 mid_turn=True 标记:这是本轮
        回复过程中途发的,不代表整轮结束,前端据此只把图片挂进当前流式气泡,不会当
        "回合已完成"提前收尾(否则本轮后续还要继续输出的正文会被拆成第二个气泡)。
        """
        if not src_path.is_file():
            return f"文件不存在:{src_path}"
        ext = src_path.suffix.lower().lstrip(".")
        if ext not in {"png", "jpg", "jpeg", "gif", "webp"}:
            return f"不支持的图片格式:{ext or '(无后缀)'}"
        config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        # "ai_"前缀是 session_store.load_history 从 turns.images 里拆分"AI发的图"
        # 与"用户上传图"的唯一依据(见 memory/images.py 的 AI_IMAGE_PREFIX),不能改名。
        name = f"{session_store.AI_IMAGE_PREFIX}{uuid.uuid4().hex}.{ext}"
        (config.IMAGES_DIR / name).write_bytes(src_path.read_bytes())
        self._emit({
            "conv": str(chat_id), "type": "message", "text": caption,
            "images": [f"/image?name={name}"], "mid_turn": True,
        })
        session_key = config.resolve_session_key("web", str(chat_id))
        # 落库进当前轮次:只推 SSE 不落库的话,断线重连/刷新页面后这张图会永久消失
        # (历史只认 turns.images 这一列),但工具调用卡片仍显示"已发送"造成错觉。
        session_store.append_turn_image(session_key, name)
        session_store.set_pending_review(session_key, True)
        self._push_notify(
            title="Wazir", body=caption or "[图片]", conv=str(chat_id),
            kind="proactive", enabled=config.PUSH_ON_PROACTIVE,
        )
        return None

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
    @staticmethod
    def _inject_asset_versions(html_bytes: bytes) -> bytes:
        """把 HTML 里的 /styles.css、/tool-card.js 引用改写成带内容哈希的 ?v= URL。

        URL 即版本:文件一变哈希就变,配合 _versioned_static 的 immutable 头,
        CF 边缘/浏览器长缓存直接命中;文件没变则 URL 稳定,缓存持续有效。
        文件读不到就原样返回(引用裸路径也能工作,只是回到协商缓存)。
        """
        for name in ("styles.css", "tool-card.js"):
            try:
                digest = hashlib.md5((_STATIC / name).read_bytes()).hexdigest()[:12]
            except OSError:
                continue
            html_bytes = html_bytes.replace(
                f'"/{name}"'.encode(), f'"/{name}?v={digest}"'.encode()
            )
        return html_bytes

    async def _handle_index(self, request: web.Request) -> web.Response:
        # 每次请求实时读盘:改了 UI 刷新浏览器即可,不用重启 serve。no-cache 的语义是
        # "每次用之前先跟服务器核对",不是"不许缓存"——配合 ETag,内容没变就回 304
        # 空包(2026-07-10:手机杀掉 PWA 重开,254KB 的 index.html 每次全量重传,走
        # 隧道+跨境链路首屏能拖好几秒;改动后没变=304 秒开,变了=照常拿到新页面)。
        try:
            html_bytes = self._inject_asset_versions((_STATIC / "index.html").read_bytes())
        except OSError:
            html_bytes = "<h1>index.html 缺失</h1>".encode("utf-8")
        etag = f'"{hashlib.md5(html_bytes).hexdigest()}"'
        if request.headers.get("If-None-Match") == etag:
            return web.Response(
                status=304, headers={"Cache-Control": "no-cache", "ETag": etag}
            )
        resp = web.Response(
            body=html_bytes,
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-cache", "ETag": etag},
        )
        resp.enable_compression()  # 起源端 gzip:250KB 文本压到几十 KB,弱网首屏立省
        return resp

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
            # 不带 SSE id(不占游标、不参与去重):告诉前端这次连的是哪个进程实例,
            # 前端跟自己记的上一个值一比对,变了就知道服务端重启过、缓冲补不全了。
            hello = json.dumps({"type": "hello", "boot_id": self._boot_id}, ensure_ascii=False)
            await resp.write(f"data: {hello}\n\n".encode("utf-8"))
            # 首连/重连都两阶段恢复「进行中那一轮」——先秒推状态帧(几十字节,让用户立刻看到
            # "还在跑、到哪步",不会误发),内容帧随后慢补。旧历史仍走 /history 拉,不在此重放。
            # 重连也要走这条路:下面的环形缓冲只有 512 条,长时间断线或多会话同时飙事件很容易
            # 把它撑爆,导致某个后台会话的 done 被挤出缓冲、前端永远等不到收尾(卡死在"思考中")。
            # `_live` 按会话只留最新一帧,不受缓冲大小影响,补发它才能保证重连必定能追平现状。
            if self._live:
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
        body, err = await self._read_json(request)
        if err is not None:
            return err
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
        await self._ingest(conv, text, images=images)
        return web.json_response({"ok": True})

    async def _ingest(
        self, conv: str, text: str, images: list[ImageAttachment] | None = None
    ) -> None:
        """把一条消息塞进指定会话的处理流水线——浏览器发送(_handle_send)和外部注入
        (语音跨端续聊,见 inject()/gateway/web_bridge.py)共用同一份逻辑,保证标题
        占位/项目 touch/用户气泡广播/入队 dispatch 完全一致的行为,不会两边走岔。"""
        images = images or []
        # 首条消息自动给会话起个名(命令 / 主会话除外):先落一个截断兜底标题,
        # 同时立刻异步起模型总结(不等 AI 首轮回复——那可能跑很久,侧边栏不能干等)
        if not text.startswith("/") and conv != "main":
            session_key = config.resolve_session_key("web", conv)
            placeholder = session_store.ensure_title(session_key, text)
            if placeholder:
                asyncio.create_task(
                    self._summarize_title(conv, session_key, text, placeholder)
                )
        # 项目会话(conv = p<hash>:<convid>)→ 刷新项目最近使用时间,好让侧边栏排序
        if conv.startswith("p") and ":" in conv:
            session_store.touch_project(conv[1:].split(":", 1)[0])
        # 广播用户消息让其他客户端(如桌面端)能实时渲染用户气泡
        if not text.startswith("/"):
            self._emit({"conv": conv, "type": "user", "text": text})
        self._inbox.put_nowait(Incoming(self.platform, conv, text, images=images))

    async def inject(self, conv: str, text: str) -> None:
        """语音跨端续聊的公开入口:把一句话当成这个网页会话的下一轮发送——
        跟用户自己在浏览器里发消息完全等价(见 gateway/web_bridge.py)。"""
        await self._ingest(conv, text)

    async def _handle_model_switch(self, request: web.Request) -> web.Response:
        """静默切模型：不走命令管道、不触发任何 SSE/推送通知。直接落库。"""
        if (g := self._guard(request)) is not None:
            return g
        body, err = await self._read_json(request)
        if err is not None:
            return err
        conv = str(body.get("conv") or "main")
        model = (body.get("model") or "").strip()
        if not model:
            return web.json_response({"error": "empty model"}, status=400)
        session_key = config.resolve_session_key("web", conv)
        session_store.backfill_chosen_models()  # 冻结老会话
        session_store.set_chosen_model(session_key, model)
        settings_store.set_web_default_model(model)
        # 同步更新 GatewayRunner 的内存缓存,否则下一条消息还会用旧模型
        if self._model_switch_callback:
            self._model_switch_callback(session_key, model)
        return web.json_response({"ok": True})

    async def _summarize_title(
        self, conv: str, session_key: str, text: str, placeholder: str
    ) -> None:
        """首条消息发出后异步总结标题(Haiku 订阅优先,DeepSeek 兜底,见 core/title)。

        完成后仅当标题仍是截断兜底时才覆盖——期间用户手动改过名就尊重用户;
        失败静默放弃,保留兜底标题,不打扰。
        """
        try:
            new_title = await title.summarize(text)
        except Exception:
            return
        if not new_title or session_store.get_title(session_key) != placeholder:
            return
        session_store.set_title(session_key, new_title)
        # 广播让各客户端刷新侧边栏/标题栏(前端收到 type=title 只做 loadConvs)
        self._emit({"conv": conv, "type": "title", "title": new_title})

    async def _handle_abort(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        body, err = await self._read_json(request)
        if err is not None:
            return err
        conv = str(body.get("conv") or "main")
        session_key = config.resolve_session_key("web", conv)
        stopped = bool(self._cancel_callback and self._cancel_callback(session_key))
        return web.json_response({"ok": True, "stopped": stopped})

    async def _handle_conversations(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        convs = _rows_with_conv("web:")
        # 主会话(与 TG/CLI 共享的统一会话)固定置顶,conv id 用 "main"
        main = session_store.session_summary(config.resolve_session_key("web", "main"))
        main.update(key="main", title="主会话", pinned=True, conv="main")
        return web.json_response({"main": main, "conversations": convs})

    async def _handle_conv_search(self, request: web.Request) -> web.Response:
        """侧边栏全局搜索(⌘F):标题优先、正文其次,含归档会话;被删除的
        会话已物理删除,天然不在结果里。"""
        if (g := self._guard(request)) is not None:
            return g
        q = (request.query.get("q") or "").strip()
        if not q:
            return web.json_response({"results": []})
        from ...core import tasks as bg_tasks  # 懒加载,同 _handle_voice_sidebar

        items = session_store.search_sessions(q, limit=50)
        for it in items:
            key = it["key"]
            it["conv"] = _conv_id_for_key(key)
            if key == config.SESSION_KEY:
                it["title"] = "主会话"
            task_id = bg_tasks.task_id_from_session_key(key)
            if task_id is not None:
                row = bg_tasks.get(task_id)
                if row is not None:
                    it["title"] = row["title"]
        return web.json_response({"results": items})

    async def _handle_voice_sidebar(self, request: web.Request) -> web.Response:
        """侧边栏"语音任务"固定分组:主语音会话 + 语音派发的各后台任务会话。

        跟 _handle_conversations 一个模板,只是数据源换成 voice-chat:/task: 前缀
        （见 03-phase2-实现记录.md 存储统一改动一节)。任务行的 conv 字段用完整 key
        (不剥前缀)——resolve_session_key 的透传分支要吃完整字符串。

        2026-07-29 统一后 task: 前缀不再是语音专属(cron/chat 触发的任务也落在这个
        前缀下),这里要按 origin="voice" 过滤,不然定时任务/网页发起的任务会混进
        "语音任务"分组。任务元数据(voice.db)行万一缺失(row is None,见
        test_voice_sidebar_task_row_survives_missing_task_row)时保留展示——判不出
        origin,历史上这种情况本就只可能是语音任务的残留数据,不因为判不出就隐藏。
        """
        if (g := self._guard(request)) is not None:
            return g
        from ...core import tasks as bg_tasks  # 懒加载,避免非任务场景也引入这块

        main = session_store.session_summary("voice-chat:main")
        main.update(key="voice-chat:main", conv="voice-chat:main", title="语音通话", pinned=True)
        task_convs = []
        for c in _rows_with_conv(bg_tasks.SESSION_KEY_PREFIX):
            task_id = bg_tasks.task_id_from_session_key(c["key"]) or c["key"]
            row = bg_tasks.get(task_id)
            if row is not None:
                if row.get("origin", "voice") != "voice":
                    continue
                c["task_status"] = row["status"]
                c["title"] = row["title"]
            task_convs.append(c)
        return web.json_response({"main": main, "tasks": task_convs})

    # ── 定时任务 ─────────────────────────────────────────────────────────
    async def _handle_cron_sidebar(self, request: web.Request) -> web.Response:
        """侧边栏"定时任务"分组:每个任务一条专属会话(task:<job_id>,job_id 本身
        就复用作统一后台任务引擎的 task_id,见 core/tasks.py 的 origin 字段说明),
        跟"语音任务"同一个模板。只在有任务时前端才渲染这个分组。"""
        if (g := self._guard(request)) is not None:
            return g
        from ...cron import scheduler

        jobs = scheduler.load_jobs()
        # session_summary() 不带 pending_review(那是 list_sessions 的字段),这里单独查一遍
        # 补上,否则任务跑完了侧边栏也不会冒未读灰点(见 _pending_map 的说明)。task: 前缀
        # 现在也装着语音/chat 触发的任务,但下面只按 jobs 自己的 conv 精确取值,不会串。
        pending_by_conv = _pending_map("task:")
        rows = []
        for j in jobs:
            conv = j.get("conv") or f"task:{j['id']}"
            row = session_store.session_summary(conv)
            row.update(
                conv=conv,
                job_id=j["id"],
                title=j.get("name") or "定时任务",
                schedule_desc=scheduler.describe_schedule(j.get("schedule", {})),
                enabled=bool(j.get("enabled")),
                pending_review=pending_by_conv.get(conv, False),
                last_status=j.get("last_status"),
                # 下面三个字段是给管理界面「编辑」表单回填用的原始数据
                prompt=j.get("prompt"),
                schedule=j.get("schedule"),
                target=j.get("target"),
            )
            rows.append(row)
        return web.json_response({"jobs": rows})

    async def _handle_cron_create(self, request: web.Request) -> web.Response:
        """管理界面直接新建定时任务(不经过建议/审批——用户在管理界面上的操作本身
        就是明确的第一方意图)。"""
        if (g := self._guard(request)) is not None:
            return g
        from ...cron import scheduler

        body, err = await self._read_json(request)
        if err is not None:
            return err
        name = (body.get("name") or "").strip()
        prompt = (body.get("prompt") or "").strip()
        schedule = body.get("schedule")
        if not name or not prompt:
            return web.json_response({"error": "name / prompt 不能为空"}, status=400)
        err = scheduler.validate_schedule(schedule or {})
        if err:
            return web.json_response({"error": err}, status=400)
        target = body.get("target") or None
        if target is not None and not (target.get("platform") and target.get("chat_id") is not None):
            target = None
        job = scheduler.create_job(
            name=name, prompt=prompt, schedule=schedule, target=target
        )
        return web.json_response({"job": job})

    async def _handle_cron_update(self, request: web.Request) -> web.Response:
        """管理界面编辑已有定时任务(名称/指令/调度/推送目标),同样不经过审批。"""
        if (g := self._guard(request)) is not None:
            return g
        from ...cron import scheduler

        body, err = await self._read_json(request)
        if err is not None:
            return err
        job_id = (body.get("id") or "").strip()
        name = (body.get("name") or "").strip()
        prompt = (body.get("prompt") or "").strip()
        schedule = body.get("schedule")
        if not job_id or not name or not prompt:
            return web.json_response({"error": "name / prompt 不能为空"}, status=400)
        err = scheduler.validate_schedule(schedule or {})
        if err:
            return web.json_response({"error": err}, status=400)
        target = body.get("target") or None
        if target is not None and not (target.get("platform") and target.get("chat_id") is not None):
            target = None
        job = scheduler.update_job(
            job_id, name=name, prompt=prompt, schedule=schedule, target=target
        )
        if job is None:
            return web.json_response({"error": "任务不存在"}, status=404)
        return web.json_response({"job": job})

    async def _handle_cron_set_enabled(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        from ...cron import scheduler

        body, err = await self._read_json(request)
        if err is not None:
            return err
        job_id = (body.get("id") or "").strip()
        enabled = bool(body.get("enabled"))
        jobs = scheduler.load_jobs()
        job = next((j for j in jobs if j.get("id") == job_id), None)
        if job is None:
            return web.json_response({"error": "任务不存在"}, status=404)
        job["enabled"] = enabled
        if not enabled:
            job["next_run_at"] = None
        scheduler.save_jobs(jobs)
        return web.json_response({"ok": True, "job": job})

    async def _handle_cron_delete(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        from ...cron import scheduler

        body, err = await self._read_json(request)
        if err is not None:
            return err
        job_id = (body.get("id") or "").strip()
        jobs = scheduler.load_jobs()
        job = next((j for j in jobs if j.get("id") == job_id), None)
        if job is None:
            return web.json_response({"error": "任务不存在"}, status=404)
        jobs.remove(job)
        scheduler.save_jobs(jobs)
        conv = job.get("conv")
        if conv:
            session_store.delete_session(conv)
        return web.json_response({"ok": True})

    # ── 项目 ─────────────────────────────────────────────────────────────
    async def _handle_projects(self, request: web.Request) -> web.Response:
        """项目列表(侧边栏按项目分组用)。"""
        if (g := self._guard(request)) is not None:
            return g
        return web.json_response({"projects": session_store.list_projects()})

    @staticmethod
    def _browse_roots() -> list[Path]:
        """可浏览的根:用户主目录 + 已登记的项目目录。只在这些子树内列目录。"""
        roots: list[Path] = []
        try:
            roots.append(Path.home().resolve())
        except (OSError, ValueError):
            pass
        for proj in session_store.list_projects():
            raw = proj.get("path") or proj.get("root") if isinstance(proj, dict) else None
            if raw:
                try:
                    roots.append(Path(raw).resolve())
                except (OSError, ValueError):
                    pass
        return roots

    def _browse_allowed(self, target: Path, roots: list[Path]) -> bool:
        """target 必须落在某个根的子树内(含根自身)。不允许其祖先(否则 / 又全盘可枚举)。"""
        for r in roots:
            if target == r or r in target.parents:
                return True
        return False

    async def _handle_browse(self, request: web.Request) -> web.Response:
        """服务端目录浏览器:列出某目录下的子文件夹(默认从用户主目录起)。

        只列目录、跳过隐藏(. 开头)项;返回可上翻的 parent。**范围被限制在
        主目录 + 已登记项目目录的子树内**(审计 web#2 / 2-4):越界的 ?dir=/、
        ?dir=/etc 会被夹回主目录,避免把整块硬盘目录结构暴露出去。
        """
        if (g := self._guard(request)) is not None:
            return g
        roots = self._browse_roots()
        home = roots[0] if roots else Path.home()
        raw = request.query.get("dir") or str(home)
        try:
            base = Path(os.path.expanduser(raw)).resolve()
        except (OSError, ValueError):
            base = home
        if not base.is_dir() or not self._browse_allowed(base, roots):
            base = home
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
        # 只在 parent 仍落在允许根内时才给「上翻」链接,免得上翻按钮把人带出沙箱
        parent = None
        if base.parent != base and self._browse_allowed(base.parent, roots):
            parent = str(base.parent)
        return web.json_response({"dir": str(base), "parent": parent, "entries": entries})

    async def _handle_project_create(self, request: web.Request) -> web.Response:
        """新建/复活项目:校验路径是真实目录 → 入库,返回项目信息。"""
        if (g := self._guard(request)) is not None:
            return g
        body, err = await self._read_json(request)
        if err is not None:
            return err
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
        body, err = await self._read_json(request)
        if err is not None:
            return err
        h = (body.get("hash") or "").strip()
        if h:
            session_store.hide_project(h)
        return web.json_response({"ok": True})

    async def _handle_project_reorder(self, request: web.Request) -> web.Response:
        """侧边栏拖拽排序落库:body={"order": [hash, ...]},按新顺序整体覆盖。"""
        if (g := self._guard(request)) is not None:
            return g
        body, err = await self._read_json(request)
        if err is not None:
            return err
        order = body.get("order")
        if not isinstance(order, list):
            return web.json_response({"error": "order 必须是数组"}, status=400)
        session_store.reorder_projects([str(h) for h in order])
        return web.json_response({"ok": True})

    async def _handle_conv_pin(self, request: web.Request) -> web.Response:
        """置顶/取消置顶会话:body={"conv": ..., "pinned": bool}。"""
        if (g := self._guard(request)) is not None:
            return g
        body, err = await self._read_json(request)
        if err is not None:
            return err
        conv = str(body.get("conv") or "")
        if not conv:
            return web.json_response({"error": "conv 不能为空"}, status=400)
        session_store.set_conv_pinned(config.resolve_session_key("web", conv), bool(body.get("pinned")))
        return web.json_response({"ok": True})

    async def _handle_models(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        # 模型清单 = 官方档 + 设置页里配好的 DeepSeek/Kimi 等(available_models);
        # label 带描述(如订阅/API 标签),前端 renderModelPop 直接展示。
        # default=当前激活的模型(跟随设置页默认或 config.MODEL)。
        choices = providers.available_models(MODEL_CHOICES)
        # default = web 端上次选定的模型;没设过才回落到 config.MODEL
        active_model = settings_store.get_web_default_model() \
            or providers.resolve(None, config.MODEL)[0]
        return web.json_response(
            {
                "default": active_model,
                "choices": [[v, label, group] for v, label, group in choices],
            }
        )

    async def _handle_commands(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        # 斜杠命令清单 = COMMAND_LIST(单一来源,和 TG /help、setMyCommands 共用),
        # 供输入框 "/" 触发的快捷菜单渲染 + 前缀过滤。
        # skills 另开一段(带分隔线):只列当前已启用、对 agent 可见的,和
        # gateway/core.py 里放行 "/skill名" 穿透给 agent 的判定(_enabled_skill_names)同一口径。
        skills = [s for s in settings_store.list_skills() if s["enabled"]]
        return web.json_response(
            {
                "commands": [{"name": n, "desc": d} for n, d in COMMAND_LIST],
                "skills": [{"name": s["name"], "desc": s["description"]} for s in skills],
            }
        )

    # ── 订阅配额查询 ───────────────────────────────────────────────────────
    async def _handle_api_usage(self, request: web.Request) -> web.Response:
        """GET /api/usage — 查询当前模型的订阅配额(5h/7d 利用率+重置时间)。

        Claude 订阅:数据来自 SDK 流式回复中的 RateLimitEvent(已缓存)。
        Kimi 订阅:主动调 api.kimi.com/coding/v1/usages。
        API 按量计费(DeepSeek 等):返回 {type:"api"}。
        """
        if (g := self._guard(request)) is not None:
            return g

        model = (request.query.get("model") or "").strip()
        if not model:
            model = providers.resolve(None, config.MODEL)[0]

        # Kimi 订阅(api.kimi.com);providers 模块内部处理 web_providers 条目的
        # snake_case/camelCase 归一化,这里只拿到规整好的 api_key。
        api_key = providers.subscription_api_key_for_model(model)
        if api_key is not None:
            if api_key:
                try:
                    data = await providers.kimi_usage(api_key)
                    return web.json_response(data)
                except Exception as ex:
                    return web.json_response(
                        {"provider": "kimi", "error": str(ex)}, status=502
                    )
            return web.json_response({"provider": "kimi", "type": "api"})

        # Claude 官方订阅:优先用 SDK 缓存的 RateLimitEvent(官方精确值),
        # 若 utilization 缺失则合并本地日志估算值作兜底,确保始终有具体百分比。
        p_entry = providers.lookup_provider_by_model(model)
        if not p_entry or p_entry.get("name", "").lower() in ("claude", "official", "anthropic"):
            official = get_rate_limits()
            local = await get_local_claude_usage()

            five_off = (official.get("five_hour") or {}) if isinstance(official, dict) else {}
            five_loc = (local.get("limits", {}).get("five_hour") or {}) if local else {}

            # 官方有利用率就用官方的;否则退回本地估算,但要明确标注来源
            source = "official"
            if five_off.get("utilization") is not None:
                merged = dict(five_off)
            elif local:
                merged = dict(five_loc)
                source = "local_estimate"
            else:
                merged = dict(five_off)

            # resets_at 两边可能都有,取官方优先
            if not merged.get("resets_at") and five_off.get("resets_at"):
                merged["resets_at"] = five_off["resets_at"]
            if not merged.get("resets_at") and five_loc.get("resets_at"):
                merged["resets_at"] = five_loc["resets_at"]

            # 7d 窗口同样合并
            seven_off = (official.get("seven_day") or {}) if isinstance(official, dict) else {}
            seven_loc = (local.get("limits", {}).get("seven_day") or {}) if local else {}
            if seven_off.get("utilization") is not None:
                merged_seven = dict(seven_off)
                seven_source = "official"
            elif local:
                merged_seven = dict(seven_loc)
                seven_source = "local_estimate"
            else:
                merged_seven = dict(seven_off)
                seven_source = None

            payload: dict = {
                "provider": "claude",
                "source": source,
                "limits": {"five_hour": merged, "seven_day": merged_seven},
            }
            if local:
                # 本地估算详情给前端 hover 卡片用
                payload["local"] = local.get("local")
                payload["forecast"] = local.get("forecast")
                payload["pace"] = local.get("pace")
                payload["local_history"] = local.get("local_history")
                payload["confidence"] = local.get("confidence")

            # 标注 7d 数据来源(如果存在)
            if merged_seven:
                merged_seven["source"] = seven_source
            merged["source"] = source

            return web.json_response(payload)

        # 其他(DeepSeek/Moonshot API 等):按量计费,无配额
        return web.json_response({"provider": "api", "type": "api"})

    # ── 语音转文字 ───────────────────────────────────────────────────────
    async def _handle_transcribe(self, request: web.Request) -> web.Response:
        """收手机录音 → 调阿里 DashScope 转成文字回给前端(填进输入框)。

        识别本体复用 voice/stt.py(与语音伴聊模式同一套阿里云实现,含转写后的
        口癖/同音字清洗),这里不再维护第二份协议实现——之前这里还留着切
        SenseVoice 前的旧代码(config.STT_MODEL 等属性已改名/删除),
        导致每次转写都直接 AttributeError、前端只能显示"没识别到内容"。
        """
        from ...voice import stt as voice_stt  # 懒加载,避免非语音场景也引入这个包

        if (g := self._guard(request)) is not None:
            return g
        if not config.DASHSCOPE_API_KEY:
            return web.json_response(
                {"error": "未配置语音转写:请在 .env 设 DASHSCOPE_API_KEY"},
                status=503,
            )
        t0 = time.monotonic()
        audio, filename, ctype = await voice_stt.read_audio(request)
        if not audio:
            return web.json_response({"error": "没收到音频"}, status=400)
        t1 = time.monotonic()
        text, error = await voice_stt.transcribe(audio, filename, ctype)
        t2 = time.monotonic()
        print(
            f"[transcribe] size={len(audio) / 1024:.1f}KB "
            f"recv={t1 - t0:.2f}s stt={t2 - t1:.2f}s total={t2 - t0:.2f}s",
            flush=True,
        )
        if text is None:
            return web.json_response({"error": error}, status=502)
        return web.json_response({"text": text})

    async def _handle_history(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        conv = request.query.get("conv", "main")
        key = config.resolve_session_key("web", conv)
        turns = session_store.load_history(key, limit=40)
        # 重会话一包 JSON 有 300~500KB(gzip 后 70~145KB),跨境隧道 ~50KB/s 一趟要好几秒;
        # 而切会话大多是"回看",内容根本没变——加 ETag/304 协商,没变只回空包。
        # 前端零改动:fetch 走浏览器 HTTP 缓存,自动带 If-None-Match、304 时透明取缓存正文。
        body = json.dumps({"turns": turns}, ensure_ascii=False).encode("utf-8")
        etag = f'"{hashlib.md5(body).hexdigest()}"'
        headers = {"Cache-Control": "no-cache", "ETag": etag}
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        resp = web.Response(body=body, content_type="application/json", headers=headers)
        resp.enable_compression()  # 源站→CF 边缘这段也压缩着走,别指望 CF 兜底
        return resp

    async def _handle_turn_events(self, request: web.Request) -> web.Response:
        """按 turn id 单独取某一轮的完整过程时间线(工具卡片懒加载:点开才拉)。"""
        if (g := self._guard(request)) is not None:
            return g
        conv = request.query.get("conv", "main")
        try:
            turn_id = int(request.query.get("id", ""))
        except ValueError:
            return web.json_response({"error": "bad id"}, status=400)
        key = config.resolve_session_key("web", conv)
        events = session_store.load_turn_events(key, turn_id)
        return web.json_response({"events": events if events is not None else []})

    async def _handle_image(self, request: web.Request) -> web.StreamResponse:
        """回显某轮用户发的图片(落盘在 config.IMAGES_DIR);name 经白名单校验挡路径穿越。"""
        if (g := self._guard(request)) is not None:
            return g
        p = session_store.image_path(request.query.get("name", ""))
        if p is None:
            return web.json_response({"error": "not found"}, status=404)
        # 图片按内容寻址(文件名带 turn_id,内容不变)→ 长缓存,刷新不重复拉
        return web.FileResponse(p, headers={"Cache-Control": "max-age=31536000, immutable"})

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
            from ...core import tasks as bg_tasks  # 懒加载

            task_id = bg_tasks.task_id_from_session_key(conv)
            if task_id is not None:
                from ...core import task_runner  # 懒加载

                # 任务还在排队/运行时先取消并等收尾写完,否则收尾 finish_turn
                # 会把刚删掉的会话又写回来(侧边栏出现"复活"的空壳任务)
                await task_runner.cancel_and_wait(task_id)
            from ...core import worktree  # 懒加载

            key = config.resolve_session_key("web", conv)
            await worktree.remove_worktree(key)  # 先清 worktree(删库会抹掉绑定字段)
            session_store.delete_session(key)
            from ...tools import danger  # 懒加载:清「本次会话都允许」记忆

            danger.clear_session_approvals(key)
        return web.json_response({"ok": True})

    # ── 项目 Git 状态 ────────────────────────────────────────────────────
    def _conv_cwd(self, conv: str) -> str | None:
        """会话对应的项目工作目录;非项目会话(main/普通 web)返回 None。"""
        key = config.resolve_session_key("web", conv)
        return config.project_cwd_for(key)

    async def _handle_conv_git(self, request: web.Request) -> web.Response:
        """会话对应项目的 git 状态;非项目会话回退到 serve 进程 cwd。"""
        if (g := self._guard(request)) is not None:
            return g
        key = config.resolve_session_key("web", request.query.get("conv", ""))
        cwd = config.project_cwd_for(key)
        bound = cwd is not None
        if not cwd:
            cwd = os.getcwd()
        info = await git_status.git_status(cwd)
        # 只有绑定项目的会话才强制显示;非项目会话仅在 cwd 真是 git 仓库时才暴露
        if not bound and not info.get("is_repo"):
            return web.json_response({"is_project": False})
        # 项目名取"仓库根目录"的文件夹名,而不是 cwd —— cwd 在有独立 worktree 的会话
        # 里是 data/worktrees/<hash>/<slug>,basename 是会话 slug,长得跟分支名一样,
        # 标题栏就会显示成"分支名"而不是真正的项目名。
        proj_root = config.project_root_for(key) if bound else cwd
        info.update(
            is_project=True, bound_project=bound, path=cwd,
            name=os.path.basename(proj_root) or proj_root, project_path=proj_root,
        )
        return web.json_response(info)

    async def _handle_conv_git_branch(self, request: web.Request) -> web.Response:
        """在项目工作目录建并切到新分支(当前改动随之带过去)。"""
        if (g := self._guard(request)) is not None:
            return g
        body, err = await self._read_json(request)
        if err is not None:
            return err
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
        code, _, _ = await git_status.run_git(cwd, "check-ref-format", "--branch", name)
        if code != 0:
            return web.json_response({"error": f"分支名非法:{name}"}, status=400)
        code, _, err = await git_status.run_git(cwd, "checkout", "-b", name)
        if code != 0:
            return web.json_response({"error": err.strip() or "创建分支失败"}, status=400)
        info = await git_status.git_status(cwd)
        proj_root = config.project_root_for(key) or cwd
        info.update(
            is_project=True, path=cwd,
            name=os.path.basename(proj_root) or proj_root, project_path=proj_root,
        )
        return web.json_response(info)

    # ── 设置:技能 / MCP ─────────────────────────────────────────────────
    async def _handle_conv_archive(self, request: web.Request) -> web.Response:
        """POST /conv/archive  {conv, archived: bool}  设置会话归档状态。"""
        if (g := self._guard(request)) is not None:
            return g
        body, err = await self._read_json(request)
        if err is not None:
            return err
        conv = (body.get("conv") or "").strip()
        if not conv:
            return web.json_response({"error": "缺少 conv"}, status=400)
        archived = bool(body.get("archived", True))
        session_key = config.resolve_session_key("web", conv)
        session_store.set_conv_archived(session_key, archived)
        return web.json_response({"ok": True})

    async def _handle_conv_read(self, request: web.Request) -> web.Response:
        """POST /conv/read  {conv}  用户已打开该会话,清零 pending_review 标记。"""
        if (g := self._guard(request)) is not None:
            return g
        body, err = await self._read_json(request)
        if err is not None:
            return err
        conv = (body.get("conv") or "").strip()
        if not conv:
            return web.json_response({"error": "缺少 conv"}, status=400)
        session_key = config.resolve_session_key("web", conv)
        session_store.set_pending_review(session_key, False)
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
        body, err = await self._read_json(request)
        if err is not None:
            return err
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be object"}, status=400)
        session_store.set_prefs(body)
        return web.json_response({"ok": True})

    async def _handle_settings(self, request: web.Request) -> web.Response:
        """设置页初始快照:技能清单 + MCP(内置 hermes + 外部)+ 记忆/AGENTS 文件列表。"""
        if (g := self._guard(request)) is not None:
            return g
        # active = 当前实际在用的模型,算法跟 _handle_models 的 default 保持一致
        active_model = settings_store.get_web_default_model() \
            or providers.resolve(None, config.MODEL)[0]
        disabled = set(settings_store.list_disabled_builtin_models())
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
                "models": {
                    "active": active_model,
                    "builtin": [
                        {"id": mid, "label": label, "disabled": mid in disabled}
                        for mid, label in MODEL_CHOICES
                    ],
                    "custom": settings_store.list_web_extra_models(),
                    "providers": settings_store.list_web_providers(),
                },
                "files": self._list_brain_files(),
                "brain_dir": str(config.AI_BRAIN_DIR),
            }
        )

    async def _handle_settings_skill(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        body, err = await self._read_json(request)
        if err is not None:
            return err
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
        body, err = await self._read_json(request)
        if err is not None:
            return err
        settings_store.set_hermes(bool(body.get("enabled")))
        return web.json_response({"ok": True})

    async def _handle_settings_mcp_external(self, request: web.Request) -> web.Response:
        """增删改 / 开关外部 MCP server。action: add|update|remove|toggle。"""
        if (g := self._guard(request)) is not None:
            return g
        body, err = await self._read_json(request)
        if err is not None:
            return err
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
        # add / update:清洗校验收在 settings_store.upsert_external 内部
        err = settings_store.upsert_external(name, body)
        if err:
            return web.json_response({"error": err}, status=400)
        return web.json_response({"ok": True})

    async def _handle_settings_model(self, request: web.Request) -> web.Response:
        """增/改/删设置页手动加的官方模型档位,或隐藏/恢复代码内置档位。
        action: add(新增/编辑,id 相同即覆盖 label)|remove|toggle_builtin。

        add/remove 用来填"新模型发布了但代码还没跟上"的空窗;toggle_builtin 是
        MODEL_CHOICES 里代码写死的档位摘出/放回选择器(不改代码,可随时恢复)。
        不用改代码、不用重启,下一次拉 /models(即刷新页面/切模型面板)就带上。
        """
        if (g := self._guard(request)) is not None:
            return g
        body, err = await self._read_json(request)
        if err is not None:
            return err
        action = (body.get("action") or "add").strip()
        model_id = (body.get("id") or "").strip()
        if not model_id:
            return web.json_response({"error": "缺少 id"}, status=400)
        if action == "toggle_builtin":
            settings_store.set_builtin_model_disabled(model_id, bool(body.get("disabled")))
            return web.json_response({"ok": True})
        if action == "remove":
            settings_store.remove_web_extra_model(model_id)
            return web.json_response({"ok": True})
        err = settings_store.upsert_web_extra_model(model_id, body.get("label") or "")
        if err:
            return web.json_response({"error": err}, status=400)
        return web.json_response({"ok": True})

    async def _handle_settings_provider(self, request: web.Request) -> web.Response:
        """新增/删除设置页手动加的第三方服务商。action: add|remove。

        直接落 web_settings.json,同样不用重启——providers.py 每次都现读现并。
        """
        if (g := self._guard(request)) is not None:
            return g
        body, err = await self._read_json(request)
        if err is not None:
            return err
        action = (body.get("action") or "add").strip()
        name = (body.get("name") or "").strip()
        if not name:
            return web.json_response({"error": "缺少服务商名称"}, status=400)
        if action == "remove":
            settings_store.remove_web_provider(name)
            return web.json_response({"ok": True})
        err = settings_store.upsert_web_provider(name, body)
        if err:
            return web.json_response({"error": err}, status=400)
        return web.json_response({"ok": True})

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
        body, err = await self._read_json(request)
        if err is not None:
            return err
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

    # ── 文档预览分屏(右侧滑出,见 web_static/index.html 的 openDocPreview)──────
    def _doc_preview_bounds(self, conv_root: str) -> list[Path]:
        """/doc/preview 允许读取的边界目录:HOME 兜底,而不是死磕会话 cwd/AI_BRAIN 这种
        短名单——这台机器是单用户私人助理,agent 本来就能用 Bash/Write 碰到 HOME 下任何
        文件(Desktop/Documents/随便哪个项目),白名单卡太窄只会把大多数真实路径挡在外面,
        误报成"文件不存在"(2026-07-30 用户反馈基本没有能预览成功的例子)。这里加一道
        "不能越出 HOME"就是全部的额外防护,不是也没必要比 agent 自身权限更严。
        AI_BRAIN 单独列进来是防它被配到 HOME 之外(.env 里 AI_BRAIN_DIR 覆盖成别处)。
        """
        bounds = [Path(conv_root).resolve(), Path.home().resolve(), config.AI_BRAIN_DIR.resolve()]
        seen: set[Path] = set()
        out: list[Path] = []
        for b in bounds:
            if b not in seen:
                seen.add(b)
                out.append(b)
        return out

    def _fuzzy_doc_path(self, base: Path, rel_parts: tuple[str, ...]) -> Path | None:
        """直接拼接找不到时的兜底:在 base 下找"路径末尾几段跟 rel_parts 一样"的文件。
        典型场景是 AI 提到项目文件时把包名前缀说漏了(比如把 claude_hermes/memory/images.py
        说成 memory/images.py)——人一看就知道该是哪个文件,这里做同样的事。候选不止一个
        时选路径最短(离 base 最近)的那个,只是个启发式,不保证猜对;猜不到才是真的
        "文件不存在"。仍然是只读、只在 base 内部搜索,不改变安全边界。
        """
        n = len(rel_parts)
        if n < 2:
            return None  # 只有孤零零一个文件名(没有目录段可"丢")→ 太容易撞同名文件,不猜
        scanned = 0
        best: Path | None = None
        best_depth = 10**9
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _DOC_SEARCH_SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                scanned += 1
                if scanned > _DOC_SEARCH_MAX_SCAN:
                    return best
                parts = (Path(dirpath) / name).relative_to(base).parts
                if len(parts) >= n and parts[-n:] == rel_parts and len(parts) < best_depth:
                    best = Path(dirpath) / name
                    best_depth = len(parts)
        return best

    def _resolve_doc_path(self, conv_root: str, rel: str) -> Path | None:
        """把前端传的路径(相对或绝对)解析成真实文件路径,越界(见 _doc_preview_bounds)拒绝。
        相对路径依次按候选根目录展开,哪个真存在就用哪个——AI 给的相对路径可能是相对会话
        cwd,也可能是相对 AI_BRAIN(比如 "00-inbox/x.md" 这种收件箱惯例)。直接拼接全部落空
        再退到 _fuzzy_doc_path 按路径尾部兜底搜一遍。
        """
        rel = (rel or "").strip()
        if not rel:
            return None
        bounds = self._doc_preview_bounds(conv_root)
        p = Path(rel)
        if p.is_absolute():
            try:
                target = p.resolve()
            except (OSError, ValueError):
                return None
            return target if any(target == b or b in target.parents for b in bounds) else None
        for base in bounds:
            try:
                target = (base / p).resolve()
            except (OSError, ValueError):
                continue
            if target != base and base not in target.parents:
                continue  # rel 里带 ../.. 跳出了这个候选根,换下一个候选根试
            if target.is_file():
                return target
        for base in bounds:
            found = self._fuzzy_doc_path(base, p.parts)
            if found is not None:
                return found
        return None

    async def _handle_doc_preview(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        root = self._conv_cwd(request.query.get("conv", "")) or os.getcwd()
        # 模糊兜底可能要 os.walk 扫大几千个文件(_DOC_SEARCH_MAX_SCAN),丢进线程池——
        # 这是单进程 server,同一个 event loop 还扛着其他会话的 SSE 长连接,同步扫盘会
        # 把大伙都卡住(实测最坏情况 1.6s+,足够让人感觉到卡顿)。
        target = await asyncio.to_thread(self._resolve_doc_path, root, request.query.get("path", ""))
        if target is None or not target.is_file():
            return web.json_response({"error": "文件不存在或路径越界"}, status=404)
        try:
            size = target.stat().st_size
        except OSError:
            return web.json_response({"error": "读取失败"}, status=500)
        if size > _DOC_PREVIEW_MAX:
            return web.json_response({"error": "文件太大(超过 3MB),暂不支持预览"}, status=413)
        try:
            data = target.read_bytes()
        except OSError:
            return web.json_response({"error": "读取失败"}, status=500)
        ctype, _ = mimetypes.guess_type(target.name)
        resp = web.Response(body=data, headers={"Cache-Control": "no-cache"})
        resp.content_type = ctype or "text/plain"
        if resp.content_type.startswith("text/") or resp.content_type == "application/json":
            resp.charset = "utf-8"
        return resp

    # ── PWA 静态资源 + 推送订阅 ──────────────────────────────────────────
    def _static_file(
        self,
        name: str,
        content_type: str,
        extra_headers: dict | None = None,
        request: web.Request | None = None,
        compress: bool = False,
    ) -> web.Response:
        """读 web_static 下的文件返回;缺失给 404。这些是公开资源(浏览器不带 token 取)。

        传入 request 时带 ETag/304 协商:内容没变回 304 空包,省一趟跨境正文传输
        (走隧道实测带宽只有 ~50KB/s,78KB 的 CSS 整传要 1.5s+)。文本类资源再开
        compress=True 让源站 gzip——隧道「源站→CF 边缘」这一段也压缩着走。
        """
        try:
            data = (_STATIC / name).read_bytes()
        except OSError:
            return web.Response(status=404, text="not found")
        headers = {"Cache-Control": "no-cache"}
        if extra_headers:
            headers.update(extra_headers)
        if request is not None:
            etag = f'"{hashlib.md5(data).hexdigest()}"'
            headers["ETag"] = etag
            if request.headers.get("If-None-Match") == etag:
                return web.Response(status=304, headers=headers)
        resp = web.Response(body=data, content_type=content_type, headers=headers)
        if compress:
            resp.enable_compression()
        return resp

    async def _handle_manifest(self, request: web.Request) -> web.Response:
        # 几乎不改,允许 CDN 边缘缓存 1 小时,省一趟跨境回源(2026-07-21)
        return self._static_file(
            "manifest.json",
            "application/manifest+json",
            {"Cache-Control": "public, max-age=3600"},
            request=request,
            compress=True,
        )

    async def _handle_sw(self, request: web.Request) -> web.Response:
        # Service-Worker-Allowed: / 让 sw 能控整站;no-cache 保证改动能刷新
        return self._static_file(
            "sw.js", "text/javascript", {"Service-Worker-Allowed": "/"},
            request=request, compress=True,
        )

    def _versioned_static(
        self, request: web.Request, name: str, content_type: str
    ) -> web.Response:
        """CSS/JS 双轨缓存策略(2026-07-23 提速):

        - index.html 里的引用被 _handle_index 改写成 /xxx?v=<内容哈希>,带 v 的请求
          返回 immutable 一年——URL 即版本,内容一变 URL 就变,CF 边缘和浏览器都能
          长缓存直接命中,不再跨境回源(此前 CF 对 css/js 套默认 4h TTL,既可能改了
          4 小时不生效,过期后又整文件重传)。
        - 不带 v 的裸请求(直接开 URL 调试等)维持 no-cache+ETag 协商。
        """
        immutable = bool(request.query.get("v"))
        headers = (
            {"Cache-Control": "public, max-age=31536000, immutable"} if immutable else None
        )
        return self._static_file(
            name, content_type, headers, request=request, compress=True
        )

    async def _handle_styles(self, request: web.Request) -> web.Response:
        # 2026-07-23 从 index.html 内联 <style> 拆出
        return self._versioned_static(request, "styles.css", "text/css")

    async def _handle_tool_card_js(self, request: web.Request) -> web.Response:
        # 2026-07-23 从 index.html 内联 <script> 拆出(工具卡片渲染那一段)
        return self._versioned_static(request, "tool-card.js", "text/javascript")

    async def _handle_favicon(self, request: web.Request) -> web.Response:
        # 跟 wazir-mark.svg 同口径:允许 CDN 边缘缓存 1 天,省跨境回源;
        # 换标后最多 1 天内生效,可接受(2026-07-21,此前是 no-cache 强制每次回源)
        return self._static_file(
            "favicon.ico",
            "image/x-icon",
            {"Cache-Control": "public, max-age=86400"},
            request=request,
        )

    async def _handle_mark(self, request: web.Request) -> web.Response:
        # 自适应深浅的 SVG favicon(内嵌 prefers-color-scheme);现代浏览器优先用它,旧的回退 PNG
        return self._static_file(
            "wazir-mark.svg",
            "image/svg+xml",
            {"Cache-Control": "public, max-age=86400"},
            request=request,
            compress=True,
        )

    async def _handle_logos(self, request: web.Request) -> web.Response:
        # Wazir logo 概念预览页:自包含 HTML(内联 SVG),公开可取;改文件刷新即生效不用重启
        # 注:content_type 不能带 charset(aiohttp 会抛 ValueError);HTML 内有 <meta charset> 兜底
        return self._static_file("wazir-logos.html", "text/html")

    # ── 发布页(公开可取,给 skill hermes-web-publish 用)────────────────────
    def _safe_published_path(self, rel: str) -> Path | None:
        """把 /pub/<rel> 解析到 config.PUBLISHED_DIR 内;越界 / 隐藏路径段一律拒绝。

        目录请求(含空路径)补 index.html,方便发多文件的小站点。
        """
        rel = (rel or "").strip().lstrip("/")
        if any(part.startswith(".") for part in rel.split("/") if part):
            return None  # 挡隐藏文件(.env 之类误放进去也取不到)
        root = config.PUBLISHED_DIR.resolve()
        try:
            target = (root / rel).resolve()
        except (OSError, ValueError):
            return None
        if target != root and root not in target.parents:
            return None  # 目录穿越,拒
        if target.is_dir():
            target = target / "index.html"
        return target

    async def _handle_publish(self, request: web.Request) -> web.StreamResponse:
        """把 data/published/ 下的文件原样公开served;丢文件进去即生效,不用加路由、不用重启。

        无口令、无 _guard——发布页本来就是要给没登录的人打开的链接。安全靠 _PUBLISH_CSP
        的 sandbox 兜底(见其定义处注释),而非鉴权。"""
        target = self._safe_published_path(request.match_info.get("path", ""))
        if target is None or not target.is_file():
            return web.Response(status=404, text="not found")
        return web.FileResponse(
            target,
            # X-Frame-Options 显式设成 SAMEORIGIN(不能不设——中间件 _security_mw 用 setdefault
            # 补 DENY,不设就会跟上面放宽的 frame-ancestors 'self' 打架,预览面板照样空白)。
            headers={
                "Cache-Control": "no-cache",
                "Content-Security-Policy": _PUBLISH_CSP,
                "X-Frame-Options": "SAMEORIGIN",
            },
        )

    # 只放行这几个图标名,防目录穿越
    _ICONS = {"icon-192", "icon-512", "icon-maskable-512", "apple-touch-icon"}

    async def _handle_icon(self, request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        if name not in self._ICONS:
            return web.Response(status=404, text="not found")
        # 同 favicon:换标 1 天内生效换取免跨境回源(2026-07-21)
        return self._static_file(
            f"{name}.png",
            "image/png",
            {"Cache-Control": "public, max-age=86400"},
            request=request,
        )

    async def _handle_push_config(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        return web.json_response(PUSH.public_config())

    async def _handle_push_subs(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        return web.json_response({"subs": PUSH.list_public()})

    async def _handle_push_subscribe(self, request: web.Request) -> web.Response:
        if (g := self._guard(request)) is not None:
            return g
        sub, err = await self._read_json(request)
        if err is not None:
            return err
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

    async def _handle_push_test(self, request: web.Request) -> web.Response:
        """自测端点:给所有已登记订阅发一条测试通知,返回送出设备数。
        kind=approval → 前台聚焦时 SW 也弹,测试时页面正开着也能看到。"""
        if (g := self._guard(request)) is not None:
            return g
        sent = await PUSH.notify(
            "🔔 测试通知",
            "看到这条 = 推送链路通了。",
            conv="main",
            kind="approval",
        )
        return web.json_response({"ok": True, "sent": sent})

    @staticmethod
    def _is_local_host(host: str) -> bool:
        """仅本机回环才算「本地」。绑到其它地址(0.0.0.0 / LAN IP)= 对外暴露。"""
        return host.strip().lower() in {"127.0.0.1", "localhost", "::1", ""}

    def _preflight(self) -> None:
        """启动前的 fail-closed 检查:绑非本机却没设口令 = 拒绝启动(审计 web#1 / 0-2)。

        127.0.0.1 本机调试不受影响;一旦 WEB_HOST 绑到 0.0.0.0/LAN(或经隧道对外),
        缺 WEB_AUTH_TOKEN 直接抛错,而不是打印一行警告后照样把「能执行任意代码的 agent」
        挂上公网。"""
        if not self._is_local_host(config.WEB_HOST) and not config.WEB_AUTH_TOKEN:
            raise config.ConfigError(
                f"拒绝启动 Web:绑定了非本机地址 WEB_HOST={config.WEB_HOST} 却没设 "
                "WEB_AUTH_TOKEN。这等于把能执行任意代码的 Claude 挂到公网裸奔。\n"
                "  解决:在 .env 设一个强口令 WEB_AUTH_TOKEN=...;"
                "或仅本机用就把 WEB_HOST 改回 127.0.0.1(隧道由 cloudflared/Tailscale 转发)。"
            )

    async def _start_server(self) -> None:
        self._preflight()
        app = web.Application(
            client_max_size=32 * 1024 * 1024,  # 允许 32MB 图片上传
            middlewares=[_security_mw],  # 跨源写拦截 + 安全响应头(2-9)
        )
        app.add_routes(
            [
                web.get("/", self._handle_index),
                web.get("/manifest.json", self._handle_manifest),
                web.get("/sw.js", self._handle_sw),
                web.get("/styles.css", self._handle_styles),
                web.get("/tool-card.js", self._handle_tool_card_js),
                web.get("/favicon.ico", self._handle_favicon),
                web.get("/wazir-mark.svg", self._handle_mark),
                web.get("/wazir-logos", self._handle_logos),
                web.get("/pub/{path:.*}", self._handle_publish),
                web.get(r"/{name}.png", self._handle_icon),
                web.get("/push/config", self._handle_push_config),
                web.get("/push/subs", self._handle_push_subs),
                web.post("/push/subscribe", self._handle_push_subscribe),
                web.post("/push/unsubscribe", self._handle_push_unsubscribe),
                web.post("/push/test", self._handle_push_test),
                web.get("/events", self._handle_events),
                web.post("/send", self._handle_send),
                web.post("/model", self._handle_model_switch),
                web.post("/abort", self._handle_abort),
                web.get("/conversations", self._handle_conversations),
                web.get("/conv/search", self._handle_conv_search),
                web.get("/voice/sidebar", self._handle_voice_sidebar),
                web.get("/cron/sidebar", self._handle_cron_sidebar),
                web.post("/cron/jobs/create", self._handle_cron_create),
                web.post("/cron/jobs/update", self._handle_cron_update),
                web.post("/cron/jobs/enable", self._handle_cron_set_enabled),
                web.post("/cron/jobs/delete", self._handle_cron_delete),
                web.get("/projects", self._handle_projects),
                web.get("/browse", self._handle_browse),
                web.post("/projects/create", self._handle_project_create),
                web.post("/projects/remove", self._handle_project_remove),
                web.post("/projects/reorder", self._handle_project_reorder),
                web.post("/conv/pin", self._handle_conv_pin),
                web.get("/models", self._handle_models),
                web.get("/api/usage", self._handle_api_usage),
                web.get("/commands", self._handle_commands),
                web.get("/history", self._handle_history),
                web.get("/turn_events", self._handle_turn_events),
                web.get("/image", self._handle_image),
                web.post("/transcribe", self._handle_transcribe),
                web.post("/conv/rename", self._handle_rename),
                web.post("/conv/delete", self._handle_delete),
                web.post("/conv/archive", self._handle_conv_archive),
                web.post("/conv/read", self._handle_conv_read),
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
                web.post("/settings/model", self._handle_settings_model),
                web.post("/settings/provider", self._handle_settings_provider),
                web.get("/file/read", self._handle_file_read),
                web.post("/file/save", self._handle_file_save),
                web.get("/doc/preview", self._handle_doc_preview),
            ]
        )
        if config.VOICE_ENABLED:  # 实验性语音伴聊模式,见 claude_hermes/voice/
            from ...voice import register_routes as _voice_register_routes

            _voice_register_routes(app)
        # F11 重启自愈(统一后台任务引擎,原来叫「语音任务板」)只能挂在这里(serve
        # 真正启动的唯一路径),不能挂在 register_routes 里——否则测试/脚本组建 app
        # 也会触发孤儿回收,误杀别的进程里正在跑的任务(2026-07-12 "假失败"事故
        # 根因之一)。2026-07-29 统一后不再挂在 VOICE_ENABLED 分支里——cron/chat
        # 触发的任务跟 VOICE_ENABLED 完全无关,关了语音也不能让它们的孤儿任务
        # 永远卡在 running/queued(会顶占并发上限,后续任务永远排不上队)。
        from ...core import task_runner as _task_runner

        asyncio.ensure_future(_task_runner.heal_after_restart())
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, config.WEB_HOST, config.WEB_PORT)
        await site.start()
        lock = "🔒 已设访问口令" if config.WEB_AUTH_TOKEN else "⚠️ 未设口令(任何人可用)"
        print(
            f"✅ Web 已上线 http://{config.WEB_HOST}:{config.WEB_PORT} · "
            f"{lock} · 模型 {config.MODEL}"
        )
