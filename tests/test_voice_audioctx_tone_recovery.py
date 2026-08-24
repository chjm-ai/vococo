"""语音提示音「断连后集体静音、重连才恢复」的回归测试(2026-08-24)。

所有提示音(连接成功/思考中/回复中/发送/断线)都从同一个 AudioContext 出声。
iOS 会在重连时(resetMicStream + getUserMedia 重新协商音频会话)把它打成
suspended,甚至 interrupted(WebKit 私有态,resume 恒 reject)。原来的 playTone
遇到非 running 就「发起 resume 然后 return」,把本次音效直接丢掉,而 resume 是
异步的——断连窗口里的那几声正好全被丢干净;卡在 interrupted 时更是一路静音到
下次重连重新激活音频会话为止。

这里钉住三条不能再退化的约束(前端无构建步骤/无 JS 测试框架,仓库既有做法就是
对静态源码做断言,见 test_web_static_cache.py)。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

VOICE_JS = Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/voice.js"


@pytest.fixture(scope="module")
def source() -> str:
    return VOICE_JS.read_text(encoding="utf-8")


def _slice(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    return source[start : source.index(end_marker, start)]


def test_play_tone_replays_after_resume(source: str) -> None:
    """playTone 不能再「发起 resume 就把这一声丢掉」,必须恢复后补播。"""
    play_tone = _slice(source, "function playTone(freq, durMs, shape, vol)", "function emitTone(")
    assert "ensureAudioCtxRunning" in play_tone
    assert "emitTone(freq, durMs, shape, vol)" in play_tone
    # 反模式:resume 之后直接 return,把本次音效丢弃
    assert not re.search(r"resume\(\);?\s*\}?\s*catch[^\n]*\}\s*return;", play_tone)
    # 补播要有时效闸,别在状态早变了之后冒出个过期提示音
    assert "1500" in play_tone


def test_no_bare_resume_and_drop_left(source: str) -> None:
    """除恢复闸自身外,不允许再出现裸 resume。

    裸 resume 的两个问题:①只认 suspended、漏掉 iOS 的 interrupted;
    ②reject 之后没人善后,调用方要么静默丢音、要么被外层 catch 吞掉。
    """
    gate = _slice(source, "function ensureAudioCtxRunning(why)", "function rebuildAudioCtx(why)")
    rebuild = _slice(source, "function rebuildAudioCtx(why)", "// 设备切换检测")
    outside = source.replace(gate, "").replace(rebuild, "")
    assert "audioCtx.resume()" not in outside, "恢复必须统一走 ensureAudioCtxRunning"


def test_ctx_rebuild_path_exists_and_resets_nodes(source: str) -> None:
    """interrupted 卡死时要能重建 ctx,并把挂在老 ctx 上的节点全部重置。

    麦克风 analyser / 输出 analyser / 远端 tap / 工作音效 gain 都绑在老实例上,
    不重置的话后面 micStreamHealthy、尾音排空全在读死节点。
    """
    gate = _slice(source, "function ensureAudioCtxRunning(why)", "function rebuildAudioCtx(why)")
    assert "_ctxResumeFails >= 2" in gate, "连续恢复失败要升级成重建"
    assert 'audioCtx.state === "closed"' in gate

    rebuild = _slice(source, "function rebuildAudioCtx(why)", "// 设备切换检测")
    assert "CTX_REBUILD_COOLDOWN_MS" in rebuild, "重建要限流,否则会反复打断正在播的音频"
    for node in ("analyser = null", "analyserSource = null", "outputAnalyser = null", "omniOutTap = null"):
        assert node in rebuild
    assert "stopWorkSound()" in rebuild
    assert "ensureAnalyser()" in rebuild


def test_unlock_audio_replaces_closed_context(source: str) -> None:
    """留下一个 closed 的 ctx 就是永久哑巴:resume 恒 reject、createOscillator 无声。"""
    unlock = _slice(source, "function unlockAudio()", "// ── AudioContext 恢复闸")
    assert 'audioCtx.state !== "closed"' in unlock


def test_reconnect_restores_context_at_mic_acquisition(source: str) -> None:
    """重连拿到新麦克风流的那一刻就要把 ctx 拉回来。

    这是 iOS 打断 AudioContext 的确切时机,也是"断连后音效全哑"的正面修复点——
    不能等 playReconnectTone 撞上挂起态才补救。
    """
    start_fn = _slice(source, "async function startOmniHandsFree()", "function scheduleOmniReconnect")
    mic_ok = start_fn.index('vdbg("omni.mic.ok")')
    assert 'ensureAudioCtxRunning("mic-acquired")' in start_fn[mic_ok : mic_ok + 600]
