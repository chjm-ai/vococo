"""system prompt 组装:项目 AGENTS.md 注入规则。"""
from pathlib import Path

from claude_hermes.core.prompt import _load_project_agents, build_system_prompt


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


def test_none_and_missing_skip(tmp_path: Path):
    """cwd=None、或目录里两个文件都没有 → 跳过。"""
    assert _load_project_agents(None) == ""
    assert _load_project_agents(str(tmp_path)) == ""


def test_build_system_prompt_threads_cwd(tmp_path: Path):
    """端到端:build_system_prompt(cwd) 把项目块拼进 append;None 则不含。"""
    (tmp_path / "AGENTS.md").write_text("PROJECT_RULE_ALPHA", encoding="utf-8")
    assert "PROJECT_RULE_ALPHA" in build_system_prompt(str(tmp_path))["append"]
    assert "本项目指南" not in build_system_prompt(None)["append"]
