"""评审子代理(core/reviewer.py):只在显式 Git 项目会话挂上,只读工具,模型跟随主会话。"""
from __future__ import annotations

import subprocess

import pytest

from vococo.core import agent, reviewer

from test_agent_caps import _turn, anyio_backend, clients  # noqa: F401 —— 复用假 SDK client 夹具


@pytest.fixture
def git_dir(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    return d


def test_definitions_only_for_explicit_git_project(git_dir, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert reviewer.definitions(str(git_dir), is_explicit_project=False) is None
    assert reviewer.definitions(str(plain), is_explicit_project=True) is None
    assert reviewer.definitions(None, is_explicit_project=True) is None
    d = reviewer.definitions(str(git_dir), is_explicit_project=True)[reviewer.NAME]
    assert d.tools == ["Read", "Grep", "Glob", "Bash"]
    assert "Edit" in d.disallowedTools and "Write" in d.disallowedTools
    assert d.model == "inherit"
    assert "不修改任何文件" in d.prompt


@pytest.mark.anyio
async def test_stream_turn_registers_reviewer(clients, monkeypatch, git_dir):  # noqa: F811
    from vococo.memory import agents

    monkeypatch.setattr(agents, "caps_for_session", lambda key: agents._normalize_caps(None))
    async for ev in agent.stream_turn([], "改完了", session_key="web:pabc:c1", cwd=str(git_dir),
                                      is_explicit_project=True):
        if isinstance(ev, agent.Done):
            break
    assert list(clients[0].options.agents) == [reviewer.NAME]
    await _turn("web:plain")  # 普通聊天不挂
    assert clients[1].options.agents is None
