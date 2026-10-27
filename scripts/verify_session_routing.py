"""用真实模型验收会话选工具；只向临时库及本地测试 inbox 投递，不触碰生产会话。"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ToolUseBlock, create_sdk_mcp_server
from dotenv import load_dotenv

# worktree 没有供应商配置；仅复用主仓库认证，不输出凭据。
ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / ".env").exists():
    load_dotenv(Path.home() / "Repos/vococo/.env", override=False)

from vococo import config, providers
from vococo.gateway import settings_store
from vococo.core.agent import _turn_env
from vococo.core.prompt import PERSONA
from vococo.gateway import web_bridge
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import _db, agents, session_store
from vococo.tools import danger, sessions

INSTRUCTION = "不改变 PM 工作节奏，以周例会驱动分析与更新；先跟我对齐思路，得到我确认后再修改。"


async def scenario(model: str, provider_env: dict, by_id: bool, root: Path) -> dict:
    config.DATA_DIR = root / "data"
    config.AGENT_WORKSPACES_DIR = root / "workspaces"
    config.AI_BRAIN_DIR = root / "brain"
    _db.reset()
    sessions._queued.clear()
    owner = agents.create("隔离验收")
    prefix = f"web:p{owner['project_hash']}:"
    source, ppt, product = [prefix + slug for slug in ("source", "ppt", "product")]
    for key, title in ((ppt, "DEMO提案PPT"), (product, "DEMO产品方案")):
        session_store.append(key, "旧方案", "等待下一步指令")
        session_store.set_title(key, title)
    # 目标不是最近 10 条，验收必须先筛标题再限条数。
    for index in range(12):
        session_store.append(prefix + str(index), "无关问题", "无关回答")
    session_store.start_turn(source, "转达产品思路")
    before = len(session_store.list_sessions("web:"))
    adapter = WebAdapter()
    adapter._emit = lambda event: None
    web_bridge.register(adapter.inject, lambda key: False)
    tools = sessions.build_tools(source, [])
    used = []

    async def guard(data, tool_use_id, context):
        result = await danger.pretool_guard_hook(data, tool_use_id, context)
        if result:
            return result
        if data["tool_name"] not in {"mcp__vococo__" + t.name for t in tools}:
            return danger._deny("隔离验收仅允许使用会话查询与续接工具。")
        return {}

    from claude_agent_sdk.types import HookMatcher
    options = ClaudeAgentOptions(
        model=model, cwd=str(root), setting_sources=[], permission_mode="bypassPermissions",
        system_prompt=PERSONA, max_turns=12, env=_turn_env(provider_env, no_auto_memory=True),
        mcp_servers={"vococo": create_sdk_mcp_server("vococo", tools=tools)},
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[guard])]},
    )
    targets = f"{ppt.removeprefix('web:')} 和 {product}" if by_id else "DEMO提案PPT 和 DEMO产品方案"
    async with ClaudeSDKClient(options=options) as client:
        await client.query(f"找到 {targets} 两个已有会话，分别原样转达以下指令，不新建会话：{INSTRUCTION}")
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        used.append(block.name)
                    elif getattr(block, "text", ""):
                        print(block.text, flush=True)
            elif getattr(message, "is_error", False):
                raise RuntimeError(f"模型调用失败：{getattr(message, 'result', '')}")
    incoming = []
    while not adapter._inbox.empty():
        incoming.append(adapter._inbox.get_nowait())
    assert {inc.session_key for inc in incoming} == {ppt, product}, used
    assert len(incoming) == 2
    assert all(INSTRUCTION in inc.text for inc in incoming), "未完整转达先确认再修改的边界"
    assert len(session_store.list_sessions("web:")) == before, "不应创建会话"
    assert not {"ListAgents", "SendMessage"}.intersection(used), used
    if not by_id:
        assert "mcp__vococo__list_sessions" in used, used
    return {"case": "完整/裸ID" if by_id else "标题查旧会话", "passed": True,
            "delivered": len(incoming), "tools": used}


async def main(model: str, settings: str | None):
    if settings:
        settings_store._PATH = Path(settings).expanduser().resolve(strict=True)
    model, provider_env = providers.resolve(model, config.MODEL)
    for by_id in (False, True):
        with tempfile.TemporaryDirectory(prefix="vococo-session-acceptance-") as path:
            try:
                result = await asyncio.wait_for(scenario(model, provider_env, by_id, Path(path)), 180)
                print(json.dumps(result, ensure_ascii=False), flush=True)
            finally:
                _db.reset()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="claude-opus-4-6")
    parser.add_argument("--settings", help="只读复用现有模型设置文件，绝不写入")
    args = parser.parse_args()
    asyncio.run(main(args.model, args.settings))
