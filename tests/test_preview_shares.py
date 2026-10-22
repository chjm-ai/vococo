"""限时预览的真实 HTTP、私有存储与浏览器边界。"""
import asyncio
import hashlib
import os
from pathlib import Path
import sqlite3
import time

from aiohttp import ClientSession
import pytest

from vococo import config
from vococo.gateway.adapters.web import WebAdapter
from vococo.gateway import preview_shares as shares
from test_library_integration import _browser_environment


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
async def server(isolated, monkeypatch):
    monkeypatch.setattr(config, 'WEB_HOST', '127.0.0.1')
    monkeypatch.setattr(config, 'WEB_PORT', 0)
    monkeypatch.setattr(config, 'WEB_AUTH_TOKEN', 'preview-share-test-auth')
    monkeypatch.setattr(config, 'VOICE_ENABLED', False)
    adapter = WebAdapter()
    await adapter._start_server()
    yield f'http://127.0.0.1:{adapter._runner.addresses[0][1]}'
    await adapter._runner.cleanup()


@pytest.mark.anyio
async def test_share_http_auth_hash_expiry_limits(server, monkeypatch):
    auth = {'X-Auth-Token': config.WEB_AUTH_TOKEN}
    async with ClientSession() as client:
        for method, path, data in [('POST', '/api/preview-shares', {'html': '<h1>hello</h1>'}), ('DELETE', '/api/preview-shares/unknown', None), ('GET', '/api/preview-shares', None)]:
            async with client.request(method, server + path, json=data) as result:
                assert result.status == 401
        async with client.post(server + '/api/preview-shares', headers={**auth, 'Origin': 'https://evil.example'}, json={'html': 'test'}) as result:
            assert result.status == 403
        for value in ['', 123, '<p>' + '中' * shares.MAX_BYTES, None, '\ud800']:
            async with client.post(server + '/api/preview-shares', headers=auth, json={'html': value}) as result:
                assert result.status == 400
        async with client.post(server + '/api/preview-shares', headers=auth, json=[]) as result:
            assert result.status == 400
        async with client.post(server + '/api/preview-shares', headers=auth, json={'html': '<h1>hello</h1>'}) as result:
            assert result.status == 200
            created = await result.json()
            assert result.headers['Cache-Control'] == 'no-store'
        token = created['path'].split('#')[1]
        assert len(token) == 43 and config.WEB_AUTH_TOKEN not in created['path']
        assert time.time() + shares.TTL - 10 < created['expires'] <= time.time() + shares.TTL
        db_path = config.DATA_DIR / 'private' / 'preview-shares.sqlite3'
        assert token.encode() not in db_path.read_bytes()
        with sqlite3.connect(db_path) as db:
            assert db.execute('SELECT token_hash FROM shares').fetchone()[0] == hashlib.sha256(token.encode()).hexdigest()
        async with client.post(server + '/preview-share/content', json={'token': token}) as result:
            assert result.status == 200 and '<h1>hello</h1>' in await result.text()
            for key, expected in [('Cache-Control', 'no-store'), ('Referrer-Policy', 'no-referrer')]:
                assert result.headers[key] == expected
            assert 'noindex' in result.headers['X-Robots-Tag']
            assert 'sandbox' in result.headers['Content-Security-Policy']
        for invalid in [token[:-1], 'x' * 43, [], None]:
            async with client.post(server + '/preview-share/content', json={'token': invalid}) as result:
                assert result.status == 404
        monkeypatch.setattr(shares, 'MAX_SHARES', 1)
        async with client.post(server + '/api/preview-shares', headers=auth, json={'html': 'another'}) as result:
            assert result.status == 429
        async with client.get(server + '/api/preview-shares', headers=auth) as result:
            listed = await result.json()
            assert listed['shares'][0]['id'] == created['id']
            assert token not in str(listed)
        async with client.delete(server + '/api/preview-shares/' + created['id'], headers=auth) as result:
            assert result.status == 200
        async with client.post(server + '/preview-share/content', json={'token': token}) as result:
            assert result.status == 404
        async with client.post(server + '/api/preview-shares', headers=auth, json={'html': 'expires'}) as result:
            second = await result.json()
        assert second['path'] != created['path']
        with sqlite3.connect(db_path) as db:
            db.execute('UPDATE shares SET expires = 0')
        async with client.post(server + '/preview-share/content', json={'token': second['path'].split('#')[1]}) as result:
            assert result.status == 404
        async with client.get(server + '/api/preview-shares', headers=auth) as result:
            assert (await result.json())['shares'] == []


@pytest.mark.anyio
async def test_share_browser(server, isolated):
    screenshots = Path(os.environ.get('PREVIEW_SCREENSHOTS', str(isolated)))
    screenshots.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec('node', str(Path(__file__).with_name('preview_shares_browser.cjs')), server, config.WEB_AUTH_TOKEN, str(screenshots), env=_browser_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    assert process.returncode == 0, stderr.decode() + stdout.decode()
    print(stdout.decode())
