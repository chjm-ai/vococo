"""记忆分区:项目 Agent 只带相关的全局记忆分节、关 CLI auto-memory,save_memory 默认存进 Agent 自己。"""
from __future__ import annotations

import asyncio

import pytest

from vococo import config
from vococo.core import agent, prompt
from vococo.memory import agents, projects
from vococo.tools import builtin, danger

MEMORY_MD = """# 记忆索引

## 用户偏好
→ memory/preferences.md — 偏好

## 经验教训
→ memory/lessons.md — 教训

## 服务器 / 基础设施
→ memory/nas.md — 群晖 NAS

## vococo 项目（原 claude-hermes / Wazir 开发）
→ memory/vococo/INDEX.md — vococo 索引

## 健康与生活
→ memory/sleep.md — 睡眠
"""


@pytest.fixture
def env(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CRON_JOBS_PATH", tmp_path / "data" / "cron_jobs.json")
    brain = tmp_path / "brain"
    (brain / "memory").mkdir(parents=True)
    (brain / "MEMORY.md").write_text(MEMORY_MD, encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    projects.upsert_project(str(proj))
    return agents.by_project_hash(projects.project_hash(str(proj)))


def test_filter_keeps_chosen_and_names_the_rest():
    out = prompt.filter_memory_sections(MEMORY_MD, ["用户偏好", "vococo 项目"])
    assert "memory/preferences.md" in out and "vococo/INDEX.md" in out  # 括号说明不影响匹配
    assert "nas.md" not in out and "sleep.md" not in out
    assert "经验教训、服务器 / 基础设施、健康与生活" in out  # 没展开的只列名字
    assert prompt.filter_memory_sections(MEMORY_MD, []).startswith("# 记忆索引")


def test_section_titles_and_build_prompt(env):
    assert prompt.memory_section_titles() == ["用户偏好", "经验教训", "服务器 / 基础设施", "vococo 项目", "健康与生活"]
    part = prompt.build_system_prompt(None, memory_sections=["经验教训"])["append"]
    assert "lessons.md" in part and "sleep.md" not in part
    full = prompt.build_system_prompt(None)["append"]
    assert "sleep.md" in full or "auto-memory" in full  # None = 老行为(整份,或指向 SDK 已注入的那份)


def test_runtime_sections_default_custom_general(env):
    key = f"web:p{env['project_hash']}:c1"
    assert agents.runtime_for_session(key)["memory_sections"] == list(agents.DEFAULT_MEMORY_SECTIONS)
    agents.update(env["id"], memory_sections=["健康与生活"])
    assert agents.runtime_for_session(key)["memory_sections"] == ["健康与生活"]
    assert agents.runtime_for_session(config.SESSION_KEY)["memory_sections"] is None  # 总助理 = 整份
    assert agents.runtime_for_session("web:abc")["memory_sections"] is None
    with pytest.raises(ValueError):
        agents.update(agents.GENERAL_ID, memory_sections=[])


def _save(args: dict) -> str:
    return asyncio.run(builtin.save_memory.handler(args))["content"][0]["text"]


def _in_session(key: str):
    return danger.set_task_session(key)


ARGS = {"topic": "lemlist-tips", "title": "Lemlist 发信", "summary": "周一回复率高", "body": "细节"}


def test_save_memory_goes_to_agent_by_default(env, tmp_path):
    tok = _in_session(f"web:p{env['project_hash']}:c1")
    try:
        out = _save(ARGS)
        assert "自己的记忆" in out
        # 文件仍在 AI_BRAIN(唯一主库),但在 agents/<id>/ 下、不登记全局 MEMORY.md
        f = tmp_path / "brain" / "memory" / "agents" / env["id"] / "lemlist-tips.md"
        assert "周一回复率高" in f.read_text(encoding="utf-8")
        notes = agents.read_doc(env["id"], "NOTES.md")
        assert f"## 记忆\n- [Lemlist 发信]({f}) — 周一回复率高" in notes
        assert not (tmp_path / "brain" / "memory" / "lemlist-tips.md").exists()
        assert "lemlist-tips" not in (tmp_path / "brain" / "MEMORY.md").read_text(encoding="utf-8")
        assert "已存在" in _save(ARGS)  # 不覆盖
        # 第二条追加在同一小节里
        _save({**ARGS, "topic": "seo", "title": "SEO", "summary": "内链不足"})
        notes = agents.read_doc(env["id"], "NOTES.md")
        assert notes.count("## 记忆") == 1 and notes.index("lemlist-tips") < notes.index("/seo.md")
        # scope=global 照旧进 AI_BRAIN
        assert "已写入" in _save({**ARGS, "topic": "pref-x", "scope": "global"})
        assert (tmp_path / "brain" / "memory" / "pref-x.md").exists()
    finally:
        danger.reset_task_session(tok)


def test_save_memory_keeps_existing_notes(env):
    agents.write_doc(env["id"], "NOTES.md", "# 笔记\n\n- 手写的一条\n\n## 联系人\n\n- 张三\n")
    tok = _in_session(f"web:p{env['project_hash']}:c1")
    try:
        _save(ARGS)
    finally:
        danger.reset_task_session(tok)
    notes = agents.read_doc(env["id"], "NOTES.md")
    assert "- 手写的一条" in notes and "- 张三" in notes and notes.rstrip().endswith("周一回复率高")


def test_save_memory_plain_session_goes_global(env, tmp_path):
    tok = _in_session("web:abc")
    try:
        assert "已写入" in _save(ARGS)
    finally:
        danger.reset_task_session(tok)
    assert (tmp_path / "brain" / "memory" / "lemlist-tips.md").exists()


def test_turn_env_disables_auto_memory_only_for_agents():
    assert agent._turn_env({}, no_auto_memory=True)["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in agent._turn_env({})
    # 缓存按分节分开存,None 和以前同一个 key
    assert agent._prompt_cache_key("/p") == agent._prompt_cache_key("/p", None)
    assert agent._prompt_cache_key("/p", ["a"]) != agent._prompt_cache_key("/p")


def test_notes_index_when_heading_is_last_line(env):
    agents.write_doc(env["id"], "NOTES.md", "- 旧笔记\n\n## 记忆")  # 网页保存常不带结尾换行
    agents._append_notes_index(env["id"], "- 新的一条")
    notes = agents.read_doc(env["id"], "NOTES.md")
    assert notes.count("## 记忆") == 1 and notes.rstrip().endswith("## 记忆\n- 新的一条")


def test_prompt_extra_trims_notes_not_rules(env, monkeypatch):
    monkeypatch.setattr(agents, "PROMPT_MAX_CHARS", 1200)
    agents.write_doc(env["id"], "AGENT.md", "# proj\n\n## 职责与人格\n\n管询盘")
    agents.write_doc(env["id"], "NOTES.md", "- 很老的笔记\n" + "- 填充\n" * 400 + "## 记忆\n- [最新](x.md) — 最新一条\n")
    agents.update(env["id"], links=[{"path": str(env["workdir"]), "note": "项目"}])
    extra = agents.prompt_extra(agents.get(env["id"]))
    assert len(extra) <= 1200
    assert "## 记忆归属" in extra and "## 关联目录" in extra  # 规则和关联不被截
    assert "最新一条" in extra and "很老的笔记" not in extra  # 笔记截前面、留最新


def test_filter_before_clip(env, monkeypatch, tmp_path):
    monkeypatch.setattr(prompt, "_INJECT_MAX_CHARS", 200)
    big = MEMORY_MD.replace("## 健康与生活", "## 填充\n" + "x" * 500 + "\n\n## 健康与生活")
    (tmp_path / "brain" / "MEMORY.md").write_text(big, encoding="utf-8")
    out = prompt._load_memory_sections(["健康与生活"])
    assert "sleep.md" in out  # 排在超长分节后面,照样注得进来
    assert "健康与生活" in prompt.memory_section_titles()
