"""息屏挂起态的回归测试(2026-08-25 真机日志定案)。

结论先行:**iOS 浏览器不允许后台/息屏录音,这不是能修的 bug,是平台硬边界。**
2026-08-25 真机日志(data/logs/vococo.out.log,11:05:24 起)证据链:
  11:05:24 bg.enter(息屏)→ 11:05:25~35 后台 JS 持续运行、还完成了一整轮对话
  → 说明前三轮修复保的"页面别被冻结"那条命【本来就没断】;
  11:05:44 mic.suspect reason="ended" ready="ended"
  → 系统在页面不可见约 20 秒后主动回收了 getUserMedia 轨道,这才是真正的断点;
  11:06:03 解锁 → 11:06:04 抢着重连(ctx 仍 suspended、wakelock 被拒)
  → 11:06:19 信令 TypeError: Load failed → 11:06:22 button-mode.return 放弃
  → 11:06:25 用户手动点按钮才恢复。

所以修复方向从"保住麦克风"(做不到)改成"断得体面、回得顺畅":后台状态下不再
空转重连,进入显式的息屏挂起态,回到前台再自动接回。本文件钉住这套状态机。
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


def test_background_mic_death_enters_suspend_not_reconnect(source: str) -> None:
    """后台状态下麦克风被回收 = 系统行为,必须进挂起态而不是排重连。"""
    suspect = _slice(source, "function suspectMicProblem(reason)", "// 麦克风流音频活动检测")
    assert "document.hidden" in suspect
    assert "MIC_DEAD_REASONS.has(reason)" in suspect
    assert "enterOsSuspend(" in suspect
    # 判为系统回收的原因要覆盖真机见过的全部形态
    reasons = _slice(source, "const MIC_DEAD_REASONS", "\n")
    for r in ("ended", "stale", "zombie", "muted"):
        assert f'"{r}"' in reasons


def test_reconnect_refuses_to_run_in_background(source: str) -> None:
    """页面不可见时重连是纯浪费:后台 getUserMedia 拿不到活轨道,信令也常 Load failed。"""
    fn = _slice(source, "function scheduleOmniReconnect(reason, delayMs, force)", "// 音色切换实时生效")
    guard = fn[: fn.index("vdbg(\"reconnect.scheduled\"")]
    assert "document.hidden" in guard
    assert "enterOsSuspend(" in guard


def test_resume_waits_for_audio_session_to_settle(source: str) -> None:
    """回前台不能抢着重连——解锁瞬间音频会话还在 interrupted↔running 抖。"""
    fn = _slice(source, "async function resumeFromOsSuspend()", "function suspectMicProblem(reason)")
    assert "ensureAudioCtxRunning(" in fn
    assert "setTimeout(r, 700)" in fn, "要等音频会话稳定再申请麦克风"
    assert "document.hidden" in fn, "等待期间用户可能又息屏了,要重新判定"
    # 接回失败不许无限重试,把主动权交还用户
    assert "returnToButtonState()" in fn


def test_visibility_change_routes_to_resume(source: str) -> None:
    """挂起态优先:回前台走自动接回,不要再跑常规的复活/嫌疑链路。"""
    assert "if(osSuspended){ resumeFromOsSuspend();" in source


def test_suspend_state_cleared_on_hangup(source: str) -> None:
    """用户已离开通话视图,下次回前台不能自作主张把通话接回来。"""
    teardown = _slice(source, "function teardownCallResources()", "window.openCallView")
    assert "osSuspended = false;" in teardown


def test_wakelock_denial_is_surfaced_to_user(source: str) -> None:
    """常亮被拒必须告诉用户。

    真机日志 85 次申请里 48 次 NotAllowedError(iOS 低电量模式直接拒发)。
    既然平台不允许后台录音,常亮拿不到就等于通话随时会被息屏打断——只记 vdbg
    等于让用户一直蒙在鼓里。
    """
    fn = _slice(source, "async function acquireWakeLock()", "let wakeLockWarned")
    assert "wakeLockWarned" in fn
    assert 'String(e).includes("NotAllowed")' in fn
    assert "addMsg(" in fn, "要有用户可见的提示,不能只打日志"
