"""仅供 Web 手动文字轮次使用的 Antigravity 试用通道。"""

import asyncio
import json
import logging
import os
import re
import signal
import stat
import uuid
from contextlib import aclosing, suppress
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, time

import anyio

log = logging.getLogger(__name__)

CHOICES = {
    "agy-trial:gemini-3.8-flash-low": "Gemini 3.8 Flash Low",
    "agy-trial:gemini-3.8-flash-medium": "Gemini 3.8 Flash Medium",
    "agy-trial:gemini-3.8-flash-high": "Gemini 3.8 Flash High",
    "agy-trial:claude-opus-4-6-thinking": "Claude Opus 4.6 Thinking",
}
MODELS = tuple(key.split(":", 1)[1] for key in CHOICES)
MAX_PROMPT = 8000
DEFAULT_MODEL = MODELS[1]
CLASH = "http://127.0.0.1:7890"
MAX_OUTPUT = 1024 * 1024
SETTINGS = json.dumps({
    "toolPermission": "strict",
    "permissions": {"deny": [f"{action}(*)" for action in (
        "read_file", "write_file", "read_url", "execute_url", "command", "mcp",
    )]},
})
# ACP 会话保留只读交接 MCP；文件、命令和联网仍然直接拒绝。
SETTINGS_ACP = json.dumps({
    "allowNonWorkspaceAccess": False,
    "toolPermission": "strict",
    "permissions": {"deny": [f"{action}(*)" for action in (
        "read_file", "write_file", "edit_file", "read_url", "execute_url", "command", "search_web",
    )]},
})


class UpstreamError(Exception):
    def __init__(self, code, status=502):
        super().__init__(code)
        self.code = code
        self.status = status


def child_env():
    # 保留 CLI 登录所需的 HOME，但不把 vococo 密钥和环境配置传给子进程。
    env = {key: os.environ[key] for key in ("HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "LANG")
           if key in os.environ}
    env.update(HTTP_PROXY=CLASH, HTTPS_PROXY=CLASH, NO_PROXY="localhost,127.0.0.1,::1")
    return env


async def drain_and_wait(proc):
    # 大量剩余输出可能把 asyncio 的管道读缓冲顶满；只 wait() 会卡在关闭管道。
    while await proc.stdout.read(65536):
        pass
    await proc.wait()


async def terminate(proc):
    # 主进程退出后，仍可能有同组子进程持有 stdout，不能只看 returncode。
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)
        try:
            await asyncio.wait_for(drain_and_wait(proc), 2)
        except TimeoutError:
            continue
        if sig == signal.SIGTERM:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        break


async def start_cli(command, model, cwd, *, resume=None):
    args = (
        "--model", model, "--input-format", "stream-json", "--output-format", "stream-json",
        "--mode", "plan", "--sandbox", "--disable-slash-commands", "--settings", SETTINGS,
    )
    if resume:
        args += ("--conversation", resume)
    return await spawn_cli((*command, *args), cwd, asyncio.subprocess.PIPE)


async def spawn_cli(args, cwd, stdin):
    # 取消恰好发生在启动过程中时，也要拿到子进程句柄后再回收。
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=child_env(), start_new_session=True,
        stdin=stdin, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=MAX_OUTPUT,
    ))
    try:
        return await asyncio.shield(spawn)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True), suppress(OSError):
            await terminate(await spawn)
        raise


async def read_events(pipe, first=None):
    total = 0
    if first is not None:
        total = len(first)
        if total > MAX_OUTPUT:
            raise UpstreamError("agy_output_limit")
        yield json.loads(first)
    while line := await pipe.readline():
        total += len(line)
        if total > MAX_OUTPUT:
            raise UpstreamError("agy_output_limit")
        event = json.loads(line)
        if not isinstance(event, dict):
            raise UpstreamError("agy_protocol_error")
        yield event


def _check_init(event, model, cwd, resume=None, native=False):
    if not isinstance(event, dict) or event.get("event") != "init":
        raise UpstreamError("agy_missing_init")
    init = event.get("init", {})
    session_id = event.get("conversation_id")
    if native and (not _valid_native_id(session_id) or (resume and resume != session_id)):
        raise UpstreamError("agy_session_mismatch")
    if init.get("model") != model:
        raise UpstreamError("agy_model_mismatch")
    actual = init.get("cwd")
    if not isinstance(actual, str) or Path(actual).resolve() != Path(cwd).resolve():
        raise UpstreamError("agy_workspace_mismatch")
    return session_id


