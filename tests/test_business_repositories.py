"""绑定仓库只授权会话副本，不放宽业务主仓库或其它会话的写边界。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vococo.core import execution
from vococo import config
from vococo.memory import agents, session_store
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
    assert "Git 仓库无需单独绑定" in note
    assert "完全访问无需绑定或重复审批" in note
    assert "先请主人在 Agent 设置中添加并绑定" not in note
    assert "不要关闭沙箱" not in note


def test_output_directory_prompt_respects_explicit_writable_link():
    note = execution.prompt_note(execution.ExecutionPolicy(
        "business", Path("/workspace"), Path("/output"),
        (Path("/workspace"), Path("/output"), Path("/linked")),
    ))
    assert "默认写到正式成果目录" in note
    assert "关联目录也可作为成果目的地" in note
    assert "标准权限可直接读写关联目录" in note
    assert "只读关联目录不能写入" not in note
    assert "正式成果必须用绝对路径写到正式成果目录" not in note


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
    assert (copy / ".git").is_file()
    assert git(copy, "rev-parse", "--path-format=absolute", "--git-common-dir") == str(source / ".git")
    assert str(copy) in git(source, "worktree", "list", "--porcelain")
    assert not git(copy, "remote")  # 没有发布地址时不保留指向主检出的 origin。
    assert danger._business_write_allowed({"file_path": str(copy / "article.md")}, str(policy.workspace), policy)
    assert danger._business_write_allowed({"file_path": str(source / "article.md")}, str(policy.workspace), policy)
    (copy / "article.md").write_text("仅修改副本\n")
    git(copy, "add", ".")
    git(copy, "-c", "user.name=测试", "-c", "user.email=test@example.invalid", "commit", "-m", "副本修改")
    await execution.prepare_repositories(policy)
    assert (copy / "article.md").read_text() == "仅修改副本\n"
    assert (source / "article.md").read_text() == "原文\n"
    assert git(source, "status", "--porcelain") == ""
    assert git(source, "branch", "--show-current") == "main"
    (copy / "article.md").write_text("未提交成果\n")
    await execution.prepare_repositories(policy)
    assert (copy / "article.md").read_text() == "未提交成果\n"
    second_key = config.new_session_key()
    session_store.bind_session(second_key, agent_id=owner["id"])
    second = execution.policy_for_session(second_key)
    assert second.agent_id == owner["id"]
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
async def test_failed_worktree_can_retry_without_touching_existing_work(isolated, monkeypatch):
    from vococo.core import business_repositories

    _, source, key = owner_with_repository(isolated)
    policy = execution.policy_for_session(key)
    real_git = business_repositories._git

    def fail(cwd, *args):
        if args[:2] == ("worktree", "move"):
            raise ValueError("模拟工作树初始化失败")
        return real_git(cwd, *args)

    monkeypatch.setattr(business_repositories, "_git", fail)
    with pytest.raises(ValueError, match="模拟工作树初始化失败"):
        await execution.prepare_repositories(policy)
    copy = policy.repositories[0].checkout
    assert not copy.exists()
    assert not list(copy.parent.glob(".initializing-*"))
    assert str(copy.parent) not in git(source, "worktree", "list", "--porcelain")
    monkeypatch.setattr(business_repositories, "_git", real_git)
    await execution.prepare_repositories(policy)
    assert (copy / "article.md").read_text() == "原文\n"
    assert git(source, "status", "--porcelain") == ""


@pytest.mark.anyio
async def test_worktree_never_clones_and_preserves_dirty_source(isolated, monkeypatch):
    from vococo.core import business_repositories

    _, source, key = owner_with_repository(isolated)
    git(source, "remote", "add", "origin", "https://example.invalid/project.git")
    (source / "article.md").write_text("原目录未提交内容\n")
    (source / ".env").write_text("LOCAL_ONLY=1\n")
    before = git(source, "status", "--porcelain")
    real_git = business_repositories._git

    def no_clone(cwd, *args):
        assert args[0] != "clone"
        return real_git(cwd, *args)

    monkeypatch.setattr(business_repositories, "_git", no_clone)
    policy = execution.policy_for_session(key)
    await execution.prepare_repositories(policy)
    copy = policy.repositories[0].checkout
    assert (copy / "article.md").read_text() == "原文\n"
    assert not (copy / ".env").exists()
    assert git(copy, "remote", "get-url", "origin") == "https://example.invalid/project.git"
    assert git(source, "status", "--porcelain") == before
    assert (source / "article.md").read_text() == "原目录未提交内容\n"


@pytest.mark.anyio
async def test_legacy_clone_preserves_commits_and_uncommitted_work(isolated):
    _, source, key = owner_with_repository(isolated)
    policy = execution.policy_for_session(key)
    binding = policy.repositories[0]
    binding.checkout.parent.mkdir(parents=True)
    git(source, "clone", "--no-local", "--", str(source), str(binding.checkout))
    git(binding.checkout, "config", "vococo.source", str(source))
    (binding.checkout / "article.md").write_text("旧副本成果\n")
    git(binding.checkout, "add", ".")
    git(binding.checkout, "-c", "user.name=测试", "-c", "user.email=test@example.invalid",
        "commit", "-m", "旧成果")
    head = git(binding.checkout, "rev-parse", "HEAD")
    (binding.checkout / "draft.md").write_text("不能丢失\n")
    await execution.prepare_repositories(policy)
    assert (binding.checkout / ".git").is_dir()
    assert git(binding.checkout, "rev-parse", "HEAD") == head
    assert (binding.checkout / "draft.md").read_text() == "不能丢失\n"


@pytest.mark.anyio
async def test_parallel_initialization_reuses_same_worktree(isolated):
    import asyncio

    _, source, key = owner_with_repository(isolated)
    policy = execution.policy_for_session(key)
    await asyncio.gather(*(execution.prepare_repositories(policy) for _ in range(3)))
    copy = policy.repositories[0].checkout
    assert (copy / ".git").is_file()
    listing = git(source, "worktree", "list", "--porcelain")
    assert listing.count(f"worktree {copy}\n") == 1
    assert ".initializing-" not in listing


@pytest.mark.anyio
async def test_interrupted_worktree_initialization_recovers_without_losing_work(isolated):
    _, source, key = owner_with_repository(isolated)
    policy = execution.policy_for_session(key)
    binding = policy.repositories[0]
    staging = binding.checkout.parent / ".initializing-interrupted"
    staging.parent.mkdir(parents=True)
    git(source, "worktree", "add", "-b", binding.branch, str(staging))
    (staging / "draft.md").write_text("中断前的成果\n")
    await execution.prepare_repositories(policy)
    assert not staging.exists()
    assert (binding.checkout / "draft.md").read_text() == "中断前的成果\n"
    assert git(binding.checkout, "branch", "--show-current") == binding.branch
    await execution.prepare_repositories(policy)
    assert (binding.checkout / "draft.md").read_text() == "中断前的成果\n"


def test_prepare_repository_tool_is_registered(monkeypatch):
    from vococo.tools import builtin

    registered = []
    monkeypatch.setattr(builtin, "create_sdk_mcp_server",
                        lambda name, tools: registered.extend(tools) or {})
    builtin.build_mcp_servers()
    assert builtin.prepare_repository in registered


@pytest.mark.anyio
async def test_prepare_tool_initializes_only_requested_repository(isolated, monkeypatch):
    from vococo.tools import builtin

    owner, source, key = owner_with_repository(isolated)
    other = repository(isolated / "other")
    agents.update(owner["id"], repositories=[str(source), str(other)])
    policy = execution.policy_for_session(key)
    monkeypatch.setattr(danger, "current_session_key", lambda: key)
    result = await builtin.prepare_repository.handler({"path": str(source)})
    assert not result.get("isError")
    assert str(policy.repositories[0].checkout) in result["content"][0]["text"]
    assert (policy.repositories[0].checkout / "article.md").read_text() == "原文\n"
    assert not policy.repositories[1].checkout.exists()
    assert not (await builtin.prepare_repository.handler({"path": str(source)})).get("isError")
    agents.update(owner["id"], repositories=[])
    assert (await builtin.prepare_repository.handler({"path": str(source)}))["isError"]
    assert policy.repositories[0].checkout.exists()


@pytest.mark.anyio
async def test_prepare_tool_rejects_missing_session_or_unbound_path(isolated, monkeypatch):
    from vococo.tools import builtin

    _, source, key = owner_with_repository(isolated)
    other = repository(isolated / "unbound")
    monkeypatch.setattr(danger, "current_session_key", lambda: None)
    assert (await builtin.prepare_repository.handler({"path": str(source)}))["isError"]
    monkeypatch.setattr(danger, "current_session_key", lambda: key)
    assert (await builtin.prepare_repository.handler({"path": str(other)}))["isError"]
    assert (await builtin.prepare_repository.handler({"path": ""}))["isError"]
    assert not (config.AGENT_WORKSPACES_DIR / ".repositories").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("locked", [True, False])
async def test_timeout_cleanup_preserves_original_error_and_incomplete_work(isolated, monkeypatch, locked):
    from vococo.core import business_repositories

    _, source, key = owner_with_repository(isolated)
    binding = execution.policy_for_session(key).repositories[0]
    real_git = business_repositories._git

    def interrupt(cwd, *args):
        if args[:2] == ("worktree", "add"):
            real_git(cwd, *args[:2], "--no-checkout", *args[2:])
            staging = Path(args[-2])
            if locked:
                real_git(cwd, "worktree", "lock", "--reason", "initializing", str(staging))
            (staging / "draft.md").write_text("必须保留\n")
            raise subprocess.TimeoutExpired("git worktree add", 300)
        return real_git(cwd, *args)

    monkeypatch.setattr(business_repositories, "_git", interrupt)
    with pytest.raises(subprocess.TimeoutExpired):
        await execution.prepare_repositories(execution.policy_for_session(key))
    monkeypatch.setattr(business_repositories, "_git", real_git)
    staging, = binding.checkout.parent.glob(".initializing-*")
    with pytest.raises(ValueError, match="初始化未完成"):
        await execution.prepare_repositories(execution.policy_for_session(key))
    assert (staging / "draft.md").read_text() == "必须保留\n"
    assert not binding.checkout.exists()
    assert git(source, "status", "--porcelain") == ""


@pytest.mark.anyio
async def test_failed_move_preserves_ignored_work_and_can_resume(isolated, monkeypatch):
    from vococo.core import business_repositories

    _, source, key = owner_with_repository(isolated)
    policy = execution.policy_for_session(key)
    excludes = isolated / "ignore"
    excludes.write_text("draft.md\n")
    git(source, "config", "core.excludesfile", str(excludes))
    real_git = business_repositories._git

    def fail(cwd, *args):
        if args[:2] == ("worktree", "move"):
            staging = Path(args[2])
            (staging / "draft.md").write_text("不能丢弃\n")
            raise ValueError("模拟移动失败")
        return real_git(cwd, *args)

    monkeypatch.setattr(business_repositories, "_git", fail)
    with pytest.raises(ValueError, match="模拟移动失败"):
        await execution.prepare_repositories(policy)
    staging, = policy.repositories[0].checkout.parent.glob(".initializing-*")
    assert (staging / "draft.md").read_text() == "不能丢弃\n"
    monkeypatch.setattr(business_repositories, "_git", real_git)
    await execution.prepare_repositories(policy)
    assert (policy.repositories[0].checkout / "draft.md").read_text() == "不能丢弃\n"


def test_worktree_checkout_has_longer_timeout_than_metadata(isolated, monkeypatch):
    from vococo.core import business_repositories

    timeouts = []

    def run(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(business_repositories.subprocess, "run", run)
    business_repositories._git(isolated, "worktree", "add", "checkout", "HEAD")
    business_repositories._git(isolated, "worktree", "list", "--porcelain")
    assert timeouts == [300, 60]


@pytest.mark.anyio
async def test_foreign_worktree_is_rejected_without_removing_work(isolated):
    _, source, key = owner_with_repository(isolated)
    policy = execution.policy_for_session(key)
    binding = policy.repositories[0]
    other = repository(isolated / "other")
    binding.checkout.parent.mkdir(parents=True)
    git(other, "worktree", "add", "-b", binding.branch, str(binding.checkout))
    (binding.checkout / "draft.md").write_text("保留\n")
    with pytest.raises(ValueError, match="来源或分支不匹配"):
        await execution.prepare_repositories(policy)
    assert (binding.checkout / "draft.md").read_text() == "保留\n"
    assert git(source, "branch", "--show-current") == "main"


@pytest.mark.anyio
async def test_full_access_prepares_unlinked_repository_and_revokes_on_downgrade(isolated, monkeypatch):
    from vococo.core import business_repositories, permissions
    from vococo.gateway.git_status import _session_repositories
    from vococo.tools import builtin

    owner = agents.create("未关联开发")
    key = agents.main_session_key(owner)
    source = repository(isolated / "unlinked")
    (source / "article.md").write_text("原目录未提交内容\n")
    before = agents.get(owner["id"])
    monkeypatch.setattr(danger, "_current_session_key", lambda: key)
    permissions.set_session(key, "full")
    result = await builtin.prepare_repository.handler({"path": str(source)})
    assert not result.get("isError"), result
    binding, = business_repositories.copies_for_session(before, key)
    assert (binding.checkout / "article.md").read_text() == "原文\n"
    assert (source / "article.md").read_text() == "原目录未提交内容\n"
    assert agents.get(owner["id"]) == before
    assert session_store.repository_grants(key, owner["id"]) == {str(source): "full"}
    (binding.checkout / "article.md").write_text("副本未提交成果\n")
    assert not (await builtin.prepare_repository.handler({"path": str(source)})).get("isError")
    assert (binding.checkout / "article.md").read_text() == "副本未提交成果\n"
    assert str(binding.checkout) in execution.prompt_note(execution.policy_for_session(key))
    assert _session_repositories(key, before)[0]["path"] == str(binding.checkout)

    permissions.set_session(key, "standard")
    assert not business_repositories.copies_for_session(before, key)
    revoked = execution.policy_for_session(key)
    assert not danger._business_write_allowed({"file_path": str(binding.checkout / "article.md")}, str(revoked.workspace), revoked)
    result = await builtin.prepare_repository.handler({"path": str(source)})
    assert result.get("isError")  # 无交互审批时保守拒绝，不能复用 full 授权。
    assert binding.checkout.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("approved", [False, True])
async def test_standard_unlinked_repository_approval_is_session_scoped(isolated, monkeypatch, approved):
    from vococo.core import business_repositories
    from vococo.tools import builtin

    owner = agents.create("标准权限")
    key = agents.main_session_key(owner)
    source = repository(isolated / "unlinked")
    monkeypatch.setattr(danger, "_current_session_key", lambda: key)
    calls = []

    async def approve(reason, detail, **kwargs):
        calls.append(detail)
        return approved

    monkeypatch.setattr(danger, "require_approval", approve)
    result = await builtin.prepare_repository.handler({"path": str(source)})
    assert bool(result.get("isError")) is not approved
    assert calls == [str(source)]
    assert agents.get(owner["id"])["repositories"] == []
    assert agents.get(owner["id"])["links"] == []
    if not approved:
        assert not session_store.repository_grants(key, owner["id"])
        assert not (config.AGENT_WORKSPACES_DIR / ".repositories").exists()
        return
    assert session_store.repository_grants(key, owner["id"]) == {str(source): "approved"}
    binding, = business_repositories.copies_for_session(owner, key)
    assert not (await builtin.prepare_repository.handler({"path": str(source)})).get("isError")
    assert len(calls) == 1
    other_key = config.new_session_key()
    session_store.bind_session(other_key, agent_id=owner["id"])
    assert not business_repositories.copies_for_session(owner, other_key)
    session_store.clear(key)
    assert not session_store.repository_grants(key, owner["id"])
    assert binding.checkout.exists()  # 清理授权不能删除成果。


@pytest.mark.anyio
async def test_full_unlinked_copies_are_isolated_and_expire(isolated, monkeypatch):
    from vococo.core import business_repositories, permissions
    from vococo.tools import builtin

    owner = agents.create("会话隔离")
    source = repository(isolated / "unlinked")
    keys = [config.new_session_key(), config.new_session_key()]
    copies = []
    for key in keys:
        session_store.bind_session(key, agent_id=owner["id"])
        permissions.set_session(key, "full")
        monkeypatch.setattr(danger, "_current_session_key", lambda: key)
        assert not (await builtin.prepare_repository.handler({"path": str(source)})).get("isError")
        copies.append(business_repositories.copies_for_session(owner, key)[0])
    assert copies[0].checkout != copies[1].checkout
    session_store.set_permission(keys[0], "full", until=1)
    assert not business_repositories.copies_for_session(owner, keys[0])
    assert business_repositories.copies_for_session(owner, keys[1])
    session_store.delete_session(keys[0])
    assert not session_store.repository_grants(keys[0], owner["id"])


@pytest.mark.anyio
async def test_unlinked_invalid_or_foreign_copy_never_creates_grant(isolated, monkeypatch):
    from vococo.core import business_repositories, permissions
    from vococo.tools import builtin

    owner = agents.create("路径检查")
    key = agents.main_session_key(owner)
    monkeypatch.setattr(danger, "_current_session_key", lambda: key)
    permissions.set_session(key, "full")
    source = repository(isolated / "source")
    for path in ("", "relative", str(isolated), str(source / "missing")):
        assert (await builtin.prepare_repository.handler({"path": path})).get("isError")
    assert not session_store.repository_grants(key, owner["id"])
    assert not (await builtin.prepare_repository.handler({"path": str(source)})).get("isError")
    binding, = business_repositories.copies_for_session(owner, key)
    result = await builtin.prepare_repository.handler({"path": str(binding.checkout)})
    assert result.get("isError")
    assert "原仓库" in result["content"][0]["text"]


@pytest.mark.anyio
@pytest.mark.parametrize("replace_root", [False, True])
async def test_approval_is_bound_to_canonical_repository(isolated, monkeypatch, replace_root):
    from vococo.tools import builtin

    owner = agents.create("路径审批")
    key = agents.main_session_key(owner)
    source = repository(isolated / "source")
    other = repository(isolated / "other")
    link = isolated / "link"
    link.symlink_to(source, target_is_directory=True)
    monkeypatch.setattr(danger, "_current_session_key", lambda: key)

    async def approve(reason, detail, **kwargs):
        assert detail == str(source)
        if replace_root:
            source.rename(isolated / "moved")
            source.symlink_to(other, target_is_directory=True)
        else:
            link.unlink()
            link.symlink_to(other, target_is_directory=True)
        return True

    monkeypatch.setattr(danger, "require_approval", approve)
    result = await builtin.prepare_repository.handler({"path": str(link)})
    assert bool(result.get("isError")) == replace_root
    grants = session_store.repository_grants(key, owner["id"])
    assert str(other) not in grants
    assert grants == ({} if replace_root else {str(source): "approved"})
    assert "vococo/business-" not in git(other, "branch", "--list")


@pytest.mark.anyio
async def test_repository_validation_runs_off_event_loop(isolated, monkeypatch):
    import threading
    from vococo.core import business_repositories
    from vococo.tools import builtin

    owner, source, key = owner_with_repository(isolated)
    monkeypatch.setattr(danger, "_current_session_key", lambda: key)
    loop_thread = threading.get_ident()
    normalize = business_repositories.normalize_repositories
    threads = []

    def validate(paths):
        threads.append(threading.get_ident())
        return normalize(paths)

    monkeypatch.setattr(business_repositories, "normalize_repositories", validate)
    assert not (await builtin.prepare_repository.handler({"path": str(source)})).get("isError")
    assert threads and loop_thread not in threads


@pytest.mark.anyio
async def test_repository_grants_survive_reopen_but_not_restricted_source(isolated, monkeypatch):
    from vococo.core import business_repositories, permissions
    from vococo.memory import _db
    from vococo.tools import builtin

    owner = agents.create("持久化")
    key = agents.main_session_key(owner)
    source = repository(isolated / "source")
    monkeypatch.setattr(danger, "_current_session_key", lambda: key)
    permissions.set_session(key, "full")
    assert not (await builtin.prepare_repository.handler({"path": str(source)})).get("isError")
    _db.reset()
    assert len(business_repositories.copies_for_session(owner, key)) == 1
    monkeypatch.setattr(session_store, "active_source_access", lambda _: {"mode": "standard"})
    assert not business_repositories.copies_for_session(owner, key)
    session_store.grant_repository(key, owner["id"], str(source), "approved")
    assert not business_repositories.copies_for_session(owner, key)


@pytest.mark.anyio
async def test_project_agent_prepares_additional_unlinked_repository(isolated, monkeypatch):
    from vococo.core import business_repositories, permissions
    from vococo.gateway.git_status import _session_repositories
    from vococo.tools import builtin

    primary = repository(isolated / "primary")
    source = repository(isolated / "skills")
    owner = agents.create("开发", str(primary))
    key = agents.main_session_key(owner)
    assert owner["execution_mode"] == "project"
    monkeypatch.setattr(danger, "_current_session_key", lambda: key)
    permissions.set_session(key, "full")
    result = await builtin.prepare_repository.handler({"path": str(source)})
    assert not result.get("isError"), result
    binding, = business_repositories.copies_for_session(owner, key)
    assert binding.source == source
    assert session_store.get_worktree(key) is None  # 不抢占主项目副本绑定。
    rows = _session_repositories(key, owner)
    assert {row["project_path"] for row in rows} == {str(primary), str(source)}
    permissions.set_session(key, "standard")
    assert not danger._inside_agent_writable(str(binding.checkout / "article.md"), str(primary))
