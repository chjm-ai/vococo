"""语音判定纯函数留档——P2 全双工管线(ws.py)退休时的幸存者。

出身:这些函数原在 voice/ws.py(随 P2 管线整体删除,见
docs/adr/0004-voice-omni-only.md),是 2026-07-08~07-10 三天真机免提
迭代里用事故换来的判定逻辑。P2 删除后暂时没有后端调用方(Omni 模式下
VAD/打断发生在浏览器↔阿里云侧),留档原因:

- 回声/假触发问题在 Omni 链路上并未根治。当前活的兜底是**前端**的
  matchOmniEcho(index.html,2026-07-11 加,容错前缀匹配+编辑距离,
  只认句子开头)——跟这里的 looks_like_self_echo(containment,全文
  片段重合率)是两种互补算法:前缀匹配抓"AEC 未收敛漏进的句头回声",
  containment 抓"播放中途被打断漏回的任意片段"。若前端版真机命中率
  不足,或将来要在 /voice/send 文字入口加服务端第二道兜底,这里可直接用;
- 注释里的调优值和"为什么不这么做"是真机换来的,重写一遍必然复踩。

当年配套的 config 常量已随 P2 删除,真机调优终值抄录如下:
- VOICE_SELF_ECHO_THRESHOLD = 0.6(containment 阈值)
- VOICE_POST_DONE_ECHO_GUARD_MS = 1500(说完后的尾音回声宽限期——
  服务端状态翻回 idle 后音箱还在播尾音,这段时间的转写也要过回声判定)
- VOICE_MIN_SPEECH_MS = 250(物理时长下限)
- VOICE_FALSE_POSITIVE_TIMEOUT_MS = 8000(假打断回滚超时,要覆盖
  "用户把话说完+上游转写完"的完整耗时,1500 会误杀正常追问)
"""
from __future__ import annotations

import difflib
import re


def build_truncated_text(emitted_sentences: list[str], played_count: int) -> str:
    """打断截断:只算用户真正听完的句子,附加"被打断"标记。

    played_count 由客户端上报(source.onended 真正播完的句子数),可能大于服务端
    实际发出的句子数(防御性 clamp,不炸)。played_count<=0 时视为一句都没听到。
    """
    n = max(0, min(played_count, len(emitted_sentences)))
    heard = "".join(emitted_sentences[:n])
    return heard + "(此处被用户打断)"


# 真机实测(见 03-phase2-实现记录.md"连环打断"一节):VAD 灵敏度再怎么调,
# 环境噪音/呼吸声偶尔还是会被识别模型强行编成一个说得过去的短语气词,而不是
# 老实返回空。2026-07-09 起改成任何状态下都直接丢弃——原先认为空闲状态触发
# 语气词无害("用户确实可能就是说了句'嗯'"),但真机免提使用发现噪音触发
# 语气词的频率远高于用户真用单字回应,放行反而让免提场景经常无缘无故起一轮
# 不该有的对话,见用户反馈"还是会识别到一些语气词或者无意义的短语"。
_FILLER_WORDS = frozenset({
    "嗯", "呃", "啊", "哦", "噢", "诶", "欸", "唉", "哈", "呀",
    "是的", "知道", "好的", "好", "对", "对的", "行", "行吧", "稍等", "等等",
})
_PUNCT_RE = re.compile(r"[，。！？,.!?、\s]+")


def looks_like_filler_only(transcript: str) -> bool:
    """转写内容整段就是一个语气词/口头禅(去掉标点后跟已知词表精确匹配),
    大概率是噪音被硬凑出来的假触发——任何状态下都用来悄悄丢弃。
    """
    stripped = _PUNCT_RE.sub("", transcript)
    return not stripped or stripped in _FILLER_WORDS


def estimate_speech_too_short(
    speech_started_at: float,
    speech_stopped_at: float | None,
    *,
    silence_ms: int,
    min_speech_ms: int,
) -> bool:
    """时长兜底:不看转写文字,只看物理时长——呼吸声/麦克风杂音这类瞬时噪声
    通常撑不到一个字的时长,即便被识别模型硬编成语气词也该丢掉,所以在空闲
    状态也生效(不像 looks_like_filler_only 只在打断场景生效)。

    2026-07-09 真机复现修正:原先假设 speech_started→speech_stopped 这段时间差
    里一定含有完整的 silence_ms(session.update 里配置的静音判停时长),减掉才是
    真正说话时长——但真机实测过好几次都是 raw_span < silence_ms(例如
    raw_span=985ms < silence_ms=1500ms),说明 DashScope 实际判"说完了"用的静音
    时长跟我们配置的对不上,减法算出来永远是负数,导致"你好呀""什么？"这类
    正常短句全部被误杀成噪声,免提模式变成几乎打不出一轮对话。改成直接用
    raw_span_ms(不做减法)判断——只挡真正瞬时的噪声(几十毫秒级别),
    宁可放过一些语气词误触发,也不能连正常说话都拦掉。silence_ms 形参保留
    但不再参与判断(签名不改,调用方不用跟着改;只是不再依赖这个不可靠的假设)。
    speech_stopped 还没来(比如被下一次打断打断)时不误伤,直接放行。
    """
    if speech_stopped_at is None:
        return False
    raw_span_ms = (speech_stopped_at - speech_started_at) * 1000
    return raw_span_ms < min_speech_ms


def looks_like_self_echo(
    transcript: str, emitted_sentences: list[str], threshold: float
) -> bool:
    """打断触发后转写出来的内容,是不是其实是 AI 自己声音漏进麦克风被听回去的。

    用"containment"而不是对称的相似度:回声通常只是 AI 那句话的一个片段
    (播放中途被打断/麦克风只收到一部分),transcript 一般比 emitted_sentences
    短很多,对称相似度会因为长度差太多而被稀释,量不出"这一小段几乎完全能在
    AI 刚说的话里找到"这件事。containment = transcript 里有多少字符能在
    emitted_sentences 拼起来的原文里找到匹配片段,除以 transcript 总长。
    """
    said = "".join(emitted_sentences)
    if not said or not transcript:
        return False
    matcher = difflib.SequenceMatcher(None, transcript, said)
    matched_chars = sum(block.size for block in matcher.get_matching_blocks())
    containment = matched_chars / len(transcript)
    return containment >= threshold
