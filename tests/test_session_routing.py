"""跨会话工具必须进入真实 SDK 配置；原生通信入口硬拦并指明替代工具。"""
import pytest

from vococo.core import prompt
from vococo.tools import builtin, danger, sessions


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["ListAgents", "SendMessage"])
@pytest.mark.parametrize("guard_enabled", [True, False])
async def test_native_peer_tools_redirect_even_when_guards_disabled(monkeypatch, name, guard_enabled):
    monkeypatch.setattr(danger.config, "DANGER_GUARD", guard_enabled)
    result = await danger.pretool_guard_hook({"tool_name": name, "tool_input": {}}, "call", {})
    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "list_sessions" in output["permissionDecisionReason"]
    assert "continue_session" in output["permissionDecisionReason"]
    assert "打开" not in output["permissionDecisionReason"]


def test_bound_session_tools_are_registered_in_builtin_server(monkeypatch):
    captured = {}
    monkeypatch.setattr(builtin, "create_sdk_mcp_server", lambda name, tools: captured.update(name=name, tools=tools) or {})
    bound = sessions.build_tools("web:example", [])
    builtin.build_mcp_servers(session_tools=bound)
    assert all(t in captured["tools"] for t in bound)
    assert {t.name for t in bound} == {"list_sessions", "query_session", "continue_session"}


def test_prompt_distinguishes_saved_chats_from_cli_peers():
    for name in ("list_sessions", "query_session", "continue_session", "ListAgents", "SendMessage"):
        assert name in prompt.PERSONA
    assert "不需要先打开" in prompt.PERSONA
