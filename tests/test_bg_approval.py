"""后台任务审批:推到 Web 任务会话等批 / 超时与免打扰进待批队列 / 待批队列处理接口。"""
from __future__ import annotations

import anyio
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.gateway import clarify
from vococo.tools import danger
from vococo.tools.danger import pretool_guard_hook

TOOL = "mcp__lemlist_lite__send_email"  # 可存规则的外部写操作,且无人值守默认拒绝


class _FakeAdapter:
    def __init__(self):
        self.choice = None
        self.chat_id = None

    async def present_choice(self, chat_id, choice):
        self.chat_id = chat_id
        self.choice = choice


@pytest.fixture
def bg(isolated, monkeypatch):
    """登记一个假的后台审批通道,关掉免打扰。"""
    adapter = _FakeAdapter()
    monkeypatch.setattr(clarify, "_bg_adapter", adapter)
    monkeypatch.setattr(config, "BG_APPROVAL_QUIET", "")
    danger._one_shot.clear()
    yield adapter
    danger._bg_pending.clear()


def _run_bg(key: str, click: str | None, adapter: _FakeAdapter | None = None):
    """以后台任务身份跑一次 hook;click 为 None 表示不点(等超时)。"""

    async def scenario():
        tok = danger.set_task_session(key)
        out: dict = {}
        try:
            async with anyio.create_task_group() as tg:

                async def run():
                    out["v"] = await pretool_guard_hook(
                        {"tool_name": TOOL, "tool_input": {"to": "a@b.c"}}, None, {}
                    )

                tg.start_soon(run)
                if click is not None and adapter is not None:
                    for _ in range(200):
                        if adapter.choice is not None or "v" in out:
                            break
                        await anyio.sleep(0.005)
                    if adapter.choice is not None:
                        assert danger.pending_background_choices()  # 等待期间可补弹
                        cmd = next(c for c, lab in adapter.choice.options if click in lab)
                        clarify.resolve_button(cmd.split()[1], cmd.split()[2])
        finally:
            danger.reset_task_session(tok)
            clarify.clear_session(key)
            danger.clear_session_approvals(key)
        return out["v"]

    return anyio.run(scenario)


def test_bg_forever_saves_rule(bg):
    from vococo.memory import approvals

    out = _run_bg("task:t1", "永远允许", bg)
    assert out == {}
    assert bg.chat_id == "task:t1"  # 弹到任务会话
    assert any("本轮任务都允许" in lab for _c, lab in bg.choice.options)
    assert approvals.list_rules()[0]["scope"] == TOOL
    assert danger.pending_background_choices() == []  # 答完就清掉


