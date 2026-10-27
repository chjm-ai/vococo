"""MCP Events:协议发现、订阅校验、回调验证、签名投递、过滤、去重、持久化与竞态补发。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from unittest.mock import Mock

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from vococo import config
from vococo.core import task_events, task_runner, tasks
from vococo.gateway import brain_mcp, brain_routes, mcp_events, settings_store
from vococo.memory import agents

pytestmark = pytest.mark.anyio
UPSTREAM = "public-test-upstream-" + "u" * 40
BRIDGE = "private-test-bridge-" + "b" * 40
PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Receiver:
    """模拟 ChatGPT 的 webhook 接收端。发送方在工作线程里同步调用它,所以这里直接记账 +
    按 Standard Webhooks 规则验签(secret 由订阅时登记的「订阅 id → secret」表提供)。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.status = 200
        self.reply_challenge = True
        self.secrets: dict[str, str] = {}

    def handle(self, url: str, body: bytes, headers: dict[str, str], timeout: float,
               with_body: bool = False):
        low = {k.lower(): v for k, v in headers.items()}
        record = {"headers": low, "body": body, "json": json.loads(body)}
        self.calls.append(record)
        secret = self.secrets.get(low.get(mcp_events.SUBSCRIPTION_ID_HEADER.lower(), ""), "")
        signed = mcp_events.verify(secret, low["webhook-id"], low["webhook-timestamp"],
                                   low["webhook-signature"], body)
        if record["json"].get("type") == "verification":
            status, reply = 200, (record["json"]["challenge"].encode() if self.reply_challenge and signed else b"nope")
            return (status, reply) if with_body else status
        return self.status if not with_body else (self.status, b"ok")

    def events(self) -> list[dict]:
        return [c for c in self.calls if c["json"].get("type") != "verification"]


@pytest.fixture
async def env(isolated, monkeypatch):
    """回调地址用公网名(校验通过),真实连接在固定网络函数处改指向本机测试接收端。"""
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(config, "CRON_JOBS_PATH", isolated / "cron.json")
    monkeypatch.setattr(settings_store, "_PATH", isolated / "settings.json")
    monkeypatch.setattr(task_runner, "_maybe_start_next", Mock())
    monkeypatch.setenv("VOCOCO_MCP_UPSTREAM_TOKEN", UPSTREAM)
    monkeypatch.setenv("VOCOCO_BRAIN_BRIDGE_TOKEN", BRIDGE)
    monkeypatch.setenv("VOCOCO_BRAIN_AGENT_ID", brain_routes.AGENT_ID)
    # 事件箱与任务/桥登记放同一个库:桥在写事务里登记,另一个连接读会等锁。
    monkeypatch.setenv("VOCOCO_MCP_EVENTS_DB", str(config.DATA_DIR / "voice" / "voice.db"))
    mcp_events._verified.clear()  # 验证缓存在进程级,用例之间不共享
    workdir = isolated / "project"
    workdir.mkdir()
    agents._write_meta(brain_routes.AGENT_ID, {"name": "开发", "workdir": str(workdir),
                                               "execution_mode": "project"})
    app = web.Application()
    brain_routes.register_routes(app)
    async with TestServer(app) as backend:
        server = brain_mcp.create_server(bridge_port=backend.port)
        frontend = brain_mcp.create_app(server)
        frontend.bridge_port = backend.port  # 官方客户端连的是异步 ASGI 传输,不能让工具再拨 http
        receiver = Receiver()
        monkeypatch.setattr(mcp_events, "_post_pinned_impl", receiver.handle)
        mcp_events.register_terminal_hook()  # 与 gateway 启动时一致:终态钩子直接写事件箱
        original_verify = mcp_events.verify_callback

        async def verify_and_remember(url, session, secret, sub_id):
            """订阅时订阅方本来就知道 secret:测试接收端照真客户端那样存起来,用于验签回显。"""
            receiver.secrets[sub_id] = secret
            await original_verify(url, session, secret, sub_id)

        monkeypatch.setattr(mcp_events, "verify_callback", verify_and_remember)
        yield backend, frontend, receiver, "https://chatgpt.example.com/hook", None


