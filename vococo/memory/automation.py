"""工作台只读投影：任务定义、运行记录与成果分开读取，不推测业务阶段。"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
from statistics import median
import time

from .. import config
from . import agents, library, session_store


def metrics(rows: list[dict]) -> dict:
    durations = [r['duration'] for r in rows if r['duration'] > 0]
    token_rows = [r for r in rows if r['tokens'] > 0]
    return {'runs': len(rows), 'ok': sum(r['status'].startswith('success') for r in rows),
            'tokens': sum(r['tokens'] for r in token_rows), 'token_samples': len(token_rows),
            'duration_samples': len(durations),
            'median_duration': median(durations) if durations else None}


def _artifact_groups() -> dict:
    rows = session_store._conn().execute(
        'SELECT a.session_key,COUNT(*) FROM artifacts a JOIN turns t '
        'ON t.id=a.turn_id AND t.session_key=a.session_key GROUP BY a.session_key'
    ).fetchall()
    counts = Counter()
    for key, count in rows:
        owner = session_store._ownership_fields(key).get('agent_id') or ''
        counts[owner] += count
    return dict(counts)


def overview(jobs: list[dict], now: float | None = None) -> dict:
    now = time.time() if now is None else now
    c = agents._conn()
    rows = [dict(zip(('id', 'agent_id', 'job_id', 'ts', 'status', 'tokens', 'duration'), row))
            for row in c.execute('SELECT id,agent_id,job_id,ts,status,tokens,duration FROM agent_runs '
                                 'ORDER BY ts DESC,id DESC')]
    recent_runs = defaultdict(list)
    for row in rows:
        if len(recent_runs[row['agent_id']]) < 24:
            recent_runs[row['agent_id']].append({k: row[k] for k in ('id', 'ts', 'status')})
    recent = [row for row in rows if now - 7 * 86400 <= row['ts'] <= now]
    by_agent, by_job = defaultdict(list), defaultdict(list)
    for row in recent:
        by_agent[row['agent_id']].append(row)
        by_job[row['job_id']].append(row)
    counts = Counter(row['agent_id'] for row in rows)
    job_counts = Counter(row['job_id'] for row in rows)
    artifacts, works = _artifact_groups(), library.visible_counts()
    owners = [{**{k: a[k] for k in ('id', 'name', 'avatar', 'main_conv')},
               'stats': metrics(by_agent[a['id']]), 'runs': counts[a['id']],
               'recent_runs': list(reversed(recent_runs[a['id']])),
               'artifacts': artifacts.get(a['id'], 0), 'works': works.get(a['id'], 0)}
              for a in agents.list_agents()]
    return {'agents': owners, 'jobs': [_public_job(j, metrics(by_job[j['id']]), job_counts[j['id']])
                                      for j in jobs],
            'stats': metrics(recent), 'runs': len(rows), 'artifacts': sum(artifacts.values()),
            'works': sum(works.values()), 'generated_at': now}


def _public_job(job: dict, stats: dict, count: int) -> dict:
    # 概览不回传 prompt、命令、目录或 webhook 密钥；编辑仍走原接口。
    sch = job.get('schedule') or {}
    safe_schedule = {k: sch[k] for k in ('kind', 'expr', 'at', 'seconds', 'minutes') if k in sch}
    return {'id': job['id'], 'name': job.get('name') or '定时任务',
            'agent_id': job.get('agent_id') or '', 'enabled': bool(job.get('enabled')),
            'mode': job.get('mode') or 'agent', 'schedule': safe_schedule,
            'conv': config.conversation_id_for_key(job.get('conv') or 'task:' + job['id']),
            'last_status': job.get('last_status') or '', 'stats': stats, 'runs': count}


def runs(agent_id: str, job_id: str = '', before: int = 0, limit: int = 30) -> dict:
    c = agents._conn()
    # 任务移动后 ID 不变，任务历史应包含转移前的运行；Agent 总历史仍按原归属读取。
    clauses, args = (['job_id=?'], [job_id]) if job_id else (['agent_id=?'], [agent_id])
    if before:
        clauses.append('id<?')
        args.append(before)
    limit = max(1, min(limit, 100))
    rows = c.execute('SELECT id,job_id,job_name,ts,status,text,tokens,duration,tool_calls,agent_id'
                     ' FROM agent_runs WHERE ' + ' AND '.join(clauses) + ' ORDER BY id DESC LIMIT ?',
                     (*args, limit + 1)).fetchall()
    fields = ('id', 'job_id', 'job_name', 'ts', 'status', 'text', 'tokens', 'duration', 'tool_calls', 'agent_id')
    items = [dict(zip(fields, row)) for row in rows[:limit]]
    return {'items': items, 'next': items[-1]['id'] if len(rows) > limit else None}


def _verified_turn(run: dict, turn_id: int | None, *, copied: bool = False):
    if turn_id is None:
        return None
    row = agents._conn().execute(
        'SELECT id,session_key,ts,assistant_text FROM turns WHERE id=? AND ts<=?', (turn_id, run['ts'])
    ).fetchone()
    if not row:
        return None
    digest = hashlib.sha256((row[1] + '\0' + row[3]).encode()).hexdigest()
    if not copied and run.get('source_text_hash'):
        if digest != run['source_text_hash']:
            return None
    elif row[3][:4000] != run['text']:
        return None
    if copied:
        owner = agents.get(run['agent_id'])
        if not owner or row[1] != agents.main_session_key(owner) or abs(row[2] - run['ts']) > .001:
            return None
    return {'id': row[0], 'key': row[1], 'ts': row[2], 'text': row[3]}


def conv_for_key(key: str) -> str:
    conv = 'main' if key == config.SESSION_KEY else config.conversation_id_for_key(key)
    if not (key == config.SESSION_KEY or key.startswith(('session:', 'web:', 'task:', 'voice-chat:'))):
        return ''
    return conv if conv and config.resolve_session_key('web', conv) == key else ''


def run_detail(run_id: int) -> dict:
    c = agents._conn()
    fields = ('id', 'agent_id', 'job_id', 'job_name', 'ts', 'status', 'text',
              'turn_id', 'source_turn_id', 'tokens', 'duration', 'tool_calls', 'source_text_hash')
    row = c.execute('SELECT ' + ','.join(fields) + ' FROM agent_runs WHERE id=?', (run_id,)).fetchone()
    if not row:
        raise KeyError('运行记录不存在')
    run = dict(zip(fields, row))
    source = _verified_turn(run, run['source_turn_id'])
    result = source or _verified_turn(run, run['turn_id'], copied=True)
    run['text'] = result['text'] if result else run['text']
    run.pop('source_text_hash', None)
    run['source_linked'] = bool(source)
    run['target'] = ({'conv': conv_for_key(result['key']), 'turn_id': result['id'], 'ts': result['ts']}
                     if result else None)
    run['tools'] = _tools(source)
    run['artifacts'] = session_store.turn_artifacts(source['key'], source['id']) if source else []
    run['artifact_conv'] = conv_for_key(source['key']) if source else ''
    return run


def _tools(source: dict | None) -> list[dict]:
    if not source:
        return []
    events = session_store.load_turn_events(source['key'], source['id']) or []
    # 只展示工具名称和明确记录的状态，不扩散工具参数、输出或凭据。
    return [{'name': str(e.get('name') or '工具'),
             'status': ('ok' if e.get('ok') else 'error') if 'preview' in e else 'unknown',
             'children': len(e.get('subs') or [])}
            for e in events if isinstance(e, dict) and e.get('type') == 'tool']


def artifacts(agent_id: str, before: int = 0, limit: int = 30) -> dict:
    limit = max(1, min(limit, 100))
    c = session_store._conn()
    rows = c.execute('SELECT a.rowid,a.id,a.filename,a.title,a.size,a.session_key,a.turn_id,t.ts '
                     'FROM artifacts a JOIN turns t ON t.id=a.turn_id AND t.session_key=a.session_key '
                     'WHERE (?=0 OR a.rowid<?) ORDER BY a.rowid DESC', (before, before))
    items = []
    for row in rows:
        owner = session_store._ownership_fields(row[5]).get('agent_id') or ''
        if owner != agent_id:
            continue
        item = dict(zip(('cursor', 'id', 'filename', 'title', 'size', 'key', 'turn_id', 'ts'), row))
        item['conv'] = conv_for_key(item.pop('key'))
        items.append(item)
        if len(items) > limit:
            break
    return {'items': items[:limit], 'next': items[limit - 1]['cursor'] if len(items) > limit else None}
