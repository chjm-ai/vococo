import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.gateway import agy_handoff, agy_trial, settings_store
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import agents, session_store


@pytest.mark.anyio
async def test_auth_and_current_agent_scope(isolated, monkeypatch):
    src = isolated / 'project'
    src.mkdir()
    (src / 'AGENTS.md').write_text('project-rule')
    (src / '.env').write_text('must-not-copy')
    for aid in ('general', 'abc123'):
        home = agents.home(aid)
        home.mkdir(parents=True)
        (home / 'AGENT.md').write_text(f'role:{aid}')
    project = {'id': 'abc123', 'workdir': str(src)}
    seen = []

    def owner(key):
        seen.append(key)
        return project

    single = isolated / 'skills' / 'standalone'
    single.mkdir(parents=True)
    (single / 'SKILL.md').write_text('one file')
    scoped = isolated / 'skills' / 'has-helper'
    scoped.mkdir()
    (scoped / 'SKILL.md').write_text('skill')
    (scoped / 'helper.py').write_text('helper')
    monkeypatch.setattr(config, 'WEB_AUTH_TOKEN', 'test-web')
    monkeypatch.setattr(agy_trial, 'cli_binary', lambda: Path('/bin/sh'))
    monkeypatch.setattr(agy_trial, 'accepts_session', lambda _: True)
    monkeypatch.setattr(agents, 'agent_for_session', owner)
    monkeypatch.setattr(agents, 'runtime_for_session', lambda _: {'skills': ['standalone', 'has-helper'], 'mcp': ['api']})
    monkeypatch.setattr(session_store, 'get_external_mcp_names', lambda _: [])
    monkeypatch.setattr(settings_store, '_SKILLS_DIR', single.parent)
    monkeypatch.setattr(settings_store, 'effective_external_mcp', lambda _: {'api': {'headers': {'Authorization': 'DO-NOT-COPY'}}})
    session_store.bind_session('session:qa', agent_id='abc123')
    app = web.Application()
    adapter = WebAdapter()
    app.router.add_post('/agy-handoff', adapter._handle_agy_handoff)
    app.router.add_get('/models', adapter._handle_models)
    async with TestClient(TestServer(app)) as client:
        assert (await client.post('/agy-handoff', json={'conv': 'session:qa'})).status == 401
        client.session.headers['X-Auth-Token'] = 'test-web'
        assert (await client.post('/agy-handoff', json={'conv': 'task:qa'})).status == 400
        assert (await client.post('/agy-handoff', json=[])).status == 400
        draft = await (await client.get('/models?conv=session:draft')).json()
        assert draft['agy_handoff'] is False
        assert (await client.post('/agy-handoff', json={'conv': 'session:draft'})).status == 400
        response = await client.post('/agy-handoff', json={'conv': 'session:qa'})
        assert response.status == 200, await response.text()
        data = await response.json()
    assert seen == ['session:qa']
    assert (data['global_agent_id'], data['agent_id']) == ('general', 'abc123')
    assert data['native_skills'] == ['standalone'] and data['unmapped_skills'] == ['has-helper']
    assert data['unmapped_vococo_mcp'] == ['api']
    w = Path(data['workspace'])
    assert w.stat().st_mode & 0o777 == 0o700
    assert (w / 'context/GLOBAL-AGENT.md').read_text() == 'role:general'
    assert (w / 'context/AGENT.md').read_text() == 'role:abc123'
    assert (w / 'context/PROJECT-RULES.md').read_text() == 'project-rule'
    assert 'DO-NOT-COPY' not in str(data) and not list(w.rglob('.env'))
    assert 'api' not in json.loads((w / '.agents/mcp_config.json').read_text())['mcpServers']


def test_command_preserves_multiline_text_without_zsh_expansion(tmp_path):
    node, zsh = shutil.which('node'), shutil.which('zsh')
    if not node or not zsh:
        pytest.skip('需要 Node 与 zsh')
    js = Path('vococo/gateway/adapters/web_static/composer.js').read_text()
    helpers = js[js.index('function quoteAgyArg'):js.index('async function copyAgyCommand')]
    workspace = tmp_path / "space ' $"
    workspace.mkdir()
    sample = "- 中文\n$(printf injected) `printf injected` ' \" ! \\ \t后续" + chr(0x2028) + '末尾'
    js_cmd = helpers + '\nprocess.stdout.write(agyShellCommand(process.argv[1], process.argv[2]));'
    command = subprocess.check_output([node, '-e', js_cmd, str(workspace), sample], text=True)
    assert len(command.splitlines()) == 1
    fake_agy = "agy() { printf '%s\\n' \"$PWD\"; printf '%s' \"${@[-1]}\"; }; "
    result = subprocess.check_output([zsh, '-f', '-c', fake_agy + command],
                                     encoding='utf-8', env={**os.environ, 'LC_ALL': 'C'})
    assert result == f'{workspace}\n--prompt-interactive={sample}'
    blank = subprocess.check_output([node, '-e', js_cmd, str(workspace), ''], text=True)
    assert '--prompt-interactive' not in blank
