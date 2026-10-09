"""待拍板事项进铃铛:request_decision 工具 → notices(kind=decision)→ 点选项发回 Agent 主会话。"""
from __future__ import annotations

import asyncio

import pytest

from vococo import config
from vococo.gateway import notice_actions
from vococo.memory import agents, notices, projects
from vococo.tools import builtin, danger


@pytest.fixture
def env(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CRON_JOBS_PATH", tmp_path / "data" / "cron_jobs.json")
    monkeypatch.setattr(config, "BG_APPROVAL_QUIET", "")
    pushed: list[dict] = []

    async def fake_notify(title, body, **kw):
        pushed.append({"title": title, "body": body, **kw})
        return 1

    from vococo.gateway.adapters.web_push import PUSH

    monkeypatch.setattr(PUSH, "notify", fake_notify)
    resumed: list[tuple[str, str]] = []

    async def fake_resume(session_key, text):
        resumed.append((session_key, text))
        return "ok"

    monkeypatch.setattr(notice_actions, "resume", fake_resume)
    proj = tmp_path / "proj"
    proj.mkdir()
    projects.upsert_project(str(proj))
    agent = agents.by_project_hash(projects.project_hash(str(proj)))
    return {"agent": agent, "pushed": pushed, "resumed": resumed}


def _call(args: dict, session_key: str) -> str:
    tok = danger.set_task_session(session_key)
    try:
        return asyncio.run(builtin.request_decision.handler(args))["content"][0]["text"]
    finally:
        danger.reset_task_session(tok)


def test_decision_goes_to_agent_main_and_resumes_there(env):
    a = env["agent"]
    sub = f"web:p{a['project_hash']}:c1"  # 从 Agent 的子会话里提
    out = _call({"question": "**要不要启用 MG02?**\n建议启用", "options": ["启用", "先不启用"]}, sub)
    assert "铃铛" in out
    [n] = notices.list_open()
    assert n["kind"] == "decision" and n["status"] == "expired"
    assert n["session_key"] == agents.main_session_key(a)  # 发回主会话,不是子会话
    assert env["pushed"] and env["pushed"][0]["body"].endswith("要不要启用 MG02?")

    res = asyncio.run(notice_actions.act(n["id"], "启用"))
    assert res["ok"]
    [(key, text)] = env["resumed"]
    assert key == agents.main_session_key(a) and "我的选择:启用" in text and "MG02" in text
    assert notices.get(n["id"])["status"] == "answered"


def test_decision_validation_and_dedup(env):
    key = f"web:p{env['agent']['project_hash']}:main"
    assert "2-4 个选项" in _call({"question": "要不要?", "options": ["只有一个"]}, key)
    assert "2-4 个选项" in _call({"question": "", "options": ["a", "b"]}, key)
    _call({"question": "要不要?", "options": ["要", "不要"]}, key)
    assert "已经在铃铛里" in _call({"question": "要不要?", "options": ["要", "不要"]}, key)
    assert len(notices.list_open()) == 1 and len(env["pushed"]) == 1  # 重复提:只留一条、不重复推


def test_dismiss_all_keeps_decisions(env):
    notices.add(session_key="web:x", kind="ask", prompt="q", options=["a"], status="expired")
    _call({"question": "拍板吗", "options": ["是", "否"]}, f"web:p{env['agent']['project_hash']}:main")
    assert notices.dismiss_all() == 1
    assert [n["kind"] for n in notices.list_open()] == ["decision"]


def test_agent_prompt_mentions_request_decision(env):
    agents.write_doc(env["agent"]["id"], "AGENT.md", "# proj\n\n## 职责与人格\n\n管询盘")
    extra = agents.prompt_extra(agents.get(env["agent"]["id"]))
    assert "request_decision" in extra