@pytest.fixture
def public_dns(monkeypatch, env):
    """只把域名解析固定成公网 IP;拨号由 env 里替换掉的 _post_pinned_impl 送给接收端。"""
    monkeypatch.setattr(mcp_events, "_resolve", lambda host, port: [PUBLIC_IP])
    return env[2]


def frontend_service() -> mcp_events.EventsService:
    """前端进程里用的服务实例(会话身份 = 上游共享凭据的哈希)。"""
    return mcp_events.EventsService(hashlib.sha256(UPSTREAM.encode()).hexdigest())


async def rpc(frontend, method: str, params: dict | None = None, extra: dict | None = None,
              token: str = UPSTREAM) -> dict:
    """发一次 JSON-RPC。桥层方法(initialize / discover / events/*)直接驱动 ASGI app:
    既不走星形 ASGITransport 的同步拨号,也不碰 httpx 的系统代理。"""
    request = {"jsonrpc": "2.0", "id": 7, "method": method, "params": {**(params or {}), **(extra or {})}}
    return await _asgi_call(frontend, request, token)


async def _asgi_call(app, request: dict, token: str) -> dict:
    scope = {"type": "http", "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/mcp",
             "raw_path": b"/mcp", "query_string": b"", "root_path": "",
             "headers": [(b"authorization", f"Bearer {token}".encode())],
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8852)}
    incoming = [{"type": "http.request", "body": json.dumps(request).encode(), "more_body": False}]

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return json.loads(payload) if payload else {}


def dispatch(frontend, key: str = "request-0001") -> dict:
    """派单落库 + 登记到桥表(与桥的 _create_task 同形;工具/协议链路由 test_brain_mcp 覆盖)。

    测试进程里异步 ASGI 客户端与 aiohttp 服务端的同步拨号会互等,所以不经过网络。
    """
    from vococo.gateway import brain_routes

    agent = agents.get(brain_routes.AGENT_ID)
    access = brain_routes._access(agent)
    access["connection"] = hashlib.sha256(UPSTREAM.encode()).hexdigest()  # 生产由桥按请求凭据写入
    task = tasks.create("查资料", "整理资料", cwd=agent["workdir"], cwd_explicit=True, origin="chat",
                        dispatch_platform="web", dispatch_chat_id=agent["main_conv"], agent_id=brain_routes.AGENT_ID,
                        source_session_key=access["session"], source_access=access)
    connection = brain_routes._registry()
    connection.execute(
        "INSERT INTO brain_bridge_tasks(task_id,agent_id,idempotency_key,payload,source_session_key) VALUES (?,?,?,?,?)",
        (task["id"], brain_routes.AGENT_ID, key, json.dumps(["查资料", "整理资料"]), access["session"]))
    connection.commit()
    return task


def finish(task_id: str, status: str = "done", summary: str = "已整理完 3 份资料") -> None:
    """模拟执行器走到终态(queued → running → 终态)。"""
    assert tasks.set_status(task_id, "running"), (tasks.get(task_id), tasks.get(task_id) and tasks._conn().in_transaction)
    if status == "done":
        assert tasks.finish(task_id, "done", "完整结果:" + summary * 20, summary)
    else:
        assert tasks.finish(task_id, "failed", "", summary)


async def test_discover_lists_events(env):
    _backend, frontend, _receiver, _url, _port = env
    discovered = await rpc(frontend, "server/discover")
    result = discovered["result"]
    assert mcp_events.PROTOCOL_VERSION in result["supportedVersions"]
    assert "events" in result["capabilities"]
    listed = (await rpc(frontend, "events/list"))["result"]
    event = listed["events"][0]
    assert event["name"] == mcp_events.EVENT_NAME and event["delivery"] == ["webhook"]
    assert set(event["payloadSchema"]["required"]) == {"task_id", "status", "title", "summary"}


