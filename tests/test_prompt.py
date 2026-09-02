"""system prompt 组装:项目 AGENTS.md 注入规则 + 与 SDK 自带注入的去重。"""
import os
import time
from pathlib import Path

from vococo.core.prompt import (
    _load_memory_index,
    _load_project_agents,
    build_system_prompt,
)


def test_agents_only_injected(tmp_path: Path):
    """只有 AGENTS.md → 注入。"""
    (tmp_path / "AGENTS.md").write_text("PROJECT_RULE_ALPHA", encoding="utf-8")
    out = _load_project_agents(str(tmp_path))
    assert "PROJECT_RULE_ALPHA" in out and "本项目指南" in out


def test_agents_injected_even_with_claude_md(tmp_path: Path):
    """AGENTS.md + CLAUDE.md 并存 → 仍注入 AGENTS.md。

    本仓约定 CLAUDE.md 只放一句指向 AGENTS.md 的指路桩,SDK 读桩、Agent 读 AGENTS.md,
    不因 CLAUDE.md 存在而跳过——否则真规则两头落空(曾致模型认错项目)。
    """
    (tmp_path / "AGENTS.md").write_text("PROJECT_RULE_ALPHA", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("规则见 AGENTS.md", encoding="utf-8")
    assert "PROJECT_RULE_ALPHA" in _load_project_agents(str(tmp_path))


def test_agents_skipped_when_claude_md_is_the_same_file(tmp_path: Path):
    """CLAUDE.md 是 AGENTS.md 的软链(本仓即如此)→ 跳过,别逐字注两遍。

    SDK 按 cwd 读 CLAUDE.md 时已经把同一份文件注进 system prompt 了。
    """
    (tmp_path / "AGENTS.md").write_text("PROJECT_RULE_ALPHA", encoding="utf-8")
    (tmp_path / "CLAUDE.md").symlink_to(tmp_path / "AGENTS.md")
    assert _load_project_agents(str(tmp_path)) == ""


def test_memory_index_skipped_when_sdk_auto_memory_is_same_file(tmp_path, monkeypatch):
    """auto-memory 软链到同一份 MEMORY.md → 只留指针,不重复注全文(省约 6k token)。"""
    from vococo.core import prompt as prompt_mod

    brain = tmp_path / "AI_BRAIN"
    brain.mkdir()
    (brain / "MEMORY.md").write_text("MEMORY_LINE_ALPHA", encoding="utf-8")
    cwd = tmp_path / "Repos" / "proj"
    cwd.mkdir(parents=True)
    # 造 SDK 的 auto-memory:~/.claude/projects/<slug>/memory/MEMORY.md 软链到主库
    home = tmp_path / "home"
    auto = home / ".claude" / "projects" / prompt_mod._slug(str(cwd.resolve())) / "memory"
    auto.mkdir(parents=True)
    (auto / "MEMORY.md").symlink_to(brain / "MEMORY.md")
    monkeypatch.setattr(prompt_mod.config, "AI_BRAIN_DIR", brain)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    out = _load_memory_index(str(cwd))
    assert "MEMORY_LINE_ALPHA" not in out and "auto-memory" in out


def test_memory_index_injected_when_no_auto_memory(tmp_path, monkeypatch):
    """没有 auto-memory 的项目 → 照旧注全文,别把索引弄丢了。"""
    from vococo.core import prompt as prompt_mod

    brain = tmp_path / "AI_BRAIN"
    brain.mkdir()
    (brain / "MEMORY.md").write_text("MEMORY_LINE_ALPHA", encoding="utf-8")
    monkeypatch.setattr(prompt_mod.config, "AI_BRAIN_DIR", brain)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty_home"))

    assert "MEMORY_LINE_ALPHA" in _load_memory_index(str(tmp_path))


def test_none_and_missing_skip(tmp_path: Path):
    """cwd=None、或目录里两个文件都没有 → 跳过。"""
    assert _load_project_agents(None) == ""
    assert _load_project_agents(str(tmp_path)) == ""


def test_build_system_prompt_threads_cwd(tmp_path: Path):
    """端到端:build_system_prompt(cwd) 把项目块拼进 append;None 则不含。"""
    (tmp_path / "AGENTS.md").write_text("PROJECT_RULE_ALPHA", encoding="utf-8")
    assert "PROJECT_RULE_ALPHA" in build_system_prompt(str(tmp_path))["append"]
    assert "本项目指南" not in build_system_prompt(None)["append"]


def test_prompt_forbids_duplicate_image_send():
    out = build_system_prompt(None)["append"]
    assert "generate_image" in out
    assert "不得再对该图片调用" in out


def test_cache_invalidated_on_agents_mtime_change(tmp_path: Path):
    """同一 cache_key 下 AGENTS.md 修改后,冻结快照自动失效重装。

    规则修复必须对旧会话即时生效——否则旧会话按旧版跑,还把新脚本报错当 bug
    (2026-08-04 踩过:--restart 已移除,旧会话仍按旧指引私改 restart.sh)。
    """
    agents = tmp_path / "AGENTS.md"
    agents.write_text("RULE_V1", encoding="utf-8")
    key = "test-cache-key"
    assert "RULE_V1" in build_system_prompt(str(tmp_path), cache_key=key)["append"]
    # 第二次同 key:快照命中,仍是 V1
    assert "RULE_V1" in build_system_prompt(str(tmp_path), cache_key=key)["append"]
    # 改 AGENTS.md 且 mtime 前移(避免同秒粒度问题)→ 快照作废,重装出新版
    agents.write_text("RULE_V2", encoding="utf-8")
    os.utime(agents, (time.time() + 60, time.time() + 60))
    out = build_system_prompt(str(tmp_path), cache_key=key)["append"]
    assert "RULE_V2" in out and "RULE_V1" not in out
