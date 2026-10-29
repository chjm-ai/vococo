"""前台与无人值守执行共用的会话流，不依赖网关或任务调度。"""
from __future__ import annotations

import time

from .. import config, providers
from ..memory import agents, session_store
from ..tools import danger
from .agent import (
    Compacted, ContextUsage, Done, SessionStarted, TextDelta, ThinkingDelta,
    ToolFinished, ToolInput, ToolStarted, UpstreamRetry,
    effective_context_window, _window_ratio,
)
from .timeline import Timeline


class SharedTurn:
    def __init__(self, session_key: str, turn_id: int | None, sink=None, *, fresh: bool = False):
        self.session_key = session_key
        self.turn_id = turn_id
        self.sink = sink
        self.chosen_model = session_store.get_chosen_model(session_key)
        # 定时触发用完整指令独立执行；手动续聊仍保留同一会话的历史。
        self.history = [] if fresh else session_store.load_recent(session_key)
        self.resume = None if fresh else session_store.get_sdk_session_id(session_key)
        self.prompt_extra = agents.prompt_extra_for_session(session_key)
        self.timeline = Timeline()
        self.draft = ""
        self.last_draft_ts = 0.0
        self.reply = None

    def alive(self) -> bool:
        return self.turn_id is None or session_store._conn().execute(
            "SELECT 1 FROM turns WHERE session_key=? AND id=?",
            (self.session_key, self.turn_id),
        ).fetchone() is not None

    def save_session(self, session_id: str | None) -> None:
        if not session_id or not self.alive():
            return
        self.resume = session_id
        session_store.set_sdk_session_id(
            self.session_key, session_id, expected_model=self.chosen_model,
        )

    def flush_draft(self) -> None:
        if self.turn_id is not None and self.draft:
            session_store.flush_draft(
                self.turn_id, danger.redact_secrets(self.draft), session_key=self.session_key,
            )

    def record_usage(self, reply) -> None:
        if not self.alive():
            return
        self.save_session(reply.sdk_session_id)
        if reply.context_tokens or reply.turn_tokens:
            session_store.record_usage(
                self.session_key, reply.context_tokens, reply.turn_tokens,
                window=reply.context_window, last_in=reply.input_fresh,
                last_cache=reply.cache_read, last_out=reply.output_tokens, model=reply.model,
            )

    async def send(self, method: str, *args) -> None:
        if self.sink is not None:
            await getattr(self.sink, method)(*args)

    async def observe(self, event) -> None:
        if isinstance(event, SessionStarted):
            self.save_session(event.session_id)
        elif isinstance(event, ContextUsage):
            if self.alive():
                session_store.update_context(self.session_key, event.tokens, event.window, event.model)
                await self.send("context_usage")
        elif isinstance(event, TextDelta):
            text = danger.redact_secrets(event.text)
            self.timeline.text(text)
            self.draft += text
            if time.monotonic() - self.last_draft_ts >= 0.7:
                self.flush_draft()
                self.last_draft_ts = time.monotonic()
            await self.send("text", text)
        elif isinstance(event, ThinkingDelta):
            await self.send("thinking", danger.redact_secrets(event.text))
        elif isinstance(event, (ToolStarted, ToolInput, ToolFinished)):
            await self.observe_tool(event)
        elif isinstance(event, Compacted):
            self.timeline.compacted(event.trigger)
            await self.send("compacted", event.trigger)
        elif isinstance(event, UpstreamRetry):
            await self.send("upstream_retry", event.status, event.attempt,
                            event.max_retries, event.delay_ms, event.reason)
        elif isinstance(event, Done):
            self.reply = event.reply
            self.reply.text = danger.redact_secrets(self.reply.text)
            self.save_session(self.reply.sdk_session_id)

    async def observe_tool(self, event) -> None:
        if isinstance(event, ToolStarted):
            args = (event.name, event.tool_id, event.parent_id)
            self.timeline.tool_started(*args)
            await self.send("tool_started", *args)
        elif isinstance(event, ToolInput):
            self.timeline.tool_input(event.tool_id, event.tool_input, event.parent_id)
            await self.send("tool_input", event.name, event.tool_id, event.tool_input, event.parent_id)
        else:
            args = (event.name, event.ok, event.preview, event.tool_id, event.detail, event.parent_id)
            self.timeline.tool_finished(*args)
            await self.send("tool_finished", *args)


async def stream_run(turn: SharedTurn, user_text: str, *, stream_fn, model=None,
                     compact: bool = False, **execution):
    """同一轮准备与预压缩；保留入口注入的 stream_fn 和各自执行限制。"""
    model = model or turn.chosen_model or config.MODEL
    resolved_model, _ = providers.resolve(model, config.MODEL)
    used = int(session_store.session_summary(turn.session_key).get("ctx_tokens", 0) or 0)
    options = dict(execution, model=model, session_key=turn.session_key,
                   system_prompt_extra=turn.prompt_extra)
    preflight_tokens = 0
    if (not compact and turn.resume
            and used >= _window_ratio(effective_context_window(model, resolved_model), 0.65)):
        compact_options = {k: v for k, v in options.items() if k not in ("images", "files")}
        async for event in stream_fn([], "", resume=turn.resume, compact_only=True, **compact_options):
            if isinstance(event, Done):
                turn.save_session(event.reply.sdk_session_id)
                turn.record_usage(event.reply)
                preflight_tokens = max(event.reply.stream_tokens, event.reply.turn_tokens)
                budget = options.get("token_budget", 0)
                if event.reply.is_error or event.reply.budget_exceeded or (budget and preflight_tokens >= budget):
                    event.reply.budget_exceeded = event.reply.budget_exceeded or bool(budget and preflight_tokens >= budget)
                    event.reply.is_error = True
                    yield event
                    return
            else:
                yield event
        if options.get("token_budget"):
            options["token_budget"] -= preflight_tokens
    # 两次调用的 callback 都是累计快照，第二阶段必须加上预压缩用量。
    callback = options.get("usage_callback")
    if callback is not None and preflight_tokens:
        options["usage_callback"] = lambda tokens: callback(preflight_tokens + tokens)
    async for event in stream_fn(turn.history, user_text, resume=turn.resume,
                                 compact_only=compact, **options):
        if isinstance(event, Done) and preflight_tokens:
            event.reply.stream_tokens = preflight_tokens + max(event.reply.stream_tokens, event.reply.turn_tokens)
        yield event
