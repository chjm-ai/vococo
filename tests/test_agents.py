"""Agent(memory/agents.py):项目升级版的存储、会话归属、提示词注入、定时结果复制、接口。"""
from __future__ import annotations

import anyio
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.memory import agents, projects, session_store


@pytest.fixture
def env(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CRON_JOBS_PATH", tmp_path / "data" / "cron_jobs.json")
    proj = tmp_path / "proj"
    proj.mkdir()
    projects.upsert_project(str(proj))
    return proj


def test_list_has_general_and_one_per_project(env):
    items = agents.list_agents()
    assert items[0]["id"] == agents.GENERAL_ID and items[0]["main_conv"] == "main"
    a = items[1]
    h = projects.project_hash(str(env))
    assert a["id"] == h and a["name"] == "proj" and a["main_conv"] == f"p{h}:main"
    assert (agents.home(h) / "agent.json").exists()  # 项目第一次出现就落了 agent.json
    assert a["avatar"]["shape"] in agents.AVATAR_SHAPES


def test_create_default_dir_and_bind_existing(env, tmp_path):
    a = agents.create("展会跟进")
    assert a["workdir"].endswith("/workspace") and str(agents.home(a["id"])) in a["workdir"]
    assert a["project_hash"] in {p["hash"] for p in projects.list_projects()}
    assert agents.read_doc(a["id"], "AGENT.md").startswith("# 展会跟进")
    ext = tmp_path / "ext"
    ext.mkdir()
    b = agents.create("外部", str(ext))
    assert b["workdir"] == str(ext.resolve())
    assert agents.create("外部改名", str(ext))["id"] == b["id"]  # 同一目录不重复建
    with pytest.raises(ValueError):
        agents.create("", None)
    with pytest.raises(ValueError):
        agents.create("x", str(tmp_path / "nope"))


def test_update_name_avatar_links(env):
    a = agents.create("旧名")
    a2 = agents.update(a["id"], name="新名", avatar={"shape": "cat", "color": "bad"},
                       links=[{"path": str(env), "note": "项目", "writable": True}, {"path": ""}])
    assert a2["name"] == "新名" and a2["avatar"]["shape"] == "cat"
    assert a2["avatar"]["color"] in agents.AVATAR_COLORS  # 非法值回落默认
    assert agents.read_doc(a["id"], "AGENT.md").startswith("# 新名")
    assert a2["links"] == [{"path": str(env.resolve()), "note": "项目", "writable": True}]
    assert agents.home(a["id"]).name == a["id"]  # 目录名不随名称变
    assert agents.update("nope", name="x") is None


def test_agent_for_session_and_prompt_extra(env):
    from vococo.cron import scheduler

    h = projects.project_hash(str(env))
    a = agents.by_project_hash(h)
    key = f"web:p{h}:c1"
    assert agents.agent_for_session(key)["id"] == a["id"]
    assert agents.agent_for_session("web:abc") is None
    # 只有模板(标题 + 空小节)→ 不注入
    agents.write_doc(a["id"], "AGENT.md", "# proj\n\n## 职责与人格\n\n## 目标\n")
    assert agents.prompt_extra_for_session(key) == ""
    agents.write_doc(a["id"], "AGENT.md", "# proj\n\n## 目标\n\n每月 20 个询盘")
    agents.write_doc(a["id"], "NOTES.md", "- 周一回复率高")
    extra = agents.prompt_extra_for_session(key)
    assert "当前 Agent:proj" in extra and "每月 20 个询盘" in extra and "周一回复率高" in extra
    # 定时任务会话按 job 的 agent_id 找 Agent
    job = scheduler.create_job(name="巡检", prompt="p", schedule={"kind": "cron", "expr": "0 9 * * *"}, agent_id=a["id"])
    assert agents.agent_for_session(f"task:{job['id']}")["id"] == a["id"]
    assert scheduler.update_job(job["id"], name="巡检", prompt="p", schedule=job["schedule"], agent_id="")
    assert "agent_id" not in scheduler.load_jobs()[0]


def test_cron_result_mirrors_into_agent_main(env):
    from vococo.cron import scheduler

    a = agents.by_project_hash(projects.project_hash(str(env)))
    job = scheduler.create_job(name="巡检", prompt="p", schedule={"kind": "cron", "expr": "0 9 * * *"}, agent_id=a["id"])
    pushed, seen = [], []
    agents.set_listener(seen.append)

    async def push(platform, chat_id, text):
        pushed.append(chat_id)

    try:
        anyio.run(scheduler._push_job_result, job["id"], "success", "发了 60 封", push)
    finally:
        agents.set_listener(None)
    assert pushed == [f"task:{job['id']}"]  # 系统推送只发一次(任务自己的会话)
    assert seen == [a["main_conv"]]
    turns = session_store.load_recent(agents.main_session_key(a))
    assert any("发了 60 封" in str(t) for t in turns)
    runs = agents.recent_runs(a["id"])
    assert runs[0]["job_name"] == "巡检" and runs[0]["text"] == "发了 60 封"
    assert agents.recent_runs(a["id"], job_id="nope") == []


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_agent_routes(env, monkeypatch):
    from vococo.gateway.adapters.web import WebAdapter

    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    ad = WebAdapter()
    app = web.Application()
    app.add_routes([
        web.get("/agents", ad._handle_agents), web.post("/agents/create", ad._handle_agent_create),
        web.post("/agents/update", ad._handle_agent_update), web.get("/agents/doc", ad._handle_agent_doc),
        web.post("/agents/doc", ad._handle_agent_doc_save), web.get("/agents/runs", ad._handle_agent_runs),
        web.get("/agents/files", ad._handle_agent_files),
    ])
    async with TestClient(TestServer(app)) as c:
        items = (await (await c.get("/agents")).json())["agents"]
        assert [x["id"] for x in items][0] == "general" and items[1]["task_count"] == 0
        r = await c.post("/agents/create", json={"name": "新 Agent"})
        aid = (await r.json())["agent"]["id"]
        assert (await c.post("/agents/create", json={"name": ""})).status == 400
        r = await c.post("/agents/update", json={"id": aid, "name": "改名", "avatar": {"shape": "drop"}})
        assert (await r.json())["agent"]["avatar"]["shape"] == "drop"
        assert (await c.post("/agents/doc", json={"id": aid, "name": "NOTES.md", "text": "笔记"})).status == 200
        assert (await (await c.get(f"/agents/doc?id={aid}&name=NOTES.md")).json())["text"] == "笔记"
        assert (await c.get(f"/agents/doc?id={aid}&name=../x")).status == 400
        files = await (await c.get(f"/agents/files?id={aid}")).json()
        assert "AGENT.md" in files["files"] and "NOTES.md" in files["files"]
        assert (await (await c.get(f"/agents/runs?id={aid}")).json())["runs"] == []
        assert (await c.get("/agents/files?id=nope")).status == 404
        assert (await c.get("/agents/doc?id=../../etc&name=AGENT.md")).status == 404
        assert (await c.post("/agents/doc", json={"id": "..", "name": "NOTES.md", "text": "x"})).status == 404
        assert (await c.post("/agents/update", json={"id": "../x", "name": "y"})).status == 404
