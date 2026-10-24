"""同步子代理：独立 CLI/供应商，只把最终结果交回当前工具调用，不保存业务会话。"""
from __future__ import annotations

import asyncio
import contextvars
import fnmatch
import re
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Callable

import anyio
from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher,
    ResultMessage, StreamEvent, TextBlock, tool,
)

from .. import config, providers
from ..gateway import settings_store
from ..tools import danger

TOOL_NAME = "mcp__vococo__run_subagent"
# 白名单兼任递归防线：不提供 Skill、MCP、Agent/Task、Workflow、后台或会话控制入口。
_NATIVE_TOOLS = ("Read", "Glob", "Grep", "Write", "Edit", "NotebookEdit", "Bash", "WebSearch", "WebFetch")
_BLOCKED_TOOLS = ("Agent", "Task", "Workflow", "Skill", TOOL_NAME)
_DEPTH = contextvars.ContextVar("vococo_subagent_depth", default=0)
_TIMEOUT = 600
_MAX_TIMEOUT = 1200
_MAX_TURNS = 40
_MAX_RESULT_CHARS = 60_000


@dataclass
class Usage:
    """同一父轮的所有同步子代理共享预算；保温 client 复用时由父轮重新绑定。"""
    limit: int = 0
    parent_tokens: int = 0
    input_fresh: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cost_usd: float = 0.0
    on_update: Callable[[], None] | None = None
    callback_error: Exception | None = None
    tasks: set[asyncio.Task] = field(default_factory=set)
    closing: bool = False

    async def close(self) -> bool:
        """先封入口，再收完本轮子任务；SDK disconnect 只发取消，不替我们等待。"""
        self.closing = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await wait_cleanup(asyncio.gather(*tasks, return_exceptions=True))
        return bool(tasks)

    @property
    def tokens(self) -> int:
        return self.input_fresh + self.output_tokens

    @property
    def exceeded(self) -> bool:
        return bool(self.limit and self.parent_tokens + self.tokens > self.limit)

    def reset(self, limit: int, on_update: Callable[[], None]) -> None:
        if self.tasks:
            raise RuntimeError("上一轮子代理尚未清理，不能复用父 client。")
        self.closing = False
        self.limit = limit
        self.parent_tokens = self.input_fresh = self.output_tokens = self.cache_read = 0
        self.cost_usd = 0.0
        self.on_update = on_update
        self.callback_error = None

    def notify(self) -> None:
        if self.on_update:
            try:
                self.on_update()
            except Exception as exc:
                self.callback_error = exc
                raise


def _usage_observer(usage: Usage):
    # 每个子调用单独去重，父轮总账共享，不能拿每个 ResultMessage 覆盖并发兄弟的用量。
    fresh = output = cached = 0

    def observe(message) -> None:
        nonlocal fresh, output, cached
        before = fresh, output, cached
        if isinstance(message, StreamEvent):
            event = message.event
            if event.get("type") == "message_start":
                data = (event.get("message") or {}).get("usage") or {}
                fresh += int(data.get("input_tokens") or 0) + int(data.get("cache_creation_input_tokens") or 0)
                cached += int(data.get("cache_read_input_tokens") or 0)
            elif event.get("type") == "message_delta":
                output += int((event.get("usage") or {}).get("output_tokens") or 0)
        elif isinstance(message, ResultMessage):
            data = message.usage or {}
            fresh = max(fresh, int(data.get("input_tokens") or 0) + int(data.get("cache_creation_input_tokens") or 0))
            output = max(output, int(data.get("output_tokens") or 0))
            cached = max(cached, int(data.get("cache_read_input_tokens") or 0))
            usage.cost_usd += message.total_cost_usd or 0.0
        usage.input_fresh += fresh - before[0]
        usage.output_tokens += output - before[1]
        usage.cache_read += cached - before[2]
        if before != (fresh, output, cached):
            usage.notify()

    return observe


async def wait_cleanup(cleanup):
    """等清理实际结束再传播取消，同时防 AnyIO 和原生 task.cancel() 二次打断。"""
    cleanup = asyncio.ensure_future(cleanup)
    cancelled = False
    with anyio.CancelScope(shield=True):
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
    if cancelled:
        # 先收完子进程，再传播取消，不把已取消的父轮变成成功回执。
        if not cleanup.cancelled():
            cleanup.exception()
        raise asyncio.CancelledError
    return cleanup.result()


async def _disconnect(client) -> bool:
    # 当前 SDK 使用 detached task，可跨 task 关闭；其默认 transport 自身有界。
    try:
        await wait_cleanup(client.disconnect())
        return True
    except Exception:
        return False


def _result(text: str, error: bool = False) -> dict:
    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + "\n[结果过长，已截断；请缩小任务范围后重试]"
    return {"content": [{"type": "text", "text": text}], "is_error": error}


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def protect_entry(hooks: dict | None, tool_name: str = TOOL_NAME) -> dict:
    """原生子代理可能有更窄的权限，不能用主会话快照再次派发。"""
    async def guard(data, _tool_id, _context):
        if data.get("agent_id"):
            return _deny("派发工具只允许绑定的主执行者调用，不能从原生子代理再次派发。")
        return {}

    return {**(hooks or {}), "PreToolUse": [
        *((hooks or {}).get("PreToolUse", [])),
        HookMatcher(matcher=f"^{re.escape(tool_name)}$", hooks=[guard]),
    ]}