async def test_discover_and_events_require_auth(env):
    _backend, frontend, _receiver, _url, _port = env
    for method in ("server/discover", "events/list"):
        result = await rpc(frontend, method, token="wrong-token-" + "x" * 40)
        assert "result" not in result


async def test_subscribe_verifies_callback_and_delivers_signed(env, public_dns):
    backend, frontend, receiver, url, _port = env
    secret = mcp_events.new_secret()
    subscribed = await rpc(frontend, "events/subscribe",
                           {"name": mcp_events.EVENT_NAME, "arguments": {}, "cursor": None,
                            "delivery": {"mode": "webhook", "url": url, "secret": secret}})
    assert "result" in subscribed and subscribed["result"]["id"].startswith("sub_")
    assert receiver.calls and receiver.calls[0]["json"]["type"] == "verification"
    assert receiver.calls[0]["headers"]["x-mcp-subscription-id"] == subscribed["result"]["id"]
    assert mcp_events.verify(secret, receiver.calls[0]["headers"]["webhook-id"],
                             receiver.calls[0]["headers"]["webhook-timestamp"],
                             receiver.calls[0]["headers"]["webhook-signature"], receiver.calls[0]["body"])

    task = dispatch(frontend)
    finish(task["id"])
    if not mcp_events.connect().execute("SELECT 1 FROM outbox").fetchone():
        mcp_events.record_terminal(tasks.get(task["id"]))  # 钩子跑在线程池的任务里
    await frontend_service().deliver_due()
    events = receiver.events()
    assert len(events) == 1
    event = events[0]
    assert event["headers"]["webhook-id"] == event["json"]["eventId"]
    assert event["json"]["name"] == mcp_events.EVENT_NAME
    assert event["json"]["data"] == {"task_id": task["id"], "status": "done", "title": "查资料",
                                     "summary": "已整理完 3 份资料"}
    assert mcp_events.verify(secret, event["headers"]["webhook-id"], event["headers"]["webhook-timestamp"],
                             event["headers"]["webhook-signature"], event["body"])
    # 已投递 → 出箱,再跑一遍不会重复
    await frontend_service().deliver_due()
    assert len(receiver.events()) == 1


async def test_rejects_bad_secret_and_internal_urls(env, monkeypatch):
    _backend, frontend, _receiver, url, _port = env
    bad_secret = await rpc(frontend, "events/subscribe",
                           {"name": mcp_events.EVENT_NAME, "arguments": {},
                            "delivery": {"mode": "webhook", "url": url, "secret": "short"}})
    assert bad_secret["error"]["code"] == -32602
    monkeypatch.setattr(mcp_events.socket, "getaddrinfo", lambda host, *a, **k: [
        (2, 1, 6, "", (host, 443))])  # 解析结果 = 主机名本身:IP 字面量/内网主机自然被拦
    for target in ("http://example.com/hook", "https://127.0.0.1/hook", "https://localhost/hook",
                   "https://10.0.0.8/hook", "https://169.254.169.254/hook", "https://user:pw@example.com/hook",
                   "https://example.com:8443/hook", "https://example.com/hook#frag"):
        result = await rpc(frontend, "events/subscribe",
                           {"name": mcp_events.EVENT_NAME, "arguments": {},
                            "delivery": {"mode": "webhook", "url": target, "secret": mcp_events.new_secret()}})
        assert "error" in result, target


def test_url_validation_blocks_private_dns():
    """真实解析函数:域名的任一解析结果落在内网/保留段即拒绝(防 DNS 指向内网)。"""
    with pytest.MonkeyPatch.context() as patch, pytest.raises(mcp_events.CallbackError) as error:
        patch.setattr(mcp_events.socket, "getaddrinfo", lambda *args, **kwargs: [
            (2, 1, 6, "", ("127.0.0.1", 443)), (2, 1, 6, "", (PUBLIC_IP, 443))])
        mcp_events.validate_url("https://example.com/hook")
    assert error.value.reason == "blocked_address"


