"""工作台只读投影与真实浏览器回归；所有业务数据均位于临时目录。"""
import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import time

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web
import pytest

from vococo import config
from vococo.core import tasks
from vococo.cron import scheduler
from vococo.gateway import automation_api, settings_store
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import agents, automation, session_store


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def sample(isolated, monkeypatch):
    monkeypatch.setattr(config, 'CRON_JOBS_PATH', config.DATA_DIR / 'cron_jobs.json')
    monkeypatch.setattr(tasks, '_DB', None)
    a = agents.create('甲 Agent')
    b = agents.create('乙 Agent（没有定时任务）')
    j = scheduler.create_job(name='真实测试定时任务', prompt='测试说明',
                             schedule={'kind': 'cron', 'expr': '17 6 * * *'}, agent_id=a['id'])
    key = j['conv']
    tid = session_store.start_turn(key, '测试输入')
    artifact = session_store.append_artifact(key, tid, 'report.html', '测试报告', b'<h1>snapshot</h1>')
    session_store.finish_turn(tid, '完整结果正文', session_key=key, events=[
        {'type': 'tool', 'name': 'Read', 'preview': '敏感输出不应进入概览', 'ok': True,
         'input': {'credential': 'never-expose'}},
        {'type': 'tool', 'name': 'Bash', 'ok': True},
        {'type': 'tool', 'name': 'Write', 'preview': '错误', 'ok': False},
    ])
    agents.record_run(a['id'], j, 'success', '完整结果正文', metrics={'tokens': 120, 'duration': 10}, source_turn_id=tid)
    c = agents._conn()
    for i in range(34):
        c.execute('INSERT INTO agent_runs(agent_id,job_id,job_name,ts,status,text) VALUES (?,?,?,?,?,?)',
                  (a['id'], j['id'], j['name'], time.time() - 100 + i, 'error', f'旧结果 {i}'))
    c.commit()
    yield a, b, j, tid, artifact
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


def test_overview_metrics_order_and_no_sensitive_configuration(sample):
    a, b, j, _, _ = sample
    d = automation.overview(scheduler.load_jobs())
    assert [r['id'] for r in d['agents']] == [r['id'] for r in agents.list_agents()]
    assert d['stats'] == {'runs': 35, 'ok': 1, 'tokens': 120, 'token_samples': 1,
                          'duration_samples': 1, 'median_duration': 10}
    assert d['artifacts'] == 1
    assert next(r for r in d['agents'] if r['id'] == a['id'])['artifacts'] == 1
    assert next(r for r in d['agents'] if r['id'] == b['id'])['runs'] == 0
    assert not {'prompt', 'command', 'cwd'} & d['jobs'][0].keys()
    j['schedule'] = {'kind': 'webhook', 'secret': 'never-expose'}
    assert 'never-expose' not in json.dumps(automation.overview([j]))


def test_recent_run_bars_use_latest_real_records_across_all_history(sample):
    a, b, j, _, _ = sample
    c = agents._conn()
    expected = [dict(zip(('id', 'ts', 'status'), row)) for row in c.execute(
        'SELECT id,ts,status FROM agent_runs WHERE agent_id=? ORDER BY ts DESC,id DESC LIMIT 24', (a['id'],))]
    owners = {r['id']: r for r in automation.overview(scheduler.load_jobs())['agents']}
    assert owners[a['id']]['recent_runs'] == list(reversed(expected))
    assert owners[a['id']]['recent_runs'][-1]['status'] == 'success'
    assert owners[b['id']]['recent_runs'] == []
    # 超过七天的记录仍是最近运行，不能用空白补满或只取统计窗口内的记录。
    old_ts = time.time() - 30 * 86400
    c.execute('INSERT INTO agent_runs(agent_id,job_id,job_name,ts,status,text) VALUES (?,?,?,?,?,?)',
              (b['id'], j['id'], '旧任务', old_ts, 'unknown', '不应进入概览的正文'))
    c.commit()
    owner = next(r for r in automation.overview(scheduler.load_jobs())['agents'] if r['id'] == b['id'])
    assert len(owner['recent_runs']) == 1 and owner['recent_runs'][0]['ts'] == old_ts
    assert owner['recent_runs'][0]['status'] == 'unknown' and owner['stats']['runs'] == 0
    assert set(owner['recent_runs'][0]) == {'id', 'ts', 'status'}


