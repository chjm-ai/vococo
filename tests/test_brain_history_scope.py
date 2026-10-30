"""跨 Agent 历史的分页与权限回归,仅使用临时数据。"""
import pytest

from test_brain_routes import brain, bridge_db, client, post, session, turn
from vococo.memory import agents, projects, session_store

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_agent_and_session_pages_cover_every_visible_record(client):
    expected = [item["agent_id"] for item in (await post(client, "list_agents", {}))["agents"]]
    collected, offset = [], 0
    while True:
        page = await post(client, "list_agents", {"offset": offset, "limit": 1})
        collected.extend(item["agent_id"] for item in page["agents"])
        offset = page["next_offset"]
        if offset is None:
            break
    assert collected == sorted(expected) and len(collected) == len(set(collected))
    other = "abcdef1234"
    own = turn(key=session())
    keys = [session(other).removesuffix("main") + str(i) for i in range(5)]
    for key in keys:
        session_store.set_title(key, "同名标题")
        turn(key=key)
    for scope, expected_keys in (({"agent_id": other}, set(keys)),
                                 ({"all_agents": True}, {*keys, session()})):
        collected, offset = [], 0
        while True:
            page = await post(client, "list_sessions", {**scope, "offset": offset, "limit": 2})
            collected.extend(row["key"] for row in page["sessions"])
            offset = page["next_offset"]
            if offset is None:
                break
        assert set(collected) == expected_keys and len(collected) == len(expected_keys)
    assert (await post(client, "read_session", {"key": session(), "agent_id": None}))["turn"]["id"] == own


async def test_hidden_legacy_project_without_agent_metadata_is_readable(client, bridge_db):
    workdir = bridge_db / "旧项目"
    workdir.mkdir()
    project = projects.upsert_project(str(workdir))
    aid = project["hash"]
    projects.hide_project(aid)
    key = f"web:p{aid}:archived"
    tid = turn("历史问题", "历史回答", key)
    assert not (agents.root_dir() / aid / "agent.json").exists()
    visible = (await post(client, "list_agents", {}))["agents"]
    assert {"agent_id": aid, "name": "旧项目", "task_dispatch": False, "session_count": 1} in visible
    assert not (agents.root_dir() / aid / "agent.json").exists()
    assert (await post(client, "list_sessions", {"agent_id": aid}))["sessions"][0]["key"] == key
    assert (await post(client, "read_session", {"agent_id": aid, "key": key}))["turn"]["id"] == tid
    assert not (agents.root_dir() / aid / "agent.json").exists()


async def test_other_agent_text_pages_and_older_turns(client):
    key, aid = session("abcdef1234"), "abcdef1234"
    older = turn("旧问题", "旧回答", key)
    latest = turn("问题" * 6000, "回答" * 7000, key)
    args = {"key": key, "agent_id": aid, "length": 7999}
    user, assistant = "", ""
    while True:
        page = await post(client, "read_session", args)
        user += page["turn"]["user_text"]
        assistant += page["turn"]["assistant_text"]
        if page["next_offset"] is None:
            break
        args.update(turn_id=latest, offset=page["next_offset"])
    assert user == "问题" * 6000 and assistant == "回答" * 7000
    previous = await post(client, "read_session", {"key": key, "agent_id": aid,
                                                   "before_id": page["next_before_id"]})
    assert previous["turn"]["id"] == older
    assert (await post(client, "read_session", {"key": key, "agent_id": aid,
                                               "before_id": older}))["turn"] is None


async def test_empty_allowlist_fails_closed_and_changes_apply_on_next_page(client, monkeypatch):
    key, aid = session("abcdef1234"), "abcdef1234"
    tid = turn("长问题" * 4000, "回答", key)
    await post(client, "read_session", {"key": key, "agent_id": aid, "length": 10})
    monkeypatch.setenv(brain.READABLE_AGENTS, "")
    assert (await post(client, "list_agents", {}))["agents"] == []
    assert (await post(client, "list_sessions", {"all_agents": True}))["sessions"] == []
    await post(client, "list_sessions", {"agent_id": aid}, 404)
    await post(client, "read_session", {"key": key, "agent_id": aid, "turn_id": tid, "offset": 10}, 404)


async def test_search_history_scope_pagination_and_literal_keywords(client, monkeypatch):
    own, other = session(), session("abcdef1234")
    first = turn("关键词 100%", "回答", own)
    other_id = turn("关键词私有", "回答", other)
    last = turn("最新关键词", "回答", own)
    assert {row["turn_id"] for row in (await post(client, "search_history", {"query": "关键词"}))["results"]} == {first, other_id, last}
    page = await post(client, "search_history", {"query": "关键词", "limit": 1, "agent_id": "123456abcd"})
    assert [row["turn_id"] for row in page["results"]] == [last]
    assert page["next_before_id"] == last
    page = await post(client, "search_history", {"query": "关键词", "limit": 1, "before_id": last, "agent_id": "123456abcd"})
    assert [row["turn_id"] for row in page["results"]] == [first]
    assert page["next_before_id"] is None
    assert len((await post(client, "search_history", {"query": "%"}))["results"]) == 1
    turn("password: mysecretvalue", "无关", own)
    assert (await post(client, "search_history", {"query": "mysecretvalue"}))["results"] == []
    assert len((await post(client, "search_history", {"query": "关键词", "agent_id": "abcdef1234"}))["results"]) == 1
    monkeypatch.setenv(brain.READABLE_AGENTS, "123456abcd")
    await post(client, "search_history", {"query": "关键词", "agent_id": "abcdef1234"}, 404)
    await post(client, "search_history", {"query": "   "}, 400)
    await post(client, "search_history", {"query": "关键词"}, 401, headers={})


@pytest.mark.parametrize("operation,payload", [
    ("list_sessions", {"all_agents": "true"}),
    ("list_sessions", {"all_agents": 1}),
    ("list_sessions", {"all_agents": True, "agent_id": "123456abcd"}),
    ("read_session", {"key": "x", "agent_id": 123}),
    ("create_task", {"title": "不应创建", "prompt": "不执行", "idempotency_key": "readonly-001", "agent_id": "abcdef1234"}),
    ("cancel_task", {"id": "x", "agent_id": "abcdef1234"}),
])
async def test_read_scope_never_expands_write_tools(client, operation, payload):
    await post(client, operation, payload, 400)


@pytest.mark.parametrize("operation,payload", [
    ("list_agents", {}), ("list_sessions", {"all_agents": True}),
    ("read_session", {"key": "x", "agent_id": "abcdef1234"}),
])
async def test_new_read_routes_require_authentication(client, operation, payload):
    await post(client, operation, payload, 401, headers={})
