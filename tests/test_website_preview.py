"""封面契约、版本与进程隔离；不访问真实发布服务。"""
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess

import pytest
from PIL import Image

from vococo.memory import library
from vococo.tools import builtin, website_preview as preview


def png(color='red'):
    stream = io.BytesIO()
    Image.new('RGB', (20, 20), color).save(stream, format='PNG')
    return stream.getvalue()


def site(version='a'):
    library.remember_website_publication({'source_key': 'publish:local:demo', 'metadata': {'version': version},
                                         'event_key': f'publish:{version}', 'url': 'http://example.com'})
    item = library.register_website('http://example.com', '站点', 'publish:local:demo',
                                    session_key='original', metadata={'version': version},
                                    event_key=f'publish:{version}')
    return item['id']


def test_cover_preserved_without_new_card_or_source(isolated, monkeypatch):
    item_id = site()
    original = library.get_item(item_id)
    captured = []
    async def capture(url, output):
        captured.append(output)
        output.write_bytes(png())
        return {'blocked_requests': 1, 'warning': '外链已阻断'}
    monkeypatch.setattr(preview, '_capture', capture)
    result = asyncio.run(preview.generate_cover(item_id))
    assert result['cover_status'] == 'ready'
    assert not captured[0].exists()
    assert library.cover_file(item_id).read_bytes() == png()
    item = library.get_item(item_id)
    assert item['sources'] == original['sources']
    assert item['agent_id'] == original['agent_id']
    assert item['cover_status'] == 'ready'
    assert len(library.list_items()) == 1


def test_failure_and_retry_preserve_old_cover(isolated, monkeypatch, tmp_path):
    item_id = site()
    old = tmp_path / 'old.png'
    old.write_bytes(png())
    generation = library.begin_website_cover(item_id, 'a')
    library.update_website_cover(item_id, 'a', old, 'ready', {}, expected_generation=generation)
    site('b')
    async def fail(*args):
        raise RuntimeError('缺少浏览器')
    monkeypatch.setattr(preview, '_capture', fail)
    result = asyncio.run(preview.generate_cover(item_id))
    assert result['cover_retry'] == {'id': item_id}
    assert library.cover_file(item_id).read_bytes() == png()
    assert library.get_item(item_id)['cover_status'] == 'pending'
    before = library.get_item(item_id)['sources']
    async def success(url, output):
        output.write_bytes(png('blue'))
        return {}
    monkeypatch.setattr(preview, '_capture', success)
    asyncio.run(builtin.retry_website_cover.handler({'id': item_id}))
    assert library.get_item(item_id)['sources'] == before
    assert library.cover_file(item_id).read_bytes() == png('blue')


@pytest.mark.parametrize('failure', [False, True])
def test_late_old_capture_cannot_overwrite_new_publication(isolated, monkeypatch, tmp_path, failure):
    item_id = site()
    new = tmp_path / 'new.png'
    new.write_bytes(png('blue'))
    async def capture(url, output):
        site('b')
        generation = library.begin_website_cover(item_id, 'b')
        library.update_website_cover(item_id, 'b', new, 'ready', {}, expected_generation=generation)
        if failure:
            raise RuntimeError('旧截图失败')
        output.write_bytes(png())
        return {}
    monkeypatch.setattr(preview, '_capture', capture)
    assert asyncio.run(preview.generate_cover(item_id))['cover_status'] == 'stale'
    assert library.cover_file(item_id).read_bytes() == png('blue')
    assert library.get_item(item_id)['cover_status'] == 'ready'
    assert not library.update_website_cover(item_id, 'a', None, 'pending', {}, expected_generation=1)


@pytest.mark.parametrize('old_outcome', ['ready', 'failed', 'cancelled'])
@pytest.mark.parametrize('new_outcome', ['ready', 'failed'])
def test_same_version_auto_cannot_overwrite_newer_retry(isolated, monkeypatch, old_outcome, new_outcome):
    from vococo.tools import publishing
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        captures = 0
        async def publish(args):
            return {'activation': 'succeeded', 'source_key': 'publish:local:demo',
                    'url': 'http://127.0.0.1:8848/pub/demo/', 'event_key': 'publish:a',
                    'metadata': {'version': 'a', 'http_verification': 'not_checked'}}
        async def capture(url, output):
            nonlocal captures
            captures += 1
            if captures == 1:
                entered.set()
                await release.wait()
                if old_outcome == 'failed':
                    raise RuntimeError('旧任务失败')
                output.write_bytes(png('red'))
                return {'marker': 'old'}
            if new_outcome == 'failed':
                raise RuntimeError('新任务失败')
            output.write_bytes(png('blue'))
            return {'marker': 'new'}
        monkeypatch.setattr(publishing, 'publish', publish)
        monkeypatch.setattr(preview, '_capture', capture)
        auto = asyncio.create_task(builtin.publish_website.handler({'title': '站点'}))
        await entered.wait()
        item_id = library.list_items()[0]['id']
        await builtin.retry_website_cover.handler({'id': item_id})
        newer = library.get_item(item_id)
        if old_outcome == 'cancelled':
            auto.cancel()
        else:
            release.set()
        receipt = await auto
        older_result = json.loads(receipt['content'][0]['text'].split('\n', 1)[1])
        assert older_result['activation'] == 'succeeded'
        assert older_result['cover_status'] == ('pending' if old_outcome == 'cancelled' else 'stale')
        assert library.get_item(item_id) == newer
        assert newer['cover_status'] == ('ready' if new_outcome == 'ready' else 'pending')
        assert len(newer['sources']) == 1
        if new_outcome == 'ready':
            assert library.cover_file(item_id).read_bytes() == png('blue')
        assert captures == 2
    asyncio.run(scenario())


