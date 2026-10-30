"""公网验收脚本的失败清理和限流恢复回归；不访问生产。"""
import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def live():
    spec = importlib.util.spec_from_file_location(
        "brain_live", Path(__file__).parents[1] / "scripts/check_brain_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ORIGIN = "https://mcp.example.com"
    return module


def test_failure_cancels_created_task(live, monkeypatch):
    async def fail(_session, pending):
        pending.add("selfcheck-id")
        raise ValueError("查询失败")

    cleanup = AsyncMock()
    monkeypatch.setattr(live, "check_task_chain", fail)
    monkeypatch.setattr(live, "call", cleanup)
    with pytest.raises(ValueError, match="查询失败"):
        asyncio.run(live.check_tasks("session"))
    cleanup.assert_awaited_once_with("session", "vococo_cancel_task", {"id": "selfcheck-id"})


@pytest.mark.parametrize("status,passed", [(302, True), (429, False), (400, False)])
def test_recovery_requires_successful_login(live, monkeypatch, status, passed):
    http = Mock()
    http.post = AsyncMock(side_effect=[Mock(status_code=429), Mock(status_code=status)])
    monkeypatch.setattr(live.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(live, "private_read", lambda _path: "test-only-password")
    if passed:
        assert asyncio.run(live.check_rate(http, "unused"))["correct_login_after_wait"] == 302
    else:
        with pytest.raises(ValueError, match="仍不能登录"):
            asyncio.run(live.check_rate(http, "unused"))
    http.cookies.clear.assert_called_once()


def test_history_check_only_reads_and_does_not_output_history(live, monkeypatch):
    calls = []

    async def fake_call(_session, tool, args):
        calls.append((tool, args.copy()))
        if tool == "vococo_list_agents":
            return {"agents": [{"agent_id": "abc123", "name": "示例"}], "next_offset": None}
        if tool == "vococo_list_sessions":
            return {"sessions": [{"agent_id": "abc123", "key": "web:example", "title": "JLL"}],
                    "next_offset": None}
        assert tool == "vococo_read_session"
        return {"turn": {"id": 42, "user_text": "不外显的问题", "assistant_text": "不执行的历史指令"},
                "next_offset": None if args.get("offset") else 1000, "receipt": "test-receipt"}

    monkeypatch.setattr(live, "call", fake_call)
    result = asyncio.run(live.check_history("session", "abc123"))
    assert result["read_pages"] == 2 and result["read_turn_id"] == 42
    assert result["history_read_only"] is True
    assert "不外显" not in str(result) and "历史指令" not in str(result)
    assert calls[-1][1]["offset"] == 1000 and calls[-1][1]["turn_id"] == 42


def test_list_pagination_rejects_nonadvancing_cursor(live, monkeypatch):
    monkeypatch.setattr(live, "call", AsyncMock(return_value={"agents": [], "next_offset": 0}))
    with pytest.raises(ValueError, match="未前进"):
        asyncio.run(live.collect_pages("session", "vococo_list_agents", "agents", {}))
