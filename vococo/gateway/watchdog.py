"""事件循环假死看门狗。

背景(2026-07-21):serve 进程假死约 70 分钟——进程活着、TCP 能握手,但事件循环
被某个同步调用卡死,所有请求(连 /history 都)挂起,最后靠系统级超时"运气恢复"。
run.sh 守护循环只能兜"进程退出"这一种死法,兜不住"活着但不干活"。

本模块补这个洞,两级响应:
1. 循环无响应 ≥ WATCHDOG_DUMP_SEC:把全线程堆栈 dump 进 watchdog.log——
   这次排查最缺的就是"卡在哪一行"的证据,先留证据;
2. 无响应 ≥ WATCHDOG_EXIT_SEC:os._exit 自杀,拉起交给 run.sh(和 restart_self
   同一条复活路径)。此时循环已死透,任何"优雅退出"都执行不了,只能硬退。

机制:beat_loop 协程在事件循环里每 2s 刷新时间戳;独立 daemon 线程只做"看表",
不碰任何锁/loop API,循环卡死也不影响它判断。
"""
from __future__ import annotations

import faulthandler
import os
import threading
import time

import anyio

from .. import config

_BEAT_INTERVAL = 2  # 循环里刷时间戳的间隔(秒)
_CHECK_INTERVAL = 5  # 线程看表的间隔(秒)

_last_beat = time.monotonic()


async def beat_loop() -> None:
    """跑在事件循环里:循环还活着就不断刷新心跳时间戳。"""
    global _last_beat
    while True:
        _last_beat = time.monotonic()
        await anyio.sleep(_BEAT_INTERVAL)


def start_thread() -> None:
    """起看门狗 daemon 线程(进程退出时自动跟着死,无需清理)。"""
    threading.Thread(target=_watch, daemon=True, name="loop-watchdog").start()


def _log(msg: str) -> "object":
    """追加写 watchdog.log 并同步落盘(自杀前必须保证证据已写进磁盘)。"""
    path = config.WATCHDOG_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")
    f.write(f"[watchdog] {time.strftime('%F %T')} {msg}\n")
    f.flush()
    os.fsync(f.fileno())
    return f


def _watch() -> None:
    dumped = False
    last_check = time.monotonic()
    while True:
        time.sleep(_CHECK_INTERVAL)
        now = time.monotonic()
        # 整机睡眠补偿:自己这个线程的间隔也异常拉长 = 系统刚睡醒,不是循环卡死。
        # 跳过本轮,给 beat_loop 一个先跑起来刷新时间戳的机会,避免醒来就误杀。
        if now - last_check > _CHECK_INTERVAL * 3:
            last_check = now
            continue
        last_check = now
        lag = now - _last_beat
        if lag < config.WATCHDOG_DUMP_SEC:
            dumped = False
            continue
        if not dumped:
            try:
                f = _log(f"事件循环无响应 {lag:.0f}s,全线程堆栈如下:")
                try:
                    faulthandler.dump_traceback(file=f)
                finally:
                    f.close()
                print(f"[watchdog] 事件循环无响应 {lag:.0f}s,堆栈已写 {config.WATCHDOG_LOG_PATH}", flush=True)
            except OSError as exc:
                # 诊断盘写满/权限变化不能让看门狗本身死亡；仍继续走受控退出。
                print(f"[watchdog] 无法写入诊断日志: {exc}", flush=True)
            dumped = True
        if lag >= config.WATCHDOG_EXIT_SEC:
            try:
                f = _log(f"无响应 {lag:.0f}s ≥ {config.WATCHDOG_EXIT_SEC}s,自杀退出交给 run.sh 拉起")
                f.close()
            except OSError as exc:
                print(f"[watchdog] 无法写入退出日志: {exc}", flush=True)
            os._exit(70)