def test_bg_timeout_goes_to_missed_queue(bg, monkeypatch):
    from vococo.memory import approvals

    monkeypatch.setattr(config, "BG_APPROVAL_WAIT_SEC", 0.05)
    out = _run_bg("task:t2", None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "待批队列" in out["hookSpecificOutput"]["permissionDecisionReason"]
    missed = approvals.list_missed()
    assert len(missed) == 1 and missed[0]["session_key"] == "task:t2"
    assert missed[0]["rule_kind"] == "mcp_tool"
    assert approvals.list_audit()[0]["decision"] == "timeout"
    # 同一轮再来一个要批的操作:不再等,直接进队列(直接设 contextvar,不走 set_task_session
    # ——后者是「新一轮开头」,会清掉超时标记)
    bg.choice = None
    monkeypatch.setattr(config, "BG_APPROVAL_WAIT_SEC", 30)

    async def same_round():
        tok = danger._task_session_var.set("task:t2")
        try:
            return await pretool_guard_hook(
                {"tool_name": TOOL, "tool_input": {"to": "x@y.z"}}, None, {}
            )
        finally:
            danger._task_session_var.reset(tok)

    out2 = anyio.run(same_round)
    assert out2["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert bg.choice is None
    assert approvals.list_audit()[0]["decision"] == "deferred"


def test_bg_quiet_hours_defers_without_prompt(bg, monkeypatch):
    from vococo.memory import approvals

    monkeypatch.setattr(danger, "_in_quiet_hours", lambda hour=None: True)
    out = _run_bg("task:t3", None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert bg.choice is None  # 免打扰:不弹
    assert approvals.list_audit()[0]["decision"] == "deferred"
    assert len(approvals.list_missed()) == 1


def test_bg_one_shot_grant_used_once(bg, monkeypatch):
    monkeypatch.setattr(danger, "_in_quiet_hours", lambda hour=None: True)
    danger.grant_once("task:t4", "外部写")
    danger._one_shot["task:t4"] = {danger._category(
        f"{TOOL} 是外部写操作(会实际发送/修改数据)")}
    assert _run_bg("task:t4", None) == {}  # 第一次用掉许可
    out = _run_bg("task:t4", None)  # 第二次又要批(免打扰 → 进队列)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bg_local_op_still_allowed_without_prompt(bg):
    """本地操作(rm -r 子目录等 restrict=False)在后台照旧放行,不弹窗。"""

    async def scenario():
        tok = danger.set_task_session("task:t5")
        try:
            return await pretool_guard_hook(
                {"tool_name": "Bash", "tool_input": {"command": "rm -rf ./build"}}, None, {}
            )
        finally:
            danger.reset_task_session(tok)

    assert anyio.run(scenario) == {}
    assert bg.choice is None


def test_quiet_hours_parsing(monkeypatch):
    monkeypatch.setattr(config, "BG_APPROVAL_QUIET", "23-8")
    assert danger._in_quiet_hours(23) and danger._in_quiet_hours(3)
    assert not danger._in_quiet_hours(8) and not danger._in_quiet_hours(15)
    monkeypatch.setattr(config, "BG_APPROVAL_QUIET", "1-5")
    assert danger._in_quiet_hours(2) and not danger._in_quiet_hours(6)
    monkeypatch.setattr(config, "BG_APPROVAL_QUIET", "")
    assert not danger._in_quiet_hours(3)


# ── 待批队列接口 ─────────────────────────────────────────────────────────
@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def sec_app(isolated, monkeypatch):
    from vococo.gateway.adapters.web import WebAdapter

    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([
        web.get("/security", adapter._handle_security),
        web.post("/security/missed", adapter._handle_security_missed),
        web.post("/security/rule/delete", adapter._handle_security_rule_delete),
    ])
    return app


def _add_missed(key: str, rule: bool = True) -> str:
    from vococo.memory import approvals

    return approvals.add_missed(
        session_key=key, tool=TOOL, reason="外部写", detail=TOOL,
        rule_kind="mcp_tool" if rule else None, rule_scope=TOOL if rule else None,
        rule_label="以后调用 send_email 不再询问" if rule else None,
    )


@pytest.mark.anyio
async def test_missed_forever_adds_rule_and_reruns(sec_app, monkeypatch):
    from vococo.core import task_runner, tasks
    from vococo.memory import approvals

    calls = []

    async def fake_append(task_id, instruction):
        calls.append((task_id, instruction))
        return {"ok": True, "message": "ok"}

    monkeypatch.setattr(task_runner, "append", fake_append)
    monkeypatch.setattr(tasks, "get", lambda tid: {"id": tid})
    mid = _add_missed("task:abc")
    async with TestClient(TestServer(sec_app)) as client:
        resp = await client.post("/security/missed", json={"id": mid, "action": "forever"})
        assert resp.status == 200
        data = await (await client.get("/security")).json()
    assert calls and calls[0][0] == "abc" and "已获主人批准" in calls[0][1]
    assert [r["scope"] for r in data["rules"]] == [TOOL]
    assert data["missed"] == []


@pytest.mark.anyio
async def test_missed_dismiss_and_once(sec_app, monkeypatch):
    from vococo.core import task_runner, tasks

    async def fake_append(task_id, instruction):
        return {"ok": True, "message": "ok"}

    monkeypatch.setattr(task_runner, "append", fake_append)
    monkeypatch.setattr(tasks, "get", lambda tid: {"id": tid})
    danger._one_shot.clear()
    m1, m2 = _add_missed("task:d1"), _add_missed("task:d2", rule=False)
    async with TestClient(TestServer(sec_app)) as client:
        assert (await client.post("/security/missed", json={"id": m1, "action": "dismiss"})).status == 200
        # 不支持永远允许的条目点 forever → 400
        assert (await client.post("/security/missed", json={"id": m2, "action": "forever"})).status == 400
        assert (await client.post("/security/missed", json={"id": m2, "action": "once"})).status == 200
        # 已处理过的再点 → 404
        assert (await client.post("/security/missed", json={"id": m2, "action": "once"})).status == 404
    assert danger._one_shot.get("task:d2")
