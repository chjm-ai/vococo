"""业务写边界独立于风险开关和完全访问权限。"""
import os
from unittest.mock import AsyncMock

import anyio
import pytest

from vococo import config
from vococo.core import execution, permissions
from vococo.tools import danger


@pytest.fixture
def business(tmp_path, monkeypatch):
    workspace = tmp_path / "repo" / "workspace"
    output = tmp_path / "Mobile Documents" / "com~apple~CloudDocs" / "成果"
    home = tmp_path / "agents" / "business"
    linked = tmp_path / "linked"
    brain = tmp_path / "brain"
    scratch = tmp_path / "scratch"
    for path in (workspace, output, home, linked, brain, scratch):
        path.mkdir(parents=True)
    policy = execution.ExecutionPolicy("business", workspace, output, (workspace, output, home, linked))
    monkeypatch.setattr(danger, "_current_session_key", lambda: "web:business")

    def resolve(session_key, cwd=None):
        assert session_key == "web:business"
        return policy

    monkeypatch.setattr(execution, "policy_for_session", resolve)
    monkeypatch.setattr(config, "AI_BRAIN_DIR", brain)
    monkeypatch.setattr(config, "OBSIDIAN_VAULT_DIR", tmp_path / "vault")
    # pytest 本身就在系统临时目录内；缩小测试临时目录，否则所有测试路径都被豁免。
    monkeypatch.setattr(
        danger, "_inside_scratch_tmp",
        lambda path, cwd: os.path.commonpath([
            os.path.realpath(os.path.join(cwd or "", path)), str(scratch),
        ]) == str(scratch),
    )
    monkeypatch.setattr(danger, "_inside_agents_root", lambda path, cwd: (
        os.path.commonpath([os.path.realpath(os.path.join(cwd or "", path)), str(home.parent)])
        == str(home.parent)
    ))
    monkeypatch.setattr(danger, "_audit", lambda *args: None)
    tokens = danger.set_cwd(str(workspace), str(tmp_path / "repo"))
    yield policy, brain, scratch
    danger.reset_cwd(tokens)


def call(tool, path=None, **kwargs):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    ti = {key: str(path)} if path is not None else {}
    ti.update(kwargs)
    return anyio.run(lambda: danger.pretool_guard_hook(
        {"tool_name": tool, "tool_input": ti}, None, {},
    ))


def assert_denied(result, text):
    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert text in output["permissionDecisionReason"]


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_all_business_roots_allowed(business, tool):
    policy, brain, scratch = business
    for root in (*policy.writable_roots, brain, scratch):
        assert call(tool, root / "new" / "file.md") == {}
    assert call(tool, "relative.md") == {}


@pytest.mark.parametrize("guard,gate", [(False, False), (True, False), (False, True), (True, True)])
def test_external_write_denied_even_full_access(business, monkeypatch, guard, gate):
    policy, _, _ = business
    monkeypatch.setattr(config, "DANGER_GUARD", guard)
    monkeypatch.setattr(config, "APPROVAL_GATE", gate)
    monkeypatch.setattr(permissions, "auto_allows", lambda *args: True)
    approve = AsyncMock(return_value=True)
    monkeypatch.setattr(danger, "_ask_approval", approve)
    for path in (
        policy.workspace.parent / "source.py",
        policy.workspace.with_name("workspace-other") / "file.md",
        config.OBSIDIAN_VAULT_DIR / "other.md",
        policy.writable_roots[2].parent / "another-agent" / "file.md",
    ):
        assert_denied(call("Write", path), "开发会话")
    approve.assert_not_called()


def test_realpath_prevents_traversal_and_symlink_escape(business):
    policy, _, scratch = business
    outside = policy.workspace.parent / "outside"
    outside.mkdir()
    (policy.workspace / "escape").symlink_to(outside, target_is_directory=True)
    (scratch / "escape").symlink_to(outside, target_is_directory=True)
    for path in ("../outside/file.md", "escape/new/file.md", scratch / "escape" / "file.md"):
        assert_denied(call("Write", path), "业务可写范围")
    alias = outside / "output-link"
    alias.symlink_to(policy.output_dir, target_is_directory=True)
    assert call("Write", alias / "file.md") == {}
    (policy.workspace / "file-link").symlink_to(outside / "not-created.md")
    assert_denied(call("Edit", "file-link"), "业务可写范围")


