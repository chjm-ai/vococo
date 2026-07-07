"""句子聚合 + edge-tts 合成。

流式文字增量攒到标点/超长阈值就切一句,交给 edge-tts 合成音频。模型输出的一切文字
都会被念出来(2026-07-07 去掉了"屏幕专属、不朗读"这条例外——真机实测发现模型一遇到
"内容有点多"就用它把实质内容全部丢进屏幕,用户根本不看屏幕,等于没回答)。
edge-tts 是非官方接口,合成失败一律吞掉异常、返回 None——前端降级为纯文字,不崩对话。
"""
from __future__ import annotations

_SENT_END_CHARS = "。!?！？\n"
_MAX_BUFFER = 60  # 超过这么多字还没遇到标点,强制切一句(兜底,防止长句迟迟不出声)


class SentenceSplitter:
    """喂入文字增量,吐出切好的完整句子。"""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        if not delta:
            return []
        self._buf += delta
        sentences, remainder = _split_complete(self._buf)
        self._buf = remainder
        if len(self._buf) > _MAX_BUFFER:
            sentences.append(self._buf.strip())
            self._buf = ""
        return sentences

    def flush(self) -> list[str]:
        """轮结束时把残留的尾巴也当一句吐出。"""
        tail = self._buf.strip()
        self._buf = ""
        return [tail] if tail else []


def _split_complete(text: str) -> tuple[list[str], str]:
    """按标点切出完整句子,返回 (句子列表, 剩余未完成的尾巴)。"""
    sentences: list[str] = []
    start = 0
    for i, ch in enumerate(text):
        if ch in _SENT_END_CHARS:
            piece = text[start:i + 1].strip()
            if piece:
                sentences.append(piece)
            start = i + 1
    return sentences, text[start:]


async def synthesize(text: str, voice: str) -> bytes | None:
    """把一句话合成 mp3 字节;失败(网络/接口失效/空文本)一律返回 None。"""
    text = text.strip()
    if not text:
        return None
    try:
        import edge_tts
    except ImportError:
        return None
    try:
        communicate = edge_tts.Communicate(text, voice)
        chunks = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                chunks.extend(chunk["data"])
        return bytes(chunks) if chunks else None
    except Exception:  # noqa: BLE001 —— 非官方接口,任何失败都降级为纯文字,不崩对话
        return None


# 干活垫话(见 01-phase0-voice-entry.md F10):模型开始跑第一个顶层工具时,
# 不等它自己开口,立即插播这句——同一句在进程生命周期内只合成一次,内存缓存复用,
# 保证"稍等"这句话不会比它本该垫的等待时间还慢出来。
FILLER_PHRASE = "稍等,我看看。"
_filler_cache: dict[str, bytes | None] = {}


async def filler_audio(voice: str) -> bytes | None:
    if voice not in _filler_cache:
        _filler_cache[voice] = await synthesize(FILLER_PHRASE, voice)
    return _filler_cache[voice]
