"""铃铛:待处理的提问/审批(memory/notices.py + gateway/notice_actions.py + 相关接口)。"""
from __future__ import annotations

import datetime

import anyio
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.gateway import clarify, notice_actions
from vococo.memory import notices
from vococo.tools import danger


@pytest.fixture
def resumed(isolated, monkeypatch):
    """把「发回原会话」换成记录,不真的跑任务/注入网页。"""
    calls: list[tuple[str, str]] = []

    async def fake_resume(session_key, text):
        calls.append((session_key, text))
        return "ok"

    monkeypatch.setattr(notice_actions, "resume", fake_resume)
    danger._one_shot.clear()
    danger._next_round.clear()
    return calls


def _expired_ask(key="web:p1:c1", options=("A", "B")) -> str:
    return notices.add(session_key=key, kind="ask", prompt="选哪个?", options=list(options),
                       clarify_id="cid1", status="expired")


def _expired_approval(key="task:t9", rule=True) -> str:
    return notices.add(
        session_key=key, kind="approval", prompt="⚠️ 需要批准", status="expired",
        options=["允许一次", "本轮任务都允许"] + (["永远允许"] if rule else []) + ["拒绝"],
        reason="外部写", detail="send_email", tool="mcp__lemlist_lite__send_email",
        rule=("mcp_tool", "mcp__lemlist_lite__send_email", "以后调用 send_email 不再询问") if rule else None,
    )


# ── 生命周期 ────────────────────────────────────────────────────────────
def test_clarify_timeout_and_answer_update_notice(isolated):
    async def scenario():
        p = clarify.register("web:x", ["是", "否"], notice={"kind": "ask", "prompt": "要不要?"})
        assert notices.get(p.notice_id)["status"] == "pending"
        assert await clarify.wait(p.clarify_id, 0.01) is None
        assert notices.get(p.notice_id)["status"] == "expired"  # 超时 → 仍在铃铛里

        p2 = clarify.register("web:x", ["是", "否"], notice={"kind": "ask", "prompt": "再问"})
        clarify.resolve_button(p2.clarify_id, "0")
        assert await clarify.wait(p2.clarify_id, 1) == "是"
        n = notices.get(p2.notice_id)
        assert n["status"] == "answered" and n["answer"] == "是"

        p3 = clarify.register("web:x", ["是"], notice={"kind": "ask", "prompt": "发不出去"})
        clarify.abandon(p3.clarify_id)
        assert notices.get(p3.notice_id)["status"] == "expired"
        assert not clarify.has_pending("web:x")

    anyio.run(scenario)


def test_expire_all_pending_and_close_asks(isolated):
    a = notices.add(session_key="web:c", kind="ask", prompt="q")
    b = _expired_approval("web:c")
    assert notices.expire_all_pending() == 1
    assert notices.get(a)["status"] == "expired"
    notices.close_asks_for_session("web:c")  # 你在会话里又发了消息
    assert notices.get(a)["status"] == "dismissed"
    assert notices.get(b)["status"] == "expired"  # 审批不受影响


# ── 事后处理 ────────────────────────────────────────────────────────────
def test_act_late_ask_resumes_session(resumed):
    nid = _expired_ask()
    res = anyio.run(notice_actions.act, nid, "B")
    assert res["ok"] and res["conv"] == "p1:c1"
    assert resumed[0][0] == "web:p1:c1" and "(补答)" in resumed[0][1] and "B" in resumed[0][1]
    assert notices.get(nid)["status"] == "answered"
    # 再点一次:已处理
    assert not anyio.run(notice_actions.act, nid, "A")["ok"]


def test_act_late_approval_once_grants_and_resumes(resumed):
    nid = _expired_approval()
    assert anyio.run(notice_actions.act, nid, "允许一次")["ok"]
    assert danger._take_one_shot("task:t9", danger._category("外部写"))
    assert "已批准" in resumed[0][1]


def test_act_late_approval_forever_saves_rule(resumed):
    from vococo.memory import approvals

    nid = _expired_approval()
    assert anyio.run(notice_actions.act, nid, "永远允许")["ok"]
    assert [r["scope"] for r in approvals.list_rules()] == ["mcp__lemlist_lite__send_email"]