def test_business_output_bypasses_old_worktree_guard(business):
    policy, _, _ = business
    # 模拟成果目录恰好位于主项目内、工作区外。
    tokens = danger.set_cwd(str(policy.workspace), str(policy.output_dir.parent))
    try:
        assert danger._writes_outside_worktree("Write", {"file_path": str(policy.output_dir / "report.md")}, str(policy.workspace))
        assert call("Write", policy.output_dir / "report.md") == {}
    finally:
        danger.reset_cwd(tokens)


def test_permission_config_cannot_disable_business_policy(business, monkeypatch):
    policy, _, _ = business
    monkeypatch.setattr(config, "APPROVAL_GATE", True)
    approve = AsyncMock(return_value=False)
    monkeypatch.setattr(danger, "_ask_approval", approve)
    monkeypatch.setattr(config, "APPROVAL_GATE", False)
    assert_denied(call("Write", policy.writable_roots[2] / "agent.json"), "禁止修改")
    approve.assert_not_called()


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"])
def test_policy_failure_denies_instead_of_falling_back(business, monkeypatch, tool):
    def fail(*args):
        raise OSError("策略读取失败")

    monkeypatch.setattr(execution, "policy_for_session", fail)
    monkeypatch.setattr(config, "DANGER_GUARD", False)
    monkeypatch.setattr(config, "APPROVAL_GATE", False)
    assert_denied(call(tool, "file.md", command="pwd", dangerouslyDisableSandbox=True), "安全判定异常")


def test_business_cannot_disable_shell_sandbox(business, monkeypatch):
    monkeypatch.setattr(config, "DANGER_GUARD", False)
    monkeypatch.setattr(config, "APPROVAL_GATE", False)
    monkeypatch.setattr(permissions, "auto_allows", lambda *args: True)
    assert_denied(call("Bash", command="pwd", dangerouslyDisableSandbox=True), "禁止关闭 Bash 沙箱")
    assert call("Bash", command="pwd", dangerouslyDisableSandbox=False) == {}


@pytest.mark.parametrize("tool", ["mcp__vococo__add_cron_job", "mcp__vococo__update_cron_job", "mcp__vococo__suggest_automation"])
def test_business_cannot_schedule_unsandboxed_shell(business, monkeypatch, tool):
    monkeypatch.setattr(config, "APPROVAL_GATE", False)
    assert_denied(call(tool, mode="script", command="python3 external.py"), "脚本定时任务")


@pytest.mark.parametrize("tool", ["mcp__vococo__add_mcp_server", "mcp__vococo__remove_mcp_server", "mcp__vococo__set_external_mcp"])
def test_business_cannot_create_unsandboxed_mcp_entry(business, monkeypatch, tool):
    monkeypatch.setattr(config, "APPROVAL_GATE", False)
    assert_denied(call(tool, name="local-command", type="stdio", command="python3"), "MCP 执行入口")


def test_missing_write_path_fails_closed(business):
    assert_denied(call("Write"), "业务可写范围")


@pytest.mark.parametrize("session_key", ["web:developer", "web:general", ""])
def test_nonbusiness_keeps_existing_guards(business, monkeypatch, session_key):
    policy, _, _ = business
    monkeypatch.setattr(danger, "_current_session_key", lambda: session_key)
    monkeypatch.setattr(execution, "policy_for_session", lambda key, cwd=None: None)
    monkeypatch.setattr(config, "APPROVAL_GATE", False)
    assert call("Bash", command="pwd", dangerouslyDisableSandbox=True) == {}
    assert call("Write", policy.output_dir / "report.md") == {}
    assert_denied(call("Write", policy.workspace.parent / "source.py"), "worktree 外")
