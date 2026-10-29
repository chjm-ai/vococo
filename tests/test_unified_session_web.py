"""统一会话身份的 Web 边界：归属、列表、复制、停止和浏览器草稿。"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.core import tasks, task_runner, worktree
from vococo.cron import scheduler
from vococo.gateway.adapters.base import FileAttachment
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import agents, session_store


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def setup_web(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "FILES_DIR", isolated / "files")
    monkeypatch.setattr(tasks, "_DB", None)
    jobs = []
    monkeypatch.setattr(scheduler, "load_jobs", lambda: list(jobs))
    monkeypatch.setattr(scheduler, "save_jobs", lambda rows: jobs.__setitem__(slice(None), rows))
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([
        web.post("/send", adapter._handle_send),
        web.get("/conversations", adapter._handle_conversations),
        web.get("/voice/sidebar", adapter._handle_voice_sidebar),
        web.get("/cron/sidebar", adapter._handle_cron_sidebar),
        web.get("/search", adapter._handle_conv_search),
        web.post("/duplicate", adapter._handle_conv_duplicate),
        web.post("/delete", adapter._handle_delete),
        web.post("/cron/delete", adapter._handle_cron_delete),
        web.post("/abort", adapter._handle_abort),
    ])
    yield adapter, app, jobs
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


@pytest.mark.anyio
async def test_first_send_binds_and_internal_inject_reuses_ownership(setup_web):
    adapter, app, _ = setup_web
    key = config.new_session_key()
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/send", json={
            "conv": key, "text": "/help", "agent_id": "general", "project_hash": None,
        })
        assert response.status == 200
        assert (await adapter._inbox.get()).chat_id == key
        assert session_store.session_binding(key) == {"agent_id": "general", "project_hash": None}
        # 已绑定 key 的浏览器元数据不可信，不得改归属。
        response = await client.post("/send", json={
            "conv": key, "text": "/help", "agent_id": "missing", "project_hash": "wrong",
        })
        assert response.status == 200
        await adapter.inject(key, "/help")
        assert session_store.session_binding(key)["agent_id"] == "general"
        rows = (await (await client.get("/conversations")).json())["conversations"]
        assert next(r for r in rows if r["conv"] == key)["conversation_id"] == key


@pytest.mark.anyio
@pytest.mark.parametrize("metadata", [
    {}, {"agent_id": "missing"}, {"agent_id": "general", "project_hash": "wrong"},
    {"agent_id": ["general"]},
])
async def test_invalid_owner_never_binds_or_dispatches(setup_web, metadata):
    adapter, app, _ = setup_web
    key = config.new_session_key()
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/send", json={"conv": key, "text": "/help", **metadata})
        assert response.status == 400
    assert not session_store.session_binding(key)
    assert adapter._inbox.empty()


@pytest.mark.anyio
async def test_project_and_agent_must_correspond_and_main_conv_can_send(setup_web):
    _, app, _ = setup_web
    owner = agents.create("项目助理")
    key = config.new_session_key()
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/send", json={
            "conv": key, "text": "/help", "agent_id": owner["id"], "project_hash": None,
        })
        assert response.status == 400
        response = await client.post("/send", json={
            "conv": key, "text": "/help", "agent_id": owner["id"],
            "project_hash": owner["project_hash"],
        })
        assert response.status == 200
        assert session_store.session_binding(key)["project_hash"] == owner["project_hash"]
        response = await client.post("/send", json={"conv": owner["main_conv"], "text": "/help"})
        assert response.status == 200


@pytest.mark.anyio
async def test_inject_cannot_create_unbound_session(setup_web):
    adapter, _, _ = setup_web
    with pytest.raises(ValueError, match="显式"):
        await adapter.inject(config.new_session_key(), "/help")
    assert adapter._inbox.empty()


@pytest.mark.anyio
async def test_lists_partition_normal_task_and_cron_without_duplicates(setup_web):
    _, app, jobs = setup_web
    normal = config.new_session_key()
    session_store.bind_session(normal, agent_id="general")
    session_store.append(normal, "普通", "答")
    session_store.append("web:legacy", "历史", "答")
    task = tasks.create("独立后台", "执行", origin="chat", agent_id="general")
    cron_key = config.new_session_key()
    session_store.bind_session(cron_key, agent_id="general")
    session_store.append(cron_key, "定时", "答")
    session_store.set_pending_review(cron_key, True)
    cron_task = tasks.create("定时执行", "执行", origin="cron", agent_id="general",
                             session_key=cron_key, job_id="job-1")
    # 脚本任务没有 tasks 记录，仍然只出现在定时分组。
    script_key = config.new_session_key()
    session_store.bind_session(script_key, agent_id="general")
    for job_id, key in (("job-1", cron_key), ("script", script_key)):
        jobs.append({"id": job_id, "conv": key, "name": job_id, "agent_id": "general",
                     "enabled": True, "schedule": {"kind": "cron", "expr": "0 8 * * *"}})
    async with TestClient(TestServer(app)) as client:
        normal_rows = (await (await client.get("/conversations")).json())["conversations"]
        background = (await (await client.get("/voice/sidebar")).json())["tasks"]
        scheduled = (await (await client.get("/cron/sidebar")).json())["jobs"]
    assert {r["conv"] for r in normal_rows} == {normal, "legacy"}
    assert {r["conv"] for r in background} == {task["session_key"]}
    assert {r["conv"] for r in scheduled} == {cron_key, script_key}
    assert background[0]["task_id"] == task["id"]
    cron_row = next(r for r in scheduled if r["conv"] == cron_key)
    assert cron_row["task_id"] == cron_task["id"] and cron_row["job_id"] == "job-1"
    assert cron_row["pending_review"] is True
    for row in normal_rows + background + scheduled:
        assert {"project_hash", "agent_id", "task_id", "job_id", "conversation_id"} <= row.keys()


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [False, True])
async def test_duplicate_uses_session_id_inherits_owner_and_clones_attachment(setup_web, legacy):
    _, app, _ = setup_web
    owner = agents.create("附件助理")
    src = f"web:p{owner['project_hash']}:old" if legacy else config.new_session_key()
    if not legacy:
        session_store.bind_session(src, agent_id=owner["id"], project_hash=owner["project_hash"])
    turn_id = session_store.start_turn(src, "附件")
    original = session_store.save_turn_files(turn_id, [FileAttachment(
        data=b"attachment", filename="note.txt", media_type="text/plain",
    )])
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/duplicate", json={"conv": config.conversation_id_for_key(src)})
        assert response.status == 200
        result = await response.json()
    dst = result["conv"]
    assert dst.startswith("session:") and dst != src
    assert result["agent_id"] == owner["id"]
    assert session_store.session_binding(dst)["project_hash"] == owner["project_hash"]
    copied = session_store.load_history(dst)[0]
    assert copied["files"][0]["name"] == "note.txt"
    copied_path = config.FILES_DIR / f"{copied['id']}_0.txt"
    assert copied_path.name != original[0]["file"]
    session_store.delete_session(src)
    assert copied_path.read_bytes() == b"attachment"


@pytest.mark.anyio
async def test_delete_cron_bound_session_is_rejected(setup_web):
    _, app, jobs = setup_web
    key = config.new_session_key()
    session_store.bind_session(key, agent_id="general")
    jobs.append({"id": "job", "conv": key})
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/delete", json={"conv": key, "force": True})
        assert response.status == 409
    assert session_store.session_binding(key) and jobs


@pytest.mark.anyio
async def test_delete_and_abort_use_task_mapping_and_wait_until_stopped(setup_web, monkeypatch):
    adapter, app, _ = setup_web
    task = tasks.create("停止测试", "执行", agent_id="general")
    key = task["session_key"]
    calls = []
    async def wait(task_id):
        calls.append(task_id)
        return False
    monkeypatch.setattr(task_runner, "cancel_and_wait", wait)
    monkeypatch.setattr(task_runner, "cancel", lambda task_id: calls.append(task_id) or True)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/abort", json={"conv": key})
        assert (await response.json())["stopped"] is True
        response = await client.post("/delete", json={"conv": key})
        assert response.status == 409
        assert calls == [task["id"], task["id"]]
        assert session_store.session_binding(key)
        tasks.set_status(task["id"], "cancelled")
        async def clean(_key):
            return None
        monkeypatch.setattr(worktree, "worktree_dirty_summary", clean)
        monkeypatch.setattr(worktree, "remove_worktree", clean)
        response = await client.post("/delete", json={"conv": key})
        assert response.status == 200
        assert not session_store.session_binding(key)


def test_browser_draft_id_stays_stable_and_metadata_drives_owner():
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node 验证浏览器会话逻辑")
    root = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static"
    source = (root / "sidebar.js").read_text()
    functions = []
    for name in ("isDraftConv", "convOwnership", "convProject", "newChatIn", "pickDraftProject", "applyConvs"):
        functions.append(re.search(r"^function " + name + r"\(.*?^}", source, re.M | re.S)[0])
    script = """
