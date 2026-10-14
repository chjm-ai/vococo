"""下线只在临时目录执行；远端命令和截图严格模拟。"""
import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image

from vococo import config
from vococo.memory import library
from vococo.tools import builtin, publishing, publishing_kimmy as kimmy, website_preview


@pytest.fixture(autouse=True)
def environment(isolated, monkeypatch):
    monkeypatch.setattr(config, 'PUBLISHED_DIR', config.DATA_DIR / 'published')
    monkeypatch.setattr(publishing, '_LOCKS', {})
    async def approve(*args, **kwargs):
        return True
    async def forbidden(*args, **kwargs):
        raise AssertionError('隔离测试禁止真实联网、SSH 和截图')
    monkeypatch.setattr(publishing.danger, 'require_approval', approve)
    monkeypatch.setattr(kimmy, '_command', forbidden)
    monkeypatch.setattr(website_preview, '_capture', forbidden)
    source = isolated / 'source'
    source.mkdir()
    (source / 'index.html').write_text('controlled')
    return {'target': 'local', 'slug': 'demo', 'source_dir': str(source), 'title': '原始标题', 'type': 'static'}


def activate(args):
    receipt = asyncio.run(publishing.publish(args))
    library.remember_website_publication(receipt)
    row = library.register_website(receipt['url'], args['title'], receipt['source_key'],
                                   event_key=receipt['event_key'], metadata=receipt['metadata'])
    return receipt, row


def offline(args, **extra):
    return asyncio.run(publishing.unpublish({**args, **extra}))


def test_local_success_idempotence_and_republish_restores_identity(environment, isolated):
    receipt, item = activate(environment)
    library.update_item(item['id'], title='人工标题', pinned=True, hidden=True)
    other = library.register_website(receipt['url'], '人工标题', 'another-site')
    result = offline(environment)
    backup = Path(result['backup_path'])
    assert result['library_sync'] == 'succeeded'
    assert not backup.is_relative_to(config.PUBLISHED_DIR)
    assert (backup / 'index.html').read_text() == 'controlled'
    assert not (config.PUBLISHED_DIR / 'demo').exists()
    assert library.get_item(item['id']) is None
    assert [row['id'] for row in library.list_items(state='all')] == [other['id']]
    assert offline(environment) == result
    with library._connect() as connection:
        assert connection.execute("SELECT count(*) FROM library_sources WHERE action='unpublished'").fetchone()[0] == 1
    for getter in (library.cover_file, library.image_file, library.thumbnail_data):
        with pytest.raises(KeyError):
            getter(item['id'])
    with pytest.raises(ValueError):
        library.remember_website_publication(receipt)
    with pytest.raises(ValueError):
        library.register_website(receipt['url'], '', receipt['source_key'], event_key=receipt['event_key'])
    with pytest.raises(ValueError):
        library.register_website(receipt['url'], '', receipt['source_key'], metadata={'activation': 'succeeded'})
    new_receipt, restored = activate(environment)
    assert new_receipt['event_key'] != receipt['event_key']
    assert restored['id'] == item['id']
    assert restored['title'] == '人工标题' and restored['pinned'] and restored['hidden']
    with pytest.raises(ValueError):
        library.remember_website_publication(receipt)


@pytest.mark.parametrize('change', ['content', 'unmanaged', 'symlink'])
def test_local_refuses_changed_or_unmanaged_site(environment, change, isolated):
    _, item = activate(environment)
    live = config.PUBLISHED_DIR / 'demo'
    if change == 'content':
        (live / 'index.html').write_text('unknown')
    elif change == 'unmanaged':
        (config.DATA_DIR / 'publish_manifests/demo.json').unlink()
    else:
        live.rename(isolated / 'real')
        live.symlink_to(isolated / 'real', target_is_directory=True)
    with pytest.raises(ValueError):
        offline(environment)
    assert library.get_item(item['id']) is not None
    assert live.exists()


def test_local_absent_does_not_remove_unrelated_card(environment):
    item = library.register_website('https://example.com', 'demo', 'publish:local:demo')
    with pytest.raises(ValueError, match='不存在'):
        offline(environment)
    assert library.get_item(item['id'])


