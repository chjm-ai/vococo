"""通过官方 ACP 协议驱动 Antigravity 会话（仅网页手动文字轮次使用）。

这里只负责协议：启动 `agy acp --stdio`、建立/恢复会话、流式收正文、
按白名单放行只读 MCP。文件、命令和联网权限由 SETTINGS_ACP 决定；
默认完全放开，vococo 写任务仍由上层回调转成网页确认。
"""

import asyncio
import json
import tempfile
import uuid
from contextlib import aclosing
from datetime import datetime
from pathlib import Path
from time import monotonic

import anyio

from . import agy_trial

MAX_LINE = 1024 * 1024
MAX_TURN_OUTPUT = 8 * 1024 * 1024
_CONTEXT_TOOLS = ('read_agent_context', 'read_agent_memory', 'read_context_file',
                  'list_skills', 'read_skill')


class AcpError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _TurnTrace:
    """只记录阶段、耗时和计数；不要把原生请求、响应或工具内容写进日志。"""

    def __init__(self):
        self.started = self.stage_started = monotonic()
        self.stage = 'prepare'
        self.stage_times = {}
        self.first_text = self.last_progress = None
        self.tool_results = 0
        self.finish_received = False
        self.extended = False
        self.rpc_error = None

    def enter(self, stage):
        now = monotonic()
        self.stage_times[self.stage] = round(now - self.stage_started, 3)
        self.stage, self.stage_started = stage, now

    def progress(self, kind):
        self.last_progress = monotonic()
        if kind == 'tool':
            self.tool_results += 1
        elif self.first_text is None:
            self.first_text = round(self.last_progress - self.started, 3)

    def summary(self):
        now = monotonic()
        stage_times = {**self.stage_times, self.stage: round(now - self.stage_started, 3)}
        idle = None if self.last_progress is None else round(now - self.last_progress, 3)
        return (f'stage={self.stage} elapsed_s={now - self.started:.3f} stage_s={stage_times} '
                f'first_text_s={self.first_text} idle_s={idle} tool_results={self.tool_results} '
                f'finish_received={self.finish_received} extended={self.extended} rpc_error={self.rpc_error}')


