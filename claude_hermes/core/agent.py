"""Agent loop —— 事件流式,走 Claude 订阅。

stream_turn() 是核心:开启 include_partial_messages,把 SDK 的流式事件
归一成一串好消费的事件(文字增量 / 思考增量 / 工具开始 / 工具结果 / 完成)。
TUI 和 Telegram 都消费这同一套事件 → 流式输出 + 工具调用过程可见。

run_turn() 是其上的便捷封装(累积成最终回复),给纯文本 chat 用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Union

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    UserMessage,
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


# === 流式事件类型 ===
@dataclass
class TextDelta:
    """正文 token 增量。"""

    text: str


@dataclass
class ThinkingDelta:
    """思考 token 增量。"""

    text: str


@dataclass
class ToolStarted:
    """模型开始调用某工具。"""

    name: str


@dataclass
class ToolFinished:
    """工具返回结果。"""

    name: str
    ok: bool
    preview: str


@dataclass
class Done:
    """本轮结束,带最终回复。"""

    reply: AgentReply


Event = Union[TextDelta, ThinkingDelta, ToolStarted, ToolFinished, Done]


def _compose_prompt(history: list[Turn], user_text: str) -> str:
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


def _preview(content, n: int = 80) -> str:
    """把工具结果压成一行预览。"""
    if isinstance(content, str):
        s = content
    elif isinstance(content, list):
        s = " ".join(
            (p.get("text", "") if isinstance(p, dict) else str(p)) for p in content
        )
    else:
        s = str(content)
    s = " ".join(s.split())
    return s[:n] + ("…" if len(s) > n else "")


async def stream_turn(
    history: list[Turn], user_text: str, model: str | None = None
) -> AsyncIterator[Event]:
    """流式跑一轮,逐个 yield 事件,最后 yield Done。"""
    options = ClaudeAgentOptions(
        model=model or config.MODEL,
        system_prompt=build_system_prompt(),
        max_turns=config.MAX_TURNS,
        permission_mode=config.PERMISSION_MODE,
        include_partial_messages=True,
    )

    text_parts: list[str] = []
    tool_calls: list[str] = []
    tool_name_by_id: dict[str, str] = {}
    cost_usd: float | None = None
    is_error = False

    async for msg in query(
        prompt=_compose_prompt(history, user_text), options=options
    ):
        if isinstance(msg, StreamEvent):
            ev = msg.event if isinstance(msg.event, dict) else {}
            etype = ev.get("type")
            if etype == "content_block_delta":
                delta = ev.get("delta", {})
                dt = delta.get("type")
                if dt == "text_delta":
                    t = delta.get("text", "")
                    if t:
                        text_parts.append(t)
                        yield TextDelta(t)
                elif dt == "thinking_delta":
                    t = delta.get("thinking", "")
                    if t:
                        yield ThinkingDelta(t)
            elif etype == "content_block_start":
                cb = ev.get("content_block", {})
                if cb.get("type") == "tool_use":
                    name = cb.get("name", "?")
                    tid = cb.get("id", "")
                    if tid:
                        tool_name_by_id[tid] = name
                    tool_calls.append(name)
                    yield ToolStarted(name)
        elif isinstance(msg, UserMessage):
            for b in msg.content:
                if isinstance(b, ToolResultBlock):
                    name = tool_name_by_id.get(b.tool_use_id, "工具")
                    yield ToolFinished(
                        name=name,
                        ok=not bool(b.is_error),
                        preview=_preview(b.content),
                    )
        elif isinstance(msg, ResultMessage):
            cost_usd = getattr(msg, "total_cost_usd", None)
            is_error = bool(getattr(msg, "is_error", False))

    yield Done(
        AgentReply(
            text="".join(text_parts).strip(),
            tool_calls=tool_calls,
            cost_usd=cost_usd,
            is_error=is_error,
        )
    )


async def run_turn(
    history: list[Turn], user_text: str, model: str | None = None
) -> AgentReply:
    """非流式便捷封装:累积事件,返回最终回复。"""
    reply = AgentReply(text="", tool_calls=[], cost_usd=None, is_error=False)
    async for ev in stream_turn(history, user_text, model):
        if isinstance(ev, Done):
            reply = ev.reply
    return reply
