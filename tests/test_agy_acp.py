import asyncio
import json
from pathlib import Path

import pytest

from vococo.gateway import agy_acp, agy_trial


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


def test_settings_open_native_tools_but_keep_task_approval():
    data = json.loads(agy_trial.SETTINGS_ACP)
    assert data['allowNonWorkspaceAccess'] is True
    assert data['toolPermission'] == 'always-proceed'
    assert sorted(data['permissions']['ask']) == [
        'mcp(vococo-local/vococo_cancel_task)',
        'mcp(vococo-local/vococo_continue_task)',
        'mcp(vococo-local/vococo_create_task)',
    ]


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
async def test_tool_updates_stream_start_and_end():
    proc = Proc([
        {'jsonrpc': '2.0', 'id': 1, 'result': {}},
        {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 's', 'update': {
            'sessionUpdate': 'tool_call', 'toolCallId': 't1', 'name': 'run_command',
            'status': 'in_progress', 'title': 'echo hi', 'kind': 'execute',
            'rawInput': {'CommandLine': 'echo hi'}}}},
        {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 's', 'update': {
            'sessionUpdate': 'tool_call_update', 'toolCallId': 't1', 'name': 'run_command',
            'status': 'completed', 'rawOutput': {'output': 'hi\n', 'exit_code': 0}}}},
        {'jsonrpc': '2.0', 'id': 2, 'result': {'stopReason': 'end_turn'}},
    ])
    client = agy_acp._Client(proc, set())
    assert await agy_acp._result(client.request('initialize', {})) == {}
    events = await collect(client.request('session/prompt', {}))
    tools = [payload for kind, payload in events if kind == 'tool']
    assert tools[0]['phase'] == 'start' and tools[0]['input']['CommandLine'] == 'echo hi'
    assert tools[1]['phase'] == 'end' and tools[1]['exit_code'] == 0 and 'hi' in tools[1]['output']


@pytest.mark.anyio
async def test_replay_suppresses_tool_updates():
    proc = Proc([
        {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 's', 'update': {
            'sessionUpdate': 'tool_call', 'toolCallId': 't1', 'name': 'run_command',
            'status': 'completed', 'rawInput': {'CommandLine': 'echo hi'}}}},
        {'jsonrpc': '2.0', 'id': 1, 'result': {'sessionId': 's'}},
    ])
    client = agy_acp._Client(proc, set())
    events = await collect(client.request('session/load', {'sessionId': 's'}, replay=True))
    assert [kind for kind, _ in events] == ['result']


def test_native_tool_mapping_matches_web_cards():
    assert agy_trial._AGY_TOOL_NAMES['run_command'] == 'Bash'
    assert agy_trial._agy_tool_input('run_command', {
        'CommandLine': 'ls', 'toolSummary': '列目录'}) == {'command': 'ls', 'description': '列目录'}
    ok, preview, detail = agy_trial._agy_tool_result('run_command', 'completed', 'a\nb', 0)
    assert ok and preview == 'a' and detail == 'a\nb'
    ok2, _, _ = agy_trial._agy_tool_result('run_command', 'completed', 'x', 1)
    assert not ok2
    # 本机 ACP 实测的文件工具入参：Path / Content / Target / Replacement
    assert agy_trial._agy_tool_input('write_file', {
        'Path': '/tmp/a', 'Content': 'new', 'Overwrite': True}) == {
        'file_path': '/tmp/a', 'content': 'new'}
    assert agy_trial._agy_tool_input('edit_file', {
        'Path': '/tmp/a', 'Target': 'old', 'Replacement': 'new'}) == {
        'file_path': '/tmp/a', 'old_string': 'old', 'new_string': 'new'}
    assert agy_trial._agy_tool_input('read_file', {'Path': '/tmp/a'}) == {'file_path': '/tmp/a'}
    # 零输出命令不能把时间戳噪声当 preview
    _, preview, detail = agy_trial._agy_tool_result(
        'run_command', 'completed',
        'Created At: x\nCommand exited with code 0.\n\nNo output from the command.', 0)
    assert preview == '' and detail == ''


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
async def test_stream_turn_creates_session_and_streams_reply(tmp_path, monkeypatch, capsys):
    sid = '3596d05e-5d0e-47a7-b5a8-77aee32011d2'
    workspace = tmp_path / 'workspace'
    (workspace / '.agents').mkdir(parents=True)
    (workspace / '.agents/mcp_config.json').write_text(json.dumps({'mcpServers': {
        'vococo-context-test': {'command': '/bin/echo', 'args': ['serve']}}}))
    proc = Proc([
        {'jsonrpc': '2.0', 'id': 1, 'result': {}},
        {'jsonrpc': '2.0', 'id': 2, 'result': {'sessionId': sid}},
        {'jsonrpc': '2.0', 'id': 3, 'result': {}},
        {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': sid, 'update': {
            'sessionUpdate': 'agent_thought_chunk', 'content': {'type': 'text', 'text': '先看目录'}}}},
        update('新回复'),
        {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': sid, 'update': {
            'sessionUpdate': 'tool_call', 'toolCallId': 't1', 'name': 'run_command',
            'status': 'in_progress', 'rawInput': {'CommandLine': 'echo hi'}}}},
        {'jsonrpc': '2.0', 'id': 4, 'result': {'stopReason': 'end_turn'}},
    ])

    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        return proc

    async def terminate(_proc):
        return None

    monkeypatch.setattr(agy_acp.agy_trial, 'spawn_cli', spawn)
    monkeypatch.setattr(agy_acp.agy_trial, 'terminate', terminate)
    monkeypatch.setattr(agy_acp.agy_trial, 'cli_binary', lambda: Path('/bin/true'))
    events = await collect(agy_acp.stream_turn(workspace, 10, 'gemini-x', '你好', turn_id=42))
    assert ('session', sid) in events and ('delta', '新回复') in events and ('done', None) in events
    log = capsys.readouterr().out
    assert 'turn_id=42 outcome=ok stage=session/prompt' in log and 'finish_received=True' in log
    assert "'session/new':" in log and 'first_text_s=' in log
    assert '你好' not in log and '新回复' not in log and 'echo hi' not in log
    assert any(kind == 'tool' and payload['phase'] == 'start' for kind, payload in events)
    assert ('thinking', '先看目录') in events
    argv = calls[0][0]
    assert '--sandbox' not in argv
    assert argv[argv.index('--mode') + 1] == 'accept-edits'
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


