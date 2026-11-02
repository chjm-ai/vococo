"""关联即读写授权；完全访问无需关联；隔离与秘密保护独立于目录权限。"""
from pathlib import Path

import anyio
import pytest

from vococo import config
from vococo.core import execution, permissions
from vococo.memory import agents
from vococo.tools import artifacts, danger
from test_business_repositories import repository


@pytest.mark.parametrize("mode", ["workspace", "project"])
def test_linked_write_and_unlinked_approval(isolated, monkeypatch, mode):
    owner = agents.create("目录授权")
    meta = agents._read_meta(owner["id"])
    meta["execution_mode"] = mode
    agents._write_meta(owner["id"], meta)
    linked = isolated / "linked"
    linked.mkdir()
    agents.update(owner["id"], links=[{"path": str(linked), "writable": False}])
    key = agents.main_session_key(owner)
    monkeypatch.setattr(danger, "_current_session_key", lambda: key)
    monkeypatch.setattr(danger, "_inside_scratch_tmp", lambda *args: False)
    cwd = str(isolated / "workspace")
    assert danger.classify("Write", {"file_path": str(linked / "a.md")}, cwd)[0] == "allow"
    assert danger.classify("Write", {"file_path": str(isolated / "unlinked/a.md")}, cwd)[0] == "escalate"
    assert "已授权读写" in agents.prompt_extra(agents.get(owner["id"]))


def test_full_delivery_without_links_and_secret_guards(isolated):
    owner = agents.create("完全访问交付")
    key = agents.main_session_key(owner)
    report = isolated / "external" / "report.txt"
    report.parent.mkdir()
    report.write_text("正式成果")
    cwd = str(isolated / "workspace")
    with pytest.raises(ValueError):
        artifacts._snapshot(str(report), key, cwd)
    agents.update(owner["id"], permission="full")
    assert agents.get(owner["id"])["links"] == []
    assert artifacts._snapshot(str(report), key, cwd) == ("report.txt", "正式成果".encode())
    assert artifacts._snapshot(str(report), key, str(Path.home()))[0] == "report.txt"
    alias = report.parent / "alias.txt"
    alias.symlink_to(report)
    with pytest.raises((ValueError, OSError)):
        artifacts._snapshot(str(alias), key, cwd)
    report.write_text("-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----")
    with pytest.raises(ValueError):
        artifacts._snapshot(str(report), key, cwd)
    runtime = config.DATA_DIR / "other-session.txt"
    runtime.write_text("其他会话")
    with pytest.raises(ValueError):
        artifacts._snapshot(str(runtime), key, cwd)
    permissions.set_session(key, "standard")
    assert not permissions.is_full(key)


@pytest.mark.anyio
async def test_multiple_agents_share_sources_but_not_copies(isolated):
    sources = [repository(isolated / name) for name in ("one", "two")]
    owners = [agents.create(name) for name in ("甲", "乙")]
    policies = []
    for owner in owners:
        agents.update(owner["id"], links=[{"path": str(path)} for path in sources])
        policy = execution.policy_for_session(agents.main_session_key(owner))
        await execution.prepare_repositories(policy)
        policies.append(policy)
    assert len(policies[0].repositories) == len(policies[1].repositories) == 2
    for first, second in zip(policies[0].repositories, policies[1].repositories):
        assert first.source == second.source
        assert first.checkout != second.checkout
        (first.checkout / "article.md").write_text("独立修改")
        assert (second.checkout / "article.md").read_text() == "原文\n"
        assert (first.source / "article.md").read_text() == "原文\n"
        own_key, other_key = (agents.main_session_key(owner) for owner in owners)
        assert artifacts._snapshot(str(first.checkout / "article.md"), own_key, str(policies[0].workspace))[0] == "article.md"
        with pytest.raises(ValueError):
            artifacts._snapshot(str(first.checkout / "article.md"), other_key, str(policies[1].workspace))


@pytest.mark.parametrize("mode", ["workspace", "project"])
def test_background_unlinked_write_requires_full_access(isolated, monkeypatch, mode):
    owner = agents.create("后台权限")
    meta = agents._read_meta(owner["id"])
    meta["execution_mode"] = mode
    agents._write_meta(owner["id"], meta)
    key = agents.main_session_key(owner)
    monkeypatch.setattr(config, "APPROVAL_GATE", True)
    monkeypatch.setattr(danger, "_inside_scratch_tmp", lambda *args: False)
    cwd = isolated / "repo/worktree"
    cwd.mkdir(parents=True)
    target = cwd.parent / "unlinked.txt"
    token = danger.set_task_session(key)
    dirs = danger.set_cwd(str(cwd), str(cwd.parent))
    try:
        def call():
            return anyio.run(lambda: danger.pretool_guard_hook(
                {"tool_name": "Write", "tool_input": {"file_path": str(target)}}, None, {},
            ))
        assert call()["hookSpecificOutput"]["permissionDecision"] == "deny"
        agents.update(owner["id"], permission="full")
        assert call() == {}
    finally:
        danger.reset_cwd(dirs)
        danger.reset_task_session(token)


def test_legacy_repository_is_visible_and_can_be_unlinked(isolated):
    source = repository(isolated / "legacy")
    owner = agents.create("旧配置")
    agents.update(owner["id"], repositories=[str(source)])
    current = agents.get(owner["id"])
    assert current["links"] == [{"path": str(source), "note": "", "writable": True}]
    agents.update(owner["id"], links=[])
    assert agents.get(owner["id"])["links"] == []
    assert agents.get(owner["id"])["repositories"] == []
