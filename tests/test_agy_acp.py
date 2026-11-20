import asyncio
import json
from pathlib import Path

import pytest

from vococo.gateway import agy_acp


class Writer:
    def __init__(self):
        self.data = b''

    def write(self, data):
        self.data += data

    async def drain(self):
        pass


class Proc:
    def __init__(self, messages):
        self.stdout = asyncio.StreamReader()
        for message in messages:
            self.stdout.feed_data((json.dumps(message) + '\n').encode())
        self.stdout.feed_eof()
        self.stdin = Writer()


async def collect(events):
    return [item async for item in events]


def permission(req_id, name):
    return {'jsonrpc': '2.0', 'id': req_id, 'method': 'session/request_permission', 'params': {
        'sessionId': 's', 'toolCall': {'toolCallId': 't', 'name': name},
        'options': [{'optionId': 'a', 'name': '允许一次', 'kind': 'allow_once'},
                    {'optionId': 'r', 'name': '拒绝', 'kind': 'reject_once'}],
    }}


def update(text):
    return {'jsonrpc': '2.0', 'method': 'session/update', 'params': {
        'sessionId': 's', 'update': {'sessionUpdate': 'agent_message_chunk',
                                     'content': {'type': 'text', 'text': text}},
    }}


@pytest.mark.anyio
async def test_permission_allows_only_exact_context_tools():
    proc = Proc([
        {'jsonrpc': '2.0', 'id': 1, 'result': {}},
        permission(0, 'mcp_vococo-context-abc_read_skill'),
        update('你好'),
        {'jsonrpc': '2.0', 'id': 2, 'result': {'stopReason': 'end_turn'}},
    ])
    client = agy_acp._Client(proc, {'mcp_vococo-context-abc_read_skill'})
    assert await agy_acp._result(client.request('initialize', {})) == {}
    events = await collect(client.request('session/prompt', {}))
    assert ('delta', '你好') in events
    replies = [json.loads(line) for line in proc.stdin.data.decode().splitlines()]
    outcomes = [reply['result']['outcome'] for reply in replies if 'result' in reply and 'id' in reply
                and reply['id'] == 0]
    assert outcomes == [{'outcome': 'selected', 'optionId': 'a'}]


@pytest.mark.anyio
async def test_forbidden_permission_stops_the_turn():
    proc = Proc([
        {'jsonrpc': '2.0', 'id': 1, 'result': {}},
        permission(0, 'mcp_vococo-context-abc_forbidden_probe'),
    ])
    client = agy_acp._Client(proc, {'mcp_vococo-context-abc_read_skill'})
    assert await agy_acp._result(client.request('initialize', {})) == {}
    with pytest.raises(agy_acp.AcpError, match='agy_unexpected_tool'):
        await collect(client.request('session/prompt', {}))
    replies = [json.loads(line) for line in proc.stdin.data.decode().splitlines()]
    denied = [reply['result']['outcome'] for reply in replies if 'result' in reply and 'id' in reply
              and reply['id'] == 0]
    assert denied == [{'outcome': 'selected', 'optionId': 'r'}]


@pytest.mark.anyio
async def test_session_load_replay_does_not_emit_old_chunks():
    proc = Proc([
        update('历史正文'),
        {'jsonrpc': '2.0', 'id': 1, 'result': {'sessionId': 's'}},
    ])
    client = agy_acp._Client(proc, set())
    events = await collect(client.request('session/load', {'sessionId': 's'}, replay=True))
    assert events == [('result', {'sessionId': 's'})]


@pytest.mark.anyio
async def test_unknown_client_request_is_rejected(tmp_path):
    proc = Proc([
        {'jsonrpc': '2.0', 'id': 9, 'method': 'fs/read_text_file', 'params': {}},
        {'jsonrpc': '2.0', 'id': 1, 'result': {}},
    ])
    client = agy_acp._Client(proc, set())
    assert await agy_acp._result(client.request('initialize', {})) == {}
    replies = [json.loads(line) for line in proc.stdin.data.decode().splitlines()]
    assert {'jsonrpc': '2.0', 'id': 9, 'error': {'code': -32601, 'message': 'unsupported'}} in replies


def requests(proc):
    return [json.loads(line) for line in proc.stdin.data.decode().splitlines()]


