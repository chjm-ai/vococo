"""Agent loop —— 事件流式,走 Claude 订阅。

stream_turn() 是核心:开启 include_partial_messages,把 SDK 的流式事件
归一成一串好消费的事件(文字增量 / 思考增量 / 工具开始 / 工具结果 / 完成)。
TUI 和 Telegram 都消费这同一套事件 → 流式输出 + 工具调用过程可见。

run_turn() 是其上的便捷封装(累积成最终回复),给纯文本 chat 用。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Union

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    UserMessage,
)

from .. import config
from ..tools.builtin import build_mcp_servers
from ..tools.danger import build_hooks
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
class ToolInput:
    """某工具调用的完整入参(流式拼装完成后发出)。

    Phase 0 keystone:没有入参,前端就渲染不出 diff / todo / 计划卡。
    在 content_block_stop 时把累积的 input_json_delta 解析成 dict 发出。
    """

    name: str
    tool_id: str
    tool_input: dict


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


Event = Union[TextDelta, ThinkingDelta, ToolStarted, ToolInput, ToolFinished, Done]


def assemble_tool_input(raw: str) -> dict:
    """把累积的 input_json_delta 片段解析成 dict;空/坏 JSON 都安全退化成 {}。"""
    s = (raw or "").strip()
    if not s:
        return {}
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
    cwd: str | None = None,
) -> AsyncIterator[Event]:
    """流式跑一轮,逐个 yield 事件,最后 yield Done。

    上下文占用(context_tokens)取 SDK 的 get_context_usage() —— 即 CLI /context
    的真实窗口占用。不再用 ResultMessage.usage 现算:后者是本轮跨多次工具调用的
    累计值,cache_read 会被反复累加,导致数字虚高("看着超了实际没超")。
    turn_tokens / 成本 / 明细仍取 ResultMessage.usage —— 那本就是本轮累计消耗,语义正确。
    """
    options = ClaudeAgentOptions(
        model=model or config.MODEL,
        system_prompt=build_system_prompt(),
        max_turns=config.MAX_TURNS,
        permission_mode=config.PERMISSION_MODE,
        include_partial_messages=True,
        mcp_servers=build_mcp_servers(),
        hooks=build_hooks(),  # PreToolUse:灾难拦截 + 危险操作审批闸
        skills=config.SKILLS,  # None=全量;白名单则只挂这些(瘦身 tool schema)
        cwd=cwd,  # 项目会话→该文件夹当工作根(自动加载其 CLAUDE.md/.claude);None=进程默认目录
    )

    text_parts: list[str] = []
    tool_calls: list[str] = []
    tool_name_by_id: dict[str, str] = {}
    tool_json: dict[int, str] = {}  # 块索引 -> 累积的入参 JSON 片段
    tool_meta: dict[int, tuple[str, str]] = {}  # 块索引 -> (tool_id, name)
    cost_usd: float | None = None
    is_error = False
    context_tokens = 0
    turn_tokens = 0
    input_fresh = 0
    cache_read = 0
    output_tokens = 0
    used_model = model or config.MODEL
    ctx_window_val = context_window(used_model)

    async with ClaudeSDKClient(options=options) as client:
        await client.query(_build_prompt(history, user_text, images or []))
        async for msg in client.receive_response():
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
                    elif dt == "input_json_delta":
                        # 工具入参是流式的 partial_json,按块索引累积,块结束时解析
                        idx = ev.get("index")
                        if isinstance(idx, int):
                            tool_json[idx] = tool_json.get(idx, "") + (
                                delta.get("partial_json", "") or ""
                            )
                elif etype == "content_block_start":
                    cb = ev.get("content_block", {})
                    if cb.get("type") == "tool_use":
                        name = cb.get("name", "?")
                        tid = cb.get("id", "")
                        if tid:
                            tool_name_by_id[tid] = name
                        idx = ev.get("index")
                        if isinstance(idx, int):
                            tool_meta[idx] = (tid, name)
                            tool_json.setdefault(idx, "")
                        tool_calls.append(name)
                        yield ToolStarted(name)
                elif etype == "content_block_stop":
                    # 该工具块的入参已流完 → 解析并发出 ToolInput(喂 diff/todo/审批)
                    idx = ev.get("index")
                    if isinstance(idx, int) and idx in tool_meta:
                        tid, name = tool_meta.pop(idx)
                        parsed = assemble_tool_input(tool_json.pop(idx, ""))
                        yield ToolInput(name=name, tool_id=tid, tool_input=parsed)
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
                # input_tokens 不含缓存;这些明细是本轮累计吞吐(展示/落库用)
                input_fresh = in_t + cache_c  # 本轮真正处理的输入(含新写入缓存的部分)
                cache_read = cache_r  # 缓存命中(便宜的复读)
                output_tokens = out_t
                turn_tokens = input_fresh + out_t  # 本轮新鲜吞吐,累计即消耗(不含缓存复读)
                # 上下文占用先用累计值兜底,下面 get_context_usage 成功则覆盖为真实值
                context_tokens = input_fresh + cache_read
                # 实际模型优先取 model_usage 的 key(SDK 报告的真实模型)
                mu = getattr(msg, "model_usage", None) or {}
                if mu:
                    used_model = next(iter(mu), used_model)

        # ResultMessage 已到、会话尚未断开 —— 此刻问 SDK 当前窗口的真实占用
        # (等价 CLI /context)。失败(旧 CLI 不支持等)则静默保留上面的兜底值。
        try:
            cu = await client.get_context_usage()
            total = int(cu.get("totalTokens", 0) or 0)
            raw_max = int(cu.get("rawMaxTokens") or cu.get("maxTokens") or 0)
            if total:
                context_tokens = total
            if raw_max:
                ctx_window_val = raw_max
        except Exception:
            ctx_window_val = context_window(used_model)  # 兜底:按模型名估窗口

    yield Done(
        AgentReply(
            text="".join(text_parts).strip(),
            tool_calls=tool_calls,
            cost_usd=cost_usd,
            is_error=is_error,
            context_tokens=context_tokens,
            turn_tokens=turn_tokens,
            context_window=ctx_window_val,
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
