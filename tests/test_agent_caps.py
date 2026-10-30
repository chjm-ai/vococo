"""Agent 运行设定里的禁用工具(技能与 MCP 名单见 test_agents.py):真正卡住运行时。"""
from __future__ import annotations

import pytest
from instance_fixture import instance_preferences
from claude_agent_sdk import ResultMessage

from vococo import config
from vococo.core import agent
from vococo.core.agent import Done
from vococo.memory import agents, projects


@pytest.fixture
def env(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CRON_JOBS_PATH", tmp_path / "data" / "cron_jobs.json")
    proj = tmp_path / "proj"
    proj.mkdir()
    projects.upsert_project(str(proj))
    return proj


def test_disallowed_tools(env):
    a = agents.create("编码")
    assert a["disallowed_tools"] is None and "model" not in a  # 默认跟随全局;默认模型这一项已取消
    a2 = agents.update(a["id"], disallowed_tools=["Bash", " Bash ", "", "mcp__vococo__dispatch_session"])
    assert a2["disallowed_tools"] == ["Bash", "mcp__vococo__dispatch_session"]
    assert agents.update(a["id"], name="编码2")["disallowed_tools"] == a2["disallowed_tools"]  # 不传 = 不动
    assert agents.update(a["id"], disallowed_tools=None)["disallowed_tools"] is None  # None = 改回跟随全局
    with pytest.raises(ValueError):
        agents.update(a["id"], disallowed_tools="Bash")  # 必须是列表
    with pytest.raises(ValueError):
        agents.update(agents.GENERAL_ID, disallowed_tools=["Bash"])  # 总助理就是全局配置


def test_runtime_for_session_carries_tools(env):
    h = projects.project_hash(str(env))
    a = agents.by_project_hash(h)
    agents.update(a["id"], disallowed_tools=["Bash"])
    rt = agents.runtime_for_session(f"web:p{h}:c1")
    assert rt["disallowed_tools"] == ["Bash"] and rt["skills"] is None and rt["mcp"] is None
    assert rt["memory_sections"] == agents.default_memory_sections() and rt["memory_dir"].endswith("/proj")
    assert agents.runtime_for_session("web:abc")["disallowed_tools"] is None


# ── stream_turn 把 Agent 运行设定落到 ClaudeAgentOptions 上 ─────────────────────────
class _Client:
    def __init__(self, options, registry):
        self.options = options
        registry.append(self)

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def query(self, prompt=None, *a, **kw):
        return None

    def receive_messages(self):
        async def gen():
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, session_id="sid",
                                is_error=False, num_turns=1, usage={"input_tokens": 1, "output_tokens": 1})
        return gen()

    async def get_context_usage(self):
        return {"totalTokens": 1, "rawMaxTokens": 200_000}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def clients(monkeypatch):
    made: list[_Client] = []
    monkeypatch.setattr(agent, "ClaudeSDKClient", lambda options=None: _Client(options, made))
    monkeypatch.setattr(agent.providers, "resolve", lambda *a: ("claude-sonnet-4", {}))
    monkeypatch.setattr(agent.settings_store, "vococo_enabled", lambda: False)
    monkeypatch.setattr(agent.settings_store, "effective_external_mcp",
                        lambda names=None: {n: {"type": "http", "url": "http://x"} for n in (names or ())})
    monkeypatch.setattr(agent.settings_store, "effective_skills", lambda cwd=None, **kw: ["global-skill"])
    monkeypatch.setattr(agent.session_store, "get_external_mcp_names", lambda key: set())
    monkeypatch.setattr(agent, "build_system_prompt", lambda cwd=None, cache_key=None: {"append": "p"})
    monkeypatch.setattr(agent, "build_mcp_servers", lambda: {})
    monkeypatch.setattr(agent, "build_hooks", lambda: {})
    monkeypatch.setattr(agent.client_pool, "enabled", lambda: False)
    return made


async def _turn(key: str, denied=None) -> None:
    async for ev in agent.stream_turn([], "你好", session_key=key,
                                      disallowed_tools=["Write"] if denied is None else denied):
        if isinstance(ev, Done):
            return


@pytest.mark.anyio
async def test_stream_turn_applies_agent_runtime(clients, monkeypatch):
    monkeypatch.setattr(agents, "runtime_for_session", lambda key: {
        "skills": ["pdf"], "mcp": ["lemlist"], "disallowed_tools": ["Bash", "Write"],
    })
    await _turn("web:pabc:c1")
    opts = clients[0].options
    assert "pdf" in opts.skills and "global-skill" not in opts.skills  # 白名单替换全局
    assert "lemlist" in opts.mcp_servers  # 不靠关键词,常驻挂上
    assert opts.disallowed_tools == ["Write", "Bash"]  # 调用方的 + Agent 的,去重


