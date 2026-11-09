"""统一派发的文字提示与语音兼容测试；隔离存储，不启动模型或联网。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from vococo import config, instance_config
from vococo.core import dispatch, prompt, task_runner, tasks
from vococo.gateway import clarify
from vococo.memory import agents, session_store
from vococo.voice import prompts, task_tools


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def voice_env(isolated, monkeypatch):
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(task_tools, "_hinted_candidates", set())
    # 只拦执行和通知，派发、权限捕获、落库、模型选择均走真实代码。
    monkeypatch.setattr(task_runner, "_maybe_start_next", lambda: None)
    monkeypatch.setattr(task_runner, "_notify_activity", lambda *_: None)
    monkeypatch.setattr(config, "MODEL", "background-default")
    owner = agents.create("语音路由测试")
    source = f"web:p{owner['project_hash']}:voice"
    session_store.bind_session(source, agent_id=owner["id"])
    session_store.set_chosen_model(source, "foreground-model")
    token = clarify.set_current(source, SimpleNamespace(platform="voice"), 123)
    yield source, owner
    clarify.reset_current(token)
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


@pytest.fixture
def prompt_env(isolated, monkeypatch):
    monkeypatch.setattr(prompt, "_APPEND_CACHE", {})
    monkeypatch.setattr(prompt, "_load_global_agents", lambda: "")
    monkeypatch.setattr(instance_config, "text", lambda *_: "Agent/Task、run_subagent 和自动调用 delegate_to_agent")


@pytest.mark.parametrize("flag", [None, "0", "false", "1", "true", "TRUE"])
def test_prompt_uses_only_enabled_dispatch_guidance(prompt_env, monkeypatch, flag):
    monkeypatch.delenv("VOCOCO_TASK_DISPATCH_V2", raising=False)
    if flag is not None:
        monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", flag)
    out = prompt.build_system_prompt()["append"]
    voice = prompts.voice_system_prompt_extra()
    if flag and flag.lower() in {"1", "true"}:
        assert "dispatch_task" in out and "dispatch_session" in out
        assert "阻塞同步" not in out
        assert "run_subagent" not in out and "delegate_to_agent" not in out
        assert "直接调用 Agent 子代理" not in out
        assert 'dispatch_task(agent="code-reviewer")' in out
        assert "independent" in voice and "config.MODEL" in voice
    else:
        assert "阻塞同步" in out
        assert "run_subagent" in out and "delegate_to_agent" in out
        assert "语音统一派发" not in voice
    assert "voice_continue_session" in voice
    assert "检测到可能有" in voice


def test_prompt_cache_changes_with_switch(prompt_env, monkeypatch):
    # 使用正常有序缓存，以同一 SDK 会话验证开关翻转不会复用旧入口提示。
    from collections import OrderedDict
    monkeypatch.setattr(prompt, "_APPEND_CACHE", OrderedDict())
    outputs = []
    for flag in ("0", "1", "0"):
        monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", flag)
        outputs.append(prompt.build_system_prompt(cache_key="same-sdk")["append"])
    assert outputs[0] == outputs[2]
    assert outputs[0] != outputs[1]


@pytest.mark.anyio
@pytest.mark.parametrize("flag", ["0", "1"])
async def test_voice_routes_real_dispatch_without_resumable_parent(voice_env, monkeypatch, tmp_path, flag):
    source, owner = voice_env
    monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", flag)
    new_call = Mock(wraps=dispatch.dispatch)
    old_call = Mock(wraps=task_runner.dispatch)
    monkeypatch.setattr(dispatch, "dispatch", new_call)
    monkeypatch.setattr(task_runner, "dispatch", old_call)
    cwd = str(tmp_path / "project")
    out = await task_tools.voice_dispatch_task.handler({"title": "检查路由", "prompt": "完整背景", "cwd": cwd})
    assert "已派发" in out["content"][0]["text"]
    row = tasks.list_recent(1, origin="voice")[0]
    assert row["cwd"] == cwd
    assert row["prompt"] == "完整背景"
    assert row["source_session_key"] == source
    assert row["agent_id"] == owner["id"]
    assert row["dispatch_platform"] == "voice" and row["dispatch_chat_id"] == "123"
    assert row["relation"] == "independent" and row["parent_execution_id"] is None
    assert session_store.get_sdk_session_id(source) is None
    if flag == "1":
        assert session_store.get_chosen_model(row["session_key"]) == "background-default"
        assert new_call.call_args.args[:2] == (source, "independent")
        assert new_call.call_args.kwargs["model"] is None
        assert new_call.call_args.kwargs["origin"] == "voice"
        old_call.assert_not_called()
    else:
        old_call.assert_called_once()
        new_call.assert_not_called()
        assert not session_store.get_chosen_model(row["session_key"])


@pytest.mark.anyio
async def test_voice_explicit_model_is_resolved_and_not_foreground(voice_env, monkeypatch):
    monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", "1")
    monkeypatch.setattr(dispatch.providers, "available_models", lambda *_: [("worker-model", "工人", "api")])
    out = await task_tools.voice_dispatch_task.handler({"title": "指定模型", "prompt": "背景", "model": "worker-model"})
    assert "已派发" in out["content"][0]["text"]
    row = tasks.list_recent(1, origin="voice")[0]
    assert session_store.get_chosen_model(row["session_key"]) == "worker-model"


@pytest.mark.anyio
@pytest.mark.parametrize("flag", ["0", "1"])
async def test_title_continuation_guard_still_blocks_then_allows(voice_env, monkeypatch, flag):
    monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", flag)
    first = await task_tools.voice_dispatch_task.handler({"title": "检查登录", "prompt": "背景"})
    assert "已派发" in first["content"][0]["text"]
    args = {"title": "检查登录补充", "prompt": "续接背景"}
    blocked = await task_tools.voice_dispatch_task.handler(args)
    assert "voice_continue_session" in blocked["content"][0]["text"]
    assert len(tasks.list_recent(10, origin="voice")) == 1
    allowed = await task_tools.voice_dispatch_task.handler(args)
    assert "已派发" in allowed["content"][0]["text"]
    assert len(tasks.list_recent(10, origin="voice")) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["empty", "no_context", "invalid_model", "denied"])
async def test_voice_dispatch_errors_do_not_create_tasks(voice_env, monkeypatch, case):
    monkeypatch.setenv("VOCOCO_TASK_DISPATCH_V2", "1")
    args = {"title": "错误边界", "prompt": "背景"}
    if case == "empty":
        args["prompt"] = " "
    elif case == "no_context":
        monkeypatch.setattr(clarify, "current", lambda: None)
    elif case == "invalid_model":
        args["model"] = "unknown-model"
        monkeypatch.setattr(dispatch.providers, "available_models", lambda *_: [])
    else:
        session_store.start_turn(voice_env[0], "受限请求", source_access={"denied": ["mcp__vococo__dispatch_session"]})
    out = await task_tools.voice_dispatch_task.handler(args)
    assert "已派发" not in out["content"][0]["text"]
    assert not tasks.list_recent(10, origin="voice")
