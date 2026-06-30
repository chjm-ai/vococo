"""Agent loop —— 单轮对话,走 Claude 订阅。

M0 用 claude-agent-sdk 的 query() 一次性接口,把会话历史拼进 prompt 实现多轮。
(M1 可换成持久化 session / ClaudeSDKClient。)
"""
from __future__ import annotations

from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from .. import config
from .prompt import build_system_prompt


@dataclass
class Turn:
    """一轮对话。"""

    user: str
    assistant: str = ""


@dataclass
class AgentReply:
    text: str
    tool_calls: list[str]
    cost_usd: float | None
    is_error: bool


def _compose_prompt(history: list[Turn], user_text: str) -> str:
    """把历史 + 当前问题拼成一段 prompt。"""
    if not history:
        return user_text
    lines = ["[此前对话]"]
    for t in history:
        lines.append(f"我:{t.user}")
        if t.assistant:
            lines.append(f"你:{t.assistant}")
    lines.append("\n[当前]")
    lines.append(f"我:{user_text}")
    return "\n".join(lines)


async def run_turn(
    history: list[Turn], user_text: str, model: str | None = None
) -> AgentReply:
    """跑一轮,返回助理回复。model 缺省用 config.MODEL。"""
    options = ClaudeAgentOptions(
        model=model or config.MODEL,
        system_prompt=build_system_prompt(),
        max_turns=config.MAX_TURNS,
        permission_mode="acceptEdits",
    )

    text_parts: list[str] = []
    tool_calls: list[str] = []
    cost_usd: float | None = None
    is_error = False

    async for msg in query(
        prompt=_compose_prompt(history, user_text), options=options
    ):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append(block.name)
        elif isinstance(msg, ResultMessage):
            cost_usd = getattr(msg, "total_cost_usd", None)
            is_error = bool(getattr(msg, "is_error", False))

    return AgentReply(
        text="".join(text_parts).strip(),
        tool_calls=tool_calls,
        cost_usd=cost_usd,
        is_error=is_error,
    )
