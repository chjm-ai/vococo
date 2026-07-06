"""会话级 ClaudeSDKClient 保温池 —— 多轮维持优化 P1-1。

背景:每条消息冷启动一次 claude CLI 子进程 + resume 重放全部历史,会话越长首字
延迟越高。ClaudeSDKClient 本就是流式输入模式(空流 + query),连接不断即可连续
多次 query() —— 于是把 client 按会话养在池里,下一轮直接发新消息,零冷启动。

池结构:{session_key: _Entry(client, base_key, live_sid, last_used)}
- base_key:兼容性哈希(模型/供应商 env/skills/MCP 配置/cwd/系统提示/clarify 路由)。
  任一变化 → 不命中 → 关旧 client 重建,保住 cc-switch「每轮读配置、改完下轮生效」
  的语义;也保证 SDK 内部任务在 connect 时快照的 contextvar(danger cwd / clarify
  路由)与当前轮一致(快照值不变才允许复用)。
- live_sid:该 client 当前活跃的 SDK 会话 id。命中还要求「本轮想 resume 的 sid」与
  它相等 —— /new 会清掉存储的 sid,自然不命中,不会把新会话接到旧上下文上。
- 空闲超 CLIENT_POOL_IDLE_TTL 秒回收(对齐 prompt cache 5 分钟 TTL);TTL≤0 = 禁用。

取用协议:checkout 把条目【弹出】池外(在用的 client 池里不可见,天然防同 key
并发两轮共用一条消息流);轮末干净收工才 checkin 放回,异常/取消一律 discard。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import anyio

from .. import config


@dataclass
class _Entry:
    client: object  # ClaudeSDKClient(不标注类型,便于测试塞 Fake)
    base_key: str  # 兼容性哈希(不含 sid)
    live_sid: str  # 该 client 当前活跃的 SDK 会话 id
    last_used: float  # time.monotonic()


_pool: dict[str, _Entry] = {}


def enabled() -> bool:
    return config.CLIENT_POOL_IDLE_TTL > 0


def _expired(e: _Entry) -> bool:
    return time.monotonic() - e.last_used > config.CLIENT_POOL_IDLE_TTL


async def discard(client: object) -> None:
    """关掉一个 client(断连 + 收尸 CLI 子进程);失败静默 —— 收尸不阻断主流程。"""
    try:
        await client.disconnect()
    except Exception:
        pass


async def checkout(session_key: str, base_key: str, expect_sid: str | None):
    """取出可复用的 client;不匹配/过期则关旧返回 None(调用方走冷启动)。

    命中条件:兼容性哈希一致 且 期望 resume 的 sid == client 活跃 sid 且 未过期。
    条目被弹出池外,用完由调用方 checkin 放回或 discard。
    """
    e = _pool.pop(session_key, None)
    if e is None:
        return None
    if e.base_key != base_key or e.live_sid != (expect_sid or "") or _expired(e):
        await discard(e.client)
        return None
    return e.client


async def checkin(session_key: str, client: object, base_key: str, live_sid: str) -> None:
    """轮末干净收工后放回池里保温;顺手回收过期条目、超容量踢最久未用。"""
    if not enabled() or not live_sid:
        await discard(client)  # 没拿到 sid 的 client 下轮无从命中,养着没意义
        return
    old = _pool.pop(session_key, None)
    if old is not None and old.client is not client:
        await discard(old.client)
    _pool[session_key] = _Entry(client, base_key, live_sid, time.monotonic())
    await sweep()
    while len(_pool) > config.CLIENT_POOL_MAX:
        lru = min(_pool, key=lambda k: _pool[k].last_used)
        await discard(_pool.pop(lru).client)


async def sweep() -> int:
    """回收所有过期条目,返回回收数。"""
    stale = [k for k, e in _pool.items() if _expired(e)]
    for k in stale:
        e = _pool.pop(k, None)
        if e is not None:
            await discard(e.client)
    return len(stale)


async def sweep_loop() -> None:
    """常驻回收循环(serve 的 task group 里跑):定期清空闲超时的 CLI 子进程。"""
    while True:
        await anyio.sleep(60)
        try:
            await sweep()
        except Exception:
            pass


async def close_all() -> None:
    """关掉池里全部 client(serve 停止 / 自我重启前调用,不留孤儿 CLI 进程)。"""
    while _pool:
        _, e = _pool.popitem()
        await discard(e.client)
