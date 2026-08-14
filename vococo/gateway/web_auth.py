"""Web 入口统一的口令校验(X-Auth-Token 头;hmac 常量时间比较)。

web.py 与 task_routes.py 共用这一个实现:token 校验规则只此一处,改规则不用改两头。
"""
from __future__ import annotations

import hmac

from aiohttp import web

from .. import config


def check_web_auth(request: web.Request, *, allow_query_token: bool = False) -> web.Response | None:
    """通过返回 None;未授权返回 401 Response(handler 直接 return 它)。

    只认请求头,不收 ?token= query:query 会进 cloudflared 访问日志 / 浏览器历史 /
    Referer,泄露即交出控制权(审计 #3 / 2-3)。唯一例外是无法自定义请求头的 SSE
    EventSource(allow_query_token=True),仅 /tasks/stream 使用。
    未设 WEB_AUTH_TOKEN = 不校验(仅本机调试;非本机绑定时启动已 fail-closed)。
    """
    if not config.WEB_AUTH_TOKEN:
        return None
    token = request.headers.get("X-Auth-Token") or ""
    if allow_query_token:
        token = token or request.query.get("token") or ""
    if hmac.compare_digest(token, config.WEB_AUTH_TOKEN):
        return None
    return web.json_response({"error": "unauthorized"}, status=401)
