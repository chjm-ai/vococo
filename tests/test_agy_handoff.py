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
    unsafe = isolated / 'skills' / 'unsafe-config'
    unsafe.mkdir()
    (unsafe / 'SKILL.md').write_text('requires local configuration')
    (unsafe / 'config.json').write_text('not exported')
    own = config.AI_BRAIN_DIR / 'memory' / 'project'
    own.mkdir(parents=True)
    (config.AI_BRAIN_DIR / 'MEMORY.md').write_text('private-preamble\n## 允许\nshared-marker\n## 不允许\nprivate-marker\n')
    (own / 'INDEX.md').write_text('own-marker')
    monkeypatch.setattr(config, 'WEB_AUTH_TOKEN', 'test-web')
    monkeypatch.setattr(agy_trial, 'cli_binary', lambda: Path('/bin/sh'))
    monkeypatch.setattr(agy_trial, 'accepts_session', lambda _: True)
    monkeypatch.setattr(agents, 'agent_for_session', owner)
    monkeypatch.setattr(agents, 'runtime_for_session', lambda _: {
        'skills': ['standalone', 'has-helper', 'not-found', 'unsafe-config'], 'mcp': ['api'],
        'memory_sections': ['允许'], 'memory_dir': str(own),
    })
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
        # 已绑定会话忽略客户端提供的其他归属。
        response = await client.post('/agy-handoff', json={'conv': 'session:qa', 'agent_id': 'general'})
        assert response.status == 200, await response.text()
        data = await response.json()
    assert seen == ['session:qa']
    assert (data['global_agent_id'], data['agent_id']) == ('general', 'abc123')
    assert data['native_skills'] == ['has-helper', 'standalone']
    assert data['unmapped_skills'] == ['not-found', 'unsafe-config']
    assert data['unmapped_vococo_mcp'] == ['api']
    w = Path(data['workspace'])
    assert w.stat().st_mode & 0o777 == 0o700
    assert (w / 'context/GLOBAL-AGENT.md').read_text() == 'role:general'
    assert (w / 'context/AGENT.md').read_text() == 'role:abc123'
    assert (w / 'context/PROJECT-RULES.md').read_text() == 'project-rule'
    assert 'DO-NOT-COPY' not in str(data) and not list(w.rglob('.env'))
    assert (w / '.agents/skills/has-helper/helper.py').read_text() == 'helper'
    memory = (w / 'context/MEMORY.md').read_text()
    assert 'shared-marker' in memory and 'private-marker' not in memory and 'private-preamble' not in memory
    link = own.with_name('another-project')
    link.symlink_to(own, target_is_directory=True)
    assert agy_handoff._memory_docs({'memory_dir': str(link), 'memory_sections': ['不存在']}) == {}
    assert (w / 'context/AGENT-MEMORY.md').read_text() == 'own-marker'
    assert list(json.loads((w / '.agents/mcp_config.json').read_text())['mcpServers']) == data['local_mcp']


def test_prepare_into_refreshes_context_and_hides_removed_skills(tmp_path, monkeypatch):
    source = tmp_path / 'project'
    source.mkdir()
    (source / 'AGENTS.md').write_text('project-rule')
    workspace = tmp_path / 'workspaces' / 'agy-handoff' / 'session-test'
    (workspace / '.agents/skills/current').mkdir(parents=True)
    (workspace / '.agents/skills/removed').mkdir(parents=True)
    (workspace / '.agents/skills/removed/SKILL.md').write_text('旧技能')
    (workspace / 'context').mkdir()
    (workspace / 'context/MEMORY.md').write_text('旧记忆')
    docs = {'.agents/skills/current/SKILL.md':
            '---\nname: current\ndescription: 当前技能。\n---\n正文'}
    meta = {'agent_id': 'abc123', 'global_agent_id': 'general', 'native_skills': ['current'],
            'unmapped_skills': [], 'unmapped_vococo_mcp': [], 'memory_files': []}
    monkeypatch.setattr(agy_handoff, '_context',
                        lambda key, draft_owner=None: ({'id': 'abc123'}, source, dict(docs), dict(meta)))
    result = agy_handoff.prepare_into(workspace, 'session:x')
    assert result['skills'] == [{'name': 'current', 'description': '当前技能。'}]
    assert not (workspace / '.agents/skills/removed').exists()
    assert not (workspace / 'context/MEMORY.md').exists()
    assert (workspace / 'context/PROJECT-RULES.md').read_text() == 'project-rule'
    agents_md = (workspace / 'AGENTS.md').read_text()
    assert 'current' in agents_md and 'read_skill' in agents_md and 'read_context_file' in agents_md
    config = json.loads((workspace / '.agents/mcp_config.json').read_text())
    assert next(iter(config['mcpServers'])).startswith('vococo-context-')
    assert (workspace / 'context/cli-settings.json').is_file()


