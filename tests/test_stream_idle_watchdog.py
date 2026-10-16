"""静默看门狗:等上游时长时间零消息 → 中断本轮并明确报错,而不是无限「思考中」。

用户现象(2026-10-15 实测):一条连到本机代理的连接挂死 26 分钟,期间没有任何消息,
连 CLI 自带的流空闲看门狗都没触发;前端一直是「思考中」,人分不清是在等还是已经死了。

修法见 core/agent.py `_stream_once`:主轮 ResultMessage 到来前,若当前没有工具在跑,
等一条消息超过 config.STREAM_IDLE_TIMEOUT(默认 600s,为什么不是更短见 config 注释)
就判连接挂死——中断 + 按错误收尾,由 describe_llm_error 渲染成一句完整说明。

边界同样重要,这里逐条钉住:
- 工具在跑(长命令/抓取)静默是正常的,不许杀;
- 上游重试风暴每条 api_retry 都重新开表(重试期间"有动静"),不许杀;
- 但子代理在跑不等于免检:Agent 调用本身不进「在跑」集,子代理自己调的工具才记账。
"""
from __future__ import annotations

import asyncio

import pytest

from claude_agent_sdk import (
    ResultMessage,
    StreamEvent,
    SystemMessage,
    ToolResultBlock,
    UserMessage,
)

from vococo.core import agent
from vococo.core.agent import Done, UpstreamRetry, describe_llm_error


def _result_msg() -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, session_id="sid-1",
        is_error=False, num_turns=1, usage={"input_tokens": 5, "output_tokens": 1},
    )


def _tool_use(tid: str, name: str = "Bash") -> StreamEvent:
    return StreamEvent(
        uuid="u", session_id="sid-1",
        event={"type": "content_block_start", "index": 0,
               "content_block": {"type": "tool_use", "id": tid, "name": name}},
    )


def _tool_result(tid: str) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(tool_use_id=tid, content="ok", is_error=False)])


def _retry_msg() -> SystemMessage:
    """上游报错、CLI 自己在退避重试(stdout 形态,见 tests/test_upstream_retry.py)。"""
    return SystemMessage(subtype="api_retry", data={
        "attempt": 1, "max_retries": 10, "retry_delay_ms": 1000,
        "error_status": 503, "error": "server_error",
    })


def _init_msg() -> SystemMessage:
    """开局的 init:带着 session_id(判死的轮次没有 ResultMessage,sess_id 只能从这来,
    而回池闸门要求 sess_id 非空 —— 见 test_idle_kill_does_not_pool_client)。"""
    return SystemMessage(subtype="init", data={"session_id": "sid-1"})


class ScriptClient:
    """按脚本吐消息。脚本里的数字 = 在这条消息【之前】静默这么久(第 2 条起就是两条
    消息之间的空档);脚本跑完就永远等下去 —— 模拟连接挂死。"""

    def __init__(self, options=None, script=None):
        self.script = list(script or [])
        self.interrupted = False

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt=None, *args, **kwargs) -> None:
        return None

    def receive_messages(self):
        async def gen():
            for item in self.script:
                if isinstance(item, (int, float)):
                    await asyncio.sleep(item)
                else:
                    yield item
            await asyncio.Event().wait()  # 永不返回

        return gen()

    async def interrupt(self) -> None:
        self.interrupted = True

    async def get_context_usage(self):
        return {"totalTokens": 100, "rawMaxTokens": 200_000}


IDLE = 0.15  # 测试里的静默上限(真实默认 600s,见 config.STREAM_IDLE_TIMEOUT)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def run_turn(monkeypatch):
    """装假 CLI 并跑一轮;返回 (client, events)。"""

    async def _run(script, session_key=None):
        client = ScriptClient(script=script)
        monkeypatch.setattr(agent, "ClaudeSDKClient", lambda options=None: client)
        monkeypatch.setattr(agent.providers, "resolve", lambda *a: ("claude-sonnet-4", {}))
        monkeypatch.setattr(agent.settings_store, "vococo_enabled", lambda: False)
        monkeypatch.setattr(agent.settings_store, "effective_external_mcp", lambda: {})
        monkeypatch.setattr(agent.settings_store, "effective_skills", lambda cwd=None: None)
        monkeypatch.setattr(
            agent, "build_system_prompt", lambda cwd=None, cache_key=None: {"append": "p"}
        )
        monkeypatch.setattr(agent, "build_mcp_servers", lambda: {})
        monkeypatch.setattr(agent, "build_hooks", lambda: {})
        monkeypatch.setattr(agent.config, "STREAM_IDLE_TIMEOUT", IDLE)
        events = [ev async for ev in agent.stream_turn([], "hi", session_key=session_key)]
        return client, events

    return _run


def _done(events) -> Done:
    return [e for e in events if isinstance(e, Done)][0]


