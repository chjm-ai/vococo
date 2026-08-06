"""租户上下文:每个请求/任务解析出 tenant_id,经 ContextVar 全链路传递。

personal 模式恒为 LOCAL_TENANT(单用户占位,路径与改造前完全一致);
server 模式由三个入口注入,覆盖全部会碰到租户数据的代码路径:
1. Web 请求中间件(gateway/adapters/web.py 的 _auth_mw,PR3)
2. cron 触发(cron/scheduler.py 的 _run_job)
3. 后台任务引擎(core/task_runner.py 执行入口)

server 模式下 current() 取到 LOCAL_TENANT 直接抛错(fail-closed):
说明某条代码路径忘了注入——宁可报错,也不能静默把租户数据写进共享位置。
"""
from __future__ import annotations

from contextvars import ContextVar, Token

from .. import config

# 单用户占位租户:personal 模式所有路径解析都落到它,保证行为与改造前一致。
LOCAL_TENANT = "local"

_current: ContextVar[str] = ContextVar("vococo_tenant", default=LOCAL_TENANT)


class TenantContextError(RuntimeError):
    """server 模式下租户上下文缺失。"""


def current() -> str:
    """当前租户 id。personal 模式恒返回 LOCAL_TENANT;server 模式缺失即抛 TenantContextError。"""
    tid = _current.get()
    if config.IS_SERVER and tid == LOCAL_TENANT:
        raise TenantContextError(
            "server 模式下租户上下文缺失(该代码路径未经请求中间件/调度器注入 tenant_id)"
        )
    return tid


def safe_current() -> str | None:
    """不抛错版本:personal 恒 LOCAL_TENANT;server 无上下文返回 None。

    给「拿不到租户时正确做法是静默不投递/不执行,而不是炸掉事件循环」的调用点用
    (如 SSE 广播 _emit:宁可这帧谁都不发,也不能跨租户发,更不能崩)。
    """
    tid = _current.get()
    if config.IS_SERVER and tid == LOCAL_TENANT:
        return None
    return tid


def set(tid: str) -> Token:  # noqa: A001 —— 与 reset() 成对,沿用 contextvars 命名习惯
    """注入当前租户,返回 Token 供 reset() 复原(请求/任务结束必须成对调用)。"""
    return _current.set(tid)


def reset(token: Token) -> None:
    _current.reset(token)