def test_act_late_approval_round_applies_next_round(resumed):
    nid = _expired_approval()
    assert anyio.run(notice_actions.act, nid, "本轮任务都允许")["ok"]
    cat = danger._category("外部写")
    assert not danger._is_session_approved("task:t9", cat)
    tok = danger.set_task_session("task:t9")  # 续跑的那一轮开头
    danger.reset_task_session(tok)
    assert danger._is_session_approved("task:t9", cat)
    danger.clear_session_approvals("task:t9")


def test_act_deny_just_dismisses(resumed):
    nid = _expired_approval()
    assert anyio.run(notice_actions.act, nid, "拒绝")["ok"]
    assert resumed == [] and notices.get(nid)["status"] == "dismissed"


def test_act_pending_resolves_live_question(resumed):
    async def scenario():
        p = clarify.register("web:y", ["好", "不好"], notice={"kind": "ask", "prompt": "?"})
        res = await notice_actions.act(p.notice_id, "好")
        assert res["ok"]
        assert await clarify.wait(p.clarify_id, 1) == "好"

    anyio.run(scenario)
    assert resumed == []  # 按时回答,不走补发


def test_late_click_on_old_button(resumed):
    from vococo.gateway.run import GatewayRunner

    runner = GatewayRunner([])
    nid = _expired_ask()
    msg = anyio.run(runner._late_click, "cid1", "1")
    assert "已按你选的「B」" in msg and resumed
    assert "已经处理过" in anyio.run(runner._late_click, "cid1", "0")
    assert "已过期" in anyio.run(runner._late_click, "nope", "0") or "处理过" in anyio.run(
        runner._late_click, "nope", "0")
    _ = nid


def test_conv_of():
    assert notice_actions.conv_of(config.SESSION_KEY) == "main"
    assert notice_actions.conv_of("web:p1:c2") == "p1:c2"
    assert notice_actions.conv_of("task:ab") == "task:ab"
    assert notice_actions.conv_of("tg:123") is None


# ── 早间汇总 ────────────────────────────────────────────────────────────
def test_morning_digest_once_per_day(isolated, monkeypatch):
    from vococo.gateway.adapters import web_push

    sent = []

    async def fake_notify(**kw):
        sent.append(kw)
        return 1

    monkeypatch.setattr(web_push.PUSH, "notify", fake_notify)
    monkeypatch.setattr(config, "BG_APPROVAL_QUIET", "23-8")
    _expired_approval()
    _expired_ask()
    at7 = datetime.datetime.now().replace(hour=7, minute=59)
    at8 = datetime.datetime.now().replace(hour=8, minute=1)
    assert anyio.run(notice_actions.maybe_digest, at7) == 0
    assert anyio.run(notice_actions.maybe_digest, at8) == 2
    assert "2 件事" in sent[0]["body"] and sent[0]["url"] == "/?notices=1"
    assert anyio.run(notice_actions.maybe_digest, at8.replace(minute=30)) == 0  # 同一天不重复
    assert len(sent) == 1


# ── 接口 ───────────────────────────────────────────────────────────────
@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def ntc_app(resumed, monkeypatch):
    from vococo.gateway.adapters.web import WebAdapter

    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([
        web.get("/notices", adapter._handle_notices),
        web.post("/notices/act", adapter._handle_notice_act),
        web.post("/notices/dismiss", adapter._handle_notice_dismiss),
    ])
    return app


@pytest.mark.anyio
async def test_notice_routes(ntc_app, resumed):
    a = _expired_ask()
    b = _expired_approval()
    c = _expired_approval("task:other")
    async with TestClient(TestServer(ntc_app)) as client:
        items = (await (await client.get("/notices")).json())["items"]
        assert {i["id"] for i in items} == {a, b, c}
        assert next(i for i in items if i["id"] == a)["conv"] == "p1:c1"
        r = await client.post("/notices/act", json={"id": a, "label": "A"})
        assert r.status == 200 and (await r.json())["conv"] == "p1:c1"
        assert (await client.post("/notices/act", json={"id": b, "label": "瞎选"})).status == 400
        assert (await client.post("/notices/dismiss", json={"id": b})).status == 200
        assert (await client.post("/notices/dismiss", json={"all": True})).status == 200
        items = (await (await client.get("/notices")).json())["items"]
    assert items == []
    assert resumed and resumed[0][0] == "web:p1:c1"
