"""Agent 能力设定(caps):agent.json 里的模型 / 技能 / 常驻 MCP / 禁用工具真正卡住运行时。"""
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


def test_caps_default_empty_and_normalized(env):
    a = agents.create("编码")
    assert a["caps"] == {"model": "", "skills": None, "mcp": [], "disallowed_tools": []}
    a2 = agents.update(a["id"], caps={
        "model": " deepseek-flash ",
        "skills": ["pdf", "pdf", "", "bad;rm -rf"],
        "mcp": "lemlist",  # 不是列表 → 当空
        "disallowed_tools": ["Bash", "mcp__vococo__dispatch_session", "Bash"],
    })
    assert a2["caps"] == {
        "model": "deepseek-flash", "skills": ["pdf"], "mcp": [],
        "disallowed_tools": ["Bash", "mcp__vococo__dispatch_session"],
    }
    # 空列表 = 一个技能都不挂(和 None 跟随全局是两回事)
    assert agents.update(a["id"], caps={"skills": []})["caps"]["skills"] == []
    # 改别的字段不会冲掉 caps
    assert agents.update(a["id"], name="编码2")["caps"]["skills"] == []


def test_caps_for_session_follows_agent(env):
    h = projects.project_hash(str(env))
    a = agents.by_project_hash(h)
    agents.update(a["id"], caps={"model": "m1", "mcp": ["lemlist"]})
    assert agents.caps_for_session(f"web:p{h}:c1")["model"] == "m1"
    assert agents.caps_for_session("web:abc")["mcp"] == []  # 不属于 Agent → 空设定
    assert agents.caps_for_session(None)["skills"] is None


# ── stream_turn 把 caps 落到 ClaudeAgentOptions 上 ─────────────────────────
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
async def test_stream_turn_applies_caps(clients, monkeypatch):
    monkeypatch.setattr(agents, "caps_for_session", lambda key: {
        "model": "", "skills": ["pdf"], "mcp": ["lemlist"], "disallowed_tools": ["Bash", "Write"],
    })
    await _turn("web:pabc:c1")
    opts = clients[0].options
    assert "pdf" in opts.skills and "global-skill" not in opts.skills  # 白名单替换全局
    assert "lemlist" in opts.mcp_servers  # 不靠关键词,常驻挂上
    assert opts.disallowed_tools == ["Write", "Bash"]  # 调用方的 + Agent 的,去重


@pytest.mark.anyio
async def test_stream_turn_without_agent_unchanged(clients, monkeypatch):
    monkeypatch.setattr(agents, "caps_for_session", lambda key: agents._normalize_caps(None))
    await _turn("web:plain")
    opts = clients[0].options
    assert "global-skill" in opts.skills
    assert opts.mcp_servers == {}
    assert opts.disallowed_tools == ["Write"]