def test_job_run_bars_are_separate_and_include_old_history(sample):
    a, b, j, _, _ = sample
    old = scheduler.create_job(name='很久以前的任务', prompt='隔离测试',
                               schedule={'kind': 'cron', 'expr': '17 6 * * *'}, agent_id=b['id'])
    empty = scheduler.create_job(name='尚未运行的任务', prompt='隔离测试',
                                 schedule={'kind': 'cron', 'expr': '17 6 * * *'}, agent_id=a['id'])
    ts = time.time() - 30 * 86400
    c = agents._conn()
    c.execute('INSERT INTO agent_runs(agent_id,job_id,job_name,ts,status,text) VALUES (?,?,?,?,?,?)',
              (b['id'], old['id'], old['name'], ts, 'unknown', '不应暴露的正文'))
    c.commit()
    jobs = {job['id']: job for job in automation.overview(scheduler.load_jobs())['jobs']}
    expected = [dict(zip(('id', 'ts', 'status'), row)) for row in c.execute(
        'SELECT id,ts,status FROM agent_runs WHERE job_id=? ORDER BY ts DESC,id DESC LIMIT 24', (j['id'],))]
    assert jobs[j['id']]['recent_runs'] == list(reversed(expected))
    assert jobs[empty['id']]['recent_runs'] == [] and jobs[empty['id']]['runs'] == 0
    assert jobs[old['id']]['recent_runs'] == [{'id': c.execute('SELECT MAX(id) FROM agent_runs').fetchone()[0],
                                             'ts': ts, 'status': 'unknown'}]
    assert jobs[old['id']]['stats']['runs'] == 0 and jobs[old['id']]['runs'] == 1
    assert len(jobs[old['id']]['run_days']) == len(jobs[empty['id']]['run_days']) == 30
    assert all(day['runs'] == 0 and day['status'] == 'unknown' for day in jobs[old['id']]['run_days'])
    assert all(day['runs'] == 0 for day in jobs[empty['id']]['run_days'])
    assert '不应暴露的正文' not in json.dumps(jobs)


def test_daily_calendar_boundaries_status_priority_and_missing_metrics():
    now = datetime(2026, 11, 11, 12).timestamp()
    def row(day, status, tokens=0, duration=0):
        return {'ts': datetime.fromisoformat(day).timestamp(), 'status': status,
                'tokens': tokens, 'duration': duration}
    rows = [row('2026-10-12T23:59:59', 'error'), row('2026-10-13T00:00:00', 'success', 120, 10),
            row('2026-11-11T12:00:01', 'error'), row('2026-11-11T11:59:59', 'success'),
            row('2026-11-10T12:00:00', 'success', 80, 20), row('2026-11-10T13:00:00', 'error'),
            row('2026-11-09T12:00:00', 'unknown')]
    days = automation._run_days(rows, now)
    assert len(days) == 30
    assert [d['date'] for d in days] == [(datetime(2026, 10, 13) + timedelta(days=i)).date().isoformat()
                                       for i in range(30)]
    assert days[0]['runs'] == 1 and days[0]['status'] == 'ok' and days[0]['tokens'] == 120
    assert days[-1]['runs'] == 1 and days[-1]['status'] == 'ok'
    assert days[-1]['median_duration'] is None and days[-1]['token_samples'] == 0
    assert days[-2]['status'] == 'error' and days[-2]['ok'] == days[-2]['failed'] == 1
    assert days[-2]['median_duration'] == 20 and days[-2]['tokens'] == 80
    assert days[-3]['status'] == 'unknown' and days[-3]['unknown'] == 1
    assert days[1]['status'] == 'unknown' and days[1]['runs'] == 0 and days[1]['last_ts'] is None
    assert all(d['runs'] == 0 for d in automation._run_days([], now))