@pytest.mark.anyio
async def test_model_and_effort_use_config_options(tmp_path, monkeypatch):
    sid = '3596d05e-5d0e-47a7-b5a8-77aee32011d2'
    workspace = tmp_path / 'workspace'
    (workspace / '.agents').mkdir(parents=True)
    (workspace / '.agents/mcp_config.json').write_text(json.dumps({'mcpServers': {
        'vococo-context-test': {'command': '/bin/echo', 'args': ['serve']}}}))
    config = [
        {'id': 'model', 'type': 'select', 'currentValue': 'gemini-4-neon',
         'options': [{'value': 'gemini-4-neon', 'name': 'Gemini 4 Neon'},
                     {'value': 'gemini-4-argon', 'name': 'Gemini 4 Argon'}]},
        {'id': 'thought_level', 'type': 'select', 'currentValue': 'high',
         'options': [{'value': x, 'name': x} for x in ('low', 'medium', 'high', 'xhigh')]},
    ]
    proc = Proc([
        {'jsonrpc': '2.0', 'id': 1, 'result': {}},
        {'jsonrpc': '2.0', 'id': 2, 'result': {'sessionId': sid, 'configOptions': config}},
        {'jsonrpc': '2.0', 'id': 3, 'result': {'configOptions': config}},
        {'jsonrpc': '2.0', 'id': 4, 'result': {'configOptions': config}},
        update('好的'),
        {'jsonrpc': '2.0', 'id': 5, 'result': {'stopReason': 'end_turn'}},
    ])

    async def spawn(*args, **kwargs):
        return proc

    async def terminate(_proc):
        return None

    monkeypatch.setattr(agy_acp.agy_trial, 'spawn_cli', spawn)
    monkeypatch.setattr(agy_acp.agy_trial, 'terminate', terminate)
    monkeypatch.setattr(agy_acp.agy_trial, 'cli_binary', lambda: Path('/bin/true'))
    events = await collect(agy_acp.stream_turn(workspace, 10, 'gemini-4-neon', '你好', effort='xhigh'))
    assert ('delta', '好的') in events
    sent = requests(proc)
    by_method = [item for item in sent if item.get('method') == 'session/set_config_option']
    assert [item['params']['configId'] for item in by_method] == ['model', 'thought_level']
    assert [item['params']['value'] for item in by_method] == ['gemini-4-neon', 'xhigh']
    assert not any(item.get('method') == 'session/set_model' for item in sent)


