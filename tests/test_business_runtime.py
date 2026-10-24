"""业务目录、SDK 沙箱与配置迁移的集成回归，不访问真实模型。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import ResultMessage
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from vococo import config
from vococo.core import agent, execution, subagent
from vococo.memory import agents


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_stream_uses_workspace_but_loads_output_rules(isolated, monkeypatch):
    output = isolated / "iCloud 成果"
    output.mkdir()
    (output / "CLAUDE.md").write_text("正式成果保存在业务目录")
    owner = agents.create("业务", str(output))
    key = agents.main_session_key(owner)
    source = isolated / "original-rules"
    source.mkdir()
    (source / "AGENTS.md").write_text("原业务执行规则不能丢失")
    meta = agents._read_meta(owner["id"])
    meta["output_dir"] = str(output)
    agents._write_meta(owner["id"], meta)
    # 保持会话归属不变，单独模拟规则来源与成果目的地分离。
    real_policy = execution.policy_for_session(key)
    from dataclasses import replace
    monkeypatch.setattr(execution, "policy_for_session", lambda *args: replace(real_policy, source_dir=source))
    captured = []

    class Client:
        def __init__(self, options):
            captured.append(options)
        async def connect(self):
            pass
        async def disconnect(self):
            pass
        async def query(self, prompt):
            pass
        async def receive_messages(self):
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id="test", result="完成")

    loader = AsyncMock(return_value={"append": "业务规则"})
    monkeypatch.setattr(agent, "_load_system_prompt", loader)
    monkeypatch.setattr(agent, "ClaudeSDKClient", Client)
    monkeypatch.setattr(agent.settings_store, "vococo_enabled", lambda: False)
    monkeypatch.setattr(agent.settings_store, "effective_skills", lambda *a, **k: [])
    events = [event async for event in agent.stream_turn(
        [], "保存报告", session_key=key, cwd=str(config.ROOT_DIR), model="claude-sonnet-5-5",
    )]
    assert events[-1].reply.is_error is False
    options = captured[0]
    policy = execution.policy_for_session(key)
    assert options.cwd == str(policy.workspace)
    assert loader.await_args.args[0] == str(source)
    assert "原业务执行规则不能丢失" in options.system_prompt["append"]
    assert str(output) in options.system_prompt["append"]
    assert "正式成果保存在业务目录" in options.system_prompt["append"]
    assert options.env["PATH"] == execution.shell_env()["PATH"]
    assert options.sandbox["enabled"] and not options.sandbox["allowUnsandboxedCommands"]
    assert str(output) in options.sandbox["filesystem"]["allowWrite"]
    serialized = json.loads(SubprocessCLITransport(prompt="test", options=options)._build_settings_value())
    assert serialized["sandbox"] == options.sandbox
    child = subagent._child_options(options, None)
    assert child.sandbox == options.sandbox and child.sandbox is not options.sandbox
    assert child.env["PATH"] == options.env["PATH"]


def test_business_output_cannot_grant_source_repository(isolated):
    source = isolated / "source"
    source.mkdir()
    (source / ".git").mkdir()
    owner = agents.create("业务", str(source))
    meta = agents._read_meta(owner["id"])
    meta["execution_mode"] = "workspace"
    agents._write_meta(owner["id"], meta)
    with pytest.raises(ValueError, match="代码仓库"):
        execution.policy_for_session(agents.main_session_key(owner))


def test_migration_preview_backup_and_idempotence(isolated):
    spec = importlib.util.spec_from_file_location(
        "workspace_migration", Path(__file__).parents[1] / "scripts/migrate_business_workspaces.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    root = isolated / "agent-config"
    path = root / "nas" / "agent.json"
    path.parent.mkdir(parents=True)
    original = json.dumps({"name": "NAS", "workdir": str(isolated)})
    path.write_text(original)
    overrides = {"nas": str(isolated / "iCloud")}
    planned = migration.changes(root, overrides)
    assert path.read_text() == original
    assert len(planned) == 1
    backup = isolated / "backup"
    migration.apply_change(*planned[0], backup)
    assert (backup / "nas" / "agent.json").read_text() == original
    assert migration.changes(root, overrides) == []
    path.write_text(original)
    planned = migration.changes(root, overrides)
    path.write_text(json.dumps({"name": "主人刚改的新名字", "workdir": str(isolated)}))
    with pytest.raises(RuntimeError, match="规划后配置已变更"):
        migration.apply_change(*planned[0], backup)
    assert json.loads(path.read_text())["name"] == "主人刚改的新名字"
