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
import json

import pytest

from claude_agent_sdk import ResultMessage, StreamEvent, SystemMessage

from vococo import config
from vococo.core import agent, client_pool
from vococo.core.agent import Done


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
        self.prompts = []
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

    async def query(self, prompt, **kwargs):
        self.prompts.append(prompt if isinstance(prompt, str) else [msg async for msg in prompt])
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
    monkeypatch.setattr(
        agent.providers, "effort_levels_for_model", lambda *a: ("low", "medium", "high", "xhigh", "max")
    )
    monkeypatch.setattr(agent.settings_store, "get_web_effort", lambda *a: "")
    monkeypatch.setattr(agent.settings_store, "vococo_enabled", lambda: False)
    monkeypatch.setattr(agent.settings_store, "effective_external_mcp", lambda: {})
    monkeypatch.setattr(agent.settings_store, "effective_skills", lambda cwd=None: None)
    monkeypatch.setattr(
        agent, "build_system_prompt", lambda cwd=None, cache_key=None: {"append": "p"}
    )
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
    assert clients[0].options.max_buffer_size == agent._SDK_MAX_BUFFER_SIZE
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
async def test_effort_change_rebuilds_warm_client(clients, monkeypatch):
    """思考深度在 connect 时固定，切换后必须丢弃保温 client，下一轮才会真生效。"""
    await _turn()
    monkeypatch.setattr(agent.settings_store, "get_web_effort", lambda *a: "xhigh")
    await _turn(resume="sid-1")

    assert len(clients) == 2
    assert clients[0].disconnected
    assert clients[1].options.effort == "xhigh"


@pytest.mark.anyio
async def test_claude_alias_effort_and_pool_identity(clients, monkeypatch):
    upstream = "claude-sonnet-5-5"
    first = f"cheapcoding-sonnet/{upstream}"
    second = f"another-sonnet/{upstream}"
    env = {"ANTHROPIC_BASE_URL": "https://cheapcoding.cc", "ANTHROPIC_API_KEY": "test",
           "CLAUDE_CODE_OAUTH_TOKEN": "", "DISABLE_PROMPT_CACHING": "1"}
    monkeypatch.setattr(agent.providers, "resolve", lambda chosen, default: (upstream, env if chosen != upstream else {}))
    saved = {upstream: "low", first: "max", second: "max"}
    monkeypatch.setattr(agent.settings_store, "get_web_effort", lambda model: saved[model])
    for selection in (upstream, first, second):
        async for _ in agent.stream_turn([], "hi", model=selection, session_key="web:alias", resume="sid-1"):
            pass
    assert len(clients) == 3
    assert [client.options.model for client in clients] == [upstream] * 3
    assert [client.options.effort for client in clients] == ["low", "max", "max"]
    assert clients[0].options.env["DISABLE_PROMPT_CACHING"] == ""
    assert clients[1].options.env["DISABLE_PROMPT_CACHING"] == "1"
    assert clients[0].disconnected and clients[1].disconnected


@pytest.mark.anyio
async def test_title_sidecar_uses_cache_policy_without_leaking_to_official(monkeypatch):
    import claude_agent_sdk
    from vococo.core import title
    seen = []
    async def query(*, prompt, options):
        seen.append(options)
        yield _result_msg("title")
    monkeypatch.setattr(claude_agent_sdk, "query", query)
    monkeypatch.setenv("DISABLE_PROMPT_CACHING", "1")
    await title._ask("标题", "claude-sonnet-5-5", {"DISABLE_PROMPT_CACHING": "1"})
    await title._ask("标题", "claude-haiku-4-5", {})
    assert seen[0].env["DISABLE_PROMPT_CACHING"] == "1"
    assert seen[1].env["DISABLE_PROMPT_CACHING"] == ""