@pytest.mark.anyio
async def test_unbound_draft_can_prepare_current_project_without_saving_chat(isolated, monkeypatch):
    project = isolated / 'project'
    project.mkdir()
    (project / 'AGENTS.md').write_text('draft rules')
    own = {'id': 'abc123', 'project_hash': 'abc999', 'workdir': str(project),
           'execution_mode': 'project', 'skills': [], 'mcp': [], 'memory_sections': []}
    monkeypatch.setattr(config, 'WEB_AUTH_TOKEN', 'test-web')
    monkeypatch.setattr(agy_trial, 'cli_binary', lambda: Path('/bin/sh'))
    monkeypatch.setattr(agents, 'get', lambda aid: own if aid == own['id'] else None)
    monkeypatch.setattr(session_store, 'path_for_hash', lambda phash: str(project) if phash == 'abc999' else None)
    key = 'session:bf3275bece8745009bd6c1e1e2363ad3'
    app = web.Application()
    adapter = WebAdapter()
    app.router.add_get('/models', adapter._handle_models)
    app.router.add_post('/agy-handoff', adapter._handle_agy_handoff)
    app.router.add_get('/conv/git', adapter._handle_conv_git)
    async with TestClient(TestServer(app), headers={'X-Auth-Token': 'test-web'}) as client:
        git = await client.get('/conv/git', params={'conv': key})
        assert git.status == 200 and await git.json() == {'is_project': False}
        models = await (await client.get('/models', params={'conv': key})).json()
        assert models['agy_handoff'] is True
        assert (await client.post('/agy-handoff', json={'conv': key})).status == 400
        valid = {'conv': key, 'agent_id': 'abc123', 'project_hash': 'abc999'}
        for bad in ({**valid, 'project_hash': 'other'}, {**valid, 'agent_id': 'missing'}):
            assert (await client.post('/agy-handoff', json=bad)).status == 400
        response = await client.post('/agy-handoff', json=valid)
        assert response.status == 200, await response.text()
        snapshot = await response.json()
        assert snapshot['agent_id'] == 'abc123'
        assert (Path(snapshot['workspace']) / 'context/PROJECT-RULES.md').read_text() == 'draft rules'
        assert not session_store.session_binding(key) and not session_store.load_recent(key)
        after = await (await client.get('/models', params={'conv': key})).json()
        assert (after['default'], after['chosen_model']) == (models['default'], models['chosen_model'])

        def concurrent_bind(session_key, *, draft_owner):
            session_store.bind_session(session_key, agent_id='general')
            return {'workspace': '/isolated/old-agent', 'agent_id': draft_owner['id']}

        monkeypatch.setattr(agy_handoff, 'prepare', concurrent_bind)
        stale = await client.post('/agy-handoff', json=valid)
        assert stale.status == 409 and 'workspace' not in await stale.json()


def test_cli_mcp_permissions_do_not_override_user_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    local = 'vococo-context-fixture'
    assert agy_handoff._cli_settings(local) == {
        'allowNonWorkspaceAccess': False, 'permissions': {'allow': [
            f'mcp({local}/read_agent_context)', f'mcp({local}/read_agent_memory)',
            f'mcp({local}/read_context_file)', f'mcp({local}/list_skills)', f'mcp({local}/read_skill)',
        ]},
    }
    path = tmp_path / '.gemini/antigravity-cli/settings.json'
    path.parent.mkdir(parents=True)
    path.write_text('{"permissions":{"deny":["mcp(*)"]}}')
    assert agy_handoff._cli_settings(local) == {'allowNonWorkspaceAccess': False}


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


def test_website_context_reports_actual_external_mcp_state(tmp_path):
    folder = tmp_path / "context"
    folder.mkdir()
    snapshot = folder / "agent-context.json"
    snapshot.write_text('{"unmapped_vococo_mcp":["dataforseo","xpoz"]}', encoding="utf-8")
    meta = {"agent_id": "general", "unmapped_vococo_mcp": ["dataforseo", "xpoz"]}
    agy_handoff.record_external_mcp(tmp_path, meta, {"dataforseo": "initialize_failed", "xpoz": "mounted"})
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["requested_external_mcp"] == ["dataforseo", "xpoz"]
    assert payload["mounted_external_mcp"] == ["xpoz"]
    assert payload["unmapped_vococo_mcp"] == ["dataforseo"]
    assert payload["external_mcp_status"]["dataforseo"] == "initialize_failed"
    agy_handoff.record_external_mcp(tmp_path, meta, {"dataforseo": "mounted", "xpoz": "timeout"})
    assert json.loads(snapshot.read_text(encoding="utf-8"))["mounted_external_mcp"] == ["dataforseo"]


def test_external_mcp_snapshot_rejects_symlinked_workspace_and_bounds_names(tmp_path):
    real = tmp_path / "real"
    (real / "context").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        agy_handoff.record_external_mcp(alias, {}, {"visible": "mounted"})
    assert not (real / "context" / "agent-context.json").exists()
    oversized_name = "字" * 50_000
    agy_handoff.record_external_mcp(real, {"unmapped_vococo_mcp": [oversized_name]},
                                   {oversized_name: "not_eligible", "visible": "mounted"})
    meta = json.loads((real / "context" / "agent-context.json").read_text())
    assert meta["requested_external_mcp"] == ["visible"]
    assert meta["mounted_external_mcp"] == ["visible"]
