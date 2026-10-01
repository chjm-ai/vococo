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


def _call(tool, args: dict) -> str:
    return asyncio.run(tool.handler(args))["content"][0]["text"]


def _save(args: dict) -> str:
    return _call(builtin.save_memory, args)


def _note(args: dict) -> str:
    return _call(builtin.note_memory, args)


def _in_session(key: str):
    return danger.set_task_session(key)


ARGS = {"topic": "lemlist-tips", "title": "Lemlist 发信", "summary": "周一回复率高", "body": "细节"}
LESSONS = "# 经验教训\n\n| 场景 | 可复用规则 |\n|---|---|\n| 验收 | 走真实路径 |\n\n## 何时查看\n\n正文\n"
DECISIONS = "# 技术决策\n\n| 日期 | 决策 | 原因 | 项目 |\n|------|------|------|------|\n| 2026-06-01 | 老决策 | 因为 | vococo |\n"


def test_memory_dir_follows_project_folder(env, tmp_path):
    from vococo.memory import deposit

    assert deposit.agent_memory_dir(env) == tmp_path / "brain" / "memory" / "proj"
    ws = agents.create("新建的")  # 默认工作目录叫 workspace,不能拿来当目录名
    assert deposit.agent_memory_dir(ws) == tmp_path / "brain" / "memory" / "agents" / ws["id"]
    assert deposit.agent_memory_dir({**env, "workdir": "/x/people"}).parent.name == "agents"  # 撞保留目录名


def test_save_memory_goes_to_agent_by_default(env, tmp_path):
    tok = _in_session(f"web:p{env['project_hash']}:c1")
    try:
        out = _save(ARGS)
        assert "专用记忆" in out
        d = tmp_path / "brain" / "memory" / "proj"
        assert "周一回复率高" in (d / "lemlist-tips.md").read_text(encoding="utf-8")
        index = (d / "INDEX.md").read_text(encoding="utf-8")
        assert index.startswith("# proj 记忆索引") and "→ lemlist-tips.md — 周一回复率高" in index
        assert not (tmp_path / "brain" / "memory" / "lemlist-tips.md").exists()  # 不进全局
        assert "lemlist-tips" not in (tmp_path / "brain" / "MEMORY.md").read_text(encoding="utf-8")
        assert "已存在" in _save(ARGS)  # 不覆盖
        assert "已写入" in _save({**ARGS, "topic": "pref-x", "scope": "global"})  # scope=global 照旧进全局
        assert (tmp_path / "brain" / "memory" / "pref-x.md").exists()
    finally:
        danger.reset_task_session(tok)


def test_note_memory_routes_by_scope(env, tmp_path):
    mem = tmp_path / "brain" / "memory"
    (mem / "lessons.md").write_text(LESSONS, encoding="utf-8")
    (mem / "tech-decisions.md").write_text(DECISIONS, encoding="utf-8")
    tok = _in_session(f"web:p{env['project_hash']}:c1")
    try:
        assert "「proj」专用" in _note({"kind": "lesson", "text": "Lemlist 分页要带 offset", "label": "Lemlist"})
        assert "专用" in _note({"kind": "decision", "text": "开发信改周一发"})
        assert "通用" in _note({"kind": "lesson", "text": "统计查询别用 head 截断", "label": "日志|查询", "scope": "global"})
        assert "通用" in _note({"kind": "decision", "text": "记忆分区", "label": "防污染", "scope": "global"})
        assert "通用" in _note({"kind": "preference", "text": "报告先给结论", "label": "汇报"})  # 偏好不看 scope
        assert "参数不对" in _note({"kind": "bad", "text": "x"})
    finally:
        danger.reset_task_session(tok)
    d = mem / "proj"
    assert "**Lemlist**:Lemlist 分页要带 offset" in (d / "lessons.md").read_text(encoding="utf-8")
    assert "开发信改周一发" in (d / "decisions.md").read_text(encoding="utf-8")
    index = (d / "INDEX.md").read_text(encoding="utf-8")
    assert index.count("→ lessons.md") == 1 and "→ decisions.md" in index  # 首次建文件才登记,不重复
    lessons = (mem / "lessons.md").read_text(encoding="utf-8")
    # 插进第一张表末尾(不是文件末尾),竖线换成全角不撑坏表格
    assert "| 验收 | 走真实路径 |\n| 日志｜查询 | 统计查询别用 head 截断 |\n\n## 何时查看" in lessons
    assert "| 记忆分区 | 防污染 | 通用 |" in (mem / "tech-decisions.md").read_text(encoding="utf-8")
    assert "## 汇报(" in (mem / "preferences.md").read_text(encoding="utf-8")