@pytest.mark.anyio
async def test_variant_only_model_options_pick_matching_effort(tmp_path, monkeypatch):
    sid = '3596d05e-5d0e-47a7-b5a8-77aee32011d2'
    workspace = tmp_path / 'workspace'
    (workspace / '.agents').mkdir(parents=True)
    (workspace / '.agents/mcp_config.json').write_text(json.dumps({'mcpServers': {
        'vococo-context-test': {'command': '/bin/echo', 'args': ['serve']}}}))
    config = [
        {'id': 'model', 'type': 'select', 'currentValue': 'gemini-4-neon-high',
         'options': [{'value': 'gemini-4-neon-high', 'name': 'Gemini 4 Neon (High)'},
                     {'value': 'gemini-4-neon-low', 'name': 'Gemini 4 Neon (Low)'}]},
        {'id': 'thought_level', 'type': 'select', 'currentValue': 'high',
         'options': [{'value': 'low', 'name': 'Low'}, {'value': 'high', 'name': 'High'}]},
    ]
    proc = Proc([
        {'jsonrpc': '2.0', 'id': 1, 'result': {}},
        {'jsonrpc': '2.0', 'id': 2, 'result': {'sessionId': sid, 'configOptions': config}},
        {'jsonrpc': '2.0', 'id': 3, 'result': {'configOptions': config}},
        {'jsonrpc': '2.0', 'id': 4, 'result': {'configOptions': config}},
        {'jsonrpc': '2.0', 'id': 5, 'result': {'stopReason': 'end_turn'}},
    ])

    async def spawn(*args, **kwargs):
        return proc

    async def terminate(_proc):
        return None

    monkeypatch.setattr(agy_acp.agy_trial, 'spawn_cli', spawn)
    monkeypatch.setattr(agy_acp.agy_trial, 'terminate', terminate)
    monkeypatch.setattr(agy_acp.agy_trial, 'cli_binary', lambda: Path('/bin/true'))
    await collect(agy_acp.stream_turn(workspace, 10, 'gemini-4-neon', '你好', effort='low'))
    sent = requests(proc)
    values = [item['params']['value'] for item in sent
              if item.get('method') == 'session/set_config_option']
    assert values[:2] == ['gemini-4-neon-low', 'low']


@pytest.mark.anyio
async def test_unavailable_family_raises_model_unavailable(tmp_path, monkeypatch):
    sid = '3596d05e-5d0e-47a7-b5a8-77aee32011d2'
    workspace = tmp_path / 'workspace'
    (workspace / '.agents').mkdir(parents=True)
    (workspace / '.agents/mcp_config.json').write_text(json.dumps({'mcpServers': {
        'vococo-context-test': {'command': '/bin/echo', 'args': ['serve']}}}))
    config = [{'id': 'model', 'type': 'select', 'currentValue': 'gemini-4-argon',
               'options': [{'value': 'gemini-4-argon', 'name': 'Gemini 4 Argon'}]},
              {'id': 'thought_level', 'type': 'select', 'currentValue': 'high',
               'options': [{'value': 'high', 'name': 'High'}]}]
    proc = Proc([
        {'jsonrpc': '2.0', 'id': 1, 'result': {}},
        {'jsonrpc': '2.0', 'id': 2, 'result': {'sessionId': sid, 'configOptions': config}},
    ])

    async def spawn(*args, **kwargs):
        return proc

    async def terminate(_proc):
        return None

    monkeypatch.setattr(agy_acp.agy_trial, 'spawn_cli', spawn)
    monkeypatch.setattr(agy_acp.agy_trial, 'terminate', terminate)
    monkeypatch.setattr(agy_acp.agy_trial, 'cli_binary', lambda: Path('/bin/true'))
    with pytest.raises(agy_acp.AcpError, match='agy_model_unavailable'):
        await collect(agy_acp.stream_turn(workspace, 10, 'gemini-4-neon', '你好', effort='high'))
    assert not any(item.get('method') == 'session/prompt' for item in requests(proc))


@pytest.mark.anyio
@pytest.mark.parametrize('levels, expected', [(['low'], ['new', 'low']), ([], ['new']), (None, ['new'])])
async def test_model_switch_uses_only_fresh_effort_options(levels, expected):
    config = {'configOptions': [
        {'id': 'model', 'currentValue': 'old', 'options': [{'value': 'old'}, {'value': 'new'}]},
        {'id': 'thought_level', 'options': [{'value': 'xhigh'}]},
    ]}
    fresh = {} if levels is None else {'configOptions': [
        {'id': 'thought_level', 'options': [{'value': level} for level in levels]},
    ]}
    messages = [{'jsonrpc': '2.0', 'id': 1, 'result': fresh}]
    if levels:
        messages.append({'jsonrpc': '2.0', 'id': 2, 'result': {}})
    proc = Proc(messages)
    await agy_acp._apply_model(agy_acp._Client(proc, set()), 's', config, 'new', 'xhigh')
    assert [item['params']['value'] for item in requests(proc)] == expected


