"""Agent 运行设定里的默认模型 / 禁用工具(技能与 MCP 名单见 test_agents.py):真正卡住运行时。"""
from __future__ import annotations

import pytest
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


def test_model_and_disallowed_tools(env):
    a = agents.create("编码")
    assert a["model"] is None and a["disallowed_tools"] is None  # 默认跟随全局
    a2 = agents.update(a["id"], model=" deepseek-flash ",
                       disallowed_tools=["Bash", " Bash ", "", "mcp__vococo__dispatch_session"])
    assert a2["model"] == "deepseek-flash"
    assert a2["disallowed_tools"] == ["Bash", "mcp__vococo__dispatch_session"]
    assert agents.update(a["id"], name="编码2")["model"] == "deepseek-flash"  # 不传 = 不动
    a3 = agents.update(a["id"], model="", disallowed_tools=None)  # 空 / None = 改回跟随全局
    assert a3["model"] is None and a3["disallowed_tools"] is None
    assert "model" not in agents._read_meta(a["id"])
    with pytest.raises(ValueError):
        agents.update(a["id"], disallowed_tools="Bash")  # 必须是列表
    with pytest.raises(ValueError):
        agents.update(agents.GENERAL_ID, model="x")  # 总助理就是全局配置


def test_runtime_for_session_carries_model_and_tools(env):
    h = projects.project_hash(str(env))
    a = agents.by_project_hash(h)
    agents.update(a["id"], model="m1", disallowed_tools=["Bash"])
    rt = agents.runtime_for_session(f"web:p{h}:c1")
    assert rt == {"skills": None, "mcp": None, "model": "m1", "disallowed_tools": ["Bash"]}
    assert agents.runtime_for_session("web:abc")["model"] is None


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


async def _turn(key: str) -> None:
    async for ev in agent.stream_turn([], "你好", session_key=key, disallowed_tools=["Write"]):
        if isinstance(ev, Done):
            return


@pytest.mark.anyio
async def test_stream_turn_applies_agent_runtime(clients, monkeypatch):
    monkeypatch.setattr(agents, "runtime_for_session", lambda key: {
        "model": None, "skills": ["pdf"], "mcp": ["lemlist"], "disallowed_tools": ["Bash", "Write"],
    })
    await _turn("web:pabc:c1")
    opts = clients[0].options
    assert "pdf" in opts.skills and "global-skill" not in opts.skills  # 白名单替换全局
    assert "lemlist" in opts.mcp_servers  # 不靠关键词,常驻挂上
    assert opts.disallowed_tools == ["Write", "Bash"]  # 调用方的 + Agent 的,去重


@pytest.mark.anyio
async def test_stream_turn_without_agent_unchanged(clients, monkeypatch):
    monkeypatch.setattr(agents, "runtime_for_session", lambda key: dict.fromkeys(agents.RUNTIME_KEYS))
    await _turn("web:plain")
    opts = clients[0].options
    assert "global-skill" in opts.skills
    assert opts.mcp_servers == {}
    assert opts.disallowed_tools == ["Write"]
