"""绑定仓库只授权会话副本，不放宽业务主仓库或其它会话的写边界。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vococo.core import execution
from vococo.memory import agents
from vococo.tools import danger


@pytest.fixture
def anyio_backend():
    return "asyncio"


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def repository(path):
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "测试")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "article.md").write_text("原文\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "初始内容")
    return path


def owner_with_repository(isolated):
    source = repository(isolated / "source")
    owner = agents.create("业务")
    agents.update(owner["id"], repositories=[str(source)])
    return owner, source, agents.main_session_key(owner)


def test_unbound_agent_requests_repository_before_coding():
    note = execution.prompt_note(execution.ExecutionPolicy(
        "business", Path("/workspace"), Path("/output"), (),
    ))
    assert "先请主人在 Agent 设置中添加并绑定 Git 仓库" in note
    assert "没有交互通道时报告缺少绑定" in note
    assert "不要关闭沙箱" not in note


def test_repository_binding_validates_and_deduplicates(isolated):
    owner, source, _ = owner_with_repository(isolated)
    updated = agents.update(owner["id"], repositories=[str(source), str(source)])
    assert updated["repositories"] == [str(source)]
    with pytest.raises(ValueError, match="仓库根目录"):
        agents.update(owner["id"], repositories=[str(isolated)])
    nested = source / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="仓库根目录"):
        agents.update(owner["id"], repositories=[str(nested)])
    assert agents.get(owner["id"])["repositories"] == [str(source)]
    assert agents.update(owner["id"], repositories=[])["repositories"] == []


@pytest.mark.anyio
async def test_copies_are_isolated_and_source_stays_readonly(isolated):
    owner, source, key = owner_with_repository(isolated)
    policy = execution.policy_for_session(key)
    await execution.prepare_repositories(policy)
    copy = policy.repositories[0].checkout
    assert git(copy, "rev-parse", "--show-toplevel") == str(copy)
    assert git(copy, "branch", "--show-current").startswith("vococo/")
    assert (copy / ".git").is_dir()
    assert not (copy / ".git/objects/info/alternates").exists()
    assert not git(copy, "remote")  # 没有发布地址时不保留指向主检出的 origin。
    assert danger._business_write_allowed({"file_path": str(copy / "article.md")}, str(policy.workspace), policy)
    assert not danger._business_write_allowed({"file_path": str(source / "article.md")}, str(policy.workspace), policy)
    (copy / "article.md").write_text("仅修改副本\n")
    git(copy, "add", ".")
    git(copy, "-c", "user.name=测试", "-c", "user.email=test@example.invalid", "commit", "-m", "副本修改")
    await execution.prepare_repositories(policy)
    assert (copy / "article.md").read_text() == "仅修改副本\n"
    assert (source / "article.md").read_text() == "原文\n"
    assert git(source, "status", "--porcelain") == ""
    second_key = key.rsplit(":", 1)[0] + ":second"
    second = execution.policy_for_session(second_key)
    await execution.prepare_repositories(second)
    assert second.repositories[0].checkout != copy
    assert not danger._business_write_allowed({"file_path": str(copy / "article.md")}, str(second.workspace), second)
    assert execution.sandbox_settings(policy) == {"enabled": False}
    note = execution.prompt_note(policy)
    assert str(copy) in note and "推送" in note
    assert "代码开发默认由当前 Agent 自行完成" in note


@pytest.mark.anyio
async def test_multiple_repositories_and_revocation(isolated):
    owner, source, key = owner_with_repository(isolated)
    other = repository(isolated / "other")
    agents.update(owner["id"], repositories=[str(source), str(other)])
    policy = execution.policy_for_session(key)
    await execution.prepare_repositories(policy)
    assert len(policy.repositories) == 2
    agents.update(owner["id"], repositories=[])
    revoked = execution.policy_for_session(key)
    for binding in policy.repositories:
        assert not danger._business_write_allowed({"file_path": str(binding.checkout / "article.md")}, str(revoked.workspace), revoked)
        assert binding.checkout.exists()  # 撤销授权不删除未提交成果。


@pytest.mark.anyio
async def test_copy_rejects_foreign_or_linked_git_directory(isolated):
    _, source, key = owner_with_repository(isolated)
    policy = execution.policy_for_session(key)
    copy = policy.repositories[0].checkout
    copy.parent.mkdir(parents=True)
    copy.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="副本"):
        await execution.prepare_repositories(policy)
    assert git(source, "status", "--porcelain") == ""


def test_initialization_previews_without_writing_and_backs_up(isolated):
    from scripts.bind_business_repositories import changes
    from scripts.migrate_business_workspaces import apply_change

    owner, source, _ = owner_with_repository(isolated)
    agents.update(owner["id"], repositories=[])
    path = agents.home(owner["id"]) / "agent.json"
    before = path.read_bytes()
    bindings = [f"{owner['id']}={source}"] * 2
    planned = changes(agents.root_dir(), bindings)
    assert path.read_bytes() == before and len(planned) == 1
    target, meta, original = planned[0]
    backup = isolated / "backup"
    apply_change(target, meta, original, backup)
    assert (backup / owner["id"] / "agent.json").read_bytes() == before
    assert agents.get(owner["id"])["repositories"] == [str(source)]
    assert changes(agents.root_dir(), bindings)[0][1] == meta


def test_writable_repository_link_does_not_prevent_business_execution(isolated):
    owner, source, key = owner_with_repository(isolated)
    agents.update(owner["id"], links=[{"path": str(source), "writable": True}])
    policy = execution.policy_for_session(key)
    assert source in policy.writable_roots
    assert policy.repositories[0].source == source


def test_slow_binding_update_does_not_restore_revoked_permissions(isolated, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from vococo.core import business_repositories

    owner, source, _ = owner_with_repository(isolated)
    agents.update(owner["id"], permission="full")
    entered, proceed = Event(), Event()
    normalize = business_repositories.normalize_repositories

    def slow(value):
        entered.set()
        assert proceed.wait(5)
        return normalize(value)

    monkeypatch.setattr(business_repositories, "normalize_repositories", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        saving = pool.submit(agents.update, owner["id"], repositories=[str(source)])
        try:
            assert entered.wait(5)
            agents.update(owner["id"], permission="standard")
        finally:
            proceed.set()
        assert saving.result()["permission"] == "standard"
    assert agents.get(owner["id"])["permission"] == "standard"


@pytest.mark.anyio
async def test_failed_clone_can_retry_without_touching_existing_work(isolated, monkeypatch):
    from vococo.core import business_repositories

    _, source, key = owner_with_repository(isolated)
    policy = execution.policy_for_session(key)
    clone = business_repositories._clone_copy

    def fail(binding):
        (binding.checkout / "partial.txt").write_text("克隆中断")
        raise ValueError("模拟克隆失败")

    monkeypatch.setattr(business_repositories, "_clone_copy", fail)
    with pytest.raises(ValueError, match="模拟克隆失败"):
        await execution.prepare_repositories(policy)
    copy = policy.repositories[0].checkout
    assert not copy.exists()
    assert list(copy.parent.iterdir()) == []
    monkeypatch.setattr(business_repositories, "_clone_copy", clone)
    await execution.prepare_repositories(policy)
    assert (copy / "article.md").read_text() == "原文\n"
    assert git(source, "status", "--porcelain") == ""
