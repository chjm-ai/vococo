"""屏幕常亮锁生命周期的源码约束(2026-08-25)。

⚠️ 这些是**源码断言**,只能证明代码写成了预期的样子,**证明不了任何真机行为**。
真机验证唯一手段是 scripts/inspect_wakelock.py 读 /voice/debug 落下的日志,
看通话期间有没有 >30s 的掉锁空窗、wakelock.held 心跳是不是全程 held=true。
别拿本文件的绿灯当"自动熄屏已经防住了"。

场景定调:主人开车时长通话、从不手动锁屏,断线全是"聊太久,手机到自动熄屏时间
自己熄"。所以常亮锁必须全程持有到通话结束——它掉了,后面那套息屏挂起/自动接回
都只是收尸的。

真机日志(data/logs/vococo.out.log)暴露的三个缺口:
  ① 11:14:20 acquired → 11:14:29 又 acquired(9 秒内两次成功申请)= 中间锁被
     释放过,而 release 回调只把 sentinel 置空,既不记日志也不重新申请;
     历史日志里 released 事件数恒为 0,这个盲区让"锁掉了"完全不可观测。
  ② fail 之后没有任何重试,要等下一次偶然的重连;fail→下次 acquired 常隔
     10~50 秒,00:27:16/20/23 还连炸三次。
  ③ 页面不可见时照样发请求,必被拒(日志有一条 "Document is hidden")。
"""
from __future__ import annotations

from pathlib import Path

import pytest

VOICE_JS = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/voice.js"


@pytest.fixture(scope="module")
def source() -> str:
    return VOICE_JS.read_text(encoding="utf-8")


def _slice(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    return source[start : source.index(end_marker, start)]


@pytest.fixture(scope="module")
def acquire_fn(source: str) -> str:
    return _slice(source, "async function acquireWakeLock()", "function scheduleWakeLockRetry")


def test_release_triggers_immediate_reacquire(acquire_fn: str) -> None:
    """核心修复:系统释放锁 = 屏幕随时会自己熄,必须立刻补申请,不留空窗。"""
    assert 'vdbg("wakelock.released"' in acquire_fn, "释放必须留日志,否则掉锁不可观测"
    assert "scheduleWakeLockRetry(0)" in acquire_fn, "释放后要零延迟补申请"
    assert "heldMs" in acquire_fn, "要记录本次持有了多久,才能算掉锁空窗"


def test_request_skipped_while_hidden(acquire_fn: str) -> None:
    """不可见时申请必被拒,应挂起等 visible,而不是浪费一次失败。"""
    assert 'document.visibilityState !== "visible"' in acquire_fn
    assert "wakeLockPendingVisible = true" in acquire_fn
    assert 'vdbg("wakelock.deferred"' in acquire_fn


def test_failure_retries_with_backoff(source: str, acquire_fn: str) -> None:
    """失败要自己退避重试,不能等下一次偶然的重连。"""
    assert "scheduleWakeLockRetry();" in acquire_fn
    retry = _slice(source, "function scheduleWakeLockRetry(forceDelayMs)", "// 持有心跳")
    assert "Math.pow(2, wakeLockRetryAttempts)" in retry
    assert "8000" in retry, "退避要封顶,开车场景不能退到几分钟才试一次"


def test_stale_sentinel_not_treated_as_held(acquire_fn: str) -> None:
    """sentinel 存在不等于锁还在——released 属性得看,否则永远不补申请。"""
    assert "wakeLockSentinel && !wakeLockSentinel.released" in acquire_fn


def test_release_race_returns_lock(acquire_fn: str) -> None:
    """申请是异步的:await 期间用户可能已挂断,拿到就得还回去。"""
    assert "if(!handsFreeActive){ try{ sentinel.release(); }catch(e){} return; }" in acquire_fn


def test_watchdog_recovers_lost_lock(source: str) -> None:
    """release 事件不是所有浏览器都派发,3s 看门狗直接查真实状态兜底。

    开车长通话没有重连事件可蹭,只有周期性检查能兜住。
    """
    watchdog = _slice(source, "function omniReadStallCheck()", "// ── 尾音排空监听")
    assert "wakeLockSentinel.released" in watchdog
    assert 'vdbg("wakelock.watchdog"' in watchdog
    assert "wakeLockSupported" in watchdog, "不支持的浏览器不能被看门狗每 3s 刷屏"


def test_heartbeat_makes_holding_observable(source: str) -> None:
    """每 30s 一条 wakelock.held —— 没有它,真机上无法证明锁是否全程持有。"""
    hb = _slice(source, "function startWakeLockHeartbeat()", "function stopWakeLockHeartbeat")
    assert 'vdbg("wakelock.held"' in hb
    assert "30000" in hb
    assert "if(!held) acquireWakeLock()" in hb, "心跳发现掉锁也要补"


def test_visibility_acquires_before_anything_else(source: str) -> None:
    """回到可见的瞬间就抢锁,不排在重连/接回后面。"""
    handler = _slice(source, 'if(document.hidden){\n      if(handsFreeActive) vdbg("bg.enter"', "flushAnnouncements();")
    acquire_at = handler.index("acquireWakeLock();")
    resume_at = handler.index("if(osSuspended){")
    assert acquire_at < resume_at, "常亮锁要比息屏接回更早处理"


def test_low_power_mode_misdiagnosis_removed(source: str) -> None:
    """撤掉「多半开了低电量模式」那句误判提示。

    真机日志证明失败是瞬时的(同一次通话里 fail 和 acquired 交替出现),
    低电量模式会是持续拒绝,对不上。现在只在【持续】拿不到时才提示。
    """
    assert "多半开了低电量模式" not in source
    assert "Date.now() - wakeLockFailStreakSince > 45000" in source, "要持续失败才提示用户"