def test_turn_env_clears_inherited_cache_policy(monkeypatch):
    monkeypatch.setenv("DISABLE_PROMPT_CACHING", "1")
    assert agent._turn_env({})["DISABLE_PROMPT_CACHING"] == ""
    assert agent._turn_env({"ANTHROPIC_API_KEY": "other"})["DISABLE_PROMPT_CACHING"] == ""
    assert agent._turn_env({"DISABLE_PROMPT_CACHING": "1"})["DISABLE_PROMPT_CACHING"] == "1"


async def _turn_with_effort(clients, monkeypatch, effort: str):
    monkeypatch.setattr(agent.providers, "resolve", lambda *a: ("deepseek-flash", {"K": "v"}))
    monkeypatch.setattr(agent.providers, "effort_levels_for_model", lambda *a: (effort,))
    monkeypatch.setattr(agent.settings_store, "get_web_effort", lambda *a: effort)
    await _turn()
    return clients[0].options


@pytest.mark.anyio
async def test_effort_off_disables_thinking_via_extra_body(clients, monkeypatch):
    """档位「关闭」:effort 留空,改由 CLI 的请求体合并口子关思考。

    SDK 的 thinking 参数对第三方模型会被 CLI 丢掉(见 agent.py 注释),所以这里断言
    的是环境变量而不是 options.thinking。
    """
    options = await _turn_with_effort(clients, monkeypatch, "off")

    assert options.effort is None
    assert json.loads(options.env["CLAUDE_CODE_EXTRA_BODY"]) == {
        "thinking": {"type": "disabled"}
    }


@pytest.mark.anyio
async def test_effort_ultra_goes_through_extra_body(clients, monkeypatch):
    """ultra 不在 CLI 的 --effort 白名单里,同样只能注入请求体。"""
    options = await _turn_with_effort(clients, monkeypatch, "ultra")

    assert options.effort is None
    assert json.loads(options.env["CLAUDE_CODE_EXTRA_BODY"]) == {
        "output_config": {"effort": "ultra"}
    }


@pytest.mark.anyio
async def test_effort_normal_level_passes_effort_only(clients, monkeypatch):
    """普通档位照旧走 --effort,不注入请求体——别把所有档位都改成注入。"""
    options = await _turn_with_effort(clients, monkeypatch, "xhigh")

    assert options.effort == "xhigh"
    assert "CLAUDE_CODE_EXTRA_BODY" not in options.env


@pytest.mark.anyio
async def test_effort_unset_keeps_both_untouched(clients):
    """没选档位(=auto)时 effort 与请求体都不动,行为与改造前一致。"""
    await _turn()

    assert clients[0].options.effort is None
    assert "CLAUDE_CODE_EXTRA_BODY" not in clients[0].options.env


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
    from vococo.gateway import clarify

    class _A:
        platform = "cli"

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


class CompactFakeClient(FakeClient):
    """安全网 /compact 场景:主轮收工后上下文超阈值 → stream_turn 追发 query("/compact");
    真机顺序(2026-07-22 事故)是 compact_boundary 先到、/compact 自己的 ResultMessage
    后到。用队列驱动消息流,query 时才入队,跟真 CLI 的行为轮廓一致。"""

    def __init__(self, options=None, *, registry: list):
        super().__init__(options, registry=registry)
        self.pending = asyncio.Queue()
        self.main_queries = 0

    async def query(self, prompt=None, *args, **kwargs):
        self.queries += 1
        if isinstance(prompt, str) and prompt.strip() == "/compact":
            self.pending.put_nowait(
                SystemMessage(subtype="compact_boundary", data={"compact_metadata": {"trigger": "auto"}})
            )
            self.pending.put_nowait(_result_msg(self.sid))
            return
        self.main_queries += 1
        self.pending.put_nowait(
            StreamEvent(
                uuid=str(self.main_queries),
                session_id=self.sid,
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": f"回复{self.main_queries}"},
                },
            )
        )
        self.pending.put_nowait(_result_msg(self.sid))

    def receive_messages(self):
        async def gen():
            while True:
                yield await self.pending.get()

        return gen()

    async def get_context_usage(self):
        # 190k/200k ≈ 95%,必超 83% 阈值 → 每轮都触发安全网 /compact
        return {"totalTokens": 190_000, "rawMaxTokens": 200_000}


