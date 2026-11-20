"""通过官方 ACP 协议驱动 Antigravity 会话（仅网页手动文字轮次使用）。

这里只负责协议：启动 `agy acp --stdio`、建立/恢复会话、流式收正文、
按白名单放行只读 MCP。文件访问、命令和联网在会话设置里直接拒绝，
模型发起的其他工具审批一律拒绝。
"""

import asyncio
import json
import uuid

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


def _valid_id(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


class _Client:
    def __init__(self, proc, allowed, approve=None):
        self.proc = proc
        self.allowed = set(allowed)
        self.approve = approve
        self.seq = 0
        self.output = 0

    async def _send(self, body):
        self.proc.stdin.write((json.dumps(body, ensure_ascii=False) + '\n').encode())
        await self.proc.stdin.drain()

    async def _read(self):
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
            # 网页模式只允许只读上下文工具；其他工具尝试一旦出现就中止本轮，
            # 不把「已拒绝」当成正常完成，避免用户误以为任务跑完了。
            raise AcpError('agy_unexpected_tool')

    async def request(self, method, params, *, replay=False):
        """流式请求：逐条产出 ('delta', 文本)，最后产出 ('result', 结果)。"""
        self.seq += 1
        request_id = self.seq
        await self._send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        while True:
            message = await self._read()
            if message.get('method') == 'session/update':
                update = (message.get('params') or {}).get('update') or {}
                if not replay and update.get('sessionUpdate') == 'agent_message_chunk':
                    content = update.get('content') or {}
                    text = content.get('text')
                    if isinstance(text, str) and text:
                        yield 'delta', text
                continue
            if message.get('method') == 'session/request_permission':
                await self._permission(message)
                continue
            if 'method' in message and 'id' in message:
                # 未声明任何客户端能力时，agent 不该反向请求；不认识的请求一律拒绝。
                await self._send({'jsonrpc': '2.0', 'id': message['id'],
                                  'error': {'code': -32601, 'message': 'unsupported'}})
                continue
            if message.get('id') == request_id:
                if 'error' in message:
                    raise AcpError('agy_session_mismatch' if method == 'session/load'
                                   else 'agy_protocol_error')
                yield 'result', message.get('result') or {}
                return


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


async def _apply_model(client, session_id, result, model, effort):
    """用官方 configOptions 切模型族和思考档位；没有 configOptions 的旧版退回 set_model。"""
    configs = {item.get('id'): item for item in (result or {}).get('configOptions') or []
               if isinstance(item, dict)}
    model_cfg = configs.get('model') or {}
    model_values = [opt.get('value') for opt in model_cfg.get('options') or []
                    if isinstance(opt, dict)]
    if model_values:
        if model not in model_values:
            # 极端灰度下 configOptions 可能只列带档位后缀的变体；按族+档位挑一个可用值。
            candidates = [value for value in model_values if value.startswith(model + '-')]
            preferred = f"{model}-{effort}" if effort else ""
            if preferred in candidates:
                model = preferred
            elif candidates:
                model = candidates[0]
            else:
                raise AcpError('agy_model_unavailable')
        try:
            await _result(client.request('session/set_config_option', {
                'sessionId': session_id, 'configId': 'model', 'value': model}))
        except AcpError:
            raise AcpError('agy_model_unavailable') from None
    else:
        try:
            await _result(client.request('session/set_model', {
                'sessionId': session_id, 'modelId': model}))
        except AcpError:
            raise AcpError('agy_model_unavailable') from None
    if not effort:
        return
    thought_cfg = configs.get('thought_level') or {}
    levels = [opt.get('value') for opt in thought_cfg.get('options') or []
              if isinstance(opt, dict)]
    if not levels:
        return
    if effort not in levels:
        # 旧灰度只有 low/medium/high；用户存了 xhigh 时按最高可用档继续。
        effort = 'high' if 'high' in levels else levels[-1]
    try:
        await _result(client.request('session/set_config_option', {
            'sessionId': session_id, 'configId': 'thought_level', 'value': effort}))
    except AcpError:
        raise AcpError('agy_effort_unavailable') from None


def _tool_names(server_name, tools):
    # ACP 里 MCP 工具名通常是 mcp_<server>_<tool>；服务名带连字符时也兼容下划线写法。
    names = {f"mcp_{server_name}_{tool}" for tool in tools}
    names.update(f"mcp_{server_name.replace('-', '_')}_{tool}" for tool in tools)
    return names


async def stream_turn(workspace, timeout, model, prompt, *, effort=None, session_id=None,
                      extra_servers=(), allowed_tools=(), approve=None):
    """运行一轮 ACP 会话；产出 ('delta', 文本)、('session', id)、('done', None)。

    model 是模型族（如 gemini-4-neon），effort 是 low/medium/high/xhigh。"""
    from pathlib import Path

    workspace = Path(workspace)
    servers = _mcp_servers(workspace / '.agents/mcp_config.json')
    allowed = {name for server in servers for name in _tool_names(server['name'], _CONTEXT_TOOLS)}
    allowed |= set(allowed_tools or ())
    servers = [*servers, *extra_servers]
    proc = None
    try:
        async with asyncio.timeout(timeout):
            proc = await agy_trial.spawn_cli(
                (str(agy_trial.cli_binary()), '--sandbox', '--mode', 'plan',
                 '--settings', agy_trial.SETTINGS_ACP, 'acp', '--stdio'),
                workspace, asyncio.subprocess.PIPE)
            client = _Client(proc, allowed, approve=approve)
            await _result(client.request('initialize', {
                'protocolVersion': 1, 'clientCapabilities': {},
                'clientInfo': {'name': 'vococo', 'version': '1'}}))
            if session_id:
                result = await _result(client.request('session/load', {
                    'sessionId': session_id, 'cwd': str(workspace), 'mcpServers': servers}, replay=True))
                if not result or result.get('sessionId') != session_id:
                    raise AcpError('agy_session_mismatch')
            else:
                result = await _result(client.request('session/new', {
                    'cwd': str(workspace), 'mcpServers': servers}))
                session_id = (result or {}).get('sessionId')
                if not _valid_id(session_id):
                    raise AcpError('agy_session_mismatch')
            await _apply_model(client, session_id, result, model, effort)
            yield 'session', session_id
            stop = None
            async for kind, payload in client.request('session/prompt', {
                    'sessionId': session_id,
                    'prompt': [{'type': 'text', 'text': prompt}]}):
                if kind == 'delta':
                    yield 'delta', payload
                elif kind == 'result':
                    stop = payload.get('stopReason')
            if stop in ('cancelled', 'refusal'):
                raise AcpError('agy_request_failed')
            yield 'done', None
    except TimeoutError as exc:
        raise AcpError('agy_timeout') from exc
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise AcpError('agy_input_failed') from exc
    except (ValueError, AttributeError, TypeError) as exc:
        raise AcpError('agy_protocol_error') from exc
    except OSError as exc:
        raise AcpError('agy_unavailable') from exc
    finally:
        # ACP 是常驻服务端，一轮结束后不会自己退出；无论成功失败都回收整组进程。
        if proc is not None:
            with anyio.CancelScope(shield=True):
                await agy_trial.terminate(proc)