@pytest.mark.anyio
async def test_silence_kills_turn(run_turn):
    """全程一个字都没有 → 按挂死收尾:中断 CLI + is_error + 一句能看懂的说明。"""
    client, events = await run_turn([])

    done = _done(events)
    assert done.reply.is_error
    assert done.reply.error.startswith("上游静默")
    assert client.interrupted, "判挂死必须真的去中断 CLI 子进程,不能只是自己收工"
    assert done.reply.api_error_status is None  # 不是上游报的错,是我们自己判的挂死
    # 判死之后不再向它要上下文占用(control 请求同样可能石沉大海,别又挂在那儿)
    assert done.reply.context_tokens == 0

    shown = describe_llm_error(None, done.reply.error)
    assert shown.startswith("⚠️ 上游静默")
    assert "出了点问题" not in shown, "这句话本身就是完整解释,不该套泛化前缀"
    assert "重发" in shown, "得告诉人下一步怎么办"


@pytest.mark.anyio
async def test_idle_kill_does_not_pool_client(run_turn, isolated, monkeypatch):
    """判死的 client 不许回池:半死的流被下一轮复用,等于把挂死传染下去。

    两组对照,缺一不可:光测「idle 后 checkin=0」是空断言——回池闸门是
    `pooling and clean_finish and sess_id`,夹具里 sess_id 常常是空串,分支根本到不了,
    把 clean_finish 写反也照样通过(评审第一版就踩了这个坑)。所以先跑一组正常收工,
    确认这条闸门在本夹具下真能 checkin=1。
    """
    calls = {"checkin": 0, "discard": 0}

    async def fake_checkout(*a, **k):
        return None

    async def fake_checkin(*a, **k):
        calls["checkin"] += 1

    async def fake_discard(client):
        calls["discard"] += 1

    monkeypatch.setattr(agent.client_pool, "enabled", lambda: True)
    monkeypatch.setattr(agent.client_pool, "checkout", fake_checkout)
    monkeypatch.setattr(agent.client_pool, "checkin", fake_checkin)
    monkeypatch.setattr(agent.client_pool, "discard", fake_discard)

    # 对照组:正常收工 → 必须真回池(证明闸门够得着)
    await run_turn([_init_msg(), _result_msg()], session_key="web:ok")
    assert calls["checkin"] == 1

    # 实验组:静默判死 → 不许回池,而且要走 discard 收尸
    client, events = await run_turn([_init_msg()], session_key="web:idle")
    assert _done(events).reply.is_error
    assert client.interrupted
    assert calls["checkin"] == 1, "判死的这一轮不该再 +1"
    assert calls["discard"] == 1, "没回池就必须走 discard 收尸,不能什么都不做"


@pytest.mark.anyio
async def test_tool_running_is_not_silence(run_turn):
    """工具在跑时长时间没消息是正常的(长命令/长抓取):不许杀。"""
    client, events = await run_turn(
        [_tool_use("t1"), IDLE * 3, _tool_result("t1"), _result_msg()]
    )

    done = _done(events)
    assert not done.reply.is_error
    assert not client.interrupted


@pytest.mark.anyio
async def test_silence_after_tool_result_still_kills(run_turn):
    """工具回来了、上游又不出声 → 照样算挂死(不能因为刚跑过工具就免检)。"""
    client, events = await run_turn([_tool_use("t1"), _tool_result("t1")])

    done = _done(events)
    assert done.reply.is_error
    assert done.reply.error.startswith("上游静默")
    assert client.interrupted


@pytest.mark.anyio
async def test_subagent_wait_is_still_guarded(run_turn):
    """子代理在跑 ≠ 免检:Agent 调用要等整个子代理跑完才回结果,记进「在跑」集等于
    子代理期间全程免检——所以它被排除,子代理自己调的工具才按各自的 id 记账。"""
    client, events = await run_turn([_tool_use("a1", name="Agent")])

    done = _done(events)
    assert done.reply.is_error
    assert done.reply.error.startswith("上游静默")
    assert client.interrupted


@pytest.mark.anyio
async def test_retry_storm_never_trips_watchdog(run_turn):
    """重试风暴不误杀:每条 api_retry 都算"有动静"。单段静默都远小于阈值,
    累计(4×0.3+0.3=1.5 倍阈值)却超过阈值——钉的就是"静默不累计"。"""
    script = []
    for _ in range(4):
        script += [IDLE * 0.3, _retry_msg()]
    script += [IDLE * 0.3, _result_msg()]

    client, events = await run_turn(script)

    done = _done(events)
    assert not done.reply.is_error
    assert not client.interrupted
    # 重试照常透传(两件事互不干扰:看门狗负责"没动静",重试事件负责"有动静且看得见")
    assert len([e for e in events if isinstance(e, UpstreamRetry)]) == 4


@pytest.mark.anyio
async def test_long_silence_after_retry_still_kills(run_turn):
    """重试事件只续一次命:那次退避过去之后仍然一个字都没有 → 照样判挂死。"""
    client, events = await run_turn([_retry_msg(), IDLE * 2])

    done = _done(events)
    assert done.reply.is_error
    assert done.reply.error.startswith("上游静默")
    assert client.interrupted
    assert [e for e in events if isinstance(e, UpstreamRetry)], "那条重试提示本身该照常透传"
