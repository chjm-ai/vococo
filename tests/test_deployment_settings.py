"""配置加载及发布安全边界；所有数据与进程都是隔离替身。"""
import asyncio
import copy
import json
import shlex

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from deployment_fixture import deployment_config
from vococo import config, deployment_settings as settings
from vococo.tools import publishing, publishing_kimmy as kimmy, website_preview


def test_origins_use_configuration_without_caching(deployment_config):
    assert settings.brain_origin() == 'https://mcp.example.com'
    assert settings.share_origin() == 'https://share.example.com'
    deployment_config['brain']['origin'] = 'https://other.example.com:8443/'
    assert settings.brain_origin() == 'https://other.example.com:8443'


@pytest.mark.parametrize('value', [None, '', 'http://example.com', 'https://user@example.com',
                                 'https://example.com/path', 'https://example.com?x=y',
                                 'https://example.com#x', 'https://example.com:99999',
                                 'https://example.com:0', 'https://example.com\n'])
def test_invalid_origins_fail_closed(deployment_config, value):
    deployment_config['brain']['origin'] = value
    deployment_config['publishing']['share_origin'] = value
    with pytest.raises(ValueError):
        settings.brain_origin()
    with pytest.raises(ValueError):
        settings.share_origin()


@pytest.mark.parametrize('field,value', [
    ('ssh_host', '-oProxyCommand=bad'), ('ssh_host', 'fake;command'),
    ('deploy_root', '/'), ('deploy_root', '//srv/fake'), ('deploy_root', '/srv/../etc'),
    ('web_root', '/srv/fake-deploy/nested'), ('snippet_root', '/srv/fake-www'),
    ('script_sha256', ''), ('script_sha256', 'a' * 63),
    ('public_origin', 'https://pub.example.com:8443'),
    ('contract_lines', []), ('contract_lines', ['URL_MODE=path'] * 4),
])
def test_invalid_target_never_connects(deployment_config, field, value, monkeypatch):
    deployment_config['publishing']['kimmy'][field] = value
    async def forbidden(*args, **kwargs):
        pytest.fail('配置无效不得启动 SSH')
    monkeypatch.setattr(kimmy, '_command', forbidden)
    with pytest.raises(ValueError):
        asyncio.run(kimmy._remote('inspect', 'sample', 'a' * 32))


@pytest.mark.parametrize('line', ['DOMAIN=other.example.com', 'URL_MODE=subdomain',
                                'SSL_KEY=/etc/letsencrypt/live/other.example.com/privkey.pem',
                                'SSL_CERT=/tmp/$(command)/fullchain.pem'])
def test_contract_cannot_silently_drop_old_restrictions(deployment_config, line):
    lines = deployment_config['publishing']['kimmy']['contract_lines']
    key = line.split('=')[0]
    lines[:] = [line if item.startswith(key + '=') else item for item in lines]
    with pytest.raises(ValueError):
        settings.kimmy_target()


def test_missing_config_and_reader_errors_are_not_suppressed(monkeypatch):
    monkeypatch.setattr(settings, '_section', lambda _: {})
    for load in (settings.brain_origin, settings.share_origin, settings.kimmy_target):
        with pytest.raises(ValueError):
            load()
    def broken(_):
        raise ValueError('无效实例配置')
    monkeypatch.setattr(settings, '_section', broken)
    with pytest.raises(ValueError, match='无效实例配置'):
        settings.kimmy_target()


def test_remote_uses_json_arguments_and_unchanged_script(deployment_config, monkeypatch):
    target = settings.kimmy_target()
    manifest = {"quote'\\\".html": 'f' * 64}
    async def command(argv, stdin=None):
        assert argv[-2] == target['ssh_host']
        remote = shlex.split(argv[-1])
        assert remote[:7] == ['sudo', '-n', 'python3', '-', 'inspect', 'sample', 'a' * 32]
        assert json.loads(remote[7]) == target
        assert json.loads(remote[8]) == manifest
        assert stdin == kimmy._REMOTE.encode()
        return '{"state": {}}'
    monkeypatch.setattr(kimmy, '_command', command)
    assert asyncio.run(kimmy._remote('inspect', 'sample', 'a' * 32, manifest)) == {'state': {}}


@pytest.mark.parametrize('legacy', [False, True])
def test_changed_target_or_legacy_record_never_unpublishes(isolated, deployment_config, monkeypatch, legacy):
    original = settings.kimmy_target()
    state = {'status': 'active', 'receipt': {'metadata': {}}}
    if not legacy:
        state['deployment_fingerprint'] = settings.target_fingerprint(original)
    publishing._write_state('kimmy', 'sample', state)
    deployment_config['publishing']['kimmy']['ssh_host'] = 'other-publisher'
    async def forbidden(*args, **kwargs):
        pytest.fail('目标绑定不符不得审批或连接远端')
    monkeypatch.setattr(kimmy, '_command', forbidden)
    monkeypatch.setattr(publishing.danger, 'require_approval', forbidden)
    with pytest.raises(ValueError, match='未绑定'):
        asyncio.run(publishing.unpublish({'target': 'kimmy', 'slug': 'sample'}))
    with pytest.raises(ValueError, match='未绑定'):
        website_preview.preview_url('publish:kimmy:sample')