def test_save_memory_plain_session_goes_global(env, tmp_path):
    tok = _in_session("web:abc")
    try:
        assert "已写入" in _save(ARGS)
        assert "通用" in _note({"kind": "lesson", "text": "x"})
    finally:
        danger.reset_task_session(tok)
    assert (tmp_path / "brain" / "memory" / "lemlist-tips.md").exists()


def test_own_index_injected_for_agent(env, tmp_path):
    d = tmp_path / "brain" / "memory" / "proj"
    d.mkdir()
    (d / "INDEX.md").write_text("# proj 记忆索引\n\n→ a.md — 自己的东西\n", encoding="utf-8")
    part = prompt.build_system_prompt(None, memory_sections=["经验教训"], own_memory_dir=str(d))["append"]
    assert "自己的东西" in part and "lessons.md" in part
    assert "自己的东西" not in prompt.build_system_prompt(None)["append"]
    agents.write_doc(env["id"], "AGENT.md", "# proj\n\n## 职责与人格\n\n管询盘")
    extra = agents.prompt_extra(agents.get(env["id"]))
    assert "## 记忆归属" in extra and str(d) in extra and "note_memory" in extra


def test_turn_env_disables_auto_memory_only_for_agents():
    assert agent._turn_env({}, no_auto_memory=True)["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in agent._turn_env({})
    # 缓存按分节分开存,None 和以前同一个 key
    assert agent._prompt_cache_key("/p") == agent._prompt_cache_key("/p", None)
    mem = {"sections": ["a"], "own_dir": "/brain/memory/p"}
    assert agent._prompt_cache_key("/p", mem) != agent._prompt_cache_key("/p")
    assert agent._prompt_cache_key("/p", mem) != agent._prompt_cache_key("/p", {**mem, "own_dir": "/x"})


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


VOCO_INDEX = "# vococo 项目记忆索引\n\n> 说明一\n> 说明二\n\n→ old.md — 老条目\n\n## 相关但未归入本目录\n\n→ other.md — 别处的\n"


def test_register_goes_to_auto_section_at_top(env, tmp_path):
    from vococo.memory import deposit

    d = deposit.agent_memory_dir(env)
    d.mkdir(parents=True)
    (d / "INDEX.md").write_text(VOCO_INDEX, encoding="utf-8")
    deposit.note(env, "lesson", "坑一")
    deposit.note(env, "decision", "决策一")
    deposit.save_topic(env, "nas-x", "NAS", "摘要\n带换行", "正文")
    text = (d / "INDEX.md").read_text(encoding="utf-8")
    head, _, rest = text.partition("→ old.md")
    # 三条都在开头的「自动登记」里(在原有条目和「相关但未归入」那节之前),按登记顺序,摘要换行被压平
    assert "## 自动登记\n→ lessons.md — 踩坑记录(按日期追加)\n→ decisions.md — 决策记录(按日期追加)\n→ nas-x.md — 摘要 带换行\n" in head
    assert head.startswith("# vococo 项目记忆索引\n\n> 说明一\n> 说明二\n\n## 自动登记")
    assert "→ other.md — 别处的" in rest  # 原有内容不动
    with pytest.raises(ValueError):
        deposit.save_topic(env, "INDEX", "x", "y", "z")  # 保留名


def test_same_folder_name_gets_suffix(env, tmp_path):
    from vococo.memory import deposit

    other = tmp_path / "elsewhere" / "proj"  # 和 env 同名的另一个项目文件夹
    other.mkdir(parents=True)
    b = agents.create("另一个", str(other))
    assert deposit.agent_memory_dir(env).name == "proj"  # 先建的占名
    assert deposit.agent_memory_dir(b).name == f"proj-{b['id'][:6]}"