def test_task_generations_increase_and_stale_write_skips_file_read(isolated):
    item_id = site()
    first = library.begin_website_cover(item_id, 'a')
    second = library.begin_website_cover(item_id, 'a')
    assert second > first
    assert not library.update_website_cover(item_id, 'a', '/missing.png', 'ready', {},
                                           expected_generation=first)
    assert not library.update_website_cover(item_id, 'a', None, 'pending', {'error': 'old'},
                                           expected_generation=first)
    assert library.get_item(item_id)['cover_details'] == {}
    site('b')
    assert library.begin_website_cover(item_id, 'a') is None
    assert library.begin_website_cover(item_id, 'b') > second


def test_forged_metadata_cannot_authorize_network(isolated, monkeypatch):
    item = library.register_website('http://169.254.169.254', '', 'publish:local:demo',
                                    metadata={'target': 'local', 'version': 'fake', 'slug': 'demo'})
    async def capture(*args):
        pytest.fail('不可信记录不得联网')
    monkeypatch.setattr(preview, '_capture', capture)
    assert asyncio.run(preview.generate_cover(item['id']))['cover_status'] == 'pending'


@pytest.mark.parametrize('key', ['url:https://example.com', 'publish:local:../x',
                                'publish:kimmy:x/y', 'publish:site:demo', 'publish:local:'])
def test_reject_uncontrolled_target(key):
    with pytest.raises(ValueError):
        preview.preview_url(key)


def test_missing_runtime_never_installs(monkeypatch):
    monkeypatch.setattr(preview.shutil, 'which', lambda _: None)
    with pytest.raises(RuntimeError, match='不会自动安装'):
        preview._runtime()


@pytest.mark.parametrize('cancel', [False, True])
def test_timeout_and_cancel_kill_process_group(tmp_path, monkeypatch, cancel):
    script = tmp_path / 'hang.py'
    script.write_text('import subprocess,time,sys\np=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\nprint(p.pid, flush=True)\ntime.sleep(60)\n')
    import sys
    monkeypatch.setattr(preview, '_runtime', lambda: (sys.executable, {'PATH': os.defpath}))
    monkeypatch.setattr(preview, '_SCRIPT', script)
    monkeypatch.setattr(preview, 'TIMEOUT', .15)
    processes = []
    original = asyncio.create_subprocess_exec
    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    async def run():
        task = asyncio.create_task(preview._capture('unused', tmp_path / 'unused'))
        if cancel:
            await asyncio.sleep(.08)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
    asyncio.run(run())
    assert processes[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)


