"""语音会话:主会话历史落进跟普通文字对话共用的 memory/session_store(data/state.db),
固定键 `voice-chat:main` —— 这样侧边栏「语音任务」分组、`/history` 接口、消息渲染都能
直接复用普通会话那一套现成代码,不用为语音再单独拼一套跨库读取逻辑(见
03-phase2-实现记录.md 存储统一改动一节)。

历史上这里曾经是完全独立的一份 sqlite(data/voice/voice.db),2026-07-08 起改为委托
session_store,对外的 load_history/append/get_resume/set_resume/run_turn 签名不变,
ws.py/routes.py 的调用点不用跟着改。
"""
from __future__ import annotations

from typing import AsyncIterator

from .. import config, providers
from ..core.agent import AgentReply, Done, Event, describe_llm_error, stream_turn
from ..memory import session_store
from . import prompts

SESSION_KEY = "voice-chat:main"
HISTORY_LIMIT = 20

# 语音前台会话代码层硬禁改代码的工具(2026-07-09):【派活规则】里"改代码要走
# voice_dispatch_task"以前完全靠 prompt 临场判断,没有代码兜底,真机测出过嘴上
# 答应却直接自己动手/漏派的情况。这里直接不给前台会话这几个工具,模型物理上叫不动
# Edit/Write,真要改代码只能走 voice_dispatch_task 派后台任务——那条路径现在会自动
# 给任务开独立 git worktree + 分支(见 core/worktree.ensure_worktree_for_task),
# 跟 Web/CLI 新对话「一任务一分支」的规矩保持一致,而不是在语音上下文里直接改主目录。
_DISALLOWED_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"]


def load_history(limit: int = HISTORY_LIMIT) -> list:
    return session_store.load_recent(SESSION_KEY, limit=limit)


def append(user_text: str, assistant_text: str) -> None:
    session_store.append(SESSION_KEY, user_text, assistant_text)


def get_resume() -> str | None:
    return session_store.get_sdk_session_id(SESSION_KEY)


def set_resume(sid: str) -> None:
    session_store.set_sdk_session_id(SESSION_KEY, sid)


def clear() -> None:
    """清空语音会话上下文:推进 watermark(旧轮次仍留库可 search)+ 抹掉 SDK resume。
    run_turn 每轮现读 load_history()/get_resume(),所以下一轮语音立即从零开始,
    通话进行中也生效(WS 层不缓存 resume 态)。"""
    session_store.new_session(SESSION_KEY)


class _CandidateFailed(Exception):
    """候选模型在尚未产出任何可见内容时就失败,允许外层换下一个候选。"""


def _model_candidates(chosen_model: str | None) -> list[str]:
    """按优先级构造候选模型列表:显式选择/全局默认 → 第三方兜底(仅当主选是官方时)。"""
    primary = chosen_model or config.MODEL
    candidates = [primary]
    _, primary_env = providers.resolve(primary, config.MODEL)
    if not primary_env:
        active = providers.load_active()
        if active and not active.is_official and active.model and active.model not in candidates:
            candidates.append(active.model)
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
            history, prompt_text, model=model, resume=resume, session_key=SESSION_KEY,
            extra_mcp_servers=extra_mcp_servers, disallowed_tools=_DISALLOWED_TOOLS,
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
