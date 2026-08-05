"""PR3 鉴权与账号体系:tenancy/store CRUD + 登录态 + SSE 租户定向投递。"""
from __future__ import annotations

import asyncio
import json

import pytest

from vococo import config
from vococo.tenancy import context, store


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    """platform.db 指到临时目录,不碰真实 data/。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    store.reset()
    yield tmp_path
    store.reset()


# ── 租户/用户 CRUD ───────────────────────────────────────────────────────
def test_create_tenant_and_user(platform_db):
    t = store.create_tenant("acme", "Acme 公司")
    assert t["tenant_id"] == "acme" and t["status"] == "active"
    assert t["markup"] == 5.0 and t["wallet_cny_balance"] == 0
    u = store.create_user("acme", "Owner@ACME.com", "password123")
    assert u["email"] == "owner@acme.com"  # 邮箱归一小写
    assert u["role"] == "owner"


def test_tenant_id_validation(platform_db):
    for bad in ("A", "x", "has:colon", "有中文", "a" * 40, "-lead"):
        with pytest.raises(ValueError):
            store.create_tenant(bad, "x")


def test_duplicate_tenant_and_email(platform_db):
    store.create_tenant("acme", "A")
    with pytest.raises(ValueError):
        store.create_tenant("acme", "A2")
    store.create_user("acme", "a@x.com", "password123")
    with pytest.raises(ValueError):
        store.create_user("acme", "a@x.com", "password456")


def test_short_password_rejected(platform_db):
    store.create_tenant("acme", "A")
    with pytest.raises(ValueError):
        store.create_user("acme", "a@x.com", "short")


# ── 登录与登录态 ─────────────────────────────────────────────────────────
def test_authenticate_and_session(platform_db):
    store.create_tenant("acme", "A")
    store.create_user("acme", "a@x.com", "password123")
    assert store.authenticate("a@x.com", "password123") is not None
    assert store.authenticate("a@x.com", "wrong-pass") is None
    assert store.authenticate("nobody@x.com", "password123") is None

    user = store.authenticate("a@x.com", "password123")
    token = store.create_session(user["user_id"])
    resolved = store.resolve_session(token)
    assert resolved is not None
    r_user, r_tenant = resolved
    assert r_user["email"] == "a@x.com" and r_tenant["tenant_id"] == "acme"
    store.delete_session(token)
    assert store.resolve_session(token) is None


def test_suspended_tenant_cannot_login(platform_db):
    store.create_tenant("acme", "A")
    store.create_user("acme", "a@x.com", "password123")
    store._conn().execute("UPDATE tenants SET status='suspended' WHERE tenant_id='acme'")
    store._conn().commit()
    assert store.authenticate("a@x.com", "password123") is None


# ── SSE 租户定向投递(_emit 层)─────────────────────────────────────────────
def _make_adapter():
    from vococo.gateway.adapters.web import WebAdapter

    return WebAdapter()


def test_emit_personal_broadcasts_to_all():
    """personal 模式:所有连接都是 local,事件广播给全部(回归:改造前行为)。"""
    ad = _make_adapter()
    qs = [asyncio.Queue(), asyncio.Queue()]
    for q in qs:
        ad._clients[q] = "local"
    ad._emit({"type": "text", "conv": "c1", "text": "hi"})
    for q in qs:
        seq, data = q.get_nowait()
        assert json.loads(data)["text"] == "hi"


def test_emit_server_filters_by_tenant(server_mode):
    """server 模式:事件只投给同租户的连接;缓冲里也带上租户标记。"""
    ad = _make_adapter()
    qa, qb = asyncio.Queue(), asyncio.Queue()
    ad._clients[qa] = "t_alice"
    ad._clients[qb] = "t_bob"
    tok = context.set("t_alice")
    try:
        ad._emit({"type": "text", "conv": "c1", "text": "只给 alice"})
    finally:
        context.reset(tok)
    assert not qa.empty() and qb.empty()
    # 环形缓冲条目带租户 id,补发路径(_handle_events)据此过滤
    _seq, etid, _data = ad._buffer[-1]
    assert etid == "t_alice"


def test_emit_server_no_context_drops(server_mode, capsys):
    """无租户上下文的事件:fail-closed 不投递(宁可丢帧,不跨租户发)。"""
    ad = _make_adapter()
    q = asyncio.Queue()
    ad._clients[q] = "t_alice"
    ad._emit({"type": "text", "conv": "c1", "text": "x"})
    assert q.empty()
    assert "无租户上下文" in capsys.readouterr().out


def test_track_live_keyed_by_tenant(server_mode):
    """进行中的回合快照按 (租户, conv) 分键:两租户同 conv 互不覆盖。"""
    ad = _make_adapter()
    for tid in ("t_alice", "t_bob"):
        tok = context.set(tid)
        try:
            ad._emit({"type": "start", "conv": "c1", "text": "go"})
        finally:
            context.reset(tok)
    assert ("t_alice", "c1") in ad._live and ("t_bob", "c1") in ad._live
    # alice 的回合结束只清自己的快照
    tok = context.set("t_alice")
    try:
        ad._emit({"type": "done", "conv": "c1"})
    finally:
        context.reset(tok)
    assert ("t_alice", "c1") not in ad._live and ("t_bob", "c1") in ad._live


# ── 登录限频 ─────────────────────────────────────────────────────────────
def test_login_rate_limit():
    ad = _make_adapter()
    for _ in range(5):
        assert ad._login_rate_limited("1.2.3.4") is False
    assert ad._login_rate_limited("1.2.3.4") is True
    assert ad._login_rate_limited("5.6.7.8") is False  # 别的 IP 不受影响