def test_cursor_and_exact_source_no_guessed_nodes(sample):
    a, _, j, tid, artifact = sample
    first = automation.runs(a['id'], j['id'])
    assert len(first['items']) == 30 and first['next']
    second = automation.runs(a['id'], j['id'], first['next'])
    assert len(second['items']) == 5 and second['next'] is None
    assert not {r['id'] for r in first['items']} & {r['id'] for r in second['items']}
    old = automation.run_detail(first['items'][0]['id'])
    assert not old['source_linked'] and old['tools'] == [] and old['artifacts'] == []
    linked = automation.run_detail(second['items'][-1]['id'])
    assert linked['source_linked'] and [r['status'] for r in linked['tools']] == ['ok', 'unknown', 'error']
    assert linked['artifacts'][0]['id'] == artifact['id']
    assert 'never-expose' not in json.dumps(linked)
    assert automation.artifacts(a['id'])['items'][0]['conv'] == j['conv']
    # 来源行被删或正文不再匹配时，不能把复用的 ID 当作原工具过程。
    session_store._conn().execute('UPDATE turns SET assistant_text=? WHERE id=?', ('另一条消息', tid))
    session_store._conn().commit()
    assert not automation.run_detail(linked['id'])['source_linked']


def test_completion_keeps_exact_source_despite_summary(sample):
    from vococo.core.completion import Completion

    a, _, j, tid, artifact = sample
    event = Completion(j['conv'], 'done', '只有摘要，不等于完整结果正文', tid, job_id=j['id'])
    scheduler._prepare_completion(event)
    row = automation.runs(a['id'], j['id'])['items'][0]
    detail = automation.run_detail(row['id'])
    assert detail['source_linked'] and detail['text'] == '完整结果正文'
    assert len(detail['tools']) == 3 and detail['artifacts'][0]['id'] == artifact['id']
    # 非定时后台任务也会存摘要，必须保留同样的来源证据。
    agents.record_run(a['id'], {'id': 'standalone', 'name': '独立任务'}, 'success',
                      '另一个摘要', to_main=False, source_turn_id=tid)
    detail = automation.run_detail(automation.runs(a['id'])['items'][0]['id'])
    assert detail['source_linked'] and detail['target']['turn_id'] == tid


def test_rename_preserves_next_run_and_moved_job_history(sample):
    a, b, j, _, _ = sample
    due = time.time() + 60
    jobs = scheduler.load_jobs()
    jobs[0]['schedule'] = {'kind': 'interval', 'minutes': 60}
    jobs[0]['next_run_at'] = due
    scheduler.save_jobs(jobs)
    renamed = scheduler.update_job(j['id'], name='只改名', prompt=j['prompt'],
                                   schedule=jobs[0]['schedule'], target=None)
    assert renamed['next_run_at'] == due
    moved = scheduler.update_job(j['id'], name='移动', prompt=j['prompt'],
                                 schedule=jobs[0]['schedule'], target=None, agent_id=b['id'])
    assert moved['conv'] != j['conv']
    assert len(automation.runs(b['id'], j['id'])['items']) == 30
    assert automation.runs(b['id'])['items'] == []
    d = automation.overview(scheduler.load_jobs())
    assert d['jobs'][0]['runs'] == 35
    expected = [dict(zip(('id', 'ts', 'status'), row)) for row in agents._conn().execute(
        'SELECT id,ts,status FROM agent_runs WHERE job_id=? ORDER BY ts DESC,id DESC LIMIT 24', (j['id'],))]
    assert d['jobs'][0]['recent_runs'] == list(reversed(expected))
    owner = next(o for o in d['agents'] if o['id'] == a['id'])
    assert d['jobs'][0]['run_days'] == owner['run_days']
    assert sum(day['runs'] for day in d['jobs'][0]['run_days']) == 35
    changed = scheduler.update_job(j['id'], name='改计划', prompt=j['prompt'],
                                   schedule={'kind': 'interval', 'minutes': 30}, target=None)
    assert changed['next_run_at'] is None


