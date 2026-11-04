"""付费真机冒烟：stream_turn → SharedTurn → WebSink → SQLite，全部使用临时业务目录。

先在进程环境中加载订阅/供应商配置，再设 CHECKLIST_PROBE_SETTINGS 与 CHECKLIST_PROBE_MODEL。
沿用生产默认工具及提示词；自然多步请求不点名清单工具。只读写临时目录，不碰生产会话库。
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from claude_agent_sdk import ClaudeSDKClient, SystemMessage
from vococo import config
from vococo.core import agent, client_pool
from vococo.core.session_execution import SharedTurn
from vococo.gateway import settings_store
from vococo.gateway.adapters.web import _WebSink
from vococo.memory import _db, session_store


class Recorder:
    def __init__(self):
        self.events = []

    def _emit(self, payload):
        self.events.append(payload)


class IsolatedClient(ClaudeSDKClient):
    tool_sets = []

    def __init__(self, options):
        # 只隔离个人技能和插件；工具集、权限及提示词必须来自真实 stream_turn 配置。
        assert options.tools is None
        options.setting_sources = []
        options.plugins = []
        super().__init__(options=options)

    async def receive_messages(self):
        async for message in super().receive_messages():
            if isinstance(message, SystemMessage) and message.subtype == "init":
                tools = set(message.data["tools"])
                assert {"Bash", "Read", "Write", "Edit", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList"} <= tools
                self.tool_sets.append(sorted(tools))
            yield message


async def run_round(prompt, model, cwd, recorder, key="web:checklist-smoke"):
    turn_id = session_store.start_turn(key, prompt)
    turn = SharedTurn(key, turn_id, _WebSink(recorder, "checklist-smoke"))
    async for event in agent.stream_turn([], prompt, model=model, cwd=cwd, session_key=key,
                                         resume=turn.resume, max_turns=20):
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
              patch.object(agent, "ClaudeSDKClient", IsolatedClient),
              patch.object(client_pool, "enabled", return_value=False)):
            _db.reset()
            load_prompt = agent._load_system_prompt

            async def old_prompt(*args, **kwargs):
                prompt = dict(await load_prompt(*args, **kwargs))
                prompt["append"] = prompt.get("append", "").replace(agent.CHECKLIST_GUIDANCE, "")
                return prompt

            # 先冻结不含清单规则的旧提示词，再以新配置冷续接，不能只测新建会话。
            with patch.object(agent, "_load_system_prompt", old_prompt):
                simple = await run_round("2+2等于几？只答数字。", model, directory, Recorder(), "web:natural-smoke")
            assert not any(b["type"] == "checklist" for b in simple.timeline.blocks)
            (root / "sales.json").write_text('[{"region":"东区","amount":12},{"region":"西区","amount":8},{"region":"东区","amount":5}]')
            natural = await run_round(
                "读取 sales.json，按地区统计总额，再生成 report.md，最后从磁盘重新读取报告核对每个数字。"
                "完成后只给出文件名与核对结果。", model, directory, Recorder(), "web:natural-smoke")
            natural_items = [b["items"] for b in natural.timeline.blocks if b["type"] == "checklist"]
            assert simple.resume == natural.resume, "旧 SDK 会话未续接"
            assert natural_items and len(natural_items[-1]) >= 3, natural_items
            assert all(item["status"] == "completed" for item in natural_items[-1]), natural_items
            report = (root / "report.md").read_text()
            assert "17" in report and "8" in report, report
            print(json.dumps({"natural_trigger": True, "simple_trigger": False, "default_tool_count": len(IsolatedClient.tool_sets[-1])}))
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
