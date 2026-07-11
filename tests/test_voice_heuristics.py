"""voice/heuristics.py——语音判定纯函数的测试。

这些用例原在 test_voice_p2_realtime.py(随 P2 全双工管线一起删除,见
docs/adr/0004-voice-omni-only.md),纯函数部分连同实现迁到 heuristics.py
留档,用例原样保留:参数化样例都是真机换来的(语气词误触发/短句误杀/
回声 containment 阈值),将来给 Omni 链路做服务端回声兜底时直接复用。
"""
from __future__ import annotations

import pytest

from claude_hermes.voice import heuristics


# ── build_truncated_text:打断截断 ─────────────────────────────────────────
def test_build_truncated_text_keeps_only_played_sentences():
    out = heuristics.build_truncated_text(["第一句。", "第二句。", "第三句。"], 2)
    assert out == "第一句。第二句。(此处被用户打断)"


def test_build_truncated_text_zero_played_is_marker_only():
    out = heuristics.build_truncated_text(["第一句。"], 0)
    assert out == "(此处被用户打断)"


def test_build_truncated_text_clamps_overcount_defensively():
    # 客户端上报的播完数比服务端实际发出的还多(竞态/防御性场景),不能越界拼接。
    out = heuristics.build_truncated_text(["仅一句。"], 99)
    assert out == "仅一句。(此处被用户打断)"


def test_build_truncated_text_clamps_negative():
    out = heuristics.build_truncated_text(["一句。"], -1)
    assert out == "(此处被用户打断)"


# ── looks_like_self_echo:回声兜底 ─────────────────────────────────────────
def test_looks_like_self_echo_detects_substring_of_ai_speech():
    said = ["好主意，今天天气适合走走，呼吸一下新鲜空气。"]
    assert heuristics.looks_like_self_echo("呼吸一下新鲜空气", said, 0.6) is True


def test_looks_like_self_echo_false_for_unrelated_text():
    said = ["好主意，今天天气适合走走，呼吸一下新鲜空气。"]
    assert heuristics.looks_like_self_echo("明天股票会涨吗", said, 0.6) is False


def test_looks_like_self_echo_false_when_nothing_was_said_yet():
    # 打断发生在 AI 一个字都还没说的时候(emitted_sentences 空)——没什么可比对的,
    # 不该被误判成回声(这也是真实一轮"完全没来得及说话就被打断"的常见场景)。
    assert heuristics.looks_like_self_echo("随便说点什么", [], 0.6) is False


def test_looks_like_self_echo_false_for_empty_transcript():
    assert heuristics.looks_like_self_echo("", ["随便说点什么。"], 0.6) is False


def test_looks_like_self_echo_respects_threshold():
    said = ["今天天气不错。"]
    # "今天" 只占 transcript 一半,containment 在 0.5 上下——阈值调高/调低应该
    # 分别判定为不是/是回声,验证阈值真的在生效,不是写死的常量。
    assert heuristics.looks_like_self_echo("今天出去玩", said, 0.9) is False
    assert heuristics.looks_like_self_echo("今天出去玩", said, 0.1) is True


# ── looks_like_filler_only:语气词兜底(真机原样例子) ─────────────────────────
@pytest.mark.parametrize(
    "transcript",
    ["嗯。", "哦。", "是的。", "知道。", "稍等。", "好的", "对的。", "嗯", ""],
)
def test_looks_like_filler_only_true_for_known_fillers(transcript):
    assert heuristics.looks_like_filler_only(transcript) is True


@pytest.mark.parametrize(
    "transcript", ["今天天气怎么样？", "有台风吗？", "很奇特。", "哈喽哈喽，听到吗？"]
)
def test_looks_like_filler_only_false_for_real_content(transcript):
    assert heuristics.looks_like_filler_only(transcript) is False


# ── estimate_speech_too_short:时长兜底(不看文字看物理时长) ───────────────────
# 2026-07-09 真机复现修正:原先假设 raw_span 里一定含有完整的 silence_ms 静音尾巴、
# 减掉才是真实说话时长——但真机实测 raw_span 经常比配置的 silence_ms 还短
# (DashScope 实际判"说完了"用的时长跟配置对不上),减法算出负数会把"你好呀"
# "什么？"这类正常短句全部误杀。已改成直接用 raw_span 判断,不再做减法,
# silence_ms 形参保留(签名不变)但不参与计算。
def test_estimate_speech_too_short_true_for_brief_burst():
    # 50ms 就结束,典型的瞬时噪声(呼吸声/麦克风杂音)。
    assert heuristics.estimate_speech_too_short(0.0, 0.05, silence_ms=1500, min_speech_ms=250) is True


def test_estimate_speech_too_short_false_for_normal_speech():
    # 985ms 的原始跨度(真机实测过的真实短句"什么？"),不该被当噪声丢掉。
    assert heuristics.estimate_speech_too_short(0.0, 0.985, silence_ms=1500, min_speech_ms=250) is False


def test_estimate_speech_too_short_false_when_speech_stopped_never_arrived():
    # speech_stopped 迟迟不来(比如又被打断)不该误伤,直接放行。
    assert heuristics.estimate_speech_too_short(0.0, None, silence_ms=1500, min_speech_ms=250) is False


def test_estimate_speech_too_short_respects_min_speech_ms():
    # 200ms 的原始跨度,验证真的在用 min_speech_ms 而不是写死的常量。
    assert heuristics.estimate_speech_too_short(0.0, 0.2, silence_ms=1500, min_speech_ms=150) is False
    assert heuristics.estimate_speech_too_short(0.0, 0.2, silence_ms=1500, min_speech_ms=250) is True