async def test_challenge_failure_refuses_subscription(env, public_dns):
    _backend, frontend, receiver, url, _port = env
    mcp_events.forget_verified(frontend_service().session, url)  # 别吃到其他用例的验证缓存
    receiver.reply_challenge = False
    result = await rpc(frontend, "events/subscribe",
                       {"name": mcp_events.EVENT_NAME, "arguments": {},
                        "delivery": {"mode": "webhook", "url": url, "secret": mcp_events.new_secret()}})
    assert result["error"]["code"] == -32015
    assert result["error"]["data"]["reason"] == "challenge_failed"
    conn = mcp_events.connect()
    assert conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0


async def test_task_filter_and_forbidden_foreign_task(env, public_dns):
    backend, frontend, receiver, url, _port = env
    mine = dispatch(frontend, "request-mine")
    other = dispatch(frontend, "request-other")
    foreign = await rpc(frontend, "events/subscribe",
                        {"name": mcp_events.EVENT_NAME, "arguments": {"task_id": "notmine0"},
                         "delivery": {"mode": "webhook", "url": url, "secret": mcp_events.new_secret()}})
    assert foreign["error"]["code"] == -32004
    await rpc(frontend, "events/subscribe",
              {"name": mcp_events.EVENT_NAME, "arguments": {"task_id": mine["id"]},
               "delivery": {"mode": "webhook", "url": url, "secret": mcp_events.new_secret()}})
    finish(mine["id"])
    finish(other["id"])
    mcp_events.scan_once()
    await frontend_service().deliver_due()
    delivered = [e["json"]["data"]["task_id"] for e in receiver.events()]
    assert delivered == [mine["id"]]


async def test_terminal_once_and_retry_then_dedupe(env, public_dns):
    backend, frontend, receiver, url, _port = env
    await rpc(frontend, "events/subscribe",
              {"name": mcp_events.EVENT_NAME, "arguments": {},
               "delivery": {"mode": "webhook", "url": url, "secret": mcp_events.new_secret()}})
    task = dispatch(frontend)
    finish(task["id"])  # 终态钩子直接把事件写进事件箱
    mcp_events.record_terminal(tasks.get(task["id"]))  # 同一终态再次触发
    mcp_events.scan_once()  # 扫描兜底也再来一遍
    assert len(mcp_events.connect().execute("SELECT * FROM outbox").fetchall()) == 1

    receiver.status = 500
    await frontend_service().deliver_due()
    assert len(receiver.events()) == 1  # 第一次失败(同一 eventId)
    conn = mcp_events.connect()
    conn.execute("UPDATE outbox SET next_attempt_at=0")
    receiver.status = 200
    await frontend_service().deliver_due()
    events = receiver.events()
    assert len(events) == 2 and events[0]["json"]["eventId"] == events[1]["json"]["eventId"]
    assert mcp_events.connect().execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


async def test_subscription_survives_restart(env, public_dns):
    _backend, frontend, _receiver, url, _port = env
    subscribed = await rpc(frontend, "events/subscribe",
                           {"name": mcp_events.EVENT_NAME, "arguments": {},
                            "delivery": {"mode": "webhook", "url": url, "secret": mcp_events.new_secret()}})
    sub_id = subscribed["result"]["id"]
    # 模拟新进程:服务实例重新创建、完全不持有旧内存状态,订阅仍在库里
    conn = mcp_events.connect()
    assert conn.execute("SELECT id FROM subscriptions WHERE id=?", (sub_id,)).fetchone()["id"] == sub_id
    assert frontend_service().list_events()["events"][0]["name"] == mcp_events.EVENT_NAME


