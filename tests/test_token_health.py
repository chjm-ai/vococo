"""订阅令牌探活巡检:被吊销才出声,断网和"没配令牌"都不许报。

背景:令牌失效后官方模型全线不可用,而日常走第三方端点 + 后台自动回落 DeepSeek,
整条链路静默降级(2026-08-16 那次五天没人发现)。判据沿用 providers 的三态探活,
这里的重点是"什么情况下不报"。
"""
from __future__ import annotations

import time

import anyio
import pytest

from vococo import config
from vococo.core import alerts, token_health


@pytest.fixture
def env(monkeypatch):
    """拦住真的网络探活,并记录报了什么。"""
    box: list[tuple] = []

    async def fake_alert(key, title, body, **kw):
        box.append((key, title, body))
        return True

    monkeypatch.setattr(alerts, "alert", fake_alert)
    monkeypatch.setattr(config, "OAUTH_TOKEN", "sk-ant-oat01-test")

    def probe(state, detail="服务端原话"):
        monkeypatch.setattr(
            "vococo.providers.probe_subscription_token", lambda token, **kw: (state, detail)
        )

    return box, probe


def _check() -> dict:
    return anyio.run(token_health.check)


def test_bad_token_wants_alert(env):
    box, probe = env
    probe("bad", "OAuth access token has been revoked.")
    r = _check()
    assert r["state"] == "bad" and r["alert"] and "令牌已失效" in r["alert"][0]
    assert not box, "check 只做判断,不该自己发(发的活是 patrol_loop 的)"


def test_403_alert_flags_network_possibility(env):
    """403 既可能是令牌也可能是出口被拒:报还是要报(宁可多报),但正文要点破。"""
    box, probe = env
    probe("bad", "HTTP 403 · Request not allowed")
    body = _check()["alert"][1]
    assert "网络出口" in body and "HTTP 403" in body
    probe("bad", "HTTP 401 · OAuth access token has been revoked.")
    assert "网络出口" not in _check()["alert"][1], "401 是明说令牌不行,不用扯网络"


def test_hung_probe_does_not_freeze_patrol(env, monkeypatch):
    """探活卡住(不抛异常)时 check 必须能返回,否则巡检永远停在这一次。

    计时必须在 anyio.run 内部:退场时 asyncio 会 join 线程池,而 to_thread 的工作线程
    是取消不掉的——那个等待是测试进程的事,生产里巡检循环照常往下走(next round)。
    """
    import threading

    gate = threading.Event()
    monkeypatch.setattr(token_health, "PROBE_BUDGET", 0.1)
    monkeypatch.setattr("vococo.providers.probe_subscription_token", lambda token, **kw: gate.wait(30))
    threading.Timer(1.0, gate.set).start()  # 1 秒后放掉工作线程,别让退场干等 30 秒

    async def _main():
        t0 = time.time()
        with pytest.raises(TimeoutError):
            await token_health.check()
        return time.time() - t0

    assert anyio.run(_main) < 5


def test_ok_and_unknown_stay_quiet(env):
    box, probe = env
    probe("ok")
    assert _check()["state"] == "ok" and _check()["alert"] is None
    probe("unknown", "连不上")
    assert _check()["alert"] is None, "断网/限流不能当成令牌坏了"


def test_no_token_is_not_applicable(env, monkeypatch):
    box, probe = env
    monkeypatch.setattr(config, "OAUTH_TOKEN", "")
    r = _check()
    assert r["applicable"] is False and r["alert"] is None


def test_probe_uses_config_token(env, monkeypatch):
    """探活必须拿 .env 里那个令牌去探,不能空着手。"""
    seen: list[str] = []
    monkeypatch.setattr(
        "vococo.providers.probe_subscription_token",
        lambda token, **kw: (seen.append(token), ("ok", ""))[1],
    )
    _check()
    assert seen == ["sk-ant-oat01-test"]


def _run_patrol_once() -> None:
    """跑 patrol_loop 一小会儿再掀桌子(它本身是无限循环;间隔由调用方 patch 短)。"""

    async def _stop_after_first():
        await anyio.sleep(0.2)
        raise KeyboardInterrupt

    async def _both():
        async with anyio.create_task_group() as tg:
            tg.start_soon(token_health.patrol_loop)
            tg.start_soon(_stop_after_first)

    with pytest.raises(BaseException):
        anyio.run(_both)


def test_patrol_reports_bad_token(env, monkeypatch):
    box, probe = env
    probe("bad", "OAuth access token has been revoked.")
    monkeypatch.setattr(token_health, "FIRST_DELAY", 0)
    monkeypatch.setattr(token_health, "INTERVAL", 0.01)
    _run_patrol_once()
    assert box and box[0][0] == token_health.ALERT_KEY


def test_patrol_self_error_is_reported(env, monkeypatch):
    """巡检自己炸了也要说一声(它哑了就再没人盯令牌)。"""
    box, probe = env

    def boom(token, **kw):
        raise RuntimeError("providers 读不到设置")

    monkeypatch.setattr("vococo.providers.probe_subscription_token", boom)
    monkeypatch.setattr(token_health, "FIRST_DELAY", 0)
    monkeypatch.setattr(token_health, "INTERVAL", 0.01)
    _run_patrol_once()
    assert any(k == "token-patrol-broken" for k, _t, _b in box)
