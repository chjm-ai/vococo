"""MCP Events(协议 2026-07-28):任务终态 → 带签名 webhook 推给订阅方(如 ChatGPT)。

- 前端(127.0.0.1:8852)负责协议三个方法与 webhook 投递,订阅库独立 SQLite;
- 原 gateway 进程跑「终态扫描」:定期把桥登记的远程任务在终态写入事件箱,
  终态只写一次(去重),前端投递成功/永久失败后出箱。跨进程重启都不丢。
- 回调地址只允许公网 HTTPS:解析后校验 IP、直连已校验 IP(TLS 仍用原主机名)、不跟随重定向。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import io
import json
import logging
import os
import re
import secrets
import socket
import ssl
import time
from http.client import HTTPResponse
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp.web import AppKey

from .. import config

_LOG = logging.getLogger("vococo.mcp_events")

EVENT_NAME = "vococo.task.finished"
PROTOCOL_VERSION = "2026-07-28"
SUBSCRIPTION_ID_HEADER = "X-MCP-Subscription-Id"
BODY_SIZE_LIMIT = 256 * 1024  # 投递出去的单个事件体上限(超了按永久失败处理)
SCANNER_KEY = AppKey("vococo.mcp_events.scanner", Any)
DEFAULT_TTL_MS = 7 * 24 * 3600 * 1000
MIN_TTL_MS = 5 * 60 * 1000
MAX_TTL_MS = 30 * 24 * 3600 * 1000
MAX_PER_SESSION = 10
MAX_TOTAL = 200
VERIFY_CACHE_SEC = 15 * 60
DELIVERY_TIMEOUT = 10.0
MAX_ATTEMPTS = 6
RETENTION_SEC = 7 * 24 * 3600
_SCAN_INTERVAL = 2.0
_ACTIVE = frozenset({"done", "failed", "cancelled"})

EVENT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "只订阅这一个任务。需要监控多个任务时分别订阅，或省略本字段以监控本连接创建的全部任务。",
        },
    },
    "required": [],
    "additionalProperties": False,
}
EVENT_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "任务 ID"},
        "status": {"type": "string", "enum": sorted(_ACTIVE), "description": "终态:done/failed/cancelled"},
        "title": {"type": "string", "description": "任务标题"},
        "summary": {"type": "string", "description": "不超 1500 字的脱敏摘要;完整结果用 vococo_get_task 读取"},
    },
    "required": ["task_id", "status", "title", "summary"],
    "additionalProperties": False,
}
EVENT_DEFINITION: dict[str, Any] = {
    "name": EVENT_NAME,
    "description": (
        "本连接创建的 Vococo 任务进入终态(完成/失败/取消)时通知一次。"
        "data 里只有任务 ID、状态和简短摘要;收到后必须调用 vococo_get_task 读取完整结果,再向用户汇报。"
        "任务已终态后再订阅,会立即补发一次最近的那个终态(最多回查 24 小时)。"
    ),
    "delivery": ["webhook"],
    "inputSchema": EVENT_INPUT_SCHEMA,
    "payloadSchema": EVENT_PAYLOAD_SCHEMA,
}


class CallbackError(ValueError):
    """回调地址不合法或验证失败;reason 对应协议 data.reason。"""

    def __init__(self, reason: str, message: str | None = None):
        super().__init__(message or reason)
        self.reason = reason


# ---------------------------------------------------------------- 订阅库

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS subscriptions("
    "id TEXT PRIMARY KEY,session TEXT NOT NULL,task_filter TEXT,url TEXT NOT NULL,secret TEXT NOT NULL,"
    "created_at REAL NOT NULL,expires_at REAL NOT NULL,last_cursor TEXT,verified_url TEXT)",
    "CREATE TABLE IF NOT EXISTS outbox(event_id TEXT PRIMARY KEY,session TEXT NOT NULL,task_id TEXT NOT NULL,"
    "payload TEXT NOT NULL,created_at REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,"
    "next_attempt_at REAL NOT NULL,last_error TEXT NOT NULL DEFAULT '')",
    "CREATE INDEX IF NOT EXISTS outbox_due ON outbox(session,next_attempt_at)",
    "CREATE TABLE IF NOT EXISTS deliveries(event_id TEXT NOT NULL,subscription_id TEXT NOT NULL,"
    "status TEXT NOT NULL,attempts INTEGER NOT NULL,last_error TEXT NOT NULL DEFAULT '',updated_at REAL NOT NULL,"
    "PRIMARY KEY(event_id,subscription_id))",
)


def db_path() -> Path:
    override = os.environ.get("VOCOCO_MCP_EVENTS_DB", "")
    return Path(override) if override else config.DATA_DIR / "mcp_events.db"


def connect() -> Any:
    import sqlite3

    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # autocommit:每个写操作单独成事务,不用 with conn(那样不会提交)。
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for statement in _SCHEMA:
        conn.execute(statement)
    return conn


def _now() -> float:
    return time.time()


def purge(conn, now: float | None = None) -> None:
    now = _now() if now is None else now
    conn.execute("DELETE FROM subscriptions WHERE expires_at<=?", (now,))
    conn.execute("DELETE FROM outbox WHERE created_at<?", (now - RETENTION_SEC,))
    conn.execute("DELETE FROM deliveries WHERE updated_at<?", (now - RETENTION_SEC,))


# ---------------------------------------------------------------- 终态入箱(gateway 进程)

_task_registered: Any = None


def _summary(task: dict, max_len: int = 1500) -> str:
    from .brain_routes import _clean

    text = task.get("result_summary") or task.get("progress_note") or ""
    text = _clean(text).strip()
    if not text:
        text = {"done": "已完成", "failed": "执行失败", "cancelled": "已取消"}.get(task["status"], task["status"])
    return text[:max_len]


def record_terminal(task: dict) -> bool:
    """把终态写进事件箱;同一任务已入箱过则不再写(去重)。只处理桥派生的远程任务。

    事件箱表与桥登记表必须在同一个库、同一连接里查(部署时 VOCOCO_MCP_EVENTS_DB 指向
    voice.db);测试夹具照此设置。
    """
    if not task or task.get("status") not in _ACTIVE or not task.get("agent_id"):
        return False
    conn = connect()
    try:
        record = conn.execute("SELECT source_session_key FROM brain_bridge_tasks WHERE task_id=?",
                              (task["id"],)).fetchone()
        if not record:
            return False
        if conn.execute("SELECT 1 FROM outbox WHERE task_id=?", (task["id"],)).fetchone():
            return False
        payload = {"task_id": task["id"], "status": task["status"],
                   "title": _summary_title(task), "summary": _summary(task)}
        session = _connection_key(task, record)
        conn.execute(
            "INSERT INTO outbox(event_id,session,task_id,payload,created_at,next_attempt_at) VALUES (?,?,?,?,?,?)",
            (secrets.token_hex(12), session, task["id"],
             json.dumps(payload, ensure_ascii=False), _now(), _now()),
        )
        return True
    except Exception:  # noqa: BLE001 —— 远程接入未启用(没有登记表)时没有可通知的任务
        return False
    finally:
        conn.close()


def _connection_key(task: dict, record: Any) -> str:
    """事件箱的 session 是「连接身份」(上游服务端凭据:每个 MCP 连接各持一份);
    任务源的 source_session_key 只是用户聊天会话,两者不同,优先取派单时记下的连接身份。"""
    try:
        access = json.loads(task.get("source_access") or "null") or {}
    except (TypeError, ValueError):
        access = {}
    return (access.get("connection") or record["source_session_key"] or "")[:64]


def _summary_title(task: dict) -> str:
    from .brain_routes import _clean

    return _clean(task.get("title") or "")[:100]


def scan_once() -> int:
    """扫描全部桥任务,把处于终态但未入箱的补写进事件箱。返回新写入数。"""
    from ..core import tasks

    try:
        rows = tasks._conn().execute(
            "SELECT t.* FROM tasks t JOIN brain_bridge_tasks b ON b.task_id=t.id "
            "WHERE t.status IN ('done','failed','cancelled') ORDER BY t.updated_at DESC LIMIT 500"
        ).fetchall()
    except Exception:  # noqa: BLE001 —— 桥还没建表(未启用远程接入)时无事可做
        return 0
    written = 0
    for row in rows:
        written += int(record_terminal(dict(row)))
    return written


def _bridge_agent_id() -> str:
    from .brain_routes import AGENT_ID

    return AGENT_ID


def register_terminal_hook() -> None:
    """在 gateway 进程里挂终态钩子:终态一落库就近写入事件箱(扫描兜底)。"""
    global _task_registered
    from ..core import task_events

    if _task_registered is task_events:
        return

    async def _on_terminal(task_id: str) -> None:
        from ..core import tasks

        task = tasks.get(task_id)
        if task is not None:
            record_terminal(task)

    task_events.register_terminal_handler(_on_terminal)
    _task_registered = task_events


async def start_scanner(app) -> None:
    """gateway 启动时拉起扫描兜底;退出时取消。"""
    stop = asyncio.Event()
    app[SCANNER_KEY] = asyncio.create_task(scanner_loop(stop), name="mcp-events-scanner")

    async def _stop(_app) -> None:
        stop.set()
        with contextlib.suppress(BaseException):
            await _app[SCANNER_KEY]

    app.on_cleanup.append(_stop)


async def scanner_loop(stop) -> None:
    """周期扫描,兜底崩溃/重启/跨进程漏掉的终态。"""
    while not stop.is_set():
        try:
            scan_once()
        except Exception as exc:  # noqa: BLE001 —— 扫描失败不能让进程退出
            _LOG.warning("终态扫描失败:%s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_SCAN_INTERVAL)
        except asyncio.TimeoutError:
            continue


# ---------------------------------------------------------------- 签名(Standard Webhooks)

def new_secret() -> str:
    return "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()


def valid_secret(secret: Any) -> bool:
    if not isinstance(secret, str) or not secret.startswith("whsec_") or len(secret) > 200:
        return False
    try:
        raw = base64.b64decode(secret[len("whsec_"):], validate=True)
    except (ValueError, TypeError):
        return False
    return 24 <= len(raw) <= 64


def _b64_key(secret: str) -> bytes:
    return base64.b64decode(secret[len("whsec_"):])


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    content = f"{msg_id}.{timestamp}.".encode() + body
    return "v1," + base64.b64encode(hmac.new(_b64_key(secret), content, hashlib.sha256).digest()).decode()


def verify(secret: str, msg_id: str, timestamp: str, signature: str, body: bytes, tolerance: int = 300) -> bool:
    """测试与自查用:按 Standard Webhooks 规则校验一次签名。"""
    try:
        if abs(int(timestamp) - int(_now())) > tolerance:
            return False
    except (TypeError, ValueError):
        return False
    expected = sign(secret, msg_id, int(timestamp), body).split(",", 1)[1]
    for part in str(signature).split(" "):
        version, _, value = part.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return True
    return False


# ---------------------------------------------------------------- 回调地址校验

def _public_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                                 or ip.is_reserved or ip.is_unspecified)


def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise CallbackError("resolve_failed") from None
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses or not all(_public_ip(a) for a in addresses):
        raise CallbackError("blocked_address")
    return addresses


def validate_url(url: Any) -> str:
    """只接受 HTTPS + 公网地址、无凭据、无自定义端口以外的怪东西;返回规范化 URL。"""
    if not isinstance(url, str) or len(url) > 2048:
        raise CallbackError("invalid_url")
    try:
        parts = urlsplit(url)
        port = parts.port
    except CallbackError:
        raise  # CallbackError 也是 ValueError,不能被下面的分支吞掉
    except ValueError:
        raise CallbackError("invalid_url") from None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise CallbackError("invalid_url")
    if parts.port is not None and port != 443:
        raise CallbackError("invalid_url", "回调地址只允许 443 端口")
    _resolve(parts.hostname, 443)
    return url


class _DeadlineReader(io.RawIOBase):
    """每次实际读 socket 都检查总截止时间,防缓慢响应占住工作线程。"""

    def __init__(self, conn, deadline: float):
        self.conn, self.deadline = conn, deadline

    def readable(self) -> bool:
        return True

    def makefile(self, mode):
        return io.BufferedReader(self)

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("callback response deadline exceeded")
        self.conn.settimeout(remaining)
        return self.conn.recv_into(buffer)


def _pinned_send(conn: ssl.SSLSocket, body: bytes, headers: dict[str, str], path: str,
                 deadline: float | None = None, with_body: bool = True) -> tuple[int, bytes]:
    lines = [f"POST {path or '/'} HTTP/1.1", f"Host: {conn.server_hostname}"]
    lines += [f"{name}: {value}" for name, value in headers.items()]
    lines += [f"Content-Length: {len(body)}", "Connection: close", "", ""]
    conn.sendall("\r\n".join(lines).encode("latin-1") + body)
    # TCP 分段和 chunked 都可能让响应头先于 challenge 到达,交给标准 HTTP 解析器。
    deadline = deadline if deadline is not None else time.monotonic() + DELIVERY_TIMEOUT
    with HTTPResponse(_DeadlineReader(conn, deadline)) as response:
        response.begin()
        if not with_body:
            return response.status, b""
        body = response.read(4097)
        if len(body) > 4096:
            raise CallbackError("response_too_large")
        return response.status, body


def _post_pinned_impl(url: str, body: bytes, headers: dict[str, str], timeout: float = DELIVERY_TIMEOUT,
                      with_body: bool = False):
    """解析 → 校验全部 IP 都是公网 → 直连校验过的 IP(TLS 校验仍针对原主机名)→ 不跟随重定向。"""
    deadline = time.monotonic() + timeout
    parts = urlsplit(url)
    host = parts.hostname or ""
    addresses = _resolve(host, 443)
    context = ssl.create_default_context()
    last_error: Exception | None = None
    for addr in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CallbackError("timeout")
        raw = socket.socket(socket.AF_INET6 if ":" in addr else socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(remaining)
        try:
            raw.connect((addr, 443))
            raw.settimeout(max(0.001, deadline - time.monotonic()))
            conn = context.wrap_socket(raw, server_hostname=host)
            try:
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                conn.settimeout(max(0.001, deadline - time.monotonic()))
                status, received = _pinned_send(conn, body, headers, path, deadline=deadline, with_body=with_body)
                return (status, received) if with_body else status
            finally:
                conn.close()
        except (OSError, ssl.SSLError) as exc:
            last_error = exc
            raw.close()
    raise CallbackError("callback_unreachable", str(last_error or "连接失败"))


_verified: dict[str, float] = {}


def forget_verified(session: str, urls) -> None:
    for url in urls:
        _verified.pop(_verify_key(session, url), None)


def _verify_key(session: str, url: str) -> str:
    return hashlib.sha256(f"{session}\n{url}".encode()).hexdigest()


async def _post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_post_pinned_impl, url, body, headers, timeout),
                                      timeout=timeout + 2)
    except asyncio.TimeoutError:
        return 0


async def verify_callback(url: str, session: str, secret: str, sub_id: str) -> None:
    """发 verification 挑战并等 2xx 回显;成功按(连接, 回调地址)缓存一段时间。"""
    key = _verify_key(session, url)
    if _verified.get(key, 0) > _now():
        return
    challenge = secrets.token_urlsafe(24)
    msg_id = "msg_" + secrets.token_hex(12)
    body = json.dumps({"type": "verification", "challenge": challenge}, separators=(",", ":")).encode()
    ts = int(_now())
    headers = {
        "Content-Type": "application/json", "webhook-id": msg_id, "webhook-timestamp": str(ts),
        "webhook-signature": sign(secret, msg_id, ts, body), SUBSCRIPTION_ID_HEADER: sub_id,
    }
    try:
        status, received = await asyncio.wait_for(
            asyncio.to_thread(_post_pinned_verbose, url, body, headers, DELIVERY_TIMEOUT),
            timeout=DELIVERY_TIMEOUT + 2)
    except asyncio.TimeoutError:
        raise CallbackError("timeout") from None
    except CallbackError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 不把可能含回调凭据的异常原文写入日志
        _LOG.info("mcp_callback_verification subscription_id=%s error=%s", sub_id, type(exc).__name__)
        raise CallbackError("challenge_failed") from None
    if not 200 <= status < 300 or not _matches_challenge(received, challenge):
        raise CallbackError("challenge_failed" if status else "callback_unreachable")
    _verified[key] = _now() + VERIFY_CACHE_SEC
    _LOG.info("mcp_callback_verified subscription_id=%s http_status=%s", sub_id, status)


def _matches_challenge(received: bytes, challenge: str) -> bool:
    try:
        payload = json.loads(received)
    except (ValueError, UnicodeError, RecursionError):
        # 兼容旧接收端的纯文本回显;ChatGPT 官方返回 JSON 对象。
        return hmac.compare_digest(received.strip(), challenge.encode())
    echoed = payload.get("challenge") if isinstance(payload, dict) else None
    if not isinstance(echoed, str):
        return False
    try:
        return hmac.compare_digest(echoed.encode(), challenge.encode())
    except UnicodeError:
        return False


async def deliver_once(url: str, secret: str, sub_id: str, event_id: str, body: bytes) -> int:
    if len(body) > BODY_SIZE_LIMIT:
        raise CallbackError("too_large")
    ts = int(_now())
    headers = {
        "Content-Type": "application/json", "webhook-id": event_id, "webhook-timestamp": str(ts),
        "webhook-signature": sign(secret, event_id, ts, body), SUBSCRIPTION_ID_HEADER: sub_id,
    }
    return await _post(url, body, headers, DELIVERY_TIMEOUT)


def _post_pinned_verbose(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    return _post_pinned_impl(url, body, headers, timeout, with_body=True)


# ---------------------------------------------------------------- 协议方法(前端进程)

def _rpc_error(code: int, message: str, data: dict | None = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "error": error}


class EventsService:
    """前端进程内的订阅管理 + 投递循环。session = 上游共享凭据的哈希(即 OAuth 连接身份)。"""

    def __init__(self, session: str):
        self.session = session
        # 进程内串行化回调验证:start/done 两个方法都会走到校验,避免重复挑战。
        self._verify_lock: dict[str, asyncio.Lock] = {}

    # --- 读
    def _active(self, where: str = "session=?", params: tuple = ()) -> list[Any]:
        conn = connect()
        try:
            purge(conn)
            return conn.execute(f"SELECT * FROM subscriptions WHERE {where} AND expires_at>?",
                                (*params, _now())).fetchall()
        finally:
            conn.close()

    # --- events/list
    def list_events(self) -> dict:
        return {"events": [EVENT_DEFINITION], "nextCursor": None}

    # --- events/subscribe
    async def subscribe(self, params: dict) -> dict:
        name = params.get("name")
        if name != EVENT_NAME:
            return _rpc_error(-32602, f"未知事件:{name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict) or args.keys() - {"task_id"}:
            return _rpc_error(-32602, "arguments 只接受 task_id")
        task_filter = args.get("task_id")
        if task_filter is not None:
            if not isinstance(task_filter, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_filter):
                return _rpc_error(-32602, "task_id 格式无效")
            if not self._owns(task_filter):
                return _rpc_error(-32004, "任务不存在或不属于本连接", {"reason": "forbidden"})
        delivery = params.get("delivery") or {}
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook":
            return _rpc_error(-32602, "只支持 delivery.mode=webhook")
        url, secret = delivery.get("url"), delivery.get("secret")
        if not valid_secret(secret):
            return _rpc_error(-32602, "secret 必须以 whsec_ 开头且 base64 解码后为 24–64 字节")
        try:
            validate_url(url)
        except CallbackError as exc:
            return _rpc_error(-32602, "回调地址不合法", {"reason": exc.reason})
        ttl_ms = delivery.get("ttlMs", params.get("ttlMs"))
        if ttl_ms is not None:
            if type(ttl_ms) is not int or not 0 <= ttl_ms <= 2**53:
                return _rpc_error(-32602, "ttlMs 必须是非负整数或 null")
            ttl = min(max(DEFAULT_TTL_MS if ttl_ms is None else ttl_ms, MIN_TTL_MS), MAX_TTL_MS)
        else:
            ttl = DEFAULT_TTL_MS
        # 按官方语义:ttlMs=null 表示请求永久有效;本服务有上限,照样返回 refreshBefore。
        sub_id = "sub_" + hashlib.sha256(
            json.dumps([self.session, url, name, {"task_id": task_filter}], sort_keys=True).encode()).hexdigest()[:32]
        conn = connect()
        try:
            purge(conn)
            count = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE session=?", (self.session,)).fetchone()[0]
            existing = conn.execute("SELECT 1 FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
            if not existing and count >= MAX_PER_SESSION:
                return _rpc_error(-32004, f"每个连接最多 {MAX_PER_SESSION} 个订阅,请先取消不用的")
            total = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
            if not existing and total >= MAX_TOTAL:
                return _rpc_error(-32004, "订阅总数已满,请稍后再试")
        finally:
            conn.close()
        sub_row = {"id": sub_id, "session": self.session, "task_id": task_filter, "url": url, "secret": secret,
                   "ttl": ttl}
        try:
            await self._verify_once(sub_row)
        except CallbackError as exc:
            return _rpc_error(-32015, "回调验证失败", {"reason": exc.reason})
        now = _now()
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO subscriptions(id,session,task_filter,url,secret,created_at,expires_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET secret=excluded.secret,expires_at=excluded.expires_at,url=excluded.url",
                (sub_id, self.session, task_filter, url, secret, now, now + ttl / 1000),
            )
            cursor = self._catch_up(conn, task_filter)
        finally:
            conn.close()
        return {"id": sub_id, "refreshBefore": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + ttl / 1000)),
                "cursor": cursor, "truncated": False}

    async def _verify_once(self, sub: dict) -> None:
        key = _verify_key(self.session, sub["url"])
        lock = self._verify_lock.get(key)
        if lock is None:
            lock = self._verify_lock[key] = asyncio.Lock()
        async with lock:
            await verify_callback(sub["url"], self.session, sub["secret"], sub["id"])

    def _owns(self, task_id: str) -> bool:
        conn = connect()
        row = conn.execute("SELECT 1 FROM brain_bridge_tasks WHERE task_id=? AND agent_id=?",
                           (task_id, _bridge_agent_id())).fetchone()
        return row is not None

    def _catch_up(self, conn, task_filter: str | None) -> str | None:
        """刚完成就订阅的竞态:库里已终态、24h 内、尚未投递过的任务,立刻补一条事件。"""
        rows = conn.execute(
            "SELECT e.event_id FROM outbox e WHERE e.session=? AND e.created_at>=? "
            "AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.event_id=e.event_id AND d.status='delivered')"
            " AND (? IS NULL OR e.task_id=?) ORDER BY e.created_at DESC LIMIT 5",
            (self.session, _now() - 86400, task_filter, task_filter),
        ).fetchall()
        cursor = rows[0]["event_id"] if rows else None
        if rows:
            conn.execute("UPDATE outbox SET next_attempt_at=? WHERE event_id IN (%s)"
                         % ",".join("?" * len(rows)), (_now(), *[r["event_id"] for r in rows]))
        return cursor

    # --- events/unsubscribe
    def unsubscribe(self, params: dict) -> dict:
        name = params.get("name")
        if name != EVENT_NAME:
            return _rpc_error(-32602, f"未知事件:{name}")
        args = params.get("arguments") or {}
        task_filter = args.get("task_id") if isinstance(args, dict) else None
        url = (params.get("delivery") or {}).get("url")
        if not isinstance(url, str):
            return _rpc_error(-32602, "缺少 delivery.url")
        conn = connect()
        try:
            conn.execute("DELETE FROM subscriptions WHERE session=? AND url=? AND task_filter IS ?",
                         (self.session, url, task_filter))
        finally:
            conn.close()
        return {}

    # --- 投递
    async def deliver_due(self, limit: int = 20) -> int:
        conn = connect()
        try:
            purge(conn)
            subs = {(r["id"]): r for r in conn.execute(
                "SELECT * FROM subscriptions WHERE session=? AND expires_at>?", (self.session, _now()))}
            if not subs:
                return 0
            events = conn.execute(
                "SELECT * FROM outbox WHERE session=? AND next_attempt_at<=? ORDER BY created_at LIMIT ?",
                (self.session, _now(), limit)).fetchall()
        finally:
            conn.close()
        sent = 0
        for event in events:
            targets = [s for s in subs.values()
                       if s["task_filter"] in (None, event["task_id"])
                       and self._pending(event["event_id"], s["id"])]
            if not targets:
                if not any(s["task_filter"] in (None, event["task_id"]) for s in subs.values()):
                    self._drop_event(event["event_id"])  # 订阅都不关心这条(已投过的不再重投)
                continue  # 还有相关订阅在等重试:留给下一轮
            body = json.dumps({"eventId": event["event_id"], "name": EVENT_NAME,
                               "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(event["created_at"])),
                               "data": json.loads(event["payload"]), "cursor": None},
                              ensure_ascii=False, separators=(",", ":")).encode()
            for sub in targets:
                await self._deliver_to(event, sub, body)
            if self._all_handled(event["event_id"], [s["id"] for s in targets]):
                self._drop_event(event["event_id"])
            else:
                sent += 1
        return sent

    def _pending(self, event_id: str, sub_id: str) -> bool:
        """还需要(继续)投递:没投过,或上次失败还能重试。已送达/已放弃的不再重试。"""
        conn = connect()
        try:
            row = conn.execute("SELECT status FROM deliveries WHERE event_id=? AND subscription_id=?",
                               (event_id, sub_id)).fetchone()
            return not row or row["status"] == "error"
        finally:
            conn.close()

    def _all_handled(self, event_id: str, sub_ids: list[str]) -> bool:
        conn = connect()
        try:
            active = conn.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE session=? AND expires_at>?", (self.session, _now())
            ).fetchone()[0]
            if not active:
                return True  # 已无订阅,事件箱里留着等下次订阅补发(7 天后回收)
            rows = conn.execute(
                "SELECT subscription_id,status FROM deliveries WHERE event_id=?", (event_id,)).fetchall()
            done = {r["subscription_id"] for r in rows if r["status"] in ("delivered", "permanent_error", "gone")}
            living = {r[0] for r in conn.execute("SELECT id FROM subscriptions WHERE session=?", (self.session,))}
            return bool(living) and living <= done
        finally:
            conn.close()

    def _drop_event(self, event_id: str) -> None:
        conn = connect()
        try:
            conn.execute("DELETE FROM outbox WHERE event_id=?", (event_id,))
            conn.execute("DELETE FROM deliveries WHERE event_id=?", (event_id,))
        finally:
            conn.close()

    async def _deliver_to(self, event: dict, sub: Any, body: bytes) -> None:
        conn = connect()
        try:
            row = conn.execute("SELECT * FROM subscriptions WHERE id=? AND expires_at>?",
                               (sub["id"], _now())).fetchone()
        finally:
            conn.close()
        if not row:
            return  # 订阅已在投递间隙到期
        attempts = int(event["attempts"]) + 1
        status, error = 0, ""
        try:
            status = await deliver_once(row["url"], row["secret"], row["id"], event["event_id"], body)
        except CallbackError as exc:
            error = exc.reason
        except Exception as exc:  # noqa: BLE001 —— 单次投递失败不影响其他订阅
            error = type(exc).__name__
        now = _now()
        if 200 <= status < 300:
            outcome = "delivered"
        elif status in (410, 413) or attempts >= MAX_ATTEMPTS:
            outcome = "gone" if status == 410 else "permanent_error"
        else:
            outcome = "error"
        error = error or ("" if outcome == "delivered" else f"http_{status}" if status else "no_2xx")
        _LOG.info("mcp_event_delivery event_id=%s subscription_id=%s outcome=%s http_status=%s attempts=%s error=%s",
                  event["event_id"], row["id"], outcome, status, attempts, error)
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO deliveries(event_id,subscription_id,status,attempts,last_error,updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(event_id,subscription_id) DO UPDATE SET "
                "status=excluded.status,attempts=excluded.attempts,last_error=excluded.last_error,"
                "updated_at=excluded.updated_at",
                (event["event_id"], row["id"], outcome, attempts, error or ("" if status else "no_2xx"), now),
            )
            if outcome == "gone":
                conn.execute("DELETE FROM subscriptions WHERE id=?", (row["id"],))
            backoff = min(300, 2 ** (attempts + 1)) * (1 + secrets.randbelow(20) / 100)
            conn.execute("UPDATE outbox SET attempts=?,next_attempt_at=?,last_error=? WHERE event_id=?",
                         (attempts, now + backoff, (error or str(status))[:200], event["event_id"]))
        finally:
            conn.close()

    async def loop(self, interval: float = 1.0) -> None:
        """常驻投递循环:轮询到期事件,失败按退避重试。"""
        while True:
            try:
                await self.deliver_due()
            except Exception as exc:  # noqa: BLE001 —— 投递循环不能因单次异常退出
                _LOG.warning("事件投递异常:%s", exc)
            await asyncio.sleep(interval)