async def test_unsubscribe_and_expiry(env, public_dns):
    backend, frontend, receiver, url, _port = env
    secret = mcp_events.new_secret()
    sub = await rpc(frontend, "events/subscribe",
                    {"name": mcp_events.EVENT_NAME, "arguments": {},
                     "delivery": {"mode": "webhook", "url": url, "secret": secret}})
    unsub = await rpc(frontend, "events/unsubscribe",
                      {"name": mcp_events.EVENT_NAME, "arguments": {},
                       "delivery": {"mode": "webhook", "url": url}})
    assert unsub["result"] == {}
    assert mcp_events.connect().execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0
    # 重复退订幂等
    assert (await rpc(frontend, "events/unsubscribe",
                      {"name": mcp_events.EVENT_NAME, "arguments": {},
                       "delivery": {"mode": "webhook", "url": url}}))["result"] == {}

    sub2 = await rpc(frontend, "events/subscribe",
                     {"name": mcp_events.EVENT_NAME, "arguments": {}, "ttlMs": 60_000,
                      "delivery": {"mode": "webhook", "url": url, "secret": secret}})
    assert sub2["result"]["refreshBefore"]
    assert sub2["result"]["id"] == sub["result"]["id"]  # 相同身份幂等刷新
    mcp_events.connect().execute("UPDATE subscriptions SET expires_at=?", (time.time() - 1,))
    task = dispatch(frontend)
    finish(task["id"])
    mcp_events.scan_once()
    await frontend_service().deliver_due()
    assert receiver.events() == []


async def test_catch_up_after_immediate_subscribe(env, public_dns):
    """竞态:任务已终态、事件箱已有记录,用户这时才订阅 → 订阅回执带 cursor,并立即补发。"""
    backend, frontend, receiver, url, _port = env
    task = dispatch(frontend)
    finish(task["id"])
    mcp_events.scan_once()  # 订阅前终态已经入箱
    subscribed = await rpc(frontend, "events/subscribe",
                           {"name": mcp_events.EVENT_NAME, "arguments": {"task_id": task["id"]},
                            "delivery": {"mode": "webhook", "url": url, "secret": mcp_events.new_secret()}})
    assert subscribed["result"]["cursor"]
    await frontend_service().deliver_due()
    assert [e["json"]["data"]["task_id"] for e in receiver.events()] == [task["id"]]


async def test_queued_cancel_and_other_connection_isolated(env, public_dns):
    backend, frontend, receiver, url, _port = env
    other_service = mcp_events.EventsService("another-connection")
    other_service.list_events()
    await rpc(frontend, "events/subscribe",
              {"name": mcp_events.EVENT_NAME, "arguments": {},
               "delivery": {"mode": "webhook", "url": url, "secret": mcp_events.new_secret()}})
    task = dispatch(frontend)
    assert tasks.set_status(task["id"], "cancelled")  # 排队中直接取消
    assert mcp_events.scan_once() == 1  # 取消不经过 finish,由扫描兜底补写
    await other_service.deliver_due()  # 别的连接看不到这的事件
    assert receiver.events() == []
    await frontend_service().deliver_due()
    assert [e["json"]["data"]["status"] for e in receiver.events()] == ["cancelled"]


async def test_terminal_hook_writes_outbox(env):
    """终态处理器链:emit_terminal 触发注册的事件钩子并落箱(不靠手工调用)。"""
    backend, frontend, _receiver, _url, _port = env
    task = dispatch(frontend)
    finish(task["id"], "failed", "编译失败")
    await task_events.emit_terminal(task["id"])  # 幂等:钩子已在 finish 时写过一次
    rows = mcp_events.connect().execute("SELECT payload FROM outbox WHERE task_id=?", (task["id"],)).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["summary"] == "编译失败"


async def test_only_bridge_tasks_emit(env):
    _backend, frontend, _receiver, _url, _port = env
    local = tasks.create("本地任务", "prompt")  # 不是经远程桥派生
    assert tasks.set_status(local["id"], "running")
    assert tasks.finish(local["id"], "done", "本地结果", "本地结果")
    await task_events.emit_terminal(local["id"])
    assert mcp_events.connect().execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