@pytest.mark.anyio
async def test_api_auth_and_invalid_parameters(sample, monkeypatch):
    monkeypatch.setattr(config, 'WEB_AUTH_TOKEN', 'test-secret')
    app = web.Application()
    automation_api.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        response = await client.get('/workbench/automation')
        assert response.status == 401 and response.headers['Cache-Control'] == 'private, no-store'
        headers = {'X-Auth-Token': 'test-secret'}
        response = await client.get('/workbench/automation', headers=headers)
        assert response.status == 200
        assert (await response.json())['stats']['runs'] == 35
        for query, code in [('run?id=no', 400), ('run?id=999999', 404), ('runs?before=no', 400)]:
            response = await client.get('/workbench/automation/' + query, headers=headers)
            assert response.status == code


@pytest.mark.anyio
async def test_real_browser_workbench(sample, isolated, monkeypatch):
    if not shutil.which('node'):
        pytest.skip('浏览器检查需要 Node')
    env = dict(os.environ, NODE_PATH='/usr/local/lib/node_modules')
    chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    if chrome.exists():
        env['CHROME_PATH'] = str(chrome)
    probe = await asyncio.create_subprocess_exec('node', '-e', "require.resolve('playwright')", env=env,
                                               stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    if await probe.wait():
        pytest.skip('浏览器检查需要已有的 Playwright，不自动安装')
    for name in ('IMAGES_DIR', 'AUDIO_DIR', 'FILES_DIR', 'VIDEOS_DIR', 'PUBLISHED_DIR'):
        monkeypatch.setattr(config, name, config.DATA_DIR / name.lower())
    for name in ('SUGGESTIONS_PATH', 'HEARTBEAT_PATH', 'WATCHDOG_LOG_PATH'):
        monkeypatch.setattr(config, name, config.DATA_DIR / name.lower())
    monkeypatch.setattr(config, 'WEB_HOST', '127.0.0.1')
    monkeypatch.setattr(config, 'WEB_PORT', 0)
    monkeypatch.setattr(config, 'WEB_AUTH_TOKEN', 'isolated-workbench')
    monkeypatch.setattr(config, 'VOICE_ENABLED', False)
    monkeypatch.setattr(config, 'OBSIDIAN_VAULT_DIR', isolated / 'vault')
    monkeypatch.setattr(settings_store, '_PATH', config.DATA_DIR / 'settings.json')
    orphan = {'id': 'deadbeef', 'name': '历史未归属任务', 'prompt': '隔离测试',
              'agent_id': 'f00d00dead', 'conv': config.new_session_key(),
              'schedule': {'kind': 'once', 'run_at': time.time() + 86400}, 'enabled': False}
    scheduler.save_jobs([*scheduler.load_jobs(), orphan])
    c = agents._conn()
    c.execute('INSERT INTO agent_runs(agent_id,job_id,job_name,ts,status,text) VALUES (?,?,?,?,?,?)',
              (orphan['agent_id'], orphan['id'], orphan['name'], time.time(), 'success', '未归属任务的历史'))
    c.commit()
    adapter = WebAdapter()
    await adapter._start_server()
    port = next(iter(adapter._runner.sites))._server.sockets[0].getsockname()[1]
    a, b, j, _, _ = sample
    try:
        process = await asyncio.create_subprocess_exec('node', str(Path(__file__).with_name('automation_workbench_browser.cjs')),
            f'http://127.0.0.1:{port}', config.WEB_AUTH_TOKEN, a['id'], b['id'], j['id'], str(isolated),
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 180)
        assert process.returncode == 0, stderr.decode() + stdout.decode()
        assert json.loads(stdout)['ok']
    finally:
        await adapter._runner.cleanup()
        await asyncio.sleep(.05)
