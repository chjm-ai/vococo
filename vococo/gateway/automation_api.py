"""Agent 工作台只读 API，管理操作复用现有定时任务接口。"""
from functools import wraps

from aiohttp import web

from ..core import tasks
from ..cron import scheduler
from ..memory import automation
from .web_auth import check_web_auth


def _endpoint(handler):
    @wraps(handler)
    async def wrapped(request):
        denied = check_web_auth(request)
        if denied is not None:
            denied.headers['Cache-Control'] = 'private, no-store'
            return denied
        try:
            response = web.json_response(await handler(request))
        except KeyError:
            response = web.json_response({'error': '记录不存在'}, status=404)
        except (ValueError, TypeError):
            response = web.json_response({'error': '查询参数无效'}, status=400)
        except Exception:
            request.app.logger.exception('工作台读取失败')
            response = web.json_response({'error': '工作台读取失败，请重试'}, status=500)
        response.headers['Cache-Control'] = 'private, no-store'
        return response
    return wrapped


@_endpoint
async def overview(request):
    jobs = scheduler.load_jobs()
    data = automation.overview(jobs)
    active = {}
    for owner in data['agents']:
        for task in tasks.active_for_agent(owner['id'], [j['id'] for j in jobs if j.get('agent_id') == owner['id']]):
            if task.get('job_id'):
                active[task['job_id']] = task['status']
    for job in data['jobs']:
        job['activity'] = ('running' if scheduler.script_session_busy(
            next((j.get('conv') or 'task:' + j['id'] for j in jobs if j['id'] == job['id']), '')
        ) else active.get(job['id'], ''))
        job['schedule_desc'] = scheduler.describe_schedule(
            next((j.get('schedule') or {} for j in jobs if j['id'] == job['id']), {}))
    return data


@_endpoint
async def runs(request):
    q = request.query
    return automation.runs(q.get('agent', ''), q.get('job', ''), int(q.get('before', '0')))


@_endpoint
async def detail(request):
    return automation.run_detail(int(request.query.get('id', '0')))


@_endpoint
async def artifacts(request):
    q = request.query
    return automation.artifacts(q.get('agent', ''), int(q.get('before', '0')))


def register_routes(app):
    app.add_routes([web.get('/workbench/automation', overview),
                    web.get('/workbench/automation/runs', runs),
                    web.get('/workbench/automation/run', detail),
                    web.get('/workbench/automation/artifacts', artifacts)])
