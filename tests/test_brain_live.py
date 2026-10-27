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
