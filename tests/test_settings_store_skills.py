"""settings_store.effective_skills:全局白名单 + 项目级收敛。"""
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


def test_falls_back_to_global_whitelist(store, tmp_path):
    """没配项目白名单 → 照旧走全局 custom 白名单。"""
    store({"skills_mode": "custom", "skills_enabled": ["alpha", "beta"]})
    assert settings_store.effective_skills(str(tmp_path)) == ["alpha", "beta"]
    assert settings_store.effective_skills(None) == ["alpha", "beta"]


def test_project_whitelist_wins(store, tmp_path):
    """cwd 命中项目配置 → 用项目白名单,不用全局那份。"""
    proj = tmp_path / "repo"
    proj.mkdir()
    store({
        "skills_mode": "custom",
        "skills_enabled": ["alpha", "beta"],
        "skills_by_project": {str(proj): ["only-this"]},
    })
    assert settings_store.effective_skills(str(proj)) == ["only-this"]
    # 别的目录不受影响
    assert settings_store.effective_skills(str(tmp_path)) == ["alpha", "beta"]


def test_worktree_inherits_repo_config(store, tmp_path):
    """项目会话跑在 <repo>/data/worktrees/… 里,只配主仓库一条就该继承。"""
    proj = tmp_path / "repo"
    wt = proj / "data" / "worktrees" / "abc123" / "sess"
    wt.mkdir(parents=True)
    store({"skills_by_project": {str(proj): ["only-this"]}})
    assert settings_store.effective_skills(str(wt)) == ["only-this"]


def test_deepest_match_wins(store, tmp_path):
    """父子目录都配了 → 取更具体(路径更深)的那条。"""
    parent = tmp_path / "repos"
    child = parent / "vococo"
    child.mkdir(parents=True)
    store({"skills_by_project": {str(parent): ["broad"], str(child): ["narrow"]}})
    assert settings_store.effective_skills(str(child)) == ["narrow"]
    assert settings_store.effective_skills(str(parent)) == ["broad"]


def test_broken_entry_ignored(store, tmp_path):
    """配置被手改坏(值不是列表)→ 忽略该条,不崩,回落全局。"""
    proj = tmp_path / "repo"
    proj.mkdir()
    store({
        "skills_mode": "custom",
        "skills_enabled": ["alpha"],
        "skills_by_project": {str(proj): "not-a-list"},
    })
    assert settings_store.effective_skills(str(proj)) == ["alpha"]
