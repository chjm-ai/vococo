"""付费真机冒烟：stream_turn → SharedTurn → WebSink → SQLite，全部使用临时业务目录。

先在进程环境中加载订阅/供应商配置，再设 CHECKLIST_PROBE_SETTINGS 与 CHECKLIST_PROBE_MODEL。
只允许内置清单工具；不会读写生产会话库，不合并、不重启。
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from claude_agent_sdk import ClaudeSDKClient
from vococo import config
from vococo.core import agent, client_pool
from vococo.core.checklist import CHECKLIST_TOOLS
from vococo.core.session_execution import SharedTurn
from vococo.gateway import settings_store
from vococo.gateway.adapters.web import _WebSink
from vococo.memory import _db, session_store


class Recorder:
    def __init__(self):
        self.events = []

    def _emit(self, payload):
        self.events.append(payload)


def restricted_client(options):
    options.setting_sources = []
    options.tools = sorted(CHECKLIST_TOOLS - {"TaskDelete"})
    options.allowed_tools = options.tools
    options.plugins = []
    return ClaudeSDKClient(options=options)


async def minimal_prompt(*_args, **_kwargs):
    return "只执行用户要求的内置任务清单操作。不要访问文件、外部服务或调用其他工具。"


async def run_round(prompt, model, cwd, recorder):
    key = "web:checklist-smoke"
    turn_id = session_store.start_turn(key, prompt)
    turn = SharedTurn(key, turn_id, _WebSink(recorder, "checklist-smoke"))
    async for event in agent.stream_turn([], prompt, model=model, cwd=cwd, session_key=key,
                                         resume=turn.resume, max_turns=8):
        await turn.observe(event)
    assert turn.reply is not None and not turn.reply.is_error, getattr(turn.reply, "text", "无结果")
    session_store.finish_turn(turn_id, turn.reply.text, events=turn.timeline.blocks, session_key=key)
    return turn


async def main():
    model = os.environ.get("CHECKLIST_PROBE_MODEL", "claude-sonnet-5-5")
    recorder = Recorder()
    with tempfile.TemporaryDirectory(prefix="vococo-checklist-smoke-") as directory:
        root = Path(directory)
        with (patch.object(config, "DATA_DIR", root / "data"),
              patch.object(config, "AI_BRAIN_DIR", root / "brain"),
              patch.object(config, "AGENT_WORKSPACES_DIR", root / "workspaces"),
              patch.object(settings_store, "_PATH", Path(os.environ["CHECKLIST_PROBE_SETTINGS"])),
              patch.object(settings_store, "vococo_enabled", return_value=False),
              patch.object(settings_store, "effective_external_mcp", return_value={}),
              patch.object(settings_store, "effective_skills", return_value=[]),
              patch.object(agent, "_load_system_prompt", minimal_prompt),
              patch.object(agent, "ClaudeSDKClient", restricted_client),
              patch.object(client_pool, "enabled", return_value=False)):
            _db.reset()
            first = await run_round(
                "用 TaskCreate 创建 subject=真机测试步骤、description=验证跨轮状态、activeForm=正在测试。"
                "然后用 TaskUpdate 改成 in_progress，保持这个状态结束回复，不要标完成。", model, directory, recorder)
            snapshots = [b for b in first.timeline.blocks if b["type"] == "checklist"]
            assert len(snapshots) == 1 and snapshots[0]["items"][0]["status"] == "in_progress", snapshots
            second = await run_round(
                "继续刚才的清单：直接用 TaskUpdate 把已有任务改为 completed，然后结束。不要重新创建。",
                model, directory, recorder)
            snapshots = [b for b in second.timeline.blocks if b["type"] == "checklist"]
            assert snapshots[0]["items"][0]["status"] == "completed", snapshots
            assert first.resume == second.resume, "未复用 SDK 会话"
            output = {"model": model, "events": recorder.events,
                      "turns": session_store.load_history("web:checklist-smoke")}
            Path("/tmp/vococo-checklist-smoke.json").write_text(json.dumps(output, ensure_ascii=False, indent=2))
            print(json.dumps({"model": model, "rounds": 2, "snapshots": [
                e["items"] for e in recorder.events if e["type"] == "checklist"],
                "same_sdk_session": True, "artifact": "/tmp/vococo-checklist-smoke.json"}, ensure_ascii=False))
            _db.reset()


if __name__ == "__main__":
    asyncio.run(main())