@pytest.mark.parametrize('cancel', [False, True])
def test_approval_denial_or_cancellation_never_changes_site(environment, monkeypatch, cancel):
    _, item = activate(environment)
    async def deny(*args):
        if cancel:
            raise asyncio.CancelledError
        return False
    monkeypatch.setattr(publishing.danger, 'require_approval', deny)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            offline(environment)
    else:
        assert offline(environment)['deactivation'] == 'denied'
    assert library.get_item(item['id'])
    assert (config.PUBLISHED_DIR / 'demo/index.html').is_file()


def test_sync_failure_retry_uses_trusted_state_only(environment, monkeypatch):
    _, item = activate(environment)
    original = library.deactivate_website
    def fail(*args):
        raise OSError('隔离数据库失败')
    monkeypatch.setattr(library, 'deactivate_website', fail)
    result = offline(environment)
    assert result['library_sync'] == 'failed'
    assert library.get_item(item['id']) is None
    assert library.list_items(state='all') == []
    monkeypatch.setattr(library, 'deactivate_website', original)
    monkeypatch.setattr(publishing, '_offline_local', lambda *args: pytest.fail('重试不能再次动站点'))
    assert offline(result['retry'])['library_sync'] == 'succeeded'
    with pytest.raises(ValueError, match='凭据'):
        offline(environment, sync_token='forged')
    activate(environment)
    with pytest.raises(ValueError, match='失效'):
        offline(result['retry'])


def test_private_backup_path_cannot_be_public(environment, monkeypatch):
    _, item = activate(environment)
    backup = config.DATA_DIR / 'publish_backups'
    backup.rmdir()
    backup.symlink_to(config.PUBLISHED_DIR, target_is_directory=True)
    with pytest.raises(ValueError, match='软链接'):
        offline(environment)
    assert library.get_item(item['id'])


def test_late_screenshot_cannot_write_after_unpublish_or_republish(environment, isolated):
    receipt, item = activate(environment)
    version = receipt['metadata']['version']
    generation = library.begin_website_cover(item['id'], version)
    image = isolated / 'late.png'
    Image.new('RGB', (2, 2), 'red').save(image)
    offline(environment)
    assert library.website_preview_record(item['id']) is None
    assert not library.update_website_cover(item['id'], version, image, 'ready', {}, expected_generation=generation)
    activate(environment)
    assert not library.update_website_cover(item['id'], version, image, 'ready', {}, expected_generation=generation)


def test_rejected_publish_preserves_original_receipt_and_preview_retry(environment):
    receipt, item = activate(environment)
    (config.PUBLISHED_DIR / 'demo/index.html').write_text('unregistered')
    with pytest.raises(ValueError, match='未登记'):
        asyncio.run(publishing.publish(environment))
    assert publishing.publication_state(receipt['source_key'])['receipt'] == receipt
    library.remember_website_publication(receipt)
    assert library.begin_website_cover(item['id'], receipt['metadata']['version']) is not None


def test_old_cover_rejected_before_new_publication_is_synced(environment, isolated):
    receipt, item = activate(environment)
    version = receipt['metadata']['version']
    generation = library.begin_website_cover(item['id'], version)
    image = isolated / 'old.png'
    Image.new('RGB', (2, 2), 'red').save(image)
    asyncio.run(publishing.publish(environment))
    assert library.website_preview_record(item['id']) is None
    assert library.begin_website_cover(item['id'], version) is None
    assert not library.update_website_cover(item['id'], version, image, 'ready', {}, expected_generation=generation)


def test_publish_unpublish_share_lock_and_late_receipt_cannot_revive(environment, monkeypatch):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        original = publishing._activate_local
        async def delayed(*args):
            entered.set()
            await release.wait()
            return await original(*args)
        monkeypatch.setattr(publishing, '_activate_local', delayed)
        task = asyncio.create_task(publishing.publish(environment))
        await entered.wait()
        removal = asyncio.create_task(publishing.unpublish(environment))
        await asyncio.sleep(0)
        assert not removal.done()
        release.set()
        receipt, removed = await asyncio.gather(task, removal)
        assert removed['deactivation'] == 'succeeded'
        with pytest.raises(ValueError):
            library.remember_website_publication(receipt)
        assert library.list_items(state='all') == []
    asyncio.run(exercise())