def _valid_id(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


_TOOL_DONE = {'completed', 'failed', 'error', 'cancelled'}


def _tool_output(update: dict) -> tuple[str, int | None]:
    """从 tool_call_update 里抽命令输出和退出码；没有就返回空串。"""
    code = None
    raw = update.get('rawOutput')
    if isinstance(raw, dict):
        if isinstance(raw.get('exit_code'), int):
            code = raw['exit_code']
        for key in ('output', 'content', 'text'):
            value = raw.get(key)
            if isinstance(value, str) and value:
                return value, code
    content = update.get('content')
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            inner = item.get('content') or {}
            if isinstance(inner, dict) and isinstance(inner.get('text'), str):
                parts.append(inner['text'])
        if parts:
            return '\n'.join(parts), code
    return '', code


def _tool_events(update: dict, state: tuple[set[str], set[str]]):
    """把一个 tool_call/tool_call_update 归并成 start/end 事件列表。

    同一个 toolCallId 只发一次 start、一次 end；pending/in_progress 之类的中转状态
    直接忽略，只在终态（completed/failed/error/cancelled）发 end。
    """
    started, ended = state
    tid = update.get('toolCallId')
    if not isinstance(tid, str) or not tid:
        return []
    name = update.get('name') if isinstance(update.get('name'), str) else ''
    status = update.get('status') if isinstance(update.get('status'), str) else ''
    events = []
    if tid not in started:
        started.add(tid)
        raw = update.get('rawInput') or update.get('args') or {}
        events.append(('tool', {'phase': 'start', 'id': tid, 'name': name,
                                'title': update.get('title') or '',
                                'kind': update.get('kind') or '',
                                'input': raw if isinstance(raw, dict) else {}}))
    if status in _TOOL_DONE and tid not in ended:
        ended.add(tid)
        output, code = _tool_output(update)
        events.append(('tool', {'phase': 'end', 'id': tid, 'name': name,
                                'status': status, 'output': output, 'exit_code': code}))
    return events


def _notified_options(message, session_id):
    params = message.get('params')
    if message.get('method') != 'session/update' or not isinstance(params, dict):
        return None
    update = params.get('update')
    if (params.get('sessionId') != session_id or not isinstance(update, dict)
            or update.get('sessionUpdate') != 'config_option_update'):
        return None
    options = update.get('configOptions')
    return options if isinstance(options, list) else None


def _visible_updates(message, tool_state):
    update = (message.get('params') or {}).get('update') or {}
    kind = update.get('sessionUpdate')
    if kind in ('tool_call', 'tool_call_update'):
        yield from _tool_events(update, tool_state)
    elif kind in ('agent_message_chunk', 'agent_thought_chunk'):
        text = (update.get('content') or {}).get('text')
        if isinstance(text, str) and text:
            yield ('delta' if kind == 'agent_message_chunk' else 'thinking'), text


def _context_usage(update):
    if not isinstance(update, dict) or update.get('sessionUpdate') != 'usage_update':
        return None
    used, size = update.get('used'), update.get('size')
    if type(used) is int and type(size) is int and 0 <= used < 2**53 and 0 < size < 2**53:
        return used, size
    return None


class _Client:
    def __init__(self, proc, allowed, approve=None, trace=None):
        self.proc = proc
        self.allowed = set(allowed)
        self.approve = approve
        self.trace = trace or _TurnTrace()
        self.seq = 0
        self.output = 0
        self.pending = None

    async def _send(self, body):
        self.proc.stdin.write((json.dumps(body, ensure_ascii=False) + '\n').encode())
        await self.proc.stdin.drain()

    async def _read(self):
        if self.pending is not None:
            message, self.pending = self.pending, None
            return message
        line = await self.proc.stdout.readline()
        if not line:
            raise AcpError('agy_unavailable')
        self.output += len(line)
        if self.output > MAX_TURN_OUTPUT:
            raise AcpError('agy_output_limit')
        try:
            message = json.loads(line)
        except ValueError:
            raise AcpError('agy_protocol_error') from None
        if not isinstance(message, dict):
            raise AcpError('agy_protocol_error')
        return message

    async def _permission(self, message):
        request = message.get('params') or {}
        call = request.get('toolCall') or {}
        name = call.get('name')
        allowed = isinstance(name, str) and name in self.allowed
        decision = 'allow' if allowed else 'abort'
        if not allowed and self.approve is not None:
            # 需要在 vococo 侧确认的工具（如派发任务）由上层回调决定；
            # deny 只拒绝这一次，让模型继续解释；abort 才中止本轮。
            decision = await self.approve(name, call)
            allowed = decision == 'allow'
        kind = 'allow_once' if allowed else 'reject_once'
        options = request.get('options') or []
        option = next((item.get('optionId') for item in options
                       if isinstance(item, dict) and item.get('kind') == kind), None)
        if option is None:
            outcome = {'outcome': 'cancelled'}
        else:
            outcome = {'outcome': 'selected', 'optionId': option}
        await self._send({'jsonrpc': '2.0', 'id': message['id'], 'result': {'outcome': outcome}})
        if decision == 'abort':
            # 调用方明确要求中止（例如没有可用的网页确认入口）；
            # 不把「已拒绝」当成正常完成，避免用户误以为任务跑完了。
            raise AcpError('agy_unexpected_tool')

    async def read_config_update(self, session_id):
        # 旧版先确认设模，再发档位通知；短暂等一帧，不吞掉其他事件。
        try:
            async with asyncio.timeout(0.15):
                message = await self._read()
        except TimeoutError:
            return None
        except AcpError as exc:
            if exc.code == 'agy_unavailable':
                return None
            raise
        options = _notified_options(message, session_id)
        if options is None:
            self.pending = message
        return options

    def _response(self, message, request_id, method):
        if not {'result', 'error'}.intersection(message):
            raise AcpError('agy_protocol_error')
        response_id = message.get('id')
        # 客户端始终只有一个未完成请求；忽略对不上的响应会等到超时。
        if type(response_id) is not int or response_id != request_id:
            raise AcpError('agy_response_mismatch')
        if 'error' in message:
            error = message['error']
            if not isinstance(error, dict):
                raise AcpError('agy_protocol_error')
            code = error.get('code')
            if type(code) is int and -(2**31) <= code < 2**31:
                self.trace.rpc_error = code
            raise AcpError('agy_session_mismatch' if method == 'session/load' else 'agy_rpc_error')
        result = message['result']
        if result is not None and not isinstance(result, dict):
            raise AcpError('agy_protocol_error')
        return result or {}

    async def request(self, method, params, *, replay=False):
        """回放只取请求结果，不重发历史正文和工具。"""
        stage = params['configId'] if method == 'session/set_config_option' else method
        self.trace.enter(stage)
        self.seq += 1
        request_id = self.seq
        await self._send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        tool_state: tuple[set[str], set[str]] = (set(), set())
        notified = None
        setting_model = method == 'session/set_model' or stage == 'model'
        while True:
            message = await self._read()
            inbound = message.get('method')
            if inbound == 'session/update':
                options = _notified_options(message, params.get('sessionId')) if setting_model else None
                if options is not None:
                    notified = options
                elif not replay:
                    if method == 'session/prompt':
                        incoming = message.get('params') or {}
                        usage = _context_usage(incoming.get('update'))
                        if usage is not None and incoming.get('sessionId') == params['sessionId']:
                            yield 'context_usage', usage
                    for event in _visible_updates(message, tool_state):
                        yield event
            elif inbound == 'session/request_permission':
                await self._permission(message)
            elif 'method' not in message:
                result = self._response(message, request_id, method)
                if notified is not None and not isinstance(result.get('configOptions'), list):
                    result = {**result, 'configOptions': notified}
                yield 'result', result
                return
            elif 'id' in message:
                # 未声明任何客户端能力时，agent 不该反向请求；不认识的请求一律拒绝。
                await self._send({'jsonrpc': '2.0', 'id': message['id'],
                                  'error': {'code': -32601, 'message': 'unsupported'}})


def _mcp_servers(config_path):
    try:
        data = json.loads(config_path.read_text(encoding='utf-8'))
        servers = data['mcpServers']
    except (OSError, ValueError, KeyError, TypeError):
        raise AcpError('agy_mcp_config_invalid') from None
    result = []
    for name, spec in servers.items():
        if not isinstance(name, str) or not name or not isinstance(spec, dict):
            raise AcpError('agy_mcp_config_invalid')
        command = spec.get('command')
        args = spec.get('args')
        if not isinstance(command, str) or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise AcpError('agy_mcp_config_invalid')
        result.append({'name': name, 'command': command, 'args': args, 'env': []})
    if not result:
        raise AcpError('agy_mcp_config_invalid')
    return result


async def _result(events):
    result = None
    async for kind, payload in events:
        if kind == 'result':
            result = payload
    return result


def _config_options(result):
    return {item['id']: item for item in (result or {}).get('configOptions') or []
            if isinstance(item, dict) and isinstance(item.get('id'), str)}


def _option_values(config):
    return [opt['value'] for opt in config.get('options') or []
            if isinstance(opt, dict) and isinstance(opt.get('value'), str)]


def _model_value(model, effort, values):
    if model in values:
        return model
    # 灰度版本可能只列出带档位后缀的模型。
    candidates = [value for value in values if value.startswith(model + '-')]
    if not candidates:
        raise AcpError('agy_model_unavailable')
    preferred = f'{model}-{effort}'
    return preferred if preferred in candidates else candidates[0]


async def _config_result(client, method, params, rejected):
    try:
        return await _result(client.request(method, params))
    except AcpError as exc:
        # 正文尚未提交，旧进程提前退出时仍允许外层重试；协议错误不能伪装成档位错误。
        if exc.code in ('agy_rpc_error', 'agy_unavailable'):
            raise AcpError(rejected) from exc
        raise


async def _set_model(client, session_id, model, effort, model_cfg):
    values = _option_values(model_cfg)
    if values:
        model = _model_value(model, effort, values)
        method = 'session/set_config_option'
        params = {'sessionId': session_id, 'configId': 'model', 'value': model}
    else:
        method = 'session/set_model'
        params = {'sessionId': session_id, 'modelId': model}
    return model, await _config_result(client, method, params, 'agy_model_unavailable')


async def _apply_model(client, session_id, result, model, effort):
    configs = _config_options(result)
    model_cfg = configs.get('model', {})
    model, updated = await _set_model(client, session_id, model, effort, model_cfg)
    if not effort:
        return
    options = updated.get('configOptions')
    if not isinstance(options, list):
        options = await client.read_config_update(session_id)
    legacy = (result or {}).get('models')
    previous = model_cfg.get('currentValue') or (legacy.get('currentModelId') if isinstance(legacy, dict) else None)
    if options is not None:
        configs = _config_options({'configOptions': options})
    elif model != previous:
        # 换了模型但上游没返回新档位时，交给该模型的默认档，不用上一个模型的档位。
        return
    levels = _option_values(configs.get('thought_level', {}))
    if not levels:
        return
    if effort not in levels:
        effort = 'high' if 'high' in levels else levels[-1]
    await _config_result(client, 'session/set_config_option', {
        'sessionId': session_id, 'configId': 'thought_level', 'value': effort}, 'agy_effort_unavailable')


def _tool_names(server_name, tools):
    # ACP 里 MCP 工具名通常是 mcp_<server>_<tool>；服务名带连字符时也兼容下划线写法。
    names = {f"mcp_{server_name}_{tool}" for tool in tools}
    names.update(f"mcp_{server_name.replace('-', '_')}_{tool}" for tool in tools)
    return names


async def _initialize(client):
    result = await _result(client.request('initialize', {
        'protocolVersion': 1, 'clientCapabilities': {},
        'clientInfo': {'name': 'vococo', 'version': '1'}}))
    return (result or {}).get('agentCapabilities') or {}


def _supports_images(caps):
    return (caps.get('promptCapabilities') or {}).get('image') is True


_image_capability = (None, 0.0, False)
_image_capability_lock = asyncio.Lock()


async def image_input_available() -> bool:
    """网页接收前检查 CLI 能力，不发模型请求；启动异常时保守保留草稿。"""
    global _image_capability
    cli = agy_trial.cli_binary()
    loop = asyncio.get_running_loop()
    try:
        file_stat = cli.stat()
        version = (str(cli), file_stat.st_mtime_ns, file_stat.st_size)
    except OSError:
        return False
    async with _image_capability_lock:
        key, expiry, known = _image_capability
        if key == version and loop.time() < expiry:
            return known
        proc = None
        try:
            async with asyncio.timeout(8):
                proc = await agy_trial.spawn_cli(
                    (str(cli), '--mode', 'plan', '--settings', agy_trial.SETTINGS, 'acp', '--stdio'),
                    tempfile.gettempdir(), asyncio.subprocess.PIPE)
                supports = _supports_images(await _initialize(_Client(proc, set())))
        except (AcpError, OSError, TimeoutError, ValueError, TypeError, AttributeError):
            return False
        finally:
            if proc is not None:
                with anyio.CancelScope(shield=True):
                    await agy_trial.terminate(proc)
        _image_capability = (version, loop.time() + 300, supports)
        return supports


async def _open_session(client, workspace, servers, session_id, *, require_images=False):
    caps = await _initialize(client)
    if require_images and not _supports_images(caps):
        raise AcpError('agy_images_unsupported')
    params = {'cwd': str(workspace), 'mcpServers': servers}
    if session_id:
        params['sessionId'] = session_id
        result = await _result(client.request('session/load', params, replay=True))
        if result.get('sessionId') != session_id:
            raise AcpError('agy_session_mismatch')
    else:
        result = await _result(client.request('session/new', params))
        session_id = result.get('sessionId')
        if not _valid_id(session_id):
            raise AcpError('agy_session_mismatch')
    return session_id, result


def _extend_for_progress(deadline, now, hard_end, progress_window):
    # 有新结果才把截止时间往后推；沉默、思考和工具开始不续时。
    current = deadline.when()
    target = min(hard_end, now + progress_window)
    if now < current < target:
        deadline.reschedule(target)
        return True
    return False


def _check_finish(result):
    stop = result.get('stopReason')
    if not isinstance(stop, str) or not stop.strip():
        raise AcpError('agy_finish_invalid')
    if stop in ('cancelled', 'refusal'):
        raise AcpError('agy_request_failed')
    if stop in ('max_tokens', 'max_turn_requests'):
        raise AcpError('agy_finish_limit')
    if stop != 'end_turn':
        raise AcpError('agy_finish_invalid')


async def _stream_prompt(client, session_id, prompt, deadline, hard_end, progress_window, *, image_blocks=()):
    trace = client.trace
    params = {'sessionId': session_id, 'prompt': [{'type': 'text', 'text': prompt}, *image_blocks]}
    try:
        async with aclosing(client.request('session/prompt', params)) as events:
            async for kind, payload in events:
                if kind == 'result':
                    trace.finish_received = True
                    _check_finish(payload)
                    continue
                if (kind == 'delta' and payload.strip()) or (kind == 'tool' and payload['phase'] == 'end'):
                    trace.progress(kind)
                    if _extend_for_progress(deadline, asyncio.get_running_loop().time(), hard_end, progress_window):
                        trace.extended = True
                yield kind, payload
    except AcpError as exc:
        if exc.code == 'agy_unavailable':
            raise AcpError('agy_finish_invalid') from exc
        raise


def _turn_failure_code(exc, deadline):
    if isinstance(exc, TimeoutError):
        return 'agy_timeout' if deadline.expired() else 'agy_request_failed'
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        return 'agy_input_failed'
    if isinstance(exc, OSError):
        return 'agy_unavailable'
    return 'agy_protocol_error'


async def stream_turn(workspace, timeout, model, prompt, *, effort=None, session_id=None,
                      extra_servers=(), allowed_tools=(), approve=None, max_timeout=None,
                      progress_window=0, started_at=None, turn_id=None, image_blocks=(), settings=None):
    """原生 ACP 一轮；所有尝试共享同一个起点和硬上限。"""
    loop = asyncio.get_running_loop()
    start = loop.time() if started_at is None else started_at
    soft_end, hard_end = start + timeout, start + max(timeout, max_timeout or timeout)
    trace, deadline = _TurnTrace(), asyncio.timeout_at(soft_end)
    workspace, proc = Path(workspace), None
    outcome = 'interrupted'
    try:
        if loop.time() >= soft_end:
            raise AcpError('agy_timeout')
        servers = _mcp_servers(workspace / '.agents/mcp_config.json')
        allowed = {name for s in servers for name in _tool_names(s['name'], _CONTEXT_TOOLS)}
        trace.enter('spawn')
        async with deadline:
            proc = await agy_trial.spawn_cli(
                (str(agy_trial.cli_binary()), '--mode', 'accept-edits',
                 '--settings', settings or agy_trial.SETTINGS_ACP, 'acp', '--stdio'),
                workspace, asyncio.subprocess.PIPE)
            client = _Client(proc, allowed.union(allowed_tools or ()), approve=approve, trace=trace)
            session_id, result = await _open_session(client, workspace, [*servers, *extra_servers], session_id,
                                                     require_images=bool(image_blocks))
            await _apply_model(client, session_id, result, model, effort)
            yield 'session', session_id
            async with aclosing(_stream_prompt(client, session_id, prompt, deadline, hard_end, progress_window,
                                                image_blocks=image_blocks)) as events:
                async for event in events:
                    yield event
        outcome = 'ok'
        yield 'done', None
    except AcpError as exc:
        outcome = exc.code
        raise
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        outcome = _turn_failure_code(exc, deadline)
        raise AcpError(outcome) from exc
    finally:
        try:
            if turn_id is not None:
                timestamp = datetime.now().astimezone().isoformat(timespec='milliseconds')
                print(f'[agy-acp] at={timestamp} turn_id={turn_id} outcome={outcome} {trace.summary()}', flush=True)
        finally:
            # ACP 不会自己退出；成功、失败和取消时都回收整组进程。
            if proc is not None:
                with anyio.CancelScope(shield=True):
                    await agy_trial.terminate(proc)