@pytest.mark.anyio
async def test_stream_turn_creates_session_and_streams_reply(tmp_path, monkeypatch):
    sid = '3596d05e-5d0e-47a7-b5a8-77aee32011d2'
    workspace = tmp_path / 'workspace'
    (workspace / '.agents').mkdir(parents=True)
    (workspace / '.agents/mcp_config.json').write_text(json.dumps({'mcpServers': {
        'vococo-context-test': {'command': '/bin/echo', 'args': ['serve']}}}))
    proc = Proc([
        {'jsonrpc': '2.0', 'id': 1, 'result': {}},
        {'jsonrpc': '2.0', 'id': 2, 'result': {'sessionId': sid}},
        {'jsonrpc': '2.0', 'id': 3, 'result': {}},
        update('新回复'),
        {'jsonrpc': '2.0', 'id': 4, 'result': {'stopReason': 'end_turn'}},
    ])

    async def spawn(*args, **kwargs):
        return proc

    async def terminate(_proc):
        return None

    monkeypatch.setattr(agy_acp.agy_trial, 'spawn_cli', spawn)
    monkeypatch.setattr(agy_acp.agy_trial, 'terminate', terminate)
    monkeypatch.setattr(agy_acp.agy_trial, 'cli_binary', lambda: Path('/bin/true'))
    events = await collect(agy_acp.stream_turn(workspace, 10, 'gemini-x', '你好'))
    assert ('session', sid) in events and ('delta', '新回复') in events and ('done', None) in events
    sent = requests(proc)
    by_method = {item['method']: item['params'] for item in sent if 'method' in item}
    assert by_method['session/new']['cwd'] == str(workspace)
    assert by_method['session/new']['mcpServers'][0]['name'] == 'vococo-context-test'
    assert by_method['session/set_model'] == {'sessionId': sid, 'modelId': 'gemini-x'}
    assert by_method['session/prompt']['sessionId'] == sid


@pytest.mark.anyio
async def test_stream_turn_loads_without_replaying_history(tmp_path, monkeypatch):
    sid = '3596d05e-5d0e-47a7-b5a8-77aee32011d2'
    workspace = tmp_path / 'workspace'
    (workspace / '.agents').mkdir(parents=True)
    (workspace / '.agents/mcp_config.json').write_text(json.dumps({'mcpServers': {
        'vococo-context-test': {'command': '/bin/echo', 'args': ['serve']}}}))
    proc = Proc([
        update('旧历史正文'),
        {'jsonrpc': '2.0', 'id': 1, 'result': {}},
        {'jsonrpc': '2.0', 'id': 2, 'result': {'sessionId': sid}},
        {'jsonrpc': '2.0', 'id': 3, 'result': {}},
        {'jsonrpc': '2.0', 'id': 4, 'result': {'stopReason': 'end_turn'}},
    ])

    async def spawn(*args, **kwargs):
        return proc

    async def terminate(_proc):
        return None

    monkeypatch.setattr(agy_acp.agy_trial, 'spawn_cli', spawn)
    monkeypatch.setattr(agy_acp.agy_trial, 'terminate', terminate)
    monkeypatch.setattr(agy_acp.agy_trial, 'cli_binary', lambda: Path('/bin/true'))
    events = await collect(agy_acp.stream_turn(workspace, 10, 'gemini-x', '继续', session_id=sid))
    assert ('delta', '旧历史正文') not in events
    sent = requests(proc)
    assert [item['method'] for item in sent if 'method' in item] == [
        'initialize', 'session/load', 'session/set_model', 'session/prompt']