@pytest.mark.parametrize('status', ['denied', 'failed', 'unknown'])
def test_kimmy_non_success_keeps_library_record(environment, monkeypatch, status):
    item = library.register_website('https://pub.chjm.cc/demo/', '站点', 'publish:kimmy:demo')
    async def remote(*args):
        return {'deactivation': status, 'error': '模拟失败'}
    monkeypatch.setattr(kimmy, 'unpublish', remote)
    result = offline({**environment, 'target': 'kimmy'})
    assert result['deactivation'] == status
    assert library.get_item(item['id'])


def test_kimmy_fixed_ssh_and_receipt_whitelist(environment, monkeypatch):
    commands = []
    token = 'a' * 32
    async def command(argv, stdin=None):
        commands.append((argv, stdin.decode()))
        return json.dumps({'deactivation': 'succeeded', 'token': token,
                           'backup_path': f'/opt/deploy/vococo-backups/demo/{token}'})
    monkeypatch.setattr(kimmy, '_command', command)
    result = asyncio.run(kimmy.unpublish('demo'))
    assert result['deactivation'] == 'succeeded'
    argv, script = commands[0]
    assert argv[:2] == ['ssh', '-T'] and 'StrictHostKeyChecking=yes' in argv
    assert argv[-2] == 'Kimmy' and 'unpublish demo ' in argv[-1]
    assert '/etc/nginx/snippets/pub.chjm.cc' in script and 'record.is_file()' in script
    for slug in ['../x', 'x/y', 'x;id', '-x', 'a' * 65]:
        with pytest.raises(ValueError):
            asyncio.run(kimmy.unpublish(slug))
    async def bad(*args, **kwargs):
        return json.dumps({'deactivation': 'succeeded', 'token': token, 'backup_path': '/var/www/demo'})
    monkeypatch.setattr(kimmy, '_command', bad)
    assert asyncio.run(kimmy.unpublish('demo'))['deactivation'] == 'unknown'


def test_republish_registration_retry_recovers_without_remember(environment):
    _, item = activate(environment)
    offline(environment)
    receipt = asyncio.run(publishing.publish(environment))
    restored = library.register_website(receipt['url'], '', receipt['source_key'], event_key=receipt['event_key'])
    assert restored['id'] == item['id']
    assert library.website_preview_record(item['id'])['version'] == receipt['metadata']['version']


def test_activation_state_write_failure_cannot_leave_stale_offline_success(environment, monkeypatch):
    activate(environment)
    offline(environment)
    original = publishing._write_state
    def fail_active(target, slug, state):
        if state['status'] == 'active':
            raise OSError('模拟状态写入失败')
        return original(target, slug, state)
    monkeypatch.setattr(publishing, '_write_state', fail_active)
    assert asyncio.run(publishing.publish(environment))['activation'] == 'unknown'
    assert (config.PUBLISHED_DIR / 'demo/index.html').exists()
    result = offline(environment)
    assert result['deactivation'] == 'succeeded'
    assert not (config.PUBLISHED_DIR / 'demo').exists()


def test_cross_process_site_lock_is_shared(environment):
    import subprocess
    import sys
    async def exercise():
        async with publishing._site_lock('local', 'demo'):
            path = config.DATA_DIR / 'publish_locks/local-demo.lock'
            script = """import fcntl, sys
with open(sys.argv[1], 'a') as stream:
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print('unlocked')
    except BlockingIOError:
        print('locked')
"""
            result = subprocess.run([sys.executable, '-c', script, str(path)],
                                    capture_output=True, text=True, timeout=5)
            assert result.returncode == 0 and result.stdout.strip() == 'locked'
    asyncio.run(exercise())