@pytest.mark.anyio
@pytest.mark.parametrize('last, error', [
    ({'id': '1', 'result': {'stopReason': 'end_turn'}}, 'agy_response_mismatch'),
    ({'id': 2, 'result': {'stopReason': 'end_turn'}}, 'agy_response_mismatch'),
    ({'id': 1, 'result': {}}, 'agy_finish_invalid'),
    ({'id': 1, 'result': {'stopReason': 'error'}}, 'agy_finish_invalid'),
    ({'id': 1, 'result': {'stopReason': 'max_tokens'}}, 'agy_finish_limit'),
    (None, 'agy_finish_invalid'),
])
async def test_prompt_end_errors_keep_partial_text(last, error):
    proc = Proc([update('部分正文'), *([{'jsonrpc': '2.0', **last}] if last else [])])
    client = agy_acp._Client(proc, set())
    events = []
    async with asyncio.timeout(10) as deadline:
        with pytest.raises(agy_acp.AcpError, match=error):
            async for event in agy_acp._stream_prompt(client, 's', '合成验证', deadline, deadline.when(), 0):
                events.append(event)
    assert events == [('delta', '部分正文')]
    assert client.trace.finish_received is (last is not None and last['id'] == 1)


@pytest.mark.anyio
async def test_new_attempt_does_not_reset_elapsed_time(tmp_path):
    started_at = asyncio.get_running_loop().time() - 361
    # 预算已经用完，不检查不存在的配置，也不再启动 CLI。
    with pytest.raises(agy_acp.AcpError, match='agy_timeout'):
        await collect(agy_acp.stream_turn(tmp_path, 360, 'new', '合成验证',
                                          started_at=started_at, max_timeout=720, progress_window=90))


@pytest.mark.anyio
@pytest.mark.parametrize('when', ['before', 'after', 'unchanged'])
async def test_legacy_model_effort_uses_current_options(when):
    options = [{'id': 'thought_level', 'options': [{'value': 'low'}]}]
    result = {'models': {'currentModelId': 'new'}}
    ack = {'jsonrpc': '2.0', 'id': 1, 'result': {}}
    notice = {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 's', 'update': {
        'sessionUpdate': 'config_option_update', 'configOptions': options,
    }}}
    if when == 'unchanged':
        result['configOptions'] = options
        messages = [ack]
    else:
        messages = [notice, ack] if when == 'before' else [ack, notice]
    messages.append({'jsonrpc': '2.0', 'id': 2, 'result': {}})
    proc = Proc(messages)
    await agy_acp._apply_model(agy_acp._Client(proc, set()), 's', result, 'new', 'low')
    assert [item['method'] for item in requests(proc)] == ['session/set_model', 'session/set_config_option']
    assert requests(proc)[-1]['params']['value'] == 'low'


@pytest.mark.anyio
async def test_close_during_model_selection_is_retryable():
    proc = Proc([])
    with pytest.raises(agy_acp.AcpError, match='agy_model_unavailable'):
        await agy_acp._apply_model(agy_acp._Client(proc, set()), 's', {}, 'new', 'low')
    assert [item['method'] for item in requests(proc)] == ['session/set_model']


@pytest.mark.anyio
async def test_progress_extension_rolls_but_stays_below_hard_cap():
    start = asyncio.get_running_loop().time()
    async with asyncio.timeout_at(start + 360) as deadline:
        assert not agy_acp._extend_for_progress(deadline, start + 269, start + 720, 90)
        assert deadline.when() == start + 360
        assert agy_acp._extend_for_progress(deadline, start + 351, start + 720, 90)
        assert deadline.when() == pytest.approx(start + 441)
        for step in (431, 511, 591, 671):
            assert agy_acp._extend_for_progress(deadline, start + step, start + 720, 90)
        assert deadline.when() == start + 720
        assert not agy_acp._extend_for_progress(deadline, start + 719, start + 720, 90)
    async with asyncio.timeout_at(start + 360) as deadline:
        assert not agy_acp._extend_for_progress(deadline, start + 360, start + 720, 90)


@pytest.mark.anyio
@pytest.mark.parametrize('kind, status, extends', [
    ('agent_message_chunk', '', True), ('agent_thought_chunk', '', False),
    ('tool_call', 'completed', True), ('tool_call', 'in_progress', False),
])
async def test_only_text_and_tool_results_extend_prompt(kind, status, extends):
    proc = Proc([
        {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 's', 'update': {
            'sessionUpdate': kind, 'content': {'type': 'text', 'text': '示例'},
            'toolCallId': 'tool-1', 'name': 'Read', 'status': status,
        }}},
        {'jsonrpc': '2.0', 'id': 1, 'result': {'stopReason': 'end_turn'}},
    ])
    client = agy_acp._Client(proc, set())
    async with asyncio.timeout(10) as deadline:
        initial = deadline.when()
        await collect(agy_acp._stream_prompt(client, 's', '合成验证', deadline, initial + 30, 20))
        assert (deadline.when() > initial) is extends
    assert client.trace.extended is extends


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
