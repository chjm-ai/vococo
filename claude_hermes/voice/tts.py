"""句子聚合 + 阿里 DashScope(qwen3-tts-flash)语音合成。

流式文字增量攒到标点/超长阈值就切一句,交给 DashScope 合成音频。模型输出的一切文字
都会被念出来(2026-07-07 去掉了"屏幕专属、不朗读"这条例外——真机实测发现模型一遇到
"内容有点多"就用它把实质内容全部丢进屏幕,用户根本不看屏幕,等于没回答)。

2026-07-09 从 edge-tts 切过来:那是扒微软 Edge 浏览器内部接口的非官方库,没有 SLA,
合成失败/无限期卡住是"语音伴聊没声音"投诉的主因。改用跟 stt.py 同一账号体系的
DashScope 官方 TTS(qwen3-tts-flash),同时补上两处欠账:显式超时(避免裸调无限挂起
拖死整轮对话)、失败重试一次(扛住短暂网络抖动)。合成异常仍旧只返回 None——前端降级
为纯文字,不崩对话,但真失败的概率应该显著低于 edge-tts。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp

from .. import config

_SENT_END_CHARS = "。!?！？\n"
_MAX_BUFFER = 60  # 超过这么多字还没遇到标点,强制切一句(兜底,防止长句迟迟不出声)

_DASHSCOPE_TTS_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
_SYNTHESIZE_TIMEOUT_SEC = 10
_RETRY_DELAY_SEC = 0.3


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


def _extract_audio_url(data: dict) -> str | None:
    """DashScope 响应里挖音频 url;字段路径没有逐字对照过官方 schema,
    多试几个常见形状兜底,而不是只认一种就直接判失败。"""
    output = data.get("output")
    if not isinstance(output, dict):
        return None
    audio = output.get("audio")
    if isinstance(audio, dict):
        url = audio.get("url")
        if isinstance(url, str) and url:
            return url
    for key in ("audio_url", "url"):
        val = output.get(key)
        if isinstance(val, str) and val:
            return val
    return None


async def _synthesize_once(sess: aiohttp.ClientSession, text: str, voice: str) -> bytes | None:
    payload = {
        "model": config.DASHSCOPE_TTS_MODEL,
        "input": {"text": text, "voice": voice, "language_type": "Chinese"},
    }
    headers = {
        "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    async with sess.post(_DASHSCOPE_TTS_URL, json=payload, headers=headers) as resp:
        body = await resp.text()
    if resp.status != 200:
        print(f"[voice/tts] 合成失败 status={resp.status} text={text[:20]!r} body={body[:200]!r}", flush=True)
        return None
    data = json.loads(body)
    audio_url = _extract_audio_url(data)
    if not audio_url:
        print(f"[voice/tts] 响应无音频url text={text[:20]!r} body={body[:200]!r}", flush=True)
        return None
    async with sess.get(audio_url) as audio_resp:
        if audio_resp.status != 200:
            print(f"[voice/tts] 下载音频失败 status={audio_resp.status} text={text[:20]!r}", flush=True)
            return None
        return await audio_resp.read()


async def synthesize(text: str, voice: str) -> bytes | None:
    """把一句话合成音频字节(qwen3-tts-flash 固定吐 wav,前端 decodeAudioData 嗅探
    字节不认 MIME,直接能播);失败(网络/接口异常/空文本)重试一次,仍失败返回 None。"""
    text = text.strip()
    if not text:
        return None
    if not config.DASHSCOPE_API_KEY:
        print("[voice/tts] 未配置 DASHSCOPE_API_KEY,无法合成", flush=True)
        return None
    timeout = aiohttp.ClientTimeout(total=_SYNTHESIZE_TIMEOUT_SEC)
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                audio = await _synthesize_once(sess, text, voice)
            if audio:
                return audio
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as e:
            print(f"[voice/tts] 合成异常(第{attempt + 1}次) text={text[:20]!r} err={e!r}", flush=True)
        if attempt == 0:
            await asyncio.sleep(_RETRY_DELAY_SEC)
    return None


# 干活垫话(见 01-phase0-voice-entry.md F10):模型开始跑第一个顶层工具时,
# 不等它自己开口,立即插播一声科技感音效(暗示"正在处理"),不再合成"稍等"这句话念出来
# ——音效不用等 TTS 网络往返,读一次盘就能复用,比任何一句念白都更贴合"垫时间"的本意。
_FILLER_SOUND_PATH = Path(__file__).resolve().parent / "static" / "tech_chime.wav"
_UNSET = object()
_filler_sound_cache: bytes | None = _UNSET  # type: ignore[assignment]


async def filler_audio() -> bytes | None:
    global _filler_sound_cache
    if _filler_sound_cache is _UNSET:
        try:
            _filler_sound_cache = _FILLER_SOUND_PATH.read_bytes()
        except OSError:
            _filler_sound_cache = None
    return _filler_sound_cache