def test_fingerprint_covers_all_target_fields_and_ignores_contract_order():
    original = settings.kimmy_target()
    changed = copy.deepcopy(original)
    changed['web_root'] = '/srv/other-www'
    assert settings.target_fingerprint(changed) != settings.target_fingerprint(original)
    assert settings.kimmy_target()['contract_lines'] == sorted(original['contract_lines'])


def test_changed_target_blocks_republish(isolated, deployment_config, monkeypatch):
    source = isolated / 'site'
    source.mkdir()
    (source / 'index.html').write_text('public')
    publishing._write_state('kimmy', 'sample', {'status': 'active',
        'deployment_fingerprint': settings.target_fingerprint(settings.kimmy_target())})
    deployment_config['publishing']['kimmy']['deploy_root'] = '/srv/other-deploy'
    async def approved(*args):
        return True
    async def forbidden(*args):
        pytest.fail('不得连接新目标')
    monkeypatch.setattr(publishing.danger, 'require_approval', approved)
    monkeypatch.setattr(kimmy, '_command', forbidden)
    with pytest.raises(ValueError, match='未绑定'):
        asyncio.run(publishing.publish({'target': 'kimmy', 'slug': 'sample', 'source_dir': str(source)}))


def test_share_receipt_uses_only_controlled_origin(isolated, deployment_config, monkeypatch):
    from vococo.gateway import preview_shares
    from vococo.gateway.adapters.web import _security_mw
    from vococo.gateway import web_auth
    monkeypatch.setattr(config, 'WEB_AUTH_TOKEN', 'fake-auth-token')
    async def exercise():
        app = web.Application(middlewares=[_security_mw])
        preview_shares.register_routes(app)
        async with TestClient(TestServer(app)) as client:
            headers = {'X-Auth-Token': 'fake-auth-token', 'X-Forwarded-Host': 'evil.example.com'}
            response = await client.post('/api/preview-shares?origin=https://evil.example.com',
                json={'html': '<p>public</p>', 'origin': 'https://evil.example.com'}, headers=headers)
            assert response.status == 200
            receipt = await response.json()
            assert receipt['url'] == 'https://share.example.com' + receipt['path']
            assert response.headers['Cache-Control'] == 'no-store'
            response = await client.post('/api/preview-shares', json={'html': 'x'},
                headers={**headers, 'Origin': 'https://evil.example.com'})
            assert response.status == 403
            deployment_config['publishing'].pop('share_origin')
            response = await client.post('/api/preview-shares', json={'html': 'x'}, headers=headers)
            assert response.status == 503
            response = await client.get('/api/preview-shares', headers=headers)
            assert len((await response.json())['shares']) == 1
    asyncio.run(exercise())


def test_migrated_legacy_record_only_matches_original_target(deployment_config):
    target = settings.kimmy_target()
    deployment_config['publishing']['legacy_kimmy_fingerprint'] = settings.target_fingerprint(target)
    legacy = {'status': 'active', 'receipt': {'metadata': {}}}
    settings.require_target_binding(legacy, target)
    changed = {**target, 'ssh_host': 'other-publisher'}
    with pytest.raises(ValueError, match='未绑定'):
        settings.require_target_binding(legacy, changed)
    with pytest.raises(ValueError, match='未绑定'):
        settings.require_target_binding({'deployment_fingerprint': 'wrong'}, target)


def test_publish_receives_approved_target_snapshot(isolated, deployment_config, monkeypatch):
    source = isolated / 'site'
    source.mkdir()
    (source / 'index.html').write_text('public')
    target = settings.kimmy_target()
    async def approved(*args):
        return True
    async def fake_publish(src, slug, manifest, *, target):
        assert target == original
        return {'activation': 'failed'}
    original = copy.deepcopy(target)
    monkeypatch.setattr(publishing.danger, 'require_approval', approved)
    monkeypatch.setattr(kimmy, 'publish', fake_publish)
    assert asyncio.run(publishing.publish({'target': 'kimmy', 'slug': 'sample', 'source_dir': str(source)}))['activation'] == 'failed'


def test_configuration_change_during_approval_requires_new_approval(isolated, deployment_config, monkeypatch):
    source = isolated / 'site'
    source.mkdir()
    (source / 'index.html').write_text('public')
    async def change(*args):
        deployment_config['publishing']['kimmy']['ssh_host'] = 'changed'
        return True
    async def forbidden(*args, **kwargs):
        pytest.fail('变更配置后不得上传')
    monkeypatch.setattr(publishing.danger, 'require_approval', change)
    monkeypatch.setattr(kimmy, 'publish', forbidden)
    with pytest.raises(ValueError, match='重新审批'):
        asyncio.run(publishing.publish({'target': 'kimmy', 'slug': 'sample', 'source_dir': str(source)}))