def test_real_browser_uses_only_isolated_slug_resources(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            hits.append(self.path)
            if self.path.endswith('redirect'):
                self.send_response(302)
                self.send_header('Location', '/private-api')
                self.end_headers()
                return
            body = b'''<html><body style="background: red">isolated
              <img src="ok.png"><img src="/private-api"><img src="/pub/other/private">
              <img src="http://169.254.169.254/latest/meta-data/">
              <iframe src="child.html"></iframe><img src="redirect">
              <script>fetch('/private-fetch');new WebSocket('ws://'+location.host+'/private-ws');
              fetch('file:///etc/passwd');</script></body></html>'''
            if self.path.endswith('child.html'):
                body = b'<script>fetch("/iframe-private")</script>child'
            if self.path.endswith('ok.png'):
                body = png()
            self.send_response(200)
            self.send_header('Content-Type', 'image/png' if self.path.endswith('.png') else 'text/html')
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    from vococo import config
    monkeypatch.setattr(config, 'WEB_PORT', server.server_port)
    output = tmp_path / 'real.png'
    try:
        result = asyncio.run(preview._capture(preview.preview_url('publish:local:demo'), output))
        assert result['blocked_requests'] >= 6
        assert output.is_file()
        assert set(hits) <= {'/pub/demo/', '/pub/demo/ok.png', '/pub/demo/child.html', '/pub/demo/redirect'}
        assert '/pub/demo/' in hits
        assert Image.open(output).size == (1440, 900)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('cancel', [False, True])
def test_real_chrome_cleanup_on_timeout_and_cancel(tmp_path, monkeypatch, cancel):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    arrived, release = threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            arrived.set()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<script>while(true){}</script>')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(preview, 'TIMEOUT', 20)
    # 只延长测试页面的导航期限，保证先命中父进程硬超时而不是网页自身超时。
    runner = tmp_path / 'runner.cjs'
    runner.write_text(preview._SCRIPT.read_text().replace('timeout: 15000', 'timeout: 60000'))
    monkeypatch.setattr(preview, '_SCRIPT', runner)
    output = tmp_path / 'cover.png'
    profile = str(tmp_path / 'profile')
    pids = []
    async def run():
        task = asyncio.create_task(preview._capture(f'http://127.0.0.1:{server.server_port}/pub/demo/', output))
        for _ in range(600):
            if arrived.is_set():
                break
            await asyncio.sleep(.02)
        assert arrived.is_set(), '浏览器未启动'
        listing = subprocess.check_output(['ps', '-axo', 'pid=,command='], text=True)
        pids.extend(int(line.split()[0]) for line in listing.splitlines() if '--user-data-dir=' + profile in line)
        assert pids, '必须验证真实 Chrome，而不是仅 Node'
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
        for _ in range(50):
            alive = []
            for pid in pids:
                try:
                    os.kill(pid, 0)
                    alive.append(pid)
                except ProcessLookupError:
                    pass
            if not alive:
                return
            await asyncio.sleep(.05)
        assert not alive, f'遗留浏览器进程：{alive}'
    try:
        asyncio.run(run())
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def test_failed_collection_can_recover_trusted_cover_without_republish(isolated, monkeypatch):
    receipt = {'source_key': 'publish:local:demo', 'metadata': {'version': 'a'},
               'event_key': 'publish:a', 'url': 'http://127.0.0.1:8848/pub/demo/'}
    library.remember_website_publication(receipt)
    assert not library.list_items()
    item = library.register_website(title='站点', **receipt, session_key='historical', turn_id=2)
    async def capture(url, output):
        output.write_bytes(png())
        return {}
    monkeypatch.setattr(preview, '_capture', capture)
    assert asyncio.run(preview.generate_cover(item['id']))['cover_status'] == 'ready'
    source = library.get_item(item['id'])['sources'][0]
    assert source['session_key'] == 'historical' and source['turn_id'] == 2
    assert len(library.get_item(item['id'])['sources']) == 1


@pytest.mark.parametrize('outcome', ['ready', 'failed', 'cancelled'])
def test_publish_returns_activation_and_collection_despite_cover_failure(isolated, monkeypatch, outcome):
    from vococo.tools import publishing
    calls = []
    async def publish(args):
        calls.append('publish')
        return {'activation': 'succeeded', 'source_key': 'publish:local:demo',
                'url': 'http://127.0.0.1:8848/pub/demo/', 'event_key': 'publish:a',
                'metadata': {'version': 'a', 'http_verification': 'not_checked'}}
    async def capture(url, output):
        calls.append('capture')
        if outcome == 'failed':
            raise RuntimeError('失败')
        if outcome == 'cancelled':
            raise asyncio.CancelledError()
        output.write_bytes(png())
        return {}
    monkeypatch.setattr(publishing, 'publish', publish)
    monkeypatch.setattr(preview, '_capture', capture)
    response = asyncio.run(builtin.publish_website.handler({'title': '站点'}))
    receipt = json.loads(response['content'][0]['text'].split('\n', 1)[1])
    assert receipt['activation'] == 'succeeded'
    assert receipt['library_status'] == 'collected'
    assert receipt['cover_status'] == ('ready' if outcome == 'ready' else 'pending')
    assert calls == ['publish', 'capture']
    assert len(library.get_item(receipt['library_id'])['sources']) == 1


def test_route_policy_and_dns_are_fail_closed():
    node, environment = preview._runtime()
    code = '''
const assert = require('node:assert/strict');
const dns = require('node:dns').promises;
const p = require(process.argv[1]);
const base = new URL('http://127.0.0.1:8848/pub/demo/');
assert(p.allowed(base.href + 'a.js', base));
for (const url of ['http://127.0.0.1:8848/api/token', 'http://127.0.0.1:8848/pub/other/',
 'http://169.254.169.254/latest/meta-data/', 'file:///etc/passwd', 'ws://127.0.0.1:8848/pub/demo/',
 'https://example.com/pub/demo/a', base.href + '%2e%2e/api', base.href + '../other/'])
 assert(!p.allowed(url, base), url);
(async () => {
 for (const ip of ['127.0.0.1','10.1.1.1','169.254.169.254','100.64.0.1','192.168.1.1','::1']) {
  dns.resolve4 = async () => [ip];
  await assert.rejects(p.addressFor(new URL('https://pub.chjm.cc/demo/')));
 }
 dns.resolve4 = async () => ['8.8.8.8'];
 assert.equal(await p.addressFor(new URL('https://pub.chjm.cc/demo/')), '8.8.8.8');
})().catch(error => {console.error(error); process.exitCode=1;});
'''
    subprocess.run([node, '-e', code, str(preview._SCRIPT)], env=environment,
                   check=True, timeout=10, capture_output=True)