async def read_answer(proc, model, cwd, *, resume=None, native=False, first=None):
    seen_model, final, chunks, session_id = None, None, [], None
    async for event in read_events(proc.stdout, first):
        kind = event.get("event")
        if native and kind == "step_update" and event.get("step_update", {}).get("step_type") in ("tool", "subagent"):
            raise UpstreamError("agy_unexpected_tool")
        if kind == "init":
            if native and seen_model is not None:
                raise UpstreamError("agy_session_mismatch")
            session_id = _check_init(event, model, cwd, resume, native)
            seen_model = model
        elif kind == "step_update" and seen_model == model and final is None:
            step = event.get("step_update", {})
            if step.get("step_type") == "agent_response" and isinstance(text := step.get("text_delta"), str) and text:
                chunks.append(text)
                yield "delta", {"text": text}
        elif kind == "result":
            result = event.get("result", {})
            if native and result.get("denied_actions"):
                raise UpstreamError("agy_unexpected_tool")
            if result.get("status") != "SUCCESS":
                raise UpstreamError("agy_request_failed")
            final = result.get("response")
    if await proc.wait() != 0 or seen_model != model or not isinstance(final, str):
        raise UpstreamError("agy_no_result")
    streamed = "".join(chunks)
    if not final.startswith(streamed):
        raise UpstreamError("agy_stream_mismatch")
    if tail := final[len(streamed):]:
        yield "delta", {"text": tail}
    if native and not final.strip():
        raise UpstreamError("agy_no_result")
    yield "done", {"model": model, "session_id": session_id} if native else {"model": model}


async def generate(command, cwd, timeout, model, prompt, *, resume=None, native=False):
    proc = None
    complete = False
    try:
        async with asyncio.timeout(timeout):
            proc = await start_cli(command, model, cwd, resume=resume) if resume else await start_cli(command, model, cwd)
            first = None
            if native:
                first = await asyncio.wait_for(proc.stdout.readline(), 18)
                _check_init(json.loads(first), model, cwd, resume, native=True)
            msg = {"event": "user", "message": {"content": prompt}}
            proc.stdin.write((json.dumps(msg) + "\n").encode())
            await proc.stdin.drain()
            proc.stdin.close()
            async for event in read_answer(proc, model, cwd, resume=resume, native=native, first=first):
                if event[0] == "done":
                    complete = True
                yield event
    except TimeoutError as exc:
        raise UpstreamError("agy_timeout", 504) from exc
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise UpstreamError("agy_input_failed") from exc
    except (ValueError, AttributeError) as exc:
        raise UpstreamError("agy_protocol_error") from exc
    except OSError as exc:
        raise UpstreamError("agy_unavailable") from exc
    finally:
        if proc is not None and not complete:
            with anyio.CancelScope(shield=True):
                await terminate(proc)


_quota_cache: tuple[float, dict, int] | None = None
_quota_refresh: asyncio.Task | None = None
_active_trials: set[asyncio.Task] = set()
_closing = False