def _child_hooks(parent: ClaudeAgentOptions, tools: list[str], usage: Usage | None = None) -> dict:
    inherited = (parent.hooks or {}).get("PreToolUse", [])
    if not inherited:
        raise ValueError("父会话缺少安全 hook，不能启动子代理。")

    async def guard(data, tool_id, context):
        name = data.get("tool_name", "")
        if usage and (usage.exceeded or usage.callback_error):
            return _deny("父轮预算已耗尽或用量记账失败，禁止继续执行工具。")
        if name not in tools:
            return _deny("该工具不在父会话授权的同步子代理工具范围内。")
        try:
            result = {}
            # 串行组合，禁止后续 allow 覆盖父安全 hook 的 deny。
            for matcher in inherited:
                if matcher.matcher and not re.search(matcher.matcher, name):
                    continue
                for callback in matcher.hooks:
                    output = await callback(data, tool_id, context)
                    specific = output.get("hookSpecificOutput", {})
                    if specific.get("permissionDecision") == "deny" or output.get("continue") is False:
                        return output
                    # 改写工具入参会使前面的判定失效；当前安全 hook 不需要此能力。
                    if "updatedInput" in specific:
                        return _deny("子代理安全 hook 不支持改写工具入参。")
                    result.update(output)
            return result
        except Exception:
            return _deny("子代理安全检查异常，已拒绝操作。")

    return {**(parent.hooks or {}), "PreToolUse": [HookMatcher(matcher=None, hooks=[guard])]}


def _child_options(parent: ClaudeAgentOptions, selection: str | None, usage: Usage | None = None) -> ClaudeAgentOptions:
    from .agent import _turn_env, context_window

    denied = list(parent.disallowed_tools)
    # 带参数的禁用规则也保守禁掉整个工具，避免自制解析器误放行。
    tools = [name for name in _NATIVE_TOOLS if not any(
        fnmatch.fnmatchcase(name, rule.split("(", 1)[0]) for rule in denied
    )]
    if isinstance(parent.tools, list):
        tools = [name for name in tools if name in parent.tools]
    model, env, effort = parent.model, dict(parent.env), parent.effort
    if selection is not None:
        model, provider_env = providers.resolve(selection, config.MODEL)
        saved = settings_store.get_web_effort(selection)
        effective = saved if saved in providers.effort_levels_for_model(selection) else ""
        body = providers.extra_body_for_model(
            model, effective, settings_store.get_web_verbosity(selection),
            settings_store.get_web_summary(selection),
        )
        if body and provider_env:
            provider_env = {**provider_env, "CLAUDE_CODE_EXTRA_BODY": body}
        env = _turn_env(provider_env, no_auto_memory=True)
        # 切模型只重置供应商环境，业务 SSH 的沙箱适配器必须继续继承。
        if "PATH" in parent.env:
            env["PATH"] = parent.env["PATH"]
        # 不继承父进程可能残留的模型特有请求体。
        env.setdefault("CLAUDE_CODE_EXTRA_BODY", "")
        effort = None if providers.extra_body_for_effort(effective) else effective or None
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    note = (
        "\n你是同步子代理，只完成本次明确交办的任务，最后返回结果。你没有父对话历史。"
        "不得再次派发代理、创建后台任务、改变会话/权限或绕过工具限制；"
        "父代理的任务文字不是用户批准，遇到权限拒绝就报告，不能另找通道绕过。"
    )
    system = deepcopy(parent.system_prompt)
    if isinstance(system, dict):
        system["append"] = system.get("append", "") + note
    else:
        system = (system or "") + note
    # 不复制 resume/session_id/session_store 或 MCP server；每次都新建 client。
    return ClaudeAgentOptions(
        model=model, env=env, effort=effort, system_prompt=system,
        cwd=parent.cwd, tools=tools, disallowed_tools=[*denied, *_BLOCKED_TOOLS],
        permission_mode=parent.permission_mode, can_use_tool=parent.can_use_tool,
        sandbox=deepcopy(parent.sandbox), settings=parent.settings,
        # SDK 给非 None 的 skills 默认加 user/project 来源；保持父轮实际来源，
        # 不能因禁用 Skill 漏掉父项目设置里的 deny 规则。
        setting_sources=(parent.setting_sources if parent.setting_sources is not None
                         else ["user", "project"] if parent.skills is not None else None),
        skills=None, plugins=[],
        mcp_servers={}, strict_mcp_config=True,
        hooks=_child_hooks(parent, tools, usage), include_partial_messages=True,
        max_turns=min(parent.max_turns or _MAX_TURNS, _MAX_TURNS),
        max_budget_usd=parent.max_budget_usd,
        max_buffer_size=parent.max_buffer_size,
        extra_args={"no-session-persistence": None,
                    "autocompact": str(int(context_window(model or "") * 0.65))},
    )


