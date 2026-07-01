"""Agent loop —— 事件流式,走 Claude 订阅。

stream_turn() 是核心:开启 include_partial_messages,把 SDK 的流式事件
归一成一串好消费的事件(文字增量 / 思考增量 / 工具开始 / 工具结果 / 完成)。
TUI 和 Telegram 都消费这同一套事件 → 流式输出 + 工具调用过程可见。

run_turn() 是其上的便捷封装(累积成最终回复),给纯文本 chat 用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Union

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    UserMessage,
    query,
)

from .. import config
from ..tools.builtin import build_mcp_servers
from .prompt import build_system_prompt


@dataclass
class Turn:
    """一轮对话。"""

    user: str
    assistant: str = ""


@dataclass
class ImageAttachment:
    """一张图片,base64 编码,直接喂给 Claude 的多模态 content block。"""

    data: str  # base64
    media_type: str  # 如 image/jpeg


# 各模型上下文窗口(token)。前缀匹配,未知默认 200k。
# 注:Sonnet 支持 1M context 需专门开 beta header,本项目未开,故按 200k。
_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4": 200_000,
    "claude-sonnet-5": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-haiku-4": 200_000,
}


def context_window(model: str) -> int:
    m = (model or "").lower()
    for prefix, win in _CONTEXT_WINDOWS.items():
        if m.startswith(prefix):
            return win
    return 200_000


@dataclass
class AgentReply:
    text: str
    tool_calls: list[str]
    cost_usd: float | None
    is_error: bool
    context_tokens: int = 0  # 当前上下文占用(input+cache),≈ 塞进窗口的总量
    turn_tokens: int = 0  # 本轮新增吞吐(input+output),累计即"消耗"
    context_window: int = 200_000  # 该模型的上下文窗口
    input_fresh: int = 0  # 本轮非缓存输入
    cache_read: int = 0  # 本轮缓存命中(便宜的复读)
    output_tokens: int = 0  # 本轮输出
    model: str = ""  # 实际使用的模型


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


async def _image_prompt_stream(
    content: list[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """把带图片的一轮包成 SDK 要的流式输入(单条 user 消息)。"""
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


def _build_prompt(
    history: list[Turn], user_text: str, images: list[ImageAttachment]
) -> str | AsyncIterator[dict[str, Any]]:
    text = _compose_prompt(history, user_text)
    if not images:
        return text
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for img in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.media_type,
                    "data": img.data,
                },
            }
        )
    return _image_prompt_stream(content)


async def stream_turn(
    history: list[Turn],
    user_text: str,
    model: str | None = None,
    images: list[ImageAttachment] | None = None,
) -> AsyncIterator[Event]:
    """流式跑一轮,逐个 yield 事件,最后 yield Done。"""
    options = ClaudeAgentOptions(
        model=model or config.MODEL,
        system_prompt=build_system_prompt(),
        max_turns=config.MAX_TURNS,
        permission_mode=config.PERMISSION_MODE,
        include_partial_messages=True,
        mcp_servers=build_mcp_servers(),
        skills=config.SKILLS,  # None=全量;白名单则只挂这些(瘦身 tool schema)
    )

    text_parts: list[str] = []
    tool_calls: list[str] = []
    tool_name_by_id: dict[str, str] = {}
    cost_usd: float | None = None
    is_error = False
    context_tokens = 0
    turn_tokens = 0
    input_fresh = 0
    cache_read = 0
    output_tokens = 0
    used_model = model or config.MODEL

    async for msg in query(
        prompt=_build_prompt(history, user_text, images or []), options=options
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
            u = getattr(msg, "usage", None) or {}
            in_t = int(u.get("input_tokens", 0) or 0)
            out_t = int(u.get("output_tokens", 0) or 0)
            cache_r = int(u.get("cache_read_input_tokens", 0) or 0)
            cache_c = int(u.get("cache_creation_input_tokens", 0) or 0)
            # input_tokens 不含缓存,上下文实际占用 = 新处理 + 缓存复用
            input_fresh = in_t + cache_c  # 本轮真正处理的输入(含新写入缓存的部分)
            cache_read = cache_r  # 缓存命中(便宜的复读)
            output_tokens = out_t
            context_tokens = input_fresh + cache_read  # 二者之和 = 塞进窗口的总量
            turn_tokens = input_fresh + out_t  # 本轮新鲜吞吐,累计即消耗(不含缓存复读)
            # 实际模型优先取 model_usage 的 key(SDK 报告的真实模型)
            mu = getattr(msg, "model_usage", None) or {}
            if mu:
                used_model = next(iter(mu), used_model)

    yield Done(
        AgentReply(
            text="".join(text_parts).strip(),
            tool_calls=tool_calls,
            cost_usd=cost_usd,
            is_error=is_error,
            context_tokens=context_tokens,
            turn_tokens=turn_tokens,
            context_window=context_window(used_model),
            input_fresh=input_fresh,
            cache_read=cache_read,
            output_tokens=output_tokens,
            model=used_model,
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