@pytest.mark.anyio
async def test_extra_mcp_write_tool_uses_approval_callback(tmp_path, monkeypatch):
    sid = '3596d05e-5d0e-47a7-b5a8-77aee32011d2'
    workspace = tmp_path / 'workspace'
    (workspace / '.agents').mkdir(parents=True)
    (workspace / '.agents/mcp_config.json').write_text(json.dumps({'mcpServers': {
        'vococo-context-test': {'command': '/bin/echo', 'args': ['serve']}}}))
    proc = Proc([
        {'jsonrpc': '2.0', 'id': 1, 'result': {}},
        {'jsonrpc': '2.0', 'id': 2, 'result': {'sessionId': sid}},
        {'jsonrpc': '2.0', 'id': 3, 'result': {}},
        permission(0, 'mcp_vococo-local_vococo_create_task'),
        {'jsonrpc': '2.0', 'id': 4, 'result': {'stopReason': 'end_turn'}},
    ])
    seen = []

    async def spawn(*args, **kwargs):
        return proc

    async def terminate(_proc):
        return None

    async def approve(name, call):
        seen.append((name, call.get('rawInput')))
        return 'allow'

    monkeypatch.setattr(agy_acp.agy_trial, 'spawn_cli', spawn)
    monkeypatch.setattr(agy_acp.agy_trial, 'terminate', terminate)
    monkeypatch.setattr(agy_acp.agy_trial, 'cli_binary', lambda: Path('/bin/true'))
    extra = [{'type': 'http', 'name': 'vococo-local', 'url': 'http://127.0.0.1:8852/mcp/token',
              'headers': [{'name': 'Authorization', 'value': 'Bearer test'}]}]
    await collect(agy_acp.stream_turn(workspace, 10, 'gemini-x', '派发任务', session_id=sid,
                                      extra_servers=extra, approve=approve))
    assert seen and seen[0][0] == 'mcp_vococo-local_vococo_create_task'
    sent = requests(proc)
    replies = [item['result']['outcome'] for item in sent if 'result' in item and item.get('id') == 0]
    assert replies == [{'outcome': 'selected', 'optionId': 'a'}]
    assert any(item.get('method') == 'session/load' and item['params']['mcpServers'][-1]['name'] == 'vococo-local'
               for item in sent)


@pytest.mark.anyio
async def test_denied_write_tool_does_not_abort_turn(tmp_path, monkeypatch):
    sid = '3596d05e-5d0e-47a7-b5a8-77aee32011d2'
    workspace = tmp_path / 'workspace'
    (workspace / '.agents').mkdir(parents=True)
    (workspace / '.agents/mcp_config.json').write_text(json.dumps({'mcpServers': {
        'vococo-context-test': {'command': '/bin/echo', 'args': ['serve']}}}))
    proc = Proc([
        {'jsonrpc': '2.0', 'id': 1, 'result': {}},
        {'jsonrpc': '2.0', 'id': 2, 'result': {'sessionId': sid}},
        {'jsonrpc': '2.0', 'id': 3, 'result': {}},
        permission(0, 'mcp_vococo-local_vococo_create_task'),
        update('已取消派发'),
        {'jsonrpc': '2.0', 'id': 4, 'result': {'stopReason': 'end_turn'}},
    ])

    async def spawn(*args, **kwargs):
        return proc

    async def terminate(_proc):
        return None

    async def approve(name, call):
        return 'deny'

    monkeypatch.setattr(agy_acp.agy_trial, 'spawn_cli', spawn)
    monkeypatch.setattr(agy_acp.agy_trial, 'terminate', terminate)
    monkeypatch.setattr(agy_acp.agy_trial, 'cli_binary', lambda: Path('/bin/true'))
    events = await collect(agy_acp.stream_turn(workspace, 10, 'gemini-x', '派发任务', session_id=sid,
                                               approve=approve))
    assert ('delta', '已取消派发') in events
    replies = [item['result']['outcome'] for item in requests(proc) if 'result' in item and item.get('id') == 0]
    assert replies == [{'outcome': 'selected', 'optionId': 'r'}]


def test_mcp_servers_requires_explicit_stdio_command(tmp_path):
    path = tmp_path / 'mcp.json'
    path.write_text(json.dumps({'mcpServers': {'x': {'command': '/bin/echo', 'args': ['a']}}}))
    assert agy_acp._mcp_servers(path) == [
        {'name': 'x', 'command': '/bin/echo', 'args': ['a'], 'env': []}]
    for bad in ({}, {'mcpServers': {}}, {'mcpServers': {'x': {'command': 1, 'args': []}}},
                {'mcpServers': {'x': {'command': '/bin/echo', 'args': [1]}}}):
        path.write_text(json.dumps(bad))
        with pytest.raises(agy_acp.AcpError, match='agy_mcp_config_invalid'):
            agy_acp._mcp_servers(path)
