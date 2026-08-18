"""重启还魂:除了发起 restart_self 的会话,其他被连坐打断的会话也要自动重发原话
(不用等用户手动回到那个会话页面),见 gateway/run.py _auto_resend_interrupted。
"""
from __future__ import annotations

import json

import pytest

from vococo import config
from vococo.gateway import core as gateway_core
from vococo.gateway.adapters.base import Incoming
from vococo.gateway.run import GatewayRunner
from vococo.memory import session_store
from vococo.tools import selfops


@pytest.fixture
def anyio_backend():
    return "asyncio"


class StubAdapter:
    platform = "web"

    def __init__(self):
        self.sent: list[tuple] = []

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    def make_sink(self, chat_id):
        return object()


@pytest.fixture
def resume_env(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(selfops, "RESUME_PATH", isolated / "resume_task.json")
    yield


@pytest.mark.anyio
async def test_other_interrupted_sessions_auto_resent_after_self_restart(resume_env, monkeypatch):
    calls = []

    async def fake_converse(key, text, model, sink, **kw):
        calls.append((key, text))

    monkeypatch.setattr(gateway_core, "converse", fake_converse)

    initiator_key = config.resolve_session_key("web", "initiator")
    other_key = config.resolve_session_key("web", "other")

    # 发起重启的会话:本轮已经完整落库(finish_turn),不算中断
    it = session_store.start_turn(initiator_key, "改代码并重启")
    session_store.finish_turn(it, "已改完,准备重启")

    # 被连坐打断的另一个会话:进程退出时正卡在这轮
    session_store.start_turn(other_key, "帮我查一下天气")

    recovered = session_store.recover_interrupted_turns()
    assert len(recovered) == 1
    assert recovered[0]["session_key"] == other_key

    task_data = {
        "platform": "web",
        "chat_id": "initiator",
        "session_key": initiator_key,
        "reason": "test",
        "verify_plan": "确认没崩",
        "rollback_commit": "deadbeef",
    }
    selfops.RESUME_PATH.write_text(json.dumps(task_data), encoding="utf-8")

    adapter = StubAdapter()
    runner = GatewayRunner(adapters=[adapter])
    runner._recovered_interrupted = recovered

    async def fast_sleep(_):
        return None

    monkeypatch.setattr("vococo.gateway.run.anyio.sleep", fast_sleep)
    await runner._resume_after_restart()

    # other 会话:被删掉的中断占位轮 + 原话重新入队(fake_converse 记到了同一句话)
    assert (other_key, "帮我查一下天气") in calls
    # 发起会话只走了还魂验证那一条路径(调用一次,且不是被当成"其他会话"又重发了
    # 原话"改代码并重启"——那样就是被 _auto_resend_interrupted 重复处理了)
    initiator_calls = [c for c in calls if c[0] == initiator_key]
    assert len(initiator_calls) == 1
    assert initiator_calls[0][1] != "改代码并重启"


@pytest.mark.anyio
async def test_stale_turn_id_skipped_without_crash(resume_env, monkeypatch):
    """重发前该轮已经不是"最新一轮"(比如用户已经手动处理过)—— 跳过,不炸。"""
    async def fake_converse(key, text, model, sink, **kw):
        pass

    monkeypatch.setattr(gateway_core, "converse", fake_converse)

    key = config.resolve_session_key("web", "race")
    session_store.start_turn(key, "第一句")
    recovered = session_store.recover_interrupted_turns()
    assert len(recovered) == 1

    # 模拟"重发前用户已经手动又发了一条,占了'最新一轮'的位置"
    session_store.start_turn(key, "第二句(用户手动重发的)")

    adapter = StubAdapter()
    runner = GatewayRunner(adapters=[adapter])
    runner._recovered_interrupted = recovered
    # 直接调用内部方法,跳过还魂/遗书部分
    await runner._auto_resend_interrupted(exclude_key=None)
    # 不应该误删用户手动发的那一轮
    assert len(session_store.load_history(key)) == 2