async def close_all():
    global _closing
    _closing = True
    try:
        tasks = {task for task in (*_active_trials, _quota_refresh) if task is not None and not task.done()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        _closing = False


def _quota_limit(bucket):
    if not isinstance(bucket, dict) or bucket.get("disabled") is True:
        return None
    fraction = bucket.get("remaining_fraction")
    if type(fraction) not in (int, float) or not 0 <= fraction <= 1:
        return None
    reset = bucket.get("reset_time")
    try:
        when = datetime.fromisoformat(reset) if isinstance(reset, str) else None
        resets_at = int(when.timestamp()) if when and when.tzinfo else None
    except (ValueError, OverflowError, OSError):
        resets_at = None
    window = bucket.get("window")
    label = bucket.get("name")
    if window == "weekly":
        label = "每周"
    elif window in ("five_hour", "five-hour", "5h"):
        label = "5 小时"
    return {"label": label[:80] if isinstance(label, str) else "额度",
            "remaining_fraction": fraction, "utilization": 1 - fraction, "resets_at": resets_at}


def _parse_quota(report):
    # 只信 CLI 命令的结构化额度，不解析模型正文或传回原始 CLI 输出。
    if report.get("status") != "SUCCESS" or report["command"]["name"] != "usage":
        raise ValueError("quota command failed")
    groups = []
    for group in report["command"]["data"]["groups"]:
        limits = [limit for bucket in group["buckets"] if (limit := _quota_limit(bucket)) is not None]
        name = group["name"]
        if isinstance(name, str) and limits:
            groups.append({"name": name[:80], "limits": limits})
    if not groups:
        raise ValueError("quota unavailable")
    primary = max((limit for group in groups for limit in group["limits"]), key=lambda limit: limit["utilization"])
    return {"provider": "agy", "groups": groups, "primary_limit": primary}


async def _read_quota():
    proc = None
    complete = False
    with TemporaryDirectory(prefix="vococo-agy-quota-") as cwd:
        try:
            async with asyncio.timeout(20):
                proc = await spawn_cli((str(cli_binary()), "--sandbox", "--mode", "plan",
                                        "--settings", SETTINGS, "--print", "/usage", "--output-format", "json"),
                                       cwd, asyncio.subprocess.DEVNULL)
                line = await proc.stdout.readline()
                if await proc.stdout.read(1) or await proc.wait() != 0:
                    raise ValueError("invalid quota response")
                complete = True
                return _parse_quota(json.loads(line))
        finally:
            if proc is not None and not complete:
                with anyio.CancelScope(shield=True):
                    await terminate(proc)


async def _refresh_quota():
    global _quota_cache
    try:
        data, status = await _read_quota(), 200
    except (ValueError, TypeError, KeyError, AttributeError, OSError, TimeoutError):
        data, status = {"error": "Antigravity 额度暂时不可用"}, 502
    ttl = 10
    if status == 200:
        resets = [limit["resets_at"] for group in data["groups"] for limit in group["limits"] if limit["resets_at"]]
        ttl = min(60, max(1, min(resets) - time())) if resets else 60
    _quota_cache = (monotonic() + ttl, data, status)
    return data, status


async def quota_usage(model):
    global _quota_refresh
    if model == "agy-handoff":
        if not _quota_cli_ready():
            return {"error": "Antigravity CLI 额度暂不可查"}, 503
    elif model not in CHOICES or not enabled():
        return {"error": "Antigravity 试用未启用或已结束"}, 404
    if _quota_cache is not None and _quota_cache[0] > monotonic():
        return _quota_cache[1:]
    # 合并额度刷新，不限制聊天请求并发。
    if _quota_refresh is None or _quota_refresh.done():
        _quota_refresh = asyncio.create_task(_refresh_quota())
    return await asyncio.shield(_quota_refresh)


NATIVE_PREFIX = "agy-native:"
# 本机 vococo MCP 前端（brain_mcp）：把会话历史/任务工具同时给网页 Antigravity 使用。
VOCOCO_MCP_URL = "http://127.0.0.1:8852/mcp/token"
VOCOCO_MCP_TOKEN_FILE = Path.home() / ".local/share/vococo-mcp/static-token.txt"
VOCOCO_MCP_READ = ("vococo_list_agents", "vococo_list_sessions", "vococo_search_history",
                   "vococo_read_session", "vococo_get_task")
VOCOCO_MCP_WRITE = ("vococo_create_task", "vococo_continue_task", "vococo_cancel_task")
_vococo_mcp_cache: tuple[float, dict | None] = (0.0, None)
_ACP_NOTE = (
    "本会话由 vococo 网页代理，可用只读 MCP 读取交接上下文、记忆索引和 Skill；"
    "如挂载了 `vococo-local` MCP，可查历史会话和任务；用户问过去聊过什么时，"
    "用 `vococo_search_history`/`vococo_read_session` 查，不要凭空回忆；"
    "派发、续接、取消任务会先请主人在网页确认，被拒绝就如实说明。"
    "不能直接读写文件、不能联网、不能执行命令。"
    "Skill 每轮可能更新：本轮要遵循任何 Skill 时，必须重新调用 read_skill 取最新正文，"
    "不要沿用历史对话里的技能内容。"
    "如果用户的任务必须联网或使用这些工具，请直接用文字说明当前模式无法完成，不要尝试调用。"
    "\n\n用户消息：\n"
)
_catalog: tuple[float, dict[str, str]] = (0, {})
_catalog_lock = asyncio.Lock()
_native_pending: set[str] = set()


def is_native_model(model: str | None) -> bool:
    return bool(model and model.startswith(NATIVE_PREFIX))


def is_trial_model(model: str | None) -> bool:
    return bool(model and (model.startswith("agy-trial:") or is_native_model(model)))


def _valid_native_id(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


async def native_catalog() -> dict[str, str]:
    global _catalog
    if not enabled():
        return {}
    async with _catalog_lock:
        if _catalog[0] > monotonic():
            return _catalog[1]
        proc = None
        previous = _catalog[1]
        try:
            async with asyncio.timeout(9):
                with TemporaryDirectory(prefix="vococo-agy-catalog-") as cwd:
                    proc = await spawn_cli((str(cli_binary()), "models"), cwd, asyncio.subprocess.DEVNULL)
                    try:
                        await proc.stdout.readexactly(65537)
                    except asyncio.IncompleteReadError as exc:
                        raw = exc.partial
                    else:
                        raise ValueError("catalog too large")
                    if await proc.wait() != 0:
                        raise ValueError("invalid catalog")
            choices = {}
            for line in raw.decode().splitlines():
                model, sep, label = line.partition("\t")
                if sep and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", model) and label.strip():
                    choices[NATIVE_PREFIX + model] = label.strip()[:100]
        except (OSError, TimeoutError, ValueError):
            # 代理/网络抖动时保留上一次成功清单，避免已锁定的会话模型组整块消失。
            choices = previous
        finally:
            if proc is not None and proc.returncode is None:
                with anyio.CancelScope(shield=True):
                    await terminate(proc)
        _catalog = (monotonic() + (90 if choices else 8), choices)
        return choices


def native_pending(key: str) -> bool:
    return key in _native_pending


def cli_binary() -> Path:
    return Path.home() / ".local/bin/agy"


def cli_config_is_clean() -> bool:
    # 临时 cwd 避开项目配置；保留登录 HOME 时，发现全局扩展就不运行。
    root = Path.home() / ".gemini"
    shared = root / "config"
    try:
        paths = [shared / name for name in ("hooks.json", "plugins", "rules", "skills", "agents", "workflows")]
        paths.extend(root / name for name in ("GEMINI.md", "AGENTS.md"))
        if any(os.path.lexists(path) for path in paths):
            return False
        mcp = shared / "mcp_config.json"
        if not os.path.lexists(mcp):
            return True
        if mcp.is_symlink() or not mcp.is_file() or mcp.stat().st_size > 65536:
            return False
        text = mcp.read_text().strip()
        return not text or json.loads(text) in ({}, {"mcpServers": {}})
    except (OSError, ValueError):
        return False


def _quota_cli_ready() -> bool:
    from .. import config

    return bool(not _closing and config.WEB_AUTH_TOKEN and os.access(cli_binary(), os.X_OK)
                and cli_config_is_clean())


def enabled() -> bool:
    # 网页 Antigravity 已不再设试用截止日；只保留 CLI 可用性与全局配置检查。
    # VOCOCO_AGY_TRIAL_UNTIL 仅作历史配置保留，不再参与判定。
    return _quota_cli_ready()


def accepts_session(session_key: str) -> bool:
    from ..core import tasks
    from ..cron import scheduler

    return tasks.task_for_session(session_key) is None and scheduler.model_for_session(session_key) is None


_ERRORS = {
    "agy_timeout": "Antigravity 会话超时，请重试。",
    "agy_unavailable": "Antigravity CLI 不可用。",
    "agy_request_failed": "Antigravity 未完成请求，请在官方 CLI 检查登录、额度或网络。",
    "agy_unexpected_tool": "Antigravity 网页只支持纯文本和只读上下文工具。本轮尝试调用其他工具，已停止。",
    "agy_session_mismatch": "无法恢复上次的 Antigravity 会话，请新开一条会话。",
    "agy_mcp_config_invalid": "Antigravity 交接配置不可用，请重新选择模型后再试。",
    "agy_unknown_native_model": "Antigravity 暂未提供这个模型，请刷新模型列表。",
    "agy_native_lock_failed": "Antigravity 会话未能锁定，请重新发送。",
    "agy_no_result": "Antigravity 未返回文字回复，请重试。",
}


def _native_root():
    from .. import config

    root = config.AGENT_WORKSPACES_DIR.resolve() / "agy-native"
    if root.is_symlink() or root.is_relative_to(config.ROOT_DIR.resolve()):
        raise ValueError("invalid native workspace root")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if root.stat().st_mode & 0o077 or root.stat().st_uid != os.getuid():
        raise ValueError("invalid native workspace permissions")
    return root


def _native_workspace(bound):
    from .. import config

    # 旧纯文本会话在 agy-native/；新版 ACP 会话复用 agy_handoff 的快照目录。
    handoff = config.AGENT_WORKSPACES_DIR.resolve() / "agy-handoff"
    if handoff.is_symlink() or handoff.is_relative_to(config.ROOT_DIR.resolve()):
        raise ValueError("invalid handoff workspace root")
    cwd = Path(bound["cwd"])
    if (cwd.is_symlink() or not cwd.is_dir() or not _valid_native_id(bound["id"])
            or cwd.resolve().parent not in (_native_root(), handoff.resolve())):
        raise ValueError("native workspace is unavailable")
    return str(cwd)


def _vococo_mcp_token() -> str:
    token = os.environ.get("VOCOCO_MCP_STATIC_TOKEN", "").strip()
    if not token:
        try:
            meta = VOCOCO_MCP_TOKEN_FILE.lstat()
            if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                    or meta.st_mode & 0o077):
                return ""
            token = VOCOCO_MCP_TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    if len(token) < 32 or not token.isascii() or any(char.isspace() for char in token):
        return ""
    return token


async def _vococo_mcp_bundle():
    """本机 brain_mcp 只读/派单工具；服务不可达时安静跳过，不影响普通聊天。"""
    global _vococo_mcp_cache
    now = monotonic()
    if _vococo_mcp_cache[0] > now:
        return _vococo_mcp_cache[1]
    bundle = None
    token = _vococo_mcp_token()
    if token:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", 8852), 0.6)
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            server = {"type": "http", "name": "vococo-local", "url": VOCOCO_MCP_URL,
                      "headers": [{"name": "Authorization", "value": f"Bearer {token}"}]}
            read = {name: tool for tool in VOCOCO_MCP_READ
                    for name in (f"mcp_vococo-local_{tool}", f"mcp_vococo_local_{tool}")}
            write = {name: tool for tool in VOCOCO_MCP_WRITE
                     for name in (f"mcp_vococo-local_{tool}", f"mcp_vococo_local_{tool}")}
            bundle = {"server": server, "read": read, "write": write}
        except (OSError, TimeoutError):
            bundle = None
    _vococo_mcp_cache = (now + 30, bundle)
    return bundle


_APPROVAL_MAX = 4000
_MD_LINK = re.compile(r'\[([^\]]*)\]\(([^)]*)\)')
_URL = re.compile(r'(?i)\b(?:https?://|www\.)[^\s<>()]+')
_APPROVAL_SECRET = re.compile(
    r'(?i)\b(?:api[_-]?key|password|passwd|access[_-]?token|secret)\b["\']?\s*[:=]\s*'
    r'["\']?[A-Za-z0-9_+./=-]{8,}')


def _fence(text: str) -> str:
    # 用代码块展示原文，避免 prompt/instruction 里的链接或 Markdown 被渲染成可点内容。
    fence = '````'
    while fence in text:
        fence += '`'
    return f"{fence}text\n{text}\n{fence}"


def _approval_text(tool: str, args: dict) -> str:
    def clip(value, limit):
        return str(value or "").strip()[:limit]

    def display(value):
        from ..tools import danger

        text = danger.redact_secrets(str(value or ''))
        text = _MD_LINK.sub(r'\1', text)
        return _URL.sub('[链接已移除]', text)

    if tool == "vococo_create_task":
        body = display(args.get('prompt'))
        detail = f"标题: {clip(args.get('title'), 100)}\n\n{body}"
        return f"Antigravity 想派发后台任务给 vococo：\n\n{_fence(detail)}\n\n共 {len(body)} 字。"
    if tool == "vococo_continue_task":
        body = display(args.get('instruction'))
        agent = clip(args.get('agent_id'), 64) or '默认'
        detail = f"会话: {clip(args.get('id'), 160)}\nAgent: {agent}\n\n{body}"
        return f"Antigravity 想向历史会话续发指令：\n\n{_fence(detail)}\n\n共 {len(body)} 字。"
    if tool == "vococo_cancel_task":
        return f"Antigravity 想取消任务：\n\n{_fence(clip(args.get('id'), 160))}"
    return f"Antigravity 想执行工具 `{tool}`，允许吗？"


async def _approve_vococo_tool(tool: str, call: dict) -> tuple[str, str]:
    """派单/续接/取消这类写操作必须由主人在网页上点一次允许。

    返回 (决定, 拒绝说明)。内容含凭据或过长时不弹窗，直接拒绝并说明原因。"""
    from .. import config
    from ..tools import danger
    from . import clarify
    from .core import Choice

    ctx = clarify.current()
    if ctx is None or not callable(getattr(ctx.adapter, "present_choice", None)):
        return "deny", "当前没有可确认的网页入口，已拒绝这次操作。"
    args = call.get("rawInput") or call.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    raw = args.get('prompt') or args.get('instruction') or ''
    scanned = ' '.join(str(args.get(key) or '') for key in ('title', 'prompt', 'instruction', 'id', 'agent_id'))
    if danger.redact_secrets(scanned) != scanned or _APPROVAL_SECRET.search(scanned):
        return "deny", "Antigravity 的指令可能包含凭据，已拒绝派发。"
    if isinstance(raw, str) and len(raw) > _APPROVAL_MAX:
        return "deny", f"Antigravity 的指令过长（{len(raw)} 字），已拒绝；请让它拆成更短的指令。"
    prompt = _approval_text(tool, args)
    pending = clarify.register(
        ctx.session_key, ["允许一次", "拒绝"],
        notice={"kind": "approval", "prompt": prompt, "reason": "Antigravity 请求写操作",
                "detail": prompt[:2000], "tool": tool},
    )
    try:
        await ctx.adapter.present_choice(ctx.chat_id, Choice(
            prompt=prompt,
            options=[(f"/clarify {pending.clarify_id} 0", "允许一次"),
                     (f"/clarify {pending.clarify_id} 1", "拒绝")],
        ))
    except Exception:
        clarify.abandon(pending.clarify_id)
        return "deny", "审批弹窗没有送达，已拒绝这次操作。"
    # agy 对权限答复的等待有上限（实测至少 75s）；留一点余量，超时就按拒绝处理。
    answer = await clarify.wait(pending.clarify_id, min(config.CLARIFY_TIMEOUT, 90))
    if answer == "允许一次":
        return "allow", ""
    return "deny", "已拒绝 Antigravity 的派发请求，本轮未执行。"


async def stream_native(key: str, turn_id: int, model: str, prompt: str, refusal: str = ""):
    from ..core.agent import AgentReply, Done, TextDelta
    from ..memory import session_store
    from . import agy_acp, agy_handoff

    chunks = []
    if not refusal and (not prompt.strip() or len(prompt) > MAX_PROMPT):
        refusal = f"Antigravity 会话只接受 1–{MAX_PROMPT} 字的纯文本。"
    if not refusal and (native_pending(key) or session_store.get_chosen_model(key) != model):
        refusal = "会话正在运行或已切换模型，请重试。"
    if not refusal:
        task = asyncio.current_task()
        _active_trials.add(task)
        _native_pending.add(key)
        try:
            if model not in await native_catalog():
                raise UpstreamError("agy_unknown_native_model")
            bound = session_store.agy_native_session(key)
            if bound:
                cwd = _native_workspace(bound)
                meta = await asyncio.to_thread(agy_handoff.prepare_into, Path(cwd), key)
                if bound.get("agent_id") and bound["agent_id"] != meta["agent_id"]:
                    raise UpstreamError("agy_session_mismatch")
            else:
                prepared = await asyncio.to_thread(agy_handoff.prepare, key)
                cwd, meta = prepared["workspace"], prepared
            session_id = None
            recovered = False
            bundle = await _vococo_mcp_bundle()
            extra_servers = [bundle["server"]] if bundle else []
            allowed_tools = set(bundle["read"]) if bundle else set()
            denied_tools = set()
            denied_note = ""

            async def approve(name, call):
                nonlocal denied_note
                short = bundle["write"].get(name) if bundle else None
                if short is None:
                    return "abort"
                if short in denied_tools:
                    return "deny"
                decision, note = await _approve_vococo_tool(short, call)
                if decision != "allow":
                    denied_tools.add(short)
                    denied_note = note
                return decision

            async def consume(sid):
                nonlocal session_id
                async with aclosing(agy_acp.stream_turn(
                        cwd, 360, model[len(NATIVE_PREFIX):], _ACP_NOTE + prompt, session_id=sid,
                        extra_servers=extra_servers, allowed_tools=allowed_tools, approve=approve)) as events:
                    async for event, payload in events:
                        if event == "delta":
                            chunks.append(payload)
                            yield TextDelta(payload)
                        elif event == "session":
                            session_id = payload

            try:
                async for delta in consume(bound["id"] if bound else None):
                    yield delta
            except agy_acp.AcpError as exc:
                # 升级前创建的纯文本会话理论上可用 ACP 载入；万一载入失败，
                # 只对这种旧会话降级为新会话，避免整条会话锁死无法继续。
                if not (bound and not bound.get("acp") and exc.code == "agy_session_mismatch"):
                    raise
                recovered = True
                async for delta in consume(None):
                    yield delta
                chunks.append("\n\n（原 Antigravity 会话无法恢复，已新建会话继续。）")
            # 真实 agy 在用户拒绝写工具后可能直接 end_turn 且不产出正文；
            # 这不是模型故障，给一条明确回执，并视为本轮没有成功执行。
            denied_only = not "".join(chunks).strip() and bool(denied_tools)
            if denied_only:
                chunks.append(denied_note or "已拒绝 Antigravity 的写操作，本轮未执行。")
            if not "".join(chunks).strip():
                raise UpstreamError("agy_no_result")
            lock = {"id": session_id, "cwd": cwd, "agent_id": meta["agent_id"], "acp": True}
            if denied_only and not bound:
                pass  # 首轮只发生被拒写操作，不算成功会话，不锁定
            elif not _valid_native_id(session_id):
                raise UpstreamError("agy_native_lock_failed")
            elif bound and recovered:
                if not session_store.replace_agy_native_session(key, model, lock):
                    raise UpstreamError("agy_native_lock_failed")
            elif not bound and not session_store.lock_agy_native_session(key, model, turn_id, lock):
                raise UpstreamError("agy_native_lock_failed")
        except (ValueError, OSError, UpstreamError, agy_acp.AcpError) as exc:
            code = exc.code if isinstance(exc, (UpstreamError, agy_acp.AcpError)) else type(exc).__name__
            log.warning("Antigravity 网页轮次失败: turn_id=%s code=%s", turn_id, code)
            refusal = _ERRORS.get(code, "Antigravity 会话未完成；请检查可用模型或稍后重试。")
        finally:
            _native_pending.discard(key)
            _active_trials.discard(task)
    yield Done(AgentReply("".join(chunks), [], None, bool(refusal), error=refusal, model=model))


async def stream_trial(model: str, prompt: str, refusal: str = ""):
    from ..core.agent import AgentReply, Done, TextDelta

    chunks = []
    if not refusal and (model not in CHOICES or not enabled()):
        refusal = "Antigravity 试用未启用或已结束，请切换模型。"
    if not refusal and (not prompt.strip() or len(prompt) > MAX_PROMPT):
        refusal = f"Antigravity 试用只接受 1–{MAX_PROMPT} 字的纯文本。"
    if not refusal:
        task = asyncio.current_task()
        _active_trials.add(task)
        try:
            with TemporaryDirectory(prefix="vococo-agy-") as cwd:
                async with aclosing(generate((str(cli_binary()),), cwd, 90, model.split(":", 1)[1], prompt)) as events:
                    async for event, payload in events:
                        if event == "delta":
                            chunks.append(payload["text"])
                            yield TextDelta(payload["text"])
        except UpstreamError as exc:
            refusal = _ERRORS.get(exc.code, "Antigravity 未返回完整回复，请重试。")
        finally:
            _active_trials.discard(task)
    yield Done(AgentReply("".join(chunks), [], None, bool(refusal), error=refusal, model=model))
