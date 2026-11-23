"""把当前网页轮次允许的内置 MCP 工具接到 AGY，不借用公网派单凭据。"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from functools import lru_cache
from types import SimpleNamespace
from urllib.parse import urlsplit

from aiohttp import web
from jsonschema import Draft202012Validator

from .. import config
from . import clarify

log = logging.getLogger(__name__)
PATH = "/internal/agy-tools"
NAME = "vococo-web"
MAX_BODY = 128 * 1024
MAX_RESULT = 32_000
_HANDSHAKE_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")

_READ = (
    "search_library", "get_library_item", "list_workbench_projects", "list_workbench_tasks", "list_cron_jobs",
)
# 这组写工具本身没有统一的强制审批；其余写工具直接复用各自的审批和内容校验。
_APPROVE = (
    "generate_image", "collect_library_item", "rename_workbench_project", "move_workbench_task",
    "update_workbench_task", "delete_workbench_task",
)
_OTHER = (
    "send_image", "send_file", "send_video", "show_html", "prepare_repository",
    "create_workbench_project", "create_workbench_task", "archive_workbench_project",
    "add_cron_job", "update_cron_job", "delete_cron_job", "set_cron_job_enabled",
)


@lru_cache(maxsize=1)
def _registry() -> dict:
    from ..tools import builtin
    from ..tools.lark_documents import lark_document

    return {tool.name: tool for tool in [*(getattr(builtin, name) for name in (*_READ, *_APPROVE, *_OTHER)), lark_document]}


def _matches_denied(names, denied) -> bool:
    return any(fnmatchcase(name.casefold(), str(rule).split("(", 1)[0].casefold())
               for name in names for rule in denied)


def _denied(name: str, denied: set[str]) -> bool:
    return _matches_denied((name, f"mcp__vococo__{name}", f"mcp__{NAME}__{name}",
                            "mcp__vococo", f"mcp__{NAME}"), denied)


def external_servers(names, denied) -> list[dict]:
    """只映射已启用的无凭据本机 HTTP MCP。远端凭据/stdio 不交给 AGY。

    ACP 无法原样继承普通通道的每个外部工具权限；有禁用规则时整组不挂载。
    """
    from .settings_store import effective_external_mcp

    servers = []
    for name, spec in effective_external_mcp(set(names)).items():
        prefix = f"mcp__{name.casefold()}"
        if (name.casefold().startswith("vococo-") or any(str(rule).casefold().startswith(prefix) for rule in denied)
                or any(str(rule).casefold().startswith("mcp__") and any(ch in str(rule) for ch in "*?[")
                       for rule in denied)):
            continue
        if spec.get("type") != "http" or set(spec) != {"type", "url"}:
            continue
        url = spec["url"]
        try:
            parts = urlsplit(url)
            if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or not parts.port
                    or parts.port == config.WEB_PORT or not parts.path or parts.query or parts.fragment
                    or parts.username is not None or parts.password is not None):
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        servers.append({"type": "http", "name": name, "url": url, "headers": []})
    return servers


@dataclass
class _Binding:
    context: contextvars.Context
    tools: dict
    owner: asyncio.Task
    server_name: str
    tasks: set[asyncio.Task] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: bool = True
    writes: set[str] = field(default_factory=set)


_bindings: dict[str, _Binding] = {}


@asynccontextmanager
async def mount(session_key: str, turn_id: int, workspace: str, *, external=None):
    from ..memory import agents
    from ..tools import danger
    from .settings_store import vococo_enabled

    current = clarify.current()
    if (not current or current.session_key != session_key or current.turn_id != turn_id
            or getattr(current.adapter, "platform", None) != "web"):
        yield None
        return
    denied = set(agents.runtime_for_session(session_key).get("disallowed_tools") or ())
    server_name = external["name"] if external else NAME
    if external:
        tools = await _external_tools(external["url"])
    elif vococo_enabled():
        tools = {name: tool for name, tool in _registry().items() if not _denied(name, denied)}
    else:
        tools = {}
    if not tools:
        yield None
        return
    token = secrets.token_urlsafe(32)
    cwd = danger.set_cwd(workspace, project_root=config.project_root_for(session_key))
    try:
        binding = _Binding(contextvars.copy_context(), tools, asyncio.current_task(), server_name)
    finally:
        danger.reset_cwd(cwd)
    _bindings[token] = binding
    try:
        yield {"type": "http", "name": server_name, "url": f"http://127.0.0.1:{config.WEB_PORT}{PATH}",
               "headers": [{"name": "Authorization", "value": f"Bearer {token}"}]}
    finally:
        binding.active = False
        _bindings.pop(token, None)
        running = list(binding.tasks)
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)


@asynccontextmanager
async def _external_client(url):
    from httpx2 import AsyncClient
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with AsyncClient(trust_env=False, timeout=10) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, *_):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def _external_handler(url, name):
    async def invoke(args):
        async with asyncio.timeout(45), _external_client(url) as session:
            result = await session.call_tool(name, args)
            return result.model_dump(by_alias=True)
    return invoke


async def _external_tools(url) -> dict:
    from ..tools.danger import redact_secrets
    from .agy_trial import _APPROVAL_SECRET, _AUTH_HEADER

    try:
        async with asyncio.timeout(6), _external_client(url) as session:
            result = await session.list_tools()
    except Exception:
        return {}  # 本机可选服务失败，不把未过滤的上游错误带进会话。
    specs = {}
    for tool in result.tools[:80]:
        wire = tool.model_dump_json(by_alias=True)
        if len(wire) > 16000 or redact_secrets(wire) != wire or _APPROVAL_SECRET.search(wire) or _AUTH_HEADER.search(wire):
            continue
        specs[tool.name] = SimpleNamespace(name=tool.name, description=tool.description or "",
                                          input_schema=tool.input_schema, handler=_external_handler(url, tool.name))
    return specs


def _active(binding: _Binding) -> bool:
    return binding.active and not binding.owner.done() and not binding.owner.cancelling()


def _fingerprint(name, args):
    if name == "create_workbench_task":
        args = {k: v.strip() if isinstance(v, str) else v for k, v in args.items()}
        args = {k: v for k, v in args.items() if v not in (None, "")}
        if args.get("assignee") == "human":
            args.pop("assignee")
    plain = json.dumps(args, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(f"{name}\0{plain}".encode()).hexdigest()


def _result(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


async def _confirm_external(server_name: str, name: str, detail: str) -> bool:
    # 外部 MCP 不能因为选了完全访问或伪造只读标记而跳过确认。
    from .agy_trial import _fence
    from .core import Choice

    ctx = clarify.current()
    if ctx is None or not callable(getattr(ctx.adapter, "present_choice", None)):
        return False
    prompt = f"Antigravity 请求调用外部工具 {server_name}/{name}：\n\n{_fence(detail)}"
    pending = clarify.register(ctx.session_key, ["允许一次", "拒绝"], notice={
        "kind": "approval", "prompt": prompt, "reason": "Antigravity 外部工具", "detail": detail[:2000], "tool": name,
    })
    try:
        await ctx.adapter.present_choice(ctx.chat_id, Choice(prompt=prompt, options=[
            (f"/clarify {pending.clarify_id} 0", "允许一次"), (f"/clarify {pending.clarify_id} 1", "拒绝"),
        ]))
        return await clarify.wait(pending.clarify_id, min(75, config.CLARIFY_TIMEOUT)) == "允许一次"
    finally:
        clarify.abandon(pending.clarify_id)


async def _invoke(binding: _Binding, name: str, args: dict) -> dict:
    from ..tools import danger
    from .agy_trial import _APPROVAL_SECRET, _AUTH_HEADER
    from .settings_store import vococo_enabled

    internal = binding.server_name == NAME
    tool = binding.tools[name]
    schema = {**tool.input_schema, "additionalProperties": False}
    if not Draft202012Validator(schema).is_valid(args):
        return _result("参数不符合工具定义。", error=True)
    detail = json.dumps(args, ensure_ascii=False, sort_keys=True)
    if (danger.redact_secrets(detail) != detail or _APPROVAL_SECRET.search(detail)
            or _AUTH_HEADER.search(detail) or any(token in detail for token in _bindings)):
        return _result("疑似包含凭据，操作未执行。", error=True)
    async with binding.lock:
        if not _active(binding) or (internal and not vococo_enabled()):
            return _result("本轮已结束或工具已关闭，操作未执行。", error=True)
        read_only = internal and (name in _READ or (name == "lark_document" and args.get("operation") == "fetch"))
        if not read_only:
            fingerprint = _fingerprint(name, args)
            if fingerprint in binding.writes:
                return _result("本轮已请求相同操作，请先核对结果，不要重复提交。", error=True)
            if len(binding.writes) >= 32:
                return _result("本轮写操作已到上限。", error=True)
            binding.writes.add(fingerprint)
        guard = await danger.pretool_guard_hook(
            {"tool_name": f"mcp__{'vococo' if internal else binding.server_name}__{name}", "tool_input": args}, "", None)
        if guard.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
            return _result("vococo 安全规则或审批阻止了操作，没有执行。", error=True)
        if not internal or name in _APPROVE:
            if len(detail) > 6000:
                return _result("审批参数太长，请拆分后重试。", error=True)
            confirmed = (await danger.require_approval(f"Antigravity 请求 {name}", detail) if internal
                         else await _confirm_external(binding.server_name, name, detail))
            if not confirmed:
                return _result("未批准，本次操作未执行。", error=True)
        if not _active(binding):
            return _result("本轮已结束，操作未执行。", error=True)
        result = await tool.handler(args)
    blocks = result.get("content", ()) if isinstance(result, dict) else ()
    raw = "\n".join(block["text"] for block in blocks if isinstance(block, dict)
                    and block.get("type") == "text" and isinstance(block.get("text"), str))
    if not raw:
        return _result("工具未返回文字结果。", error=True)
    text = danger.redact_secrets(raw)
    for token in _bindings:
        text = text.replace(token, "[凭据已隐藏]")
    return _result(text[:MAX_RESULT] + ("\n…结果已截断" if len(text) > MAX_RESULT else ""),
                   error=bool(result.get("isError") or result.get("is_error")))


async def _call(binding: _Binding, params) -> dict:
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _result("工具或参数无效。", error=True)
    name, args = params["name"], params.get("arguments", {})
    if not _active(binding) or name not in binding.tools or not isinstance(args, dict):
        return _result("工具不可用或参数无效。", error=True)
    task = asyncio.create_task(_invoke(binding, name, args), context=binding.context.copy())
    binding.tasks.add(task)
    try:
        return await task
    except Exception:
        log.warning("AGY 内置工具执行失败: %s", name)
        return _result("工具执行失败，请核对状态，勿重复提交有副作用的操作。", error=True)
    finally:
        binding.tasks.discard(task)


async def handle(request: web.Request) -> web.Response:
    """MCP Streamable HTTP 的无状态 JSON 子集；不提供远程访问或服务端事件流。"""
    peer = request.transport and request.transport.get_extra_info("peername")
    if not peer or peer[0] != "127.0.0.1" or request.host.split(":", 1)[0] != "127.0.0.1":
        raise web.HTTPNotFound()
    auth = request.headers.getall("Authorization", ())
    binding = _bindings.get(auth[0][7:]) if len(auth) == 1 and auth[0].startswith("Bearer ") else None
    if not binding or not _active(binding):
        raise web.HTTPUnauthorized(headers={"Cache-Control": "no-store"})
    if request.method != "POST":
        raise web.HTTPMethodNotAllowed(request.method, ["POST"])
    if request.headers.get("Content-Encoding", "identity") != "identity":
        raise web.HTTPUnsupportedMediaType()
    if request.content_length is None or request.content_length > MAX_BODY:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY, actual_size=request.content_length or 0)
    try:
        message = await request.json()
    except (ValueError, UnicodeError):
        raise web.HTTPBadRequest() from None
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise web.HTTPBadRequest()
    method, params = message.get("method"), message.get("params", {})
    if "id" not in message:
        return web.Response(status=202, headers={"Cache-Control": "no-store"})
    request_id = message["id"]
    if type(request_id) not in (str, int):
        raise web.HTTPBadRequest()
    envelope = {"jsonrpc": "2.0", "id": request_id}
    if method == "initialize" and isinstance(params, dict):
        version = params.get("protocolVersion")
        protocol = version if version in _HANDSHAKE_VERSIONS else _HANDSHAKE_VERSIONS[-1]
        envelope["result"] = {"protocolVersion": protocol, "capabilities": {"tools": {}},
                              "serverInfo": {"name": binding.server_name, "version": "1"}}
    elif method == "tools/list":
        envelope["result"] = {"tools": [{"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
                                        for tool in binding.tools.values()]}
    elif method == "tools/call" and isinstance(params, dict):
        envelope["result"] = await _call(binding, params)
    elif method == "ping":
        envelope["result"] = {}
    else:
        envelope["error"] = {"code": -32601, "message": "Method not found"}
    return web.json_response(envelope, headers={"Cache-Control": "no-store"})
