"""保温池(会话级常驻 ClaudeSDKClient)—— 多轮维持优化 P1-1。

核心承诺:
- 同会话第二轮命中池 → 不新建 client,直接在活 client 上 query(零冷启动);
- 兼容性哈希(模型/供应商/路由…)或 SDK 会话 id 一变 → 关旧建新(cc-switch
  「改完下轮生效」与 /new 语义都靠这个);
- 保温 client 半途坏死 → 自动降级冷启动,对话不中断(三级降级链);
- 取消 / 收工不干净 → 不回池(残留半截消息会污染下一轮);
- TTL 过期回收、close_all 全清(serve 停止不留孤儿 CLI 进程)。
"""
from __future__ import annotations

import asyncio

import pytest

from claude_agent_sdk import ResultMessage, StreamEvent

from claude_hermes import config
from claude_hermes.core import agent, client_pool
from claude_hermes.core.agent import Done


def _result_msg(sid: str) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=sid,
        usage={
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    )


class FakeClient:
    """可连续多轮 query 的假 SDK client(流式输入模式的行为轮廓)。"""

    def __init__(self, options=None, *, registry: list):
        self.options = options
        self.queries = 0
        self.connected = False
        self.disconnected = False
        self.interrupted = False
        self.sid = "sid-1"
        self.fail_next_query = False  # 模拟进程半途坏死
        self.hang_stream = False  # 模拟吐一个字后永久挂起(供取消用例)
        registry.append(self)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def query(self, *args, **kwargs):
        if self.fail_next_query:
            raise RuntimeError("CLI 子进程已死")
        self.queries += 1

    def receive_messages(self):
        async def gen():
            yield StreamEvent(
                uuid="1",
                session_id=self.sid,
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": f"回复{self.queries}"},
                },
            )
            if self.hang_stream:
                await asyncio.Event().wait()
            yield _result_msg(self.sid)

        return gen()

    async def get_context_usage(self):
        return {"totalTokens": 100, "rawMaxTokens": 200_000}

    async def interrupt(self):
        self.interrupted = True


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _empty_pool():
    client_pool._pool.clear()
    yield
    client_pool._pool.clear()


@pytest.fixture
def clients(monkeypatch):
    """打桩 stream_turn 的全部外部依赖,返回 FakeClient 实例登记表。"""
    made: list[FakeClient] = []
    monkeypatch.setattr(
        agent, "ClaudeSDKClient", lambda options=None: FakeClient(options, registry=made)
    )
    monkeypatch.setattr(agent.providers, "resolve", lambda *a: ("claude-sonnet-4", {}))
    monkeypatch.setattr(agent.settings_store, "hermes_enabled", lambda: False)
    monkeypatch.setattr(agent.settings_store, "effective_external_mcp", lambda: {})
    monkeypatch.setattr(agent.settings_store, "effective_skills", lambda: None)
    monkeypatch.setattr(agent, "build_system_prompt", lambda cwd=None: {"append": "p"})
    monkeypatch.setattr(agent, "build_mcp_servers", lambda: {})
    monkeypatch.setattr(agent, "build_hooks", lambda: {})
    return made


async def _turn(session_key="web:pool-test", resume=None):
    """跑一轮,返回最终回复。"""
    reply = None
    async for ev in agent.stream_turn([], "hi", session_key=session_key, resume=resume):
        if isinstance(ev, Done):
            reply = ev.reply
    assert reply is not None
    return reply


@pytest.mark.anyio
async def test_warm_hit_reuses_client(clients):
    """第二轮命中保温池:不新建 client,同一 client 上第二次 query。"""
    r1 = await _turn()
    assert r1.sdk_session_id == "sid-1"
    assert len(clients) == 1 and clients[0].queries == 1
    assert "web:pool-test" in client_pool._pool  # 冷启动收工后已入池

    r2 = await _turn(resume="sid-1")
    assert len(clients) == 1  # 零冷启动:没有新建 client
    assert clients[0].queries == 2
    assert not clients[0].disconnected
    assert r2.sdk_session_id == "sid-1"
    assert "web:pool-test" in client_pool._pool  # 用完放回


