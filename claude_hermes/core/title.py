"""会话标题总结:首条消息发出后,用便宜模型把它浓缩成侧边栏标题。

不等 AI 首轮回复(可能跑很久),用户消息一落就异步开始总结;期间侧边栏先显示
截断兜底标题(session_store.ensure_title),总结完成后覆盖并广播刷新。

模型两级:
  1. 首选 Haiku —— 走 Claude 订阅(SDK 一次性 query,不烧 API 钱);
  2. 失败(撞限额/网络)→ 回落设置页里名为 deepseek 的第三方供应商
     (当前配置 = deepseek-v4-flash,按量但极便宜);
  3. 都失败 → 返回 None,调用方保留截断兜底,静默放弃。
"""
from __future__ import annotations

import asyncio

from .. import providers

# 标题硬上限(字符)。截断兜底与模型输出清洗共用,session_store.ensure_title 的
# 默认 limit 与此保持一致。
MAX_LEN = 40

_PRIMARY_MODEL = "claude-haiku-4-5"  # 走订阅
_FALLBACK_PROVIDER = "deepseek"  # 设置页里的第三方供应商名(按名查,不写死模型)
_TIMEOUT = 45  # 单次尝试秒数;标题不急,但别无限挂着

_PROMPT = (
    "把下面这条用户消息概括成一个简短的会话标题,不超过 20 个字。"
    "只输出标题本身:不要引号、不要句号、不要「关于」「请求」「帮我」这类空话开头。\n\n"
    "用户消息:\n{text}"
)


def _clean(raw: str) -> str:
    """清洗模型输出:只留第一行,去首尾引号/标点,硬截 MAX_LEN。"""
    stripped = raw.strip()
    line = stripped.splitlines()[0] if stripped else ""
    line = line.strip().strip("\"'「」『』《》【】。,,.!!??::;;").strip()
    return " ".join(line.split())[:MAX_LEN]


async def _ask(text: str, model: str, env: dict[str, str]) -> str:
    """SDK 一次性 query:无工具无 MCP,纯文本问答,取 ResultMessage.result。"""
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    options = ClaudeAgentOptions(
        model=model,
        max_turns=1,
        tools=[],  # 标题任务不需要任何工具,顺带把 schema 瘦到最小
        system_prompt="你是标题生成器,只输出标题文本,别的什么都不说。",
        env=env,
    )
    out = ""
    async for msg in query(prompt=_PROMPT.format(text=text[:500]), options=options):
        if isinstance(msg, ResultMessage):
            out = msg.result or ""
    return out


async def summarize(text: str) -> str | None:
    """用户首条消息 → 标题;两级模型都失败返回 None(调用方保留截断兜底)。"""
    candidates: list[tuple[str, dict[str, str]]] = [(_PRIMARY_MODEL, {})]
    fallback = providers.sidecar_env(_FALLBACK_PROVIDER)
    if fallback:
        candidates.append(fallback)
    for model, env in candidates:
        try:
            raw = await asyncio.wait_for(_ask(text, model, env), timeout=_TIMEOUT)
        except Exception:
            continue  # 限额/断网/CLI 异常都走兜底,标题失败不值得报错打扰
        title = _clean(raw)
        if title:
            return title
    return None
