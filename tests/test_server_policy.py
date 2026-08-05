"""PR4:server 模式安全策略——危险分级降级、沙箱 cwd、模型白名单、cron 按租户。"""
from __future__ import annotations

import asyncio

import pytest

from vococo import config, providers
from vococo.cron import scheduler
from vococo.tenancy import context, store


def _hook(input_data):
    from vococo.tools.danger import pretool_guard_hook

    return asyncio.run(pretool_guard_hook(input_data, "t1", None))


# ── 危险分级:server 模式 escalate 降为 block ─────────────────────────────
def test_escalate_blocked_in_server(server_mode, tmp_path):
    """写 cwd 外文件:personal 会弹审批(无通道放行),server 直接拦。"""
    from vococo.tools import danger

    tok_t = context.set("t_alice")
    ws = tmp_path / "ws"
    ws.mkdir()
    tok_c = danger.set_cwd(str(ws))
    try:
        out = _hook({
            "tool_name": "Write",
            "tool_input": {"file_path": "/etc/passwd-x", "content": "x"},
        })
    finally:
        danger.reset_cwd(tok_c)
        context.reset(tok_t)
    decision = out["hookSpecificOutput"]["permissionDecision"]
    assert decision == "deny"
    assert "沙箱" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_allow_operations_still_pass_in_server(server_mode, tmp_path, monkeypatch):
    """沙箱内的正常写不受影响(allow 档不变)。"""
    from vococo.tools import danger

    tok_t = context.set("t_alice")
    ws = tmp_path / "ws"
    ws.mkdir()
    tok = danger.set_cwd(str(ws))
    try:
        out = _hook({
            "tool_name": "Write",
            "tool_input": {"file_path": str(ws / "a.txt"), "content": "x"},
        })
    finally:
        danger.reset_cwd(tok)
        context.reset(tok_t)
    assert out == {}  # allow:hook 不干预


def test_block_tier_unchanged_in_server(server_mode):
    """灾难级命令两种模式都拦(rm -rf /)。"""
    out = _hook({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# ── 内置 MCP 工具按模式摘取 ──────────────────────────────────────────────
def _capture_builtin_tools(monkeypatch) -> list[str]:
    """patch create_sdk_mcp_server 抓住 tools 参数,拿到实际挂出的工具名。"""
    from vococo.tools import builtin

    captured: list[str] = []

    def fake_server(name, tools, **kw):
        captured.extend(getattr(t, "name", str(t)) for t in tools)
        return {"name": name}

    monkeypatch.setattr(builtin, "create_sdk_mcp_server", fake_server)
    builtin.build_mcp_servers()
    return captured


def test_builtin_tools_trimmed_in_server(server_mode, monkeypatch):
    names = _capture_builtin_tools(monkeypatch)
    for banned in ("restart_self", "add_mcp_server", "remove_mcp_server", "set_external_mcp"):
        assert banned not in names
    for kept in ("recall_past", "save_memory", "add_cron_job", "dispatch_session"):
        assert kept in names


def test_builtin_tools_full_in_personal(monkeypatch):
    names = _capture_builtin_tools(monkeypatch)
    for kept in ("restart_self", "add_mcp_server", "recall_past"):
        assert kept in names


# ── 模型白名单 ───────────────────────────────────────────────────────────
def test_model_whitelist(server_mode, monkeypatch):
    monkeypatch.setattr(config, "SERVER_ALLOWED_MODELS", ["deepseek-v4-flash", "kimi-k3"])
    tok = context.set("t_alice")
    try:
        # 白名单外的选择被剥掉 → 落默认
        model, _env = providers.resolve("claude-opus-5", "deepseek-v4-flash")
        assert model == "deepseek-v4-flash"
        # 默认也不在名单 → 落名单第一个
        model, _env = providers.resolve(None, "claude-sonnet-5")
        assert model == "deepseek-v4-flash"
        # 名单内原样通过
        model, _env = providers.resolve("kimi-k3", "deepseek-v4-flash")
        assert model == "kimi-k3"
    finally:
        context.reset(tok)


def test_model_whitelist_empty_means_no_limit(server_mode, monkeypatch):
    monkeypatch.setattr(config, "SERVER_ALLOWED_MODELS", [])
    tok = context.set("t_alice")
    try:
        model, _env = providers.resolve("claude-opus-5", "claude-sonnet-5")
        assert model == "claude-opus-5"
    finally:
        context.reset(tok)


# ── cron 按租户 ─────────────────────────────────────────────────────────
@pytest.fixture
def cron_db(server_mode):
    store.reset()
    yield server_mode
    store.reset()


def test_cron_jobs_per_tenant(cron_db):
    tok = context.set("t_alice")
    try:
        scheduler.create_job(
            name="a 的任务", prompt="p",
            schedule={"kind": "interval", "minutes": 60},
        )
    finally:
        context.reset(tok)
    tok = context.set("t_bob")
    try:
        assert scheduler.load_jobs() == []  # bob 看不到 alice 的任务
        scheduler.create_job(
            name="b 的任务", prompt="p",
            schedule={"kind": "interval", "minutes": 30},
        )
        assert len(scheduler.load_jobs()) == 1
    finally:
        context.reset(tok)
    # 调度器的全量扫描能拿到两个租户、且各带归属
    all_jobs = store.cron_jobs_all()
    assert {j["_tenant"] for j in all_jobs} == {"t_alice", "t_bob"}


def test_cron_tick_isolates_tenants(cron_db, monkeypatch):
    """_tick 逐租户注入上下文执行;触发时任务落在正确租户的上下文里。"""
    fired: list[str] = []
    monkeypatch.setattr(
        scheduler, "_run_job",
        lambda job: fired.append(context.current()),
    )
    for tid in ("t_alice", "t_bob"):
        tok = context.set(tid)
        try:
            job = scheduler.create_job(
                name=f"{tid} 到期任务", prompt="p",
                schedule={"kind": "interval", "minutes": 1},
            )
            # 直接置为已到期
            jobs = scheduler.load_jobs()
            jobs[0]["next_run_at"] = 0
            scheduler.save_jobs(jobs)
        finally:
            context.reset(tok)
    scheduler._tick()
    assert sorted(fired) == ["t_alice", "t_bob"]
