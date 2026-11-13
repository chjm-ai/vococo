"""文字和语音共用聊天身份及 session_store 历史。

HTTP 请求通过 ContextVar 绑定当前聊天；旧客户端未传身份时仍使用 voice-chat:main。
聊天只读工具限制统一由 core.agent 添加，不传给用户明确派出的开发任务。
"""
from __future__ import annotations

from typing import AsyncIterator
from contextvars import ContextVar

from .. import config, providers
from ..core.agent import AgentReply, Done, Event, describe_llm_error, stream_turn
from ..memory import session_store
from . import prompts

SESSION_KEY = "voice-chat:main"
_active_key: ContextVar[str | None] = ContextVar("voice_chat_key", default=None)


def current_key() -> str:
    return _active_key.get() or SESSION_KEY


def activate(key: str | None):
    return _active_key.set(key)


HISTORY_LIMIT = 20

def load_history(limit: int = HISTORY_LIMIT) -> list:
    return session_store.load_recent(current_key(), limit=limit)


def append(user_text: str, assistant_text: str) -> None:
    if session_store.get_title(current_key()) in (None, "", "新聊天"):
        session_store.set_title(current_key(), user_text[:40])
    session_store.append(current_key(), user_text, assistant_text)


def get_resume() -> str | None:
    return session_store.get_sdk_session_id(current_key())


def set_resume(sid: str) -> None:
    session_store.set_sdk_session_id(current_key(), sid)


def clear() -> None:
    """清空语音会话上下文:推进 watermark(旧轮次仍留库可 search)+ 抹掉 SDK resume。
    run_turn 每轮现读 load_history()/get_resume(),所以下一轮语音立即从零开始,
    通话进行中也生效(WS 层不缓存 resume 态)。"""
    session_store.new_session(current_key())


class _CandidateFailed(Exception):
    """候选模型在尚未产出任何可见内容时就失败,允许外层换下一个候选。"""


def _model_candidates(chosen_model: str | None) -> list[str]:
    """按优先级构造候选模型列表:显式选择/全局默认 → 第三方兜底(仅当主选是官方时)。"""
    primary = chosen_model or config.MODEL
    candidates = [primary]
    _, primary_env = providers.resolve(primary, config.MODEL)
    if not primary_env:
        active = providers.load_active()
        if active and not active.is_official and active.model:
            fallback = providers._selection_id(active)
            if fallback not in candidates:
                candidates.append(fallback)
    return candidates


async def _candidate_stream(
    history: list,
    prompt_text: str,
    model: str,
    resume: str | None,
    extra_mcp_servers: dict | None,
) -> AsyncIterator[Event]:
    """跑单个候选模型。若失败前没有任何非 Done 事件,不吐出 Done,由外层换候选重试。"""
    emitted = False
    buffer: list[Event] = []
    try:
        async for ev in stream_turn(
            history, prompt_text, model=model, resume=resume, session_key=current_key(),
            extra_mcp_servers=extra_mcp_servers,
            system_prompt_extra=prompts.voice_system_prompt_extra(),
        ):
            if isinstance(ev, Done):
                if ev.reply.is_error and not emitted:
                    raise _CandidateFailed(ev.reply.error or f"模型 {model} 返回错误")
                if buffer:
                    for bev in buffer:
                        yield bev
                    buffer.clear()
                yield ev
                return
            emitted = True
            if buffer:
                for bev in buffer:
                    yield bev
                buffer.clear()
            yield ev
    except Exception as exc:
        if not emitted:
            raise _CandidateFailed(str(exc)) from exc
        raise


async def run_turn(
    prompt_text: str,
    model: str | None = None,
    extra_mcp_servers: dict | None = None,
) -> AsyncIterator[Event]:
    """载入历史、调 stream_turn,把事件流原样透传给调用方消费。

    调用方负责:收到 Done 后把 (原始 user_text, reply.text) 落库(见 append)、
    存回 reply.sdk_session_id(见 set_resume)——本函数只管跑一轮,不做落库,
    因为落库要存的是剥离指令块后的原文,这层信息只有调用方(routes.py)知道。

    model: 与文字聊天相同的模型选择逻辑;None 时回退到 config.MODEL,并自动尝试
    已配置的第三方供应商兜底(仅当主模型是官方/订阅时)。

    extra_mcp_servers:P1 任务板的工具(见 task_tools.build_server()),只有
    语音前台会话传它;后台任务会话(core/task_runner.py)直接调 stream_turn,
    不经过这里。
    """
    history = load_history()
    resume_sid = get_resume()
    last_error = ""
    for idx, cand in enumerate(_model_candidates(model)):
        try:
            async for ev in _candidate_stream(
                history, prompt_text, cand,
                resume=resume_sid if idx == 0 else None,
                extra_mcp_servers=extra_mcp_servers,
            ):
                yield ev
            return
        except _CandidateFailed as exc:
            last_error = str(exc)
    yield Done(
        AgentReply(
            text=describe_llm_error(None, last_error)
            if last_error
            else "所有可用模型都失败,请检查供应商配置。",
            tool_calls=[],
            cost_usd=None,
            is_error=True,
            error=last_error,
        )
    )