const assert=require('node:assert/strict');
const crypto=require('node:crypto').webcrypto;
const S={convs:[], agents:[{id:'general',project_hash:null},{id:'agent',project_hash:'abc'}],
  sending:{},live:{},expanded:new Set(),searchConvs:[],homeConvs:[],convFilter:'all',homeConvsVersion:0};
const box={style:{display:'none'},focus(){}};
const $=()=>box;
function findConv(conv){return S.convs.find(x=>x.conv===conv);}
function projAgent(hash){return S.agents.find(a=>(a.project_hash||null)===(hash||null));}
function grpKey(hash){return hash||'default';}
function openConv(conv){S.conv=conv;}
function saveExpanded(){} function renderConvs(){} function renderProjSelChip(){}
let metadataRefills=0;
function refillCurrentMeta(){metadataRefills++;}
""" + "\n".join(functions) + """
newChatIn(null,false);
const key=S.conv;
assert.match(key,/^session:[0-9a-f]{32}$/);
pickDraftProject('abc');
assert.equal(S.conv,key);
assert.equal(convProject(key),'abc');
assert.deepEqual(convOwnership(key),{agent_id:'agent',project_hash:'abc'});
newChatIn('abc',false);
assert.equal(S.conv,key);
applyConvs({main:{},conversations:[]});
assert.equal(S.convs.filter(c=>c.conv===key).length,1);
S.convs.find(c=>c.conv===key).draft=false;
applyConvs({main:{},conversations:[{conv:key,agent_id:'agent',project_hash:'abc'}]});
assert.equal(S.convs.filter(c=>c.conv===key).length,1);
pickDraftProject(null);
assert.equal(convProject(key),'abc');
assert.equal(metadataRefills,2,'普通侧栏刷新也要补回元数据');
"""
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


@pytest.mark.anyio
async def test_bad_session_uuid_is_rejected(setup_web):
    adapter, app, _ = setup_web
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/send", json={
            "conv": "session:invalid", "text": "/help", "agent_id": "general",
        })
        assert response.status == 400
    assert adapter._inbox.empty()


@pytest.mark.anyio
async def test_cron_delete_does_not_remove_job_before_task_stops(setup_web, monkeypatch):
    _, app, jobs = setup_web
    task = tasks.create("定时执行", "执行", origin="cron", agent_id="general", job_id="job")
    key = task["session_key"]
    jobs.append({"id": "job", "conv": key})
    calls = []
    async def wait(task_id):
        calls.append(task_id)
        return False
    monkeypatch.setattr(task_runner, "cancel_and_wait", wait)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/cron/delete", json={"id": "job"})
        assert response.status == 409
        assert jobs and session_store.session_binding(key)
        tasks.set_status(task["id"], "cancelled")
        response = await client.post("/cron/delete", json={"id": "job"})
        assert response.status == 200
    assert calls == [task["id"], task["id"]]
    assert not jobs and not session_store.session_binding(key)


@pytest.mark.anyio
async def test_empty_send_does_not_persist_draft_binding(setup_web):
    _, app, _ = setup_web
    key = config.new_session_key()
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/send", json={"conv": key, "text": "", "agent_id": "general"})
        assert response.status == 400
    assert not session_store.session_binding(key)