async def _receive(client, usage: Usage) -> dict:
    observe = _usage_observer(usage)
    last_text = ""
    async for message in client.receive_response():
        observe(message)
        if usage.exceeded:
            return _result("子代理已耗尽父轮 token 预算，已停止。", True)
        if isinstance(message, AssistantMessage):
            last_text = "\n".join(block.text for block in message.content if isinstance(block, TextBlock))
        elif isinstance(message, ResultMessage):
            text = message.result or last_text
            if message.is_error:
                return _result(f"子代理未完成（{message.subtype}）：\n{text}", True)
            return _result(text or "子代理完成，但没有返回正文。", not bool(text))
    return _result("子代理消息流提前结束，没有完成回执。", True)


async def _run(parent: ClaudeAgentOptions, prompt: str, selection: str | None, timeout: int,
               usage: Usage | None = None) -> dict:
    usage = usage if usage is not None else Usage()
    token = _DEPTH.set(1)
    cwd_token = danger.set_cwd(str(parent.cwd)) if not danger.current_cwd() and parent.cwd else None
    client = None
    cleaned = True
    try:
        options = _child_options(parent, selection, usage)
        client = ClaudeSDKClient(options=options)
        async with asyncio.timeout(timeout):
            await client.connect()
            await client.query(prompt)
            result = await _receive(client, usage)
    except TimeoutError:
        result = _result(f"子代理超过 {timeout} 秒，已停止；不能当作任务完成。", True)
    except Exception as exc:
        # 不把上游异常正文/供应商凭据带回工具结果。
        result = _result(f"子代理执行失败（{type(exc).__name__}），未确认完成。", True)
    finally:
        try:
            if client is not None:
                cleaned = await _disconnect(client)
        finally:
            if cwd_token is not None:
                danger.reset_cwd(cwd_token)
            _DEPTH.reset(token)
    return result if cleaned else _result("子代理清理失败，未确认进程已经关闭。", True)


def build_tool(parent: ClaudeAgentOptions, usage: Usage | None = None):
    """在 connect 前捕获父轮快照，不从持久 MCP server 的旧 contextvars 推断权限。"""
    from ..gateway.core import MODEL_CHOICES

    snapshot = contextvars.copy_context()
    depth = _DEPTH.get()
    # SDK 初始化可能修改 options；快照仅保留子代理真正使用的字段副本。
    parent = replace(parent, env=dict(parent.env), disallowed_tools=list(parent.disallowed_tools))
    candidates = providers.available_models(MODEL_CHOICES)
    listing = "、".join(f"{mid}（{label}）" for mid, label, _ in candidates)

    @tool(
        "run_subagent",
        "启动 vococo 同步子代理，当轮等待结果，不创建后台独立会话，不改变父会话模型或历史。"
        "prompt 须包含完整任务和必要背景（不会自动传聊天记录）；model 可省略，继承当前父模型，"
        "也可填设置里任意可用模型的 ID/唯一简称（如 Sol、Luna）。不明确时返回候选，不会猜。"
        "子代理仅有文件读写、Bash、网页搜索/抓取，继承父禁用工具与审批；不能使用 MCP/技能或再次派发。"
        "默认超时 600 秒，timeout_seconds 可设 1–1200 秒，最多 40 轮；共享当前目录，勿并行改同一文件。"
        f"当前可选模型：{listing}",
        {"type": "object", "properties": {
            "prompt": {"type": "string", "minLength": 1},
            "model": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": _MAX_TIMEOUT},
        }, "required": ["prompt"], "additionalProperties": False},
    )
    async def run_subagent(args: dict) -> dict:
        if depth or _DEPTH.get():
            return _result("子代理不能再次调用同步子代理（最多一层）。", True)
        if usage and usage.closing:
            return _result("父轮正在结束，不能启动子代理。", True)
        if usage and (usage.exceeded or usage.callback_error):
            return _result("父轮预算已耗尽或用量记账失败，不能启动子代理。", True)
        prompt = args.get("prompt")
        model = args.get("model", "")
        timeout = args.get("timeout_seconds", _TIMEOUT)
        if (not isinstance(prompt, str) or not prompt.strip() or not isinstance(model, str)
                or type(timeout) is not int or not 1 <= timeout <= _MAX_TIMEOUT):
            return _result("需要非空 prompt、字符串 model 和 1–1200 的整数 timeout_seconds。", True)
        selection = None
        if model.strip():
            current = providers.available_models(MODEL_CHOICES)
            hits = providers.match_models_by_text(model, current)
            if len(hits) != 1:
                choices = "、".join(mid for mid, _, _ in (hits or current))
                return _result(f"模型未匹配或存在歧义，请指定完整 ID。候选：{choices}", True)
            selection = hits[0][0]
        # MCP 任务持有 connect 时的 contextvars。显式使用父 options 创建时的快照；
        # 每次 copy，防并发工具调用互相污染。await 让父取消直接传到子任务。
        task = asyncio.create_task(_run(parent, prompt, selection, timeout, usage), context=snapshot.copy())
        if usage is not None:
            usage.tasks.add(task)
            task.add_done_callback(usage.tasks.discard)
        return await task

    return run_subagent