def test_cover_commit_and_new_activation_have_one_cross_process_order(environment, isolated, monkeypatch):
    import threading
    receipt, item = activate(environment)
    version = receipt['metadata']['version']
    generation = library.begin_website_cover(item['id'], version)
    image = isolated / 'racing.png'
    Image.new('RGB', (2, 2), 'red').save(image)
    checked, release, stored = threading.Event(), threading.Event(), threading.Event()
    original_read, original_activate = library._read_cover, publishing._activate_local
    original_update = library._update_website_cover
    def tracked_commit(*args, **kwargs):
        result = original_update(*args, **kwargs)
        stored.set()
        return result
    monkeypatch.setattr(library, '_update_website_cover', tracked_commit)
    def delayed_read(path):
        checked.set()
        assert release.wait(5)
        return original_read(path)
    async def checked_activation(*args):
        assert stored.is_set(), '激活必须排在已通过校验的封面提交之后'
        return await original_activate(*args)
    monkeypatch.setattr(library, '_read_cover', delayed_read)
    monkeypatch.setattr(publishing, '_activate_local', checked_activation)
    async def exercise():
        async def commit():
            result = await asyncio.to_thread(library.update_website_cover, item['id'], version, image,
                                             'ready', {}, expected_generation=generation)
            stored.set()
            return result
        cover = asyncio.create_task(commit())
        assert await asyncio.to_thread(checked.wait, 3)
        deployment = asyncio.create_task(publishing.publish(environment))
        try:
            await asyncio.sleep(.1)
            assert not deployment.done()
            assert publishing.publication_state(receipt['source_key'])['receipt'] == receipt
        finally:
            release.set()
        assert await asyncio.wait_for(cover, 5)
        assert (await asyncio.wait_for(deployment, 5))['activation'] == 'succeeded'
    asyncio.run(exercise())


def test_waiting_cover_never_holds_db_lock_needed_by_unpublish(environment, isolated):
    receipt, item = activate(environment)
    version = receipt['metadata']['version']
    generation = library.begin_website_cover(item['id'], version)
    image = isolated / 'queued.png'
    Image.new('RGB', (2, 2), 'red').save(image)
    async def exercise():
        async with publishing._site_lock('local', 'demo'):
            cover = asyncio.create_task(asyncio.to_thread(library.update_website_cover,
                item['id'], version, image, 'ready', {}, expected_generation=generation))
            await asyncio.sleep(.1)
            assert not cover.done()
            def remove():
                state = publishing._offline_local('demo', publishing.publication_state(receipt['source_key']))
                return publishing._finish_offline('local', 'demo', state)
            result = await asyncio.wait_for(asyncio.to_thread(remove), 3)
            assert result['library_sync'] == 'succeeded'
        assert not await asyncio.wait_for(cover, 3)
    asyncio.run(exercise())


@pytest.mark.parametrize('phase', ['inspect', 'prepare', 'upload', 'activate'])
def test_kimmy_transport_failure_restores_only_before_activation(environment, monkeypatch, phase):
    key = 'publish:kimmy:demo'
    receipt = {'source_key': key, 'event_key': 'publish:original', 'url': 'https://pub.chjm.cc/demo/',
               'metadata': {'version': 'original'}}
    publishing._write_state('kimmy', 'demo', {'status': 'active', 'receipt': receipt})
    library.remember_website_publication(receipt)
    item = library.register_website(receipt['url'], '', key, event_key=receipt['event_key'])
    async def remote(current, slug, version, *args):
        if current == phase:
            raise RuntimeError('模拟 SSH 断开')
        return {'stage': f'/opt/deploy/vococo-staging/{slug}-{version}'}
    async def command(*args, **kwargs):
        if phase == 'upload':
            raise RuntimeError('模拟上传失败')
        return ''
    monkeypatch.setattr(kimmy, '_remote', remote)
    monkeypatch.setattr(kimmy, '_command', command)
    args = {**environment, 'target': 'kimmy'}
    if phase == 'activate':
        assert asyncio.run(publishing.publish(args))['activation'] == 'unknown'
        assert publishing.publication_state(key)['status'] == 'publishing'
        assert library.website_preview_record(item['id']) is None
    else:
        with pytest.raises(publishing.ActivationNotStartedError):
            asyncio.run(publishing.publish(args))
        assert publishing.publication_state(key)['receipt'] == receipt
        assert library.website_preview_record(item['id'])['version'] == 'original'
        assert library.begin_website_cover(item['id'], 'original') is not None