@pytest.mark.anyio
async def test_safety_net_compact_drains_result_before_checkin(clients, monkeypatch):
    """回归 2026-07-22 语音「每轮回的都是上一轮的答案」事故:安全网 /compact 的
    drain 见到 compact_boundary 就 break,把 /compact 自己的 ResultMessage 留在流里
    回池 → 下一轮 query 后第一条读到旧 Result,0 文本瞬间收工,回复从此错一位。
    修复后必须把 /compact 读到它的 ResultMessage 为止再回池。"""
    made: list[FakeClient] = []
    monkeypatch.setattr(
        agent, "ClaudeSDKClient", lambda options=None: CompactFakeClient(options, registry=made)
    )

    r1 = await _turn()
    assert r1.text == "回复1"
    fake = made[0]
    # 修复的核心断言:回池前流必须读干净,不能留下 /compact 的 ResultMessage
    assert fake.pending.qsize() == 0
    assert "web:pool-test" in client_pool._pool

    # 第二轮命中保温池:拿到的必须是本轮的回答,不是空文本/上一轮残留
    r2 = await _turn(resume="sid-1")
    assert len(made) == 1
    assert r2.text == "回复2"
    assert fake.pending.qsize() == 0


@pytest.mark.anyio
async def test_agent_lists_override_skills_and_mcp(clients, monkeypatch):
    """会话所属 Agent 配了名单:技能只用名单里的(插件 skill 照挂),MCP 名单里的每轮都挂。"""
    monkeypatch.setattr(agent.agents, "runtime_for_session",
                        lambda key: {"skills": ["pdf"], "mcp": ["lemlist"]})
    monkeypatch.setattr(agent.settings_store, "effective_external_mcp",
                        lambda names=None: {n: {"type": "stdio", "command": "x"} for n in names or ()})
    monkeypatch.setattr(agent.session_store, "get_external_mcp_names", lambda key: set())
    await _turn()
    opts = clients[0].options
    assert opts.skills == ["pdf", *agent._PLUGIN_SKILLS]
    assert set(opts.mcp_servers) == {"lemlist"}  # 用户说的只是「hi」,也照样挂


@pytest.mark.anyio
@pytest.mark.parametrize("selected,resolved,supported", [
    ("gpt-6.1-sol", "gpt-6.1-sol", True),
    ("自定义显示名", "GPT-6-astra", True),
    ("gpt-looking-label", "claude-sonnet-5-5", False),
    ("deepseek-flash", "deepseek-flash", False),
])
@pytest.mark.parametrize("verbosity", ["", "low", "medium", "high"])
async def test_native_verbosity_uses_resolved_model_without_prompt_injection(
    clients, isolated, monkeypatch, selected, resolved, supported, verbosity,
):
    monkeypatch.setattr(agent.providers, "resolve", lambda *a: (resolved, {"ANTHROPIC_API_KEY": "fake"}))
    monkeypatch.setattr(agent.settings_store, "get_web_verbosity", lambda *a: verbosity)
    monkeypatch.setattr(agent.settings_store, "get_web_summary", lambda *a: "")
    async for _ in agent.stream_turn([], "解释一下", model=selected, session_key="web:native-verbosity"):
        pass
    client = clients[0]
    assert client.prompts == ["解释一下"]
    assert "[本轮表达提醒" not in str(client.options.system_prompt)
    if supported and verbosity:
        assert json.loads(client.options.env["CLAUDE_CODE_EXTRA_BODY"]) == {"text": {"verbosity": verbosity}}
    else:
        assert "CLAUDE_CODE_EXTRA_BODY" not in client.options.env


