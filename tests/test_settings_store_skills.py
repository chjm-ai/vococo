"""settings_store.effective_skills:全局 Skill + 显式 Git 项目的 coding Profile。"""
import json

import pytest

from vococo.gateway import settings_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    """把设置文件指到临时目录,别动真库。"""
    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "web_settings.json")

    def write(data: dict):
        (tmp_path / "web_settings.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    return write


def _git_repo(path):
    path.mkdir()
    (path / ".git").mkdir()
    return path


def test_falls_back_to_global_whitelist(store, tmp_path):
    """没选项目或目录不是 Git 工作区 → 照旧走全局 custom 白名单。"""
    store({"skills_mode": "custom", "skills_enabled": ["alpha", "beta"]})
    assert settings_store.effective_skills(str(tmp_path)) == ["alpha", "beta"]
    assert settings_store.effective_skills(None) == ["alpha", "beta"]
    assert settings_store.effective_skills(str(tmp_path), is_explicit_project=True) == ["alpha", "beta"]


def test_explicit_git_project_uses_coding_profile(store, tmp_path):
    """用户显式选择的任意 Git 仓库都加载 coding Profile。"""
    repo = _git_repo(tmp_path / "new-project")
    store({
        "skills_mode": "custom",
        "skills_enabled": ["assistant"],
        "skill_profiles": {"coding": ["code"]},
    })

    assert settings_store.effective_skills(str(repo), is_explicit_project=True) == ["code"]


def test_explicit_git_subdirectory_uses_coding_profile(store, tmp_path):
    """从仓库子目录启动也应识别为代码项目。"""
    repo = _git_repo(tmp_path / "repo")
    subdir = repo / "packages" / "web"
    subdir.mkdir(parents=True)
    store({"skill_profiles": {"coding": ["code"]}})

    assert settings_store.effective_skills(str(subdir), is_explicit_project=True) == ["code"]


def test_explicit_git_worktree_uses_coding_profile(store, tmp_path):
    """linked worktree 的 .git 是文件，仍应识别为 Git 工作区。"""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /tmp/repo/.git/worktrees/session\n")
    store({"skill_profiles": {"coding": ["code"]}})

    assert settings_store.effective_skills(str(worktree), is_explicit_project=True) == ["code"]


def test_default_cwd_does_not_use_coding_profile(store, tmp_path):
    """普通聊天即使默认 cwd 恰好是 Git 仓库，也不能误进编码模式。"""
    repo = _git_repo(tmp_path / "repo")
    store({
        "skills_mode": "custom",
        "skills_enabled": ["assistant"],
        "skill_profiles": {"coding": ["code"]},
    })

    assert settings_store.effective_skills(str(repo)) == ["assistant"]


def test_legacy_path_configuration_is_ignored(store, tmp_path):
    """路径白名单已废弃，Git 项目一律由 coding Profile 决定。"""
    repo = _git_repo(tmp_path / "repo")
    store({
        "skills_mode": "custom",
        "skills_enabled": ["assistant"],
        "skills_by_project": {str(repo): ["legacy"]},
        "skill_profiles": {"coding": ["code"]},
        "project_profiles": {str(repo): "other"},
    })

    assert settings_store.effective_skills(str(repo), is_explicit_project=True) == ["code"]


def test_coding_skills_inherit_general_until_first_change(store, monkeypatch):
    """未单独配置 coding 时继承通用名单；首次改动再复制为独立名单。"""
    monkeypatch.setattr(settings_store, "_scan_skills", lambda: [
        {"name": "assistant", "description": ""}, {"name": "code", "description": ""},
    ])
    store({"skills_mode": "custom", "skills_enabled": ["assistant"]})

    before = {item["name"]: item for item in settings_store.list_skills()}
    assert settings_store.coding_skills_mode() == "inherit"
    assert before["assistant"]["coding_enabled"] is True
    assert before["code"]["coding_enabled"] is False

    settings_store.set_skill("code", enabled=True, scope="coding")

    after = {item["name"]: item for item in settings_store.list_skills()}
    assert settings_store.coding_skills_mode() == "custom"
    assert after["assistant"]["enabled"] is True  # 通用名单没被改动
    assert after["code"]["enabled"] is False
    assert after["assistant"]["coding_enabled"] is True
    assert after["code"]["coding_enabled"] is True


def test_reset_coding_skills_returns_to_general_inheritance(store, monkeypatch):
    monkeypatch.setattr(settings_store, "_scan_skills", lambda: [
        {"name": "assistant", "description": ""}, {"name": "code", "description": ""},
    ])
    store({
        "skills_mode": "custom",
        "skills_enabled": ["assistant"],
        "skill_profiles": {"coding": ["code"]},
    })

    settings_store.reset_coding_skills()

    items = {item["name"]: item for item in settings_store.list_skills()}
    assert settings_store.coding_skills_mode() == "inherit"
    assert items["assistant"]["coding_enabled"] is True
    assert items["code"]["coding_enabled"] is False
