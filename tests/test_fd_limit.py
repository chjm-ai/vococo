"""文件句柄上限:launchd 默认软限 256 太紧,serve 启动时要抬到 8192。

背景(2026-10-21):网络抖动引发重连风暴时进程撞上 256 上限,连 cron_jobs.json
都打不开(Errno 24),语音侧栏整块 500。修法见 gateway/run.py `_raise_fd_limit`,
观测见 gateway/watchdog.py `_check_fds`。
"""
from __future__ import annotations

import asyncio
import errno
import os
import resource

from vococo.gateway import watchdog
from vococo.gateway.run import _raise_fd_limit
from vococo.gateway.watchdog import _fd_count


def test_raise_fd_limit_when_hard_is_unlimited(monkeypatch):
    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda _r: (256, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda _r, v: calls.append(v))

    _raise_fd_limit()

    assert calls == [(8192, resource.RLIM_INFINITY)]


def test_raise_fd_limit_caps_at_hard_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda _r: (256, 1024))
    monkeypatch.setattr(resource, "setrlimit", lambda _r, v: calls.append(v))

    _raise_fd_limit()

    assert calls == [(1024, 1024)]


def test_raise_fd_limit_noop_when_already_high(monkeypatch):
    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda _r: (8192, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda _r, v: calls.append(v))

    _raise_fd_limit()

    assert calls == []


def test_raise_fd_limit_never_raises(monkeypatch):
    def boom(*_args):
        raise OSError("nope")

    monkeypatch.setattr(resource, "getrlimit", boom)

    _raise_fd_limit()  # 提不上限只打日志,不能拦住 serve 启动


def test_run_serve_raises_fd_limit_first(monkeypatch):
    """run_serve 必须先抬上限再起服务;有人删掉调用这里要红。"""
    from vococo.gateway import run

    calls = []
    monkeypatch.setattr(run, "_raise_fd_limit", lambda: calls.append("raised"))
    monkeypatch.setattr(run.config, "WEB_ENABLED", False)

    class FakeRunner:
        def __init__(self, adapters):
            assert adapters == []

        async def run(self) -> None:
            calls.append("run")

    monkeypatch.setattr(run, "GatewayRunner", FakeRunner)

    asyncio.run(run.run_serve())

    assert calls == ["raised", "run"]


def test_fd_warn_line_scales_with_low_soft_limit(monkeypatch):
    """setrlimit 失败(仍是 256)时警戒线必须跟着降,否则永远够不到。"""
    monkeypatch.setattr(resource, "getrlimit", lambda _r: (256, resource.RLIM_INFINITY))
    assert watchdog._fd_warn_line() == 192

    monkeypatch.setattr(resource, "getrlimit", lambda _r: (8192, resource.RLIM_INFINITY))
    assert watchdog._fd_warn_line() == 1024

    monkeypatch.setattr(resource, "getrlimit", lambda _r: (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    assert watchdog._fd_warn_line() == 1024


class _FakeLog:
    def close(self) -> None:
        pass


def _logged_msgs(monkeypatch) -> list[str]:
    msgs: list[str] = []

    def fake_log(msg: str) -> _FakeLog:
        msgs.append(msg)
        return _FakeLog()

    monkeypatch.setattr(watchdog, "_log", fake_log)
    monkeypatch.setattr(watchdog, "_last_fd_warn", 0.0)
    return msgs


def test_check_fds_logs_over_line_once_per_cooldown(monkeypatch):
    msgs = _logged_msgs(monkeypatch)
    monkeypatch.setattr(watchdog, "_fd_count", lambda: 2000)
    monkeypatch.setattr(watchdog, "_fd_warn_line", lambda: 1000)

    watchdog._check_fds(10_000.0)
    watchdog._check_fds(10_100.0)  # 冷却期内不再记
    assert len(msgs) == 1
    assert "2000 个" in msgs[0]

    watchdog._check_fds(10_600.0)  # 冷却结束再记一条
    assert len(msgs) == 2


def test_check_fds_below_line_does_not_start_cooldown(monkeypatch):
    """低于阈值的采样不能推进冷却,否则超限要等 10 分钟才报。"""
    msgs = _logged_msgs(monkeypatch)
    counts = iter([10, 2000])
    monkeypatch.setattr(watchdog, "_fd_count", lambda: next(counts))
    monkeypatch.setattr(watchdog, "_fd_warn_line", lambda: 1000)

    watchdog._check_fds(10_000.0)  # 低于阈值
    watchdog._check_fds(10_100.0)  # 100s 后超限,必须立即记

    assert len(msgs) == 1


def test_check_fds_reports_right_after_startup(monkeypatch):
    """monotonic 是开机秒数:刚开机(<600s)也必须能记第一条。"""
    msgs = _logged_msgs(monkeypatch)
    monkeypatch.setattr(watchdog, "_last_fd_warn", float("-inf"))
    monkeypatch.setattr(watchdog, "_fd_count", lambda: 2000)
    monkeypatch.setattr(watchdog, "_fd_warn_line", lambda: 1000)

    watchdog._check_fds(100.0)

    assert len(msgs) == 1


def test_fd_count_maps_errno(monkeypatch):
    """errno 映射写反会让 EMFILE 静默,这里钉住。"""

    def raise_oserror(code: int):
        def boom(_path: str):
            raise OSError(code, os.strerror(code))

        return boom

    monkeypatch.setattr(watchdog.os, "listdir", raise_oserror(errno.EMFILE))
    assert _fd_count() == -2

    monkeypatch.setattr(watchdog.os, "listdir", raise_oserror(errno.ENFILE))
    assert _fd_count() == -2

    monkeypatch.setattr(watchdog.os, "listdir", raise_oserror(errno.EACCES))
    assert _fd_count() == -1


def test_check_fds_silent_below_line(monkeypatch):
    msgs = _logged_msgs(monkeypatch)
    monkeypatch.setattr(watchdog, "_fd_count", lambda: 10)
    monkeypatch.setattr(watchdog, "_fd_warn_line", lambda: 1000)

    watchdog._check_fds(10_000.0)

    assert msgs == []


def test_check_fds_reports_exhaustion_even_when_count_unavailable(monkeypatch):
    """EMFILE 时 os.listdir 也失败,不能因此静默。"""
    msgs = _logged_msgs(monkeypatch)
    monkeypatch.setattr(watchdog, "_fd_count", lambda: -2)

    watchdog._check_fds(10_000.0)

    assert len(msgs) == 1
    assert "耗尽" in msgs[0]


def test_check_fds_log_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(watchdog, "_last_fd_warn", 0.0)
    monkeypatch.setattr(watchdog, "_fd_count", lambda: 2000)
    monkeypatch.setattr(watchdog, "_fd_warn_line", lambda: 1000)

    def boom(_msg: str):
        raise OSError("disk full")

    monkeypatch.setattr(watchdog, "_log", boom)

    watchdog._check_fds(10_000.0)  # 不抛即通过


def test_fd_count_counts_open_fd():
    before = _fd_count()
    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        after = _fd_count()
    finally:
        os.close(fd)
    assert after >= before + 1
