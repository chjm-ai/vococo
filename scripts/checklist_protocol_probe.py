"""只运行 SDK 内置清单工具，记录真实协议；不接触业务任务库。"""
import asyncio
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, AssistantMessage, UserMessage
from vococo.core.agent import _turn_env


async def main():
    from vococo import providers
    from vococo.gateway import settings_store

    if os.environ.get("CHECKLIST_PROBE_SETTINGS"):
        settings_store._PATH = Path(os.environ["CHECKLIST_PROBE_SETTINGS"])
    model, provider_env = providers.resolve(os.environ.get("CHECKLIST_PROBE_MODEL", "claude-sonnet-5-5"), "claude-sonnet-5-5")
    records = []
    with tempfile.TemporaryDirectory(prefix="checklist-probe-") as cwd:
        options = ClaudeAgentOptions(
            model=model, cwd=cwd, env=_turn_env(provider_env),
            setting_sources=[], max_turns=12,
            allowed_tools=["TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TodoWrite"],
            tools=["TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TodoWrite"],
            system_prompt="你是内置任务清单工具的协议测试员。只调用指定工具，不操作文件，不调用其他工具。",
        )
        async with ClaudeSDKClient(options=options) as client:
            await client.query(
                "依次完成：用 TaskCreate 创建 subject=协议测试甲、description=测试状态同步、activeForm=正在测试；"
                "用 TaskUpdate 改为 in_progress；用 TaskGet 读取；用 TaskList 读取；"
                "用 TaskUpdate 改为 completed；用 TaskUpdate 改为 deleted；用 TaskList 读取空清单；"
                "最后用 TaskUpdate 尝试更新不存在的 taskId=999 为 completed，以核对失败回执。然后结束。"
                "若工具不可用，直接说明，不要模拟结果。"
            )
            async for msg in client.receive_response():
                if isinstance(msg, (AssistantMessage, UserMessage)):
                    for block in msg.content:
                        if hasattr(block, "name") or hasattr(block, "tool_use_id"):
                            record = asdict(block)
                            records.append(record)
                            print(json.dumps(record, ensure_ascii=False), flush=True)
                elif hasattr(msg, "result"):
                    print("RESULT", getattr(msg, "is_error", None), msg.result, flush=True)
    output = Path("/tmp/vococo-checklist-protocol.json")
    output.write_text(json.dumps(records, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
