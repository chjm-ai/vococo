"""仅供 Web 手动文字轮次使用的 Antigravity 试用通道。"""

import asyncio
import json
import os
import signal
from contextlib import aclosing, suppress
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, time

import anyio

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


async def start_cli(command, model, cwd):
    args = (
        "--model", model, "--input-format", "stream-json", "--output-format", "stream-json",
        "--mode", "plan", "--sandbox", "--disable-slash-commands", "--settings", SETTINGS,
    )
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


async def read_events(pipe):
    total = 0
    while line := await pipe.readline():
        total += len(line)
        if total > MAX_OUTPUT:
            raise UpstreamError("agy_output_limit")
        event = json.loads(line)
        if not isinstance(event, dict):
            raise UpstreamError("agy_protocol_error")
        yield event


async def read_answer(proc, model, cwd):
    seen_model, final, chunks = None, None, []
    async for event in read_events(proc.stdout):
        kind = event.get("event")
        if kind == "init":
            init = event.get("init", {})
            seen_model = init.get("model")
            if seen_model != model:
                raise UpstreamError("agy_model_mismatch")
            reported_cwd = init.get("cwd")
            if not isinstance(reported_cwd, str) or Path(reported_cwd).resolve() != Path(cwd).resolve():
                raise UpstreamError("agy_workspace_mismatch")
        elif kind == "step_update" and seen_model == model and final is None:
            step = event.get("step_update", {})
            if step.get("step_type") == "agent_response" and isinstance(text := step.get("text_delta"), str) and text:
                chunks.append(text)
                yield "delta", {"text": text}
        elif kind == "result":
            result = event.get("result", {})
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
    yield "done", {"model": model}


async def generate(command, cwd, timeout, model, prompt):
    proc = None
    complete = False
    try:
        async with asyncio.timeout(timeout):
            proc = await start_cli(command, model, cwd)
            msg = {"event": "user", "message": {"content": prompt}}
            proc.stdin.write((json.dumps(msg) + "\n").encode())
            await proc.stdin.drain()
            proc.stdin.close()
            async for event in read_answer(proc, model, cwd):
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


def is_trial_model(model: str | None) -> bool:
    return bool(model and model.startswith("agy-trial:"))


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
    try:
        until = date.fromisoformat(os.environ.get("VOCOCO_AGY_TRIAL_UNTIL", ""))
    except ValueError:
        return False
    return date.today() < until and _quota_cli_ready()


def accepts_session(session_key: str) -> bool:
    from ..core import tasks
    from ..cron import scheduler

    return tasks.task_for_session(session_key) is None and scheduler.model_for_session(session_key) is None


_ERRORS = {
    "agy_timeout": "Antigravity 试用请求超时。",
    "agy_unavailable": "Antigravity CLI 不可用。",
    "agy_request_failed": "Antigravity 未完成请求，请在官方 CLI 检查登录、额度或网络。",
}


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