@pytest.mark.anyio
async def test_stream_turn_keeps_html_preview_disabled(clients, monkeypatch):
    monkeypatch.setattr(agents, "runtime_for_session", lambda key: {
        **dict.fromkeys(agents.RUNTIME_KEYS), "disallowed_tools": ["mcp__vococo__show_html"],
    })
    await _turn("web:html-denied")
    assert "mcp__vococo__show_html" in clients[0].options.disallowed_tools


@pytest.mark.anyio
async def test_stream_turn_without_agent_unchanged(clients, monkeypatch):
    monkeypatch.setattr(agents, "runtime_for_session", lambda key: dict.fromkeys(agents.RUNTIME_KEYS))
    await _turn("web:plain")
    opts = clients[0].options
    assert "global-skill" in opts.skills
    assert opts.mcp_servers == {}
    assert opts.disallowed_tools == ["Write"]


@pytest.mark.anyio
@pytest.mark.parametrize("target_skills", [["pdf", "new-skill"], None])
async def test_return_caps_apply_at_execution_after_target_changes(clients, env, monkeypatch, target_skills):
    monkeypatch.setattr(agents, "runtime_for_session", lambda key: {
        "skills": target_skills, "mcp": ["lemlist", "new-server"], "disallowed_tools": ["Edit"],
    })
    monkeypatch.setattr(agent.session_store, "get_external_mcp_names", lambda key: {"session-only"})
    key = "web:return-caps"
    agent.session_store.start_turn(key, "回传", source_access={
        "session": "task:source", "denied": ["Bash"], "mode": "standard",
        "capabilities": {"skills": ["pdf"], "mcp": ["lemlist"]},
    })
    await _turn(key)
    opts = clients[0].options
    assert opts.skills == (["pdf"] if target_skills else [])
    assert set(opts.mcp_servers) == {"lemlist"}
    assert set(opts.disallowed_tools) == {"Bash", "Write", "Edit"}


@pytest.mark.anyio
@pytest.mark.parametrize("source_skills", [None, "all", ["pdf"]])
async def test_return_snapshot_freezes_all_skills_before_target_changes(clients, env, monkeypatch, source_skills):
    from vococo.tools import builtin, sessions

    snapshots = []
    build = sessions.build_tools

    def capture(key, denied, capabilities=None):
        snapshots.append(capabilities)
        return build(key, denied, capabilities)

    monkeypatch.setattr(sessions, "build_tools", capture)
    monkeypatch.setattr(agent.settings_store, "vococo_enabled", lambda: True)
    monkeypatch.setattr(builtin, "create_sdk_mcp_server", lambda name, tools: {})
    monkeypatch.setattr(agent, "build_mcp_servers", builtin.build_mcp_servers)
    monkeypatch.setattr(agent.settings_store, "list_skills", lambda: [{"name": "pdf"}])
    monkeypatch.setattr(agent.settings_store, "effective_skills", lambda *a, **kw: source_skills)
    await _turn("web:source-all")
    snapshot = snapshots[0]
    assert isinstance(snapshot["skills"], list) and "pdf" in snapshot["skills"]
    monkeypatch.setattr(agent.settings_store, "effective_skills", lambda *a, **kw: ["pdf", "new-skill"])
    agent.session_store.start_turn("web:target-all", "自动回传", source_access={
        "session": "task:source", "mode": "standard", "capabilities": snapshot,
    })
    await _turn("web:target-all")
    assert "pdf" in clients[-1].options.skills
    assert "new-skill" not in clients[-1].options.skills


@pytest.mark.anyio
async def test_web_session_tools_reach_sdk_with_bound_source(clients, env, monkeypatch):
    from vococo.tools import builtin

    captured = []
    def server(name, tools):
        captured.append(tools)
        return {}

    monkeypatch.setattr(agent.settings_store, "vococo_enabled", lambda: True)
    monkeypatch.setattr(builtin, "create_sdk_mcp_server", server)
    monkeypatch.setattr(agent, "build_mcp_servers", builtin.build_mcp_servers)
    source = "web:bound-source"
    agent.session_store.start_turn(source, "查找已有会话")
    await _turn(source, denied=[])
    assert "vococo" in clients[0].options.mcp_servers
    tools = {t.name: t for t in captured[-1]}
    assert {"list_sessions", "query_session", "continue_session"} <= tools.keys()
    result = await tools["list_sessions"].handler({})
    import json
    assert json.loads(result["content"][0]["text"])["source_session"] == source