@pytest.mark.parametrize('phase', ['inspect', 'prepare', 'upload', 'activate'])
@pytest.mark.parametrize('repeat_cancel', [False, True])
def test_kimmy_cancel_restores_only_before_activation_and_still_propagates(environment, monkeypatch,
                                                                           phase, repeat_cancel):
    key = 'publish:kimmy:demo'
    receipt = {'source_key': key, 'event_key': 'publish:original', 'url': 'https://pub.chjm.cc/demo/',
               'metadata': {'version': 'original'}}
    publishing._write_state('kimmy', 'demo', {'status': 'active', 'receipt': receipt})
    library.remember_website_publication(receipt)
    item = library.register_website(receipt['url'], '', key, event_key=receipt['event_key'])
    async def exercise():
        entered, cleaning, never = asyncio.Event(), asyncio.Event(), asyncio.Event()
        phases = []
        async def wait_for_cancel():
            entered.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                if repeat_cancel:
                    cleaning.set()
                    # 模拟 SSH/上传在取消清理期间再次收到取消；不吞掉任一次取消。
                    await never.wait()
                raise
        async def remote(current, slug, version, *args):
            phases.append(current)
            if current == phase:
                await wait_for_cancel()
            return {'stage': f'/opt/deploy/vococo-staging/{slug}-{version}'}
        async def command(*args, **kwargs):
            phases.append('upload')
            if phase == 'upload':
                await wait_for_cancel()
            return ''
        async def capture(url, output):
            Image.new('RGB', (2, 2), 'blue').save(output)
            return {}
        monkeypatch.setattr(kimmy, '_remote', remote)
        monkeypatch.setattr(kimmy, '_command', command)
        monkeypatch.setattr(website_preview, '_capture', capture)
        task = asyncio.create_task(publishing.publish({**environment, 'target': 'kimmy'}))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel('首次取消')
        if repeat_cancel:
            await asyncio.wait_for(cleaning.wait(), 3)
            task.cancel('重复取消')
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(task, 3)
        assert task.cancelled() and task.cancelling() == (2 if repeat_cancel else 1)
        assert str(cancelled.value) == ('重复取消' if repeat_cancel else '首次取消')
        assert not publishing._LOCKS['kimmy:demo'].locked()
        if phase == 'activate':
            assert not isinstance(cancelled.value, publishing.ActivationNotStartedCancelledError)
            assert publishing.publication_state(key)['status'] == 'publishing'
            assert library.website_preview_record(item['id']) is None
        else:
            assert isinstance(cancelled.value, publishing.ActivationNotStartedCancelledError)
            assert 'activate' not in phases
            assert publishing.publication_state(key)['receipt'] == receipt
            library.remember_website_publication(receipt)
            assert library.website_preview_record(item['id'])['version'] == 'original'
            assert (await asyncio.wait_for(website_preview.generate_cover(item['id']), 3))['cover_status'] == 'ready'
    asyncio.run(exercise())


def test_list_drops_item_unpublished_between_filter_and_materialization(environment, monkeypatch):
    activate(environment)
    original = library._item
    triggered = False
    def concurrent_removal(connection, item_id):
        nonlocal triggered
        if not triggered:
            triggered = True
            assert offline(environment)['deactivation'] == 'succeeded'
        return original(connection, item_id)
    monkeypatch.setattr(library, '_item', concurrent_removal)
    assert library.list_items(state='all') == []


def test_tool_returns_sync_only_retry(environment, monkeypatch):
    activate(environment)
    def fail(*args):
        raise OSError('模拟失败')
    monkeypatch.setattr(library, 'deactivate_website', fail)
    result = asyncio.run(builtin.unpublish_website.handler(environment))
    receipt = json.loads(result['content'][0]['text'].split('\n', 1)[1])
    assert receipt['deactivation'] == 'succeeded' and receipt['retry']['sync_token']
