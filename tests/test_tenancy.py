"""tenancy 层单测:租户上下文 + 按租户路径解析。

personal 模式是硬约束——主人日用不受影响:所有路径必须回落到 config 现有全局路径;
server 模式则必须 fail-closed(没注入租户上下文就抛错,不静默写共享位置)。
"""
from __future__ import annotations

import pytest

from vococo import config
from vococo.tenancy import context, paths


@pytest.fixture
def server_mode(tmp_path, monkeypatch):
    """把 config 切到 server 模式(租户根指到临时目录),用完还原。"""
    monkeypatch.setattr(config, "IS_SERVER", True)
    monkeypatch.setattr(config, "TENANTS_DIR", tmp_path / "tenants", raising=False)
    return tmp_path / "tenants"


# ── context ──────────────────────────────────────────────────────────────
def test_context_personal_default_local():
    assert context.current() == context.LOCAL_TENANT == "local"


def test_context_server_missing_raises(server_mode):
    with pytest.raises(context.TenantContextError):
        context.current()


def test_context_server_set_and_reset(server_mode):
    tok = context.set("t_alice")
    try:
        assert context.current() == "t_alice"
    finally:
        context.reset(tok)
    with pytest.raises(context.TenantContextError):
        context.current()


# ── paths:personal 回落(零行为变化的硬证据)────────────────────────────────
def test_paths_personal_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "AI_BRAIN_DIR", tmp_path / "brain")
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "data" / "images")
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "data" / "audio")
    assert paths.data_dir() == tmp_path / "data"
    assert paths.brain_dir() == tmp_path / "brain"
    assert paths.workspace_dir() is None  # personal 维持 worktree/项目体系
    assert paths.settings_path() == tmp_path / "data" / "web_settings.json"
    assert paths.images_dir() == tmp_path / "data" / "images"
    assert paths.audio_dir() == tmp_path / "data" / "audio"


# ── paths:server 按租户隔离 ───────────────────────────────────────────────
def test_paths_server_per_tenant(server_mode):
    tok = context.set("t_alice")
    try:
        root = server_mode / "t_alice"
        assert paths.data_dir() == root
        assert paths.brain_dir() == root / "brain"
        assert paths.workspace_dir() == root / "workspace"
        assert paths.settings_path() == root / "settings.json"
        assert paths.images_dir() == root / "images"
        assert paths.audio_dir() == root / "audio"
    finally:
        context.reset(tok)


def test_paths_server_two_tenants_isolated(server_mode):
    """两个租户解析出的路径必须互不相同且互不包含(物理隔离的地基)。"""
    seen: dict[str, str] = {}
    for tid in ("t_alice", "t_bob"):
        tok = context.set(tid)
        try:
            seen[tid] = str(paths.data_dir())
        finally:
            context.reset(tok)
    assert seen["t_alice"] != seen["t_bob"]
    assert "t_alice" in seen["t_alice"] and "t_bob" in seen["t_bob"]


# ── settings_store 路径跟随租户 ──────────────────────────────────────────
def test_settings_path_personal(tmp_path, monkeypatch):
    from vococo.gateway import settings_store

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    assert settings_store._path() == tmp_path / "data" / "web_settings.json"


def test_settings_path_server_follows_tenant(server_mode):
    from vococo.gateway import settings_store

    tok = context.set("t_alice")
    try:
        assert settings_store._path() == server_mode / "t_alice" / "settings.json"
    finally:
        context.reset(tok)


# ── PR2:会话 key 租户前缀 + per-tenant 会话库 ────────────────────────────
def test_session_key_server_prefixed(server_mode):
    tok = context.set("t_alice")
    try:
        key = config.resolve_session_key("web", "c1")
        assert key == "t:t_alice:web:c1"
        # 剥前缀后回到原形态
        assert config.strip_tenant_prefix(key) == "web:c1"
        # 保留前缀透传分支也带租户前缀
        assert config.resolve_session_key("web", "task:abc") == "t:t_alice:task:abc"
    finally:
        context.reset(tok)


def test_session_key_personal_unprefixed():
    assert config.resolve_session_key("web", "c1") == "web:c1"
    assert config.strip_tenant_prefix("web:c1") == "web:c1"
    # 非规范 t: 形态不误剥(单段 / 空 tid)
    assert config.strip_tenant_prefix("t:") == "t:"
    assert config.strip_tenant_prefix("t::x") == "t::x"


def test_project_hash_server_disabled(server_mode):
    tok = context.set("t_alice")
    try:
        assert config.project_hash_from_key("t:t_alice:web:p12345:c1") is None
    finally:
        context.reset(tok)


def test_db_per_tenant_isolated(server_mode):
    """两个租户的 state.db 是不同文件,数据互不可见(物理隔离的硬证据)。"""
    from vococo.memory import _db, session_store

    tok_a = context.set("t_alice")
    session_store.append("web:c1", "你好", "你好呀")
    context.reset(tok_a)
    tok_b = context.set("t_bob")
    try:
        # bob 的库里没有 alice 的会话
        assert session_store.load_recent("web:c1") == []
        assert (server_mode / "t_bob" / "state.db").is_file()
    finally:
        context.reset(tok_b)
    assert (server_mode / "t_alice" / "state.db").is_file()
    # alice 的数据还在自己的库里
    tok_a2 = context.set("t_alice")
    try:
        rows = session_store.load_recent("web:c1")
        assert len(rows) == 1 and rows[0].user == "你好"
    finally:
        context.reset(tok_a2)
    _db.reset()
