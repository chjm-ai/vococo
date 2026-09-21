"""Web 入口统一的口令校验(X-Auth-Token 头;hmac 常量时间比较)。

web.py 与 task_routes.py 共用这一个实现:token 校验规则只此一处,改规则不用改两头。
"""
from __future__ import annotations

import hashlib
import hmac
import time

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


# ── 媒体票据(scoped ticket)────────────────────────────────────────────────
# <video src> 带不了自定义请求头,但视频又必须能被浏览器当普通 URL 直接拉(否则只能
# 整段 fetch 成 blob 才能播:几十 MB 全下完才起播、还拖不动进度条)。这里签一张
# 【只对某一个文件名有效、且会过期】的票据放进 query。
#
# 跟"把 WEB_AUTH_TOKEN 放 query"是两回事,别混为一谈:全局口令泄露 = 交出整个 agent
# 的控制权(能执行任意代码);这张票据泄露的上限是【那一个视频文件】,而且到期自动作废。
# 密钥就是 WEB_AUTH_TOKEN 本身,但只以 HMAC 派生值出现,原值不进 URL/日志。
_TICKET_TTL = 12 * 3600  # 有效期:够一次长会话浏览;过期后刷新页面就会拿到新票据
# exp 按小时对齐,不用"此刻+12h"这个每秒都在变的值:/history 的 ETag 是整包正文的
# md5,exp 一变 URL 就变、ETag 跟着变,带视频的会话会永远命中不了 304(那是切会话
# 回看时最大的一笔流量)。对齐到小时后,同一小时内重复请求拿到的是同一串 URL。
_TICKET_BUCKET = 3600


def sign_media_ticket(kind: str, name: str, *, now: float | None = None) -> str:
    """给 kind(如 "video")下的某个文件名签一张票,返回 "&exp=..&sig=.." 查询串片段。

    未设 WEB_AUTH_TOKEN(本机调试、本就不校验)时返回空串,URL 保持原样。
    """
    if not config.WEB_AUTH_TOKEN:
        return ""
    base = now if now is not None else time.time()
    exp = int(base // _TICKET_BUCKET * _TICKET_BUCKET) + _TICKET_TTL
    return f"&exp={exp}&sig={_media_sig(kind, name, exp)}"


def check_media_ticket(request: web.Request, kind: str, name: str) -> web.Response | None:
    """媒体取流专用闸:先走正常的请求头鉴权,没过再验票据。通过返回 None,否则 401。"""
    if check_web_auth(request) is None:
        return None
    try:
        exp = int(request.query.get("exp") or 0)
    except ValueError:
        exp = 0
    sig = request.query.get("sig") or ""
    if exp > time.time() and hmac.compare_digest(sig, _media_sig(kind, name, exp)):
        return None
    return web.json_response({"error": "unauthorized"}, status=401)


def _media_sig(kind: str, name: str, exp: int) -> str:
    # 把 kind/name/exp 全签进去:换个文件名或改过期时间,签名立刻对不上
    msg = f"{kind}:{name}:{exp}".encode()
    return hmac.new(config.WEB_AUTH_TOKEN.encode(), msg, hashlib.sha256).hexdigest()[:32]
