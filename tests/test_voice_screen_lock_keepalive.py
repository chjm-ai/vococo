"""语音通话「息屏后无法继续对话」的回归测试(2026-08-24 根治)。

这个 bug 前后修过三次(wakeLock → 保活音轨 → 防熄屏视频),每次都只加【预防】
层,没人管【息屏之后怎么恢复】,而且最新那层因为一个结构性疏漏从未真正生效。
本文件按源码断言的方式,把四条不能再退化的约束钉住(前端无构建步骤、无 JS
测试框架,仓库既有做法就是对静态源码做断言,见 test_web_static_cache.py)。
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


def test_keepalive_media_attached_to_document(source: str) -> None:
    """保活音轨/防熄屏视频必须挂进文档。

    根因:游离(无父节点)的媒体元素在 iOS WebKit 上不参与渲染,系统侧的
    「后台音频会话」「有视频在播就不自动锁屏」两个豁免都不认它——元素建了、
    play() 也 resolve 了,却完全没有保活效果。
    """
    audio = _slice(source, "function startKeepAliveAudio()", "function stopKeepAliveAudio()")
    video = _slice(source, "function startKeepAliveVideo()", "function stopKeepAliveVideo()")
    assert "document.body.appendChild(el)" in audio
    assert "document.body.appendChild(v)" in video
    # 停止时要摘干净,否则反复进出通话会在 body 上堆残留元素
    assert ".remove()" in _slice(source, "function stopKeepAliveAudio()", "// ── 通话防熄屏视频")
    assert ".remove()" in _slice(source, "function stopKeepAliveVideo()", "resumeKeepAliveOnGesture")


def test_keepalive_video_stays_rendered(source: str) -> None:
    """防熄屏视频不能用 display:none / opacity:0 / 远离视口的方式隐藏。

    这些写法会让 WebKit 判定元素「不可见」,idle-lock 豁免同样拿不到。
    """
    video = _slice(source, "function startKeepAliveVideo()", "function stopKeepAliveVideo()")
    style_line = next(line for line in video.splitlines() if "v.style.cssText" in line)
    assert "display:none" not in style_line
    assert "opacity:0;" not in style_line
    assert "-9999px" not in style_line


def test_keepalive_survives_reconnect(source: str) -> None:
    """重连/切音色复用 stopOmniHandsFree 时不能顺手把保活拆掉。

    重连要几秒,期间若保活停了、屏幕又恰好是锁着的,页面直接被冻结,重连
    永远跑不完——表现就是「息屏后再也接不回来」。
    """
    stop_fn = _slice(source, "function stopOmniHandsFree()", "// ── 按钮模式:")
    assert "if(!handsFreeActive){" in stop_fn, "保活收尾必须只在真挂断(handsFreeActive=false)时执行"
    # 真挂断的两条路径各自负责收尾:teardownCallResources 先置 false 再调用,
    # returnToButtonState 置 false 之后自己显式收。
    button_state = _slice(source, "function returnToButtonState()", "// 开始/继续录音按钮")
    assert "stopKeepAliveAudio();" in button_state
    assert "stopKeepAliveVideo();" in button_state


def test_resume_pipeline_wired_and_probes(source: str) -> None:
    """回前台必须走完整复活流程,并以「真·探活」收尾。

    历史修复只在 visibilitychange 里补了 wakeLock 和视频;AudioContext、保活
    音轨、远端 <audio> 三样没人管。其中 AudioContext 挂起最致命:僵尸麦克风
    的两处检测(micStreamHealthy / checkMicAudioActivity)都以 state==="running"
    为前提,ctx 一直挂着等于把麦克风自检整个关掉,界面显示「聆听中」但全链路失聪。
    """
    assert 'resumeAudioPipeline("vischange")' in source
    pipeline = _slice(source, "function resumeAudioPipeline(why)", "function probeAfterResume(why)")
    assert "audioCtx.resume()" in pipeline
    assert "startKeepAliveAudio();" in pipeline
    assert "startKeepAliveVideo();" in pipeline
    assert "omniAudioEl.play()" in pipeline
    assert "probeAfterResume" in pipeline

    probe = _slice(source, "function probeAfterResume(why)", "// ═══ 声波区")
    # 三种判死方式都要在:ctx 拉不回来 / 轨道死了 / 电平恒静音
    assert '"resume:ctx-stuck", 300, true' in probe
    assert '"resume:track-dead", 300, true' in probe
    assert '"resume:mic-zombie", 300, true' in probe
    # 必须 force 重连:这类故障里 WebRTC 连接状态恰恰一直显示 connected,不能信它
    assert probe.count(", true)") >= 3


def test_watchdog_self_heals_suspended_context(source: str) -> None:
    """没有 visibilitychange 事件的中断(来电、页面被直接冻结)也要兜住。"""
    watchdog = _slice(source, "function omniReadStallCheck()", "// ── 尾音排空监听")
    assert "_ctxStuckTicks" in watchdog
    assert 'resumeAudioPipeline("watchdog")' in watchdog