@pytest.mark.anyio
@pytest.mark.parametrize("verbosity", ["", "low", "medium", "high"])
async def test_attachment_retry_keeps_native_controls_without_prompt_injection(clients, isolated, monkeypatch, verbosity):
    monkeypatch.setattr(agent.providers, "resolve", lambda *a: ("gpt-6.1-sol", {"ANTHROPIC_API_KEY": "fake"}))
    monkeypatch.setattr(agent.settings_store, "get_web_verbosity", lambda *a: verbosity)
    monkeypatch.setattr(agent.settings_store, "get_web_summary", lambda *a: "concise")
    monkeypatch.setattr(agent.settings_store, "get_web_effort", lambda *a: "high")
    files = [agent.FileAttachment(b"hello", "text/plain", "test.txt")]
    await _turn()
    clients[0].fail_next_query = True
    async for _ in agent.stream_turn([], "看看附件", files=files, session_key="web:pool-test", resume="sid-1"):
        pass
    assert len(clients) == 2 and clients[0].disconnected
    assert clients[0].prompts[-1] == clients[1].prompts[0]
    content = clients[1].prompts[0][0]["message"]["content"]
    assert content == [{"type": "text", "text": "看看附件"},
                       {"type": "text", "text": "[文件附件: test.txt]\nhello"}]
    for client in clients:
        assert "[本轮表达提醒" not in str(client.prompts)
        assert client.options.effort == "high"
        expected = {"reasoning": {"summary": "concise"}}
        if verbosity:
            expected["text"] = {"verbosity": verbosity}
        assert json.loads(client.options.env["CLAUDE_CODE_EXTRA_BODY"]) == expected


@pytest.mark.anyio
async def test_original_text_unchanged_on_warm_or_retry(clients, isolated, monkeypatch):
    monkeypatch.setattr(agent.providers, "resolve", lambda *a: ("gpt-6.1-sol", {}))
    await _turn()
    await _turn(resume="sid-1")
    assert len(clients) == 1
    clients[0].fail_next_query = True
    await _turn(resume="sid-1")
    assert len(clients) == 2
    for client in clients:
        for payload in client.prompts:
            assert payload == "hi"
            assert "[本轮表达提醒" not in payload


@pytest.mark.anyio
async def test_resume_failure_restores_history_without_prompt_injection(clients, isolated, monkeypatch):
    monkeypatch.setattr(agent.providers, "resolve", lambda *a: ("gpt-6.1-sol", {}))

    def make_client(options=None):
        client = FakeClient(options, registry=clients)
        client.fail_next_query = bool(options.resume)
        return client

    monkeypatch.setattr(agent, "ClaudeSDKClient", make_client)
    history = [agent.Turn(user="历史问题", assistant="历史回复")]
    async for _ in agent.stream_turn(history, "继续", resume="missing-sid"):
        pass
    assert len(clients) == 2
    assert clients[0].prompts == ["继续"]
    assert clients[1].prompts == [agent._compose_prompt(history, "继续")]
    assert clients[1].prompts[0].count("历史问题") == 1
    assert all("[本轮表达提醒" not in client.prompts[0] for client in clients)
    assert history == [agent.Turn(user="历史问题", assistant="历史回复")]


@pytest.mark.anyio
@pytest.mark.parametrize("compact,research", [(True, False), (False, True)])
async def test_compaction_and_isolated_research_keep_original_payload(clients, isolated, monkeypatch, compact, research):
    from vococo.tools.danger import build_hooks

    monkeypatch.setattr(agent, "build_hooks", build_hooks)
    monkeypatch.setattr(agent.providers, "resolve", lambda *a: ("gpt-6.1-sol", {}))
    async for _ in agent.stream_turn(
        [], "完整研究任务", model="gpt-6.1-sol", compact_only=compact,
        tool_guard=(lambda *a: None) if research else None,
    ):
        pass
    assert clients[0].prompts == ["/compact" if compact else "完整研究任务"]


