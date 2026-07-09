"""语音转文字:主界面语音输入(gateway/adapters/web.py 的 /transcribe)和
语音伴聊模式共用同一套阿里 DashScope 转写实现,避免两边各写一份、切供应商时忘了同步
(2026-07-08 切阿里云时就出过这个问题:web.py 曾经留着一份没跟着切的旧 SenseVoice
实现,属性名对不上导致每次转写都报错)。
"""
from __future__ import annotations

import base64
import json
import time

import aiohttp
from aiohttp import web

from .. import config

_DASHSCOPE_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"

# 与 web.py 的 _STT_CLEANUP_PROMPT 同一份措辞,清洗逐字稿口癖/同音字/音译错的专名,
# 额外加了 Obsidian 这个语音场景实测识别率差的例子(笔记软件,常被听成不相关的中文谐音)
_CLEANUP_PROMPT = (
    "你是语音转文字校对员。下面是一段语音识别生成的逐字稿,请只做这几件事:"
    "删掉「呃、啊、嗯、然后、就是、那个」这类无意义口头禅;"
    "修正同音字/错别字;修正被听写成中文谐音的英文专有名词"
    "(常见于科技/AI 话题,比如把 OpenAI、Codex、Anthropic、Claude Code、Obsidian 这类词"
    "听成谐音汉字或走音英文,要按读音猜回正确的英文原词,保留其标准大小写拼写;"
    "这类专名不一定是本例举出的,只要读音明显是某个科技/软件专有名词的谐音就应该按此规则纠正);"
    "补全必要标点。"
    "禁止:不要改写句子结构,不要删减或添加信息,不要翻译。"
    "只输出清洗后的文本,不要加引号,不要任何解释。"
)


async def read_audio(request: web.Request) -> tuple[bytes | None, str, str]:
    """从 multipart 里取出 audio 字段(字节、文件名、类型)。"""
    try:
        reader = await request.multipart()
    except (ValueError, AssertionError):
        return None, "", ""
    async for part in reader:
        if part.name == "audio":
            data = await part.read(decode=False)
            ctype = part.headers.get("Content-Type", "application/octet-stream")
            return data, (part.filename or "voice.webm"), ctype
    return None, "", ""


async def transcribe(audio: bytes, filename: str, ctype: str) -> tuple[str | None, str]:
    """转写音频,返回 (text, error)。text 为 None 表示失败,error 是给用户看的提示。

    识别本体是阿里 DashScope 的 qwen3-asr-flash(2026-07-08 从 SiliconFlow 的
    SenseVoiceSmall 切过来,真机实测 SenseVoice 单次识别要 8~17 秒,隔离测试
    确认是那个接口本身慢、不是我们代码的问题;qwen3-asr-flash 同等准确度下
    只要 0.5~1 秒,见 03-phase2-实现记录.md)。协议跟 SiliconFlow 完全不同——
    这边是 JSON + base64 音频(data URI),不是 multipart 文件上传。
    """
    if not config.DASHSCOPE_API_KEY:
        return None, "未配置语音转写:请在 .env 设 DASHSCOPE_API_KEY"
    mime = (ctype or "audio/wav").split(";")[0].strip() or "audio/wav"
    b64 = base64.b64encode(audio).decode("ascii")
    payload = {
        "model": config.DASHSCOPE_STT_MODEL,
        "input": {
            "messages": [
                {"role": "user", "content": [{"audio": f"data:{mime};base64,{b64}"}]}
            ]
        },
    }
    headers = {
        "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=30)
    t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(_DASHSCOPE_URL, json=payload, headers=headers) as resp:
                body = await resp.text()
        t1 = time.monotonic()
        if resp.status != 200:
            return None, f"转写服务返回 {resp.status}"
        choices = json.loads(body).get("output", {}).get("choices") or []
        parts = choices[0]["message"]["content"] if choices else []
        text = "".join(p.get("text", "") for p in parts if "text" in p).strip()
        cleaned = await _cleanup(text)
        t2 = time.monotonic()
        print(
            f"[voice/stt] size={len(audio)/1024:.1f}KB stt={t1-t0:.2f}s clean={t2-t1:.2f}s\n"
            f"  raw:     {text!r}\n"
            f"  cleaned: {cleaned!r}",
            flush=True,
        )
        return cleaned, ""
    except (aiohttp.ClientError, TimeoutError):
        return None, "转写服务连接失败"
    except (json.JSONDecodeError, ValueError, KeyError, IndexError):
        return None, "转写返回解析失败"


async def _cleanup(text: str) -> str:
    """清洗口癖/同音字;关闭或失败都原样返回未清洗文本,不阻塞主流程。"""
    if not text or not config.STT_CLEANUP_ENABLED or not config.STT_API_KEY:
        return text
    url = f"{config.STT_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.STT_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.STT_CLEANUP_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _CLEANUP_PROMPT},
            {"role": "user", "content": text},
        ],
    }
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload, headers=headers) as resp:
                body = await resp.text()
        if resp.status != 200:
            return text
        content = json.loads(body)["choices"][0]["message"]["content"]
        return content.strip() or text
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, ValueError):
        return text