@pytest.mark.anyio
async def test_compat_change_rebuilds(clients, monkeypatch):
    """cc-switch 换模型 → 兼容性哈希变 → 关旧 client 重建(改完下轮生效)。"""
    await _turn()
    monkeypatch.setattr(agent.providers, "resolve", lambda *a: ("deepseek-chat", {"K": "v"}))
    await _turn(resume="sid-1")
    assert len(clients) == 2  # 新建了
    assert clients[0].disconnected  # 旧的被关
    assert clients[1].queries == 1


@pytest.mark.anyio
async def test_new_session_not_hit(clients):
    """/new 清掉 sdk_session_id → resume 为空与池内活跃 sid 对不上 → 不命中。"""
    await _turn()
    await _turn(resume=None)  # 模拟 /new 后首轮
    assert len(clients) == 2
    assert clients[0].disconnected


@pytest.mark.anyio
async def test_dead_warm_client_falls_back_cold(clients):
    """保温 client 半途坏死且未吐内容 → 自动降级冷启动,对话不中断。"""
    await _turn()
    clients[0].fail_next_query = True
    r = await _turn(resume="sid-1")
    assert len(clients) == 2  # 降级新建
    assert clients[0].disconnected  # 死 client 被收尸
    assert r.text  # 这一轮照常有回复
    assert client_pool._pool["web:pool-test"].client is clients[1]


@pytest.mark.anyio
async def test_cancel_discards_not_pooled(clients):
    """取消的轮次:interrupt 通知 CLI 停止,client 不回池(残留消息会污染下一轮)。"""
    await _turn()
    clients[0].hang_stream = True

    async def consume():
        async for _ev in agent.stream_turn(
            [], "hi", session_key="web:pool-test", resume="sid-1"
        ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)  # 让流进入挂起点
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert clients[0].interrupted
    assert clients[0].disconnected
    assert client_pool._pool == {}  # 没回池


@pytest.mark.anyio
async def test_route_change_rebuilds(clients):
    """clarify 路由(统一会话换入口)一变 → 重建 client。

    SDK 内部任务在 connect 时快照 contextvar,复用的前提是路由快照仍与当前轮
    一致 —— 否则审批弹窗会发回旧入口,故路由进兼容性哈希。
    """
    from claude_hermes.gateway import clarify

    class _A:
        platform = "telegram"

    class _B:
        platform = "web"

    tok = clarify.set_current("web:pool-test", _A(), 1)
    try:
        await _turn()
    finally:
        clarify.reset_current(tok)
    tok = clarify.set_current("web:pool-test", _B(), "conv")
    try:
        await _turn(resume="sid-1")
    finally:
        clarify.reset_current(tok)
    assert len(clients) == 2
    assert clients[0].disconnected


@pytest.mark.anyio
async def test_ttl_expiry_and_close_all(clients):
    """空闲超时的条目被回收;close_all 清光全池。"""
    await _turn()
    entry = client_pool._pool["web:pool-test"]
    entry.last_used -= config.CLIENT_POOL_IDLE_TTL + 1  # 人为过期
    got = await client_pool.checkout(
        "web:pool-test", entry.base_key, entry.live_sid
    )
    assert got is None  # 过期不命中
    assert clients[0].disconnected  # 且已被收尸

    await _turn()  # 再养一个
    assert client_pool._pool
    await client_pool.close_all()
    assert client_pool._pool == {}
    assert clients[-1].disconnected


@pytest.mark.anyio
async def test_pool_disabled_by_ttl_zero(clients, monkeypatch):
    """CLIENT_POOL_IDLE_TTL=0 → 保温池整体禁用,回到每轮冷启动的老路径。"""
    monkeypatch.setattr(config, "CLIENT_POOL_IDLE_TTL", 0)
    await _turn()
    assert client_pool._pool == {}  # 不入池
    assert clients[0].disconnected  # 轮末即关
    await _turn(resume="sid-1")
    assert len(clients) == 2  # 每轮新建