@pytest.mark.anyio
async def test_model_switch_isolates_sdk_session_and_restores_original_history(clients, isolated, monkeypatch):
    from vococo.gateway import core
    from vococo.memory import session_store

    monkeypatch.setattr(agent.providers, "resolve", lambda chosen, default: (chosen or default, {}))
    key = "web:style-switch"
    session_store.set_chosen_model(key, "gpt-6.1-sol")
    await core.converse(key, "原始问题", "gpt-6.1-sol", core.Sink(),
                        cwd_override=str(isolated), is_explicit_project_override=False)
    assert clients[0].prompts == ["原始问题"]
    assert session_store.load_recent(key)[0].user == "原始问题"
    session_store.set_chosen_model(key, "claude-sonnet-5-5")
    await core.converse(key, "继续", "claude-sonnet-5-5", core.Sink(),
                        cwd_override=str(isolated), is_explicit_project_override=False)
    assert len(clients) == 2 and clients[0].disconnected
    assert clients[1].options.resume is None
    assert "原始问题" in clients[1].prompts[0]
    assert "[本轮表达提醒" not in clients[1].prompts[0]


@pytest.mark.anyio
async def test_verbosity_change_reconnects_and_keeps_effort(clients, monkeypatch, tmp_path):
    monkeypatch.setattr(agent.settings_store, "_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(agent.providers, "resolve", lambda *a: (
        "gpt-6.1-sol", {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_API_KEY": "fake"}))
    monkeypatch.setattr(agent.settings_store, "get_web_effort", lambda *a: "high")
    selected = {"value": "low"}
    monkeypatch.setattr(agent.settings_store, "get_web_verbosity", lambda *a: selected["value"])
    await _turn()
    first = clients[0]
    assert json.loads(first.options.env["CLAUDE_CODE_EXTRA_BODY"])["text"]["verbosity"] == "low"
    assert first.options.effort == "high"
    selected["value"] = "high"
    await _turn(resume="sid-1")
    assert len(clients) == 2 and first.disconnected
    assert json.loads(clients[1].options.env["CLAUDE_CODE_EXTRA_BODY"])["text"]["verbosity"] == "high"
    assert clients[1].prompts == ["hi"]
    selected["value"] = ""
    await _turn(resume="sid-1")
    assert len(clients) == 3
    assert "CLAUDE_CODE_EXTRA_BODY" not in clients[2].options.env


@pytest.mark.anyio
async def test_summary_change_reconnects_without_changing_effort_or_verbosity(clients, monkeypatch, tmp_path):
    monkeypatch.setattr(agent.settings_store, '_PATH', tmp_path / 'settings.json')
    monkeypatch.setattr(agent.providers, 'resolve', lambda *a: (
        'gpt-6.1-sol', {'ANTHROPIC_BASE_URL':'http://127.0.0.1:8317','ANTHROPIC_API_KEY':'fake'}))
    monkeypatch.setattr(agent.settings_store, 'get_web_effort', lambda *a: 'high')
    agent.settings_store.set_web_verbosity('low', model=config.MODEL)
    agent.settings_store.set_web_summary('concise', model=config.MODEL)
    await _turn()
    assert clients[0].options.effort == 'high'
    body = json.loads(clients[0].options.env['CLAUDE_CODE_EXTRA_BODY'])
    assert body == {'text':{'verbosity':'low'},'reasoning':{'summary':'concise'}}
    agent.settings_store.set_web_summary('none', model=config.MODEL)
    await _turn(resume='sid-1')
    assert len(clients) == 2 and clients[0].disconnected
    assert clients[1].options.effort == 'high'
    body = json.loads(clients[1].options.env['CLAUDE_CODE_EXTRA_BODY'])
    assert body == {'text':{'verbosity':'low'},'reasoning':{'summary':None}}
