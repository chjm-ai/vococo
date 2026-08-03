"""图片转文字:主模型不支持视觉时(如 DeepSeek),先用阿里百炼的 qwen-vl 把
图片读成文字描述,再以纯文本喂给主模型 —— 复用语音模块同一个 DASHSCOPE_API_KEY
和 multimodal-generation 端点,不新增任何配置(2026-08-03 新增)。

为什么不做成"直传图片让主模型自己看":DeepSeek 的 Anthropic 兼容端点根本不接受
image content block,硬传直接报错。转文字后描述文本会拼进 user_text,随对话落库,
后续轮次主模型也"记得"图的内容(虽然只是描述版)。
"""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlsplit

import aiohttp

from .. import config
from .agent import ImageAttachment  # 复用同一数据结构,不重复定义

# 与 voice/stt.py 同一个 DashScope 多模态端点(qwen-vl / ASR / TTS 全走这)。
_DASHSCOPE_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"

# 描述指令:要求逐字读出文字/数据,而不是泛泛"看图说话"——转文字的意义就在于
# 把主模型(DeepSeek)看不到的信息无损搬过去,报错截图/表格这类场景缺一个字就废。
_DESCRIBE_PROMPT = (
    "请仔细查看这张图片,用中文详细、客观地描述它的内容。要求:\n"
    "1. 图片里的文字必须逐字读出(报错信息、代码、数字、表格、界面文案都算);\n"
    "2. 截图/图表:说明整体结构和其中的关键数据、数值;\n"
    "3. 其他图片:说明主体、场景、可辨识的细节;\n"
    "4. 不要评论、不要推测用途、不要输出与图片内容无关的话。"
)

# 走 Anthropic 兼容端点、本身支持图片的第三方供应商 host。默认空:官方订阅
# (provider_env 为空)永远直传,第三方默认按非视觉转文字——因为这类端点上的
# 模型几乎全是纯文本(DeepSeek/Kimi 等)。以后配了支持视觉的第三方中转,
# 把它的 host 加进这里即可直传原图。
_VISION_BASE_URL_HINTS: frozenset[str] = frozenset()


def is_vision_capable(provider_env: dict[str, str]) -> bool:
    """当前供应商是否支持图片直传:官方订阅→是;第三方→看 host 是否命中视觉名单。"""
    if not provider_env:
        return True
    host = (urlsplit(provider_env.get("ANTHROPIC_BASE_URL", "")).hostname or "").lower()
    return host in _VISION_BASE_URL_HINTS


async def _describe_one(img: ImageAttachment) -> tuple[str | None, str]:
    """单张图转文字,返回 (text, error);失败 text 为 None,error 是给用户看的提示。"""
    if not config.DASHSCOPE_API_KEY:
        return None, "未配置图片识别:请在 .env 设 DASHSCOPE_API_KEY"
    mime = (img.media_type or "image/jpeg").split(";")[0].strip() or "image/jpeg"
    payload = {
        "model": config.DASHSCOPE_VL_MODEL,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"image": f"data:{mime};base64,{img.data}"},
                        {"text": _DESCRIBE_PROMPT},
                    ],
                }
            ]
        },
    }
    headers = {
        "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as sess:
            async with sess.post(_DASHSCOPE_URL, json=payload, headers=headers) as resp:
                body = await resp.text()
        t1 = time.monotonic()
        if resp.status != 200:
            # 把百炼返回的错误摘要带出来,方便定位(如图片超限/格式不支持)
            hint = ""
            try:
                hint = json.loads(body).get("message") or json.loads(body).get("code") or ""
            except json.JSONDecodeError:
                hint = body[:200]
            return None, f"图片识别服务返回 {resp.status}({hint})"
        choices = json.loads(body).get("output", {}).get("choices") or []
        parts = choices[0]["message"]["content"] if choices else []
        text = "".join(p.get("text", "") for p in parts if "text" in p).strip()
        print(
            f"[core/vision] {mime} size={len(img.data) * 3 // 4 // 1024}KB "
            f"vl={t1 - t0:.2f}s desc={len(text)}字",
            flush=True,
        )
        if not text:
            return None, "图片识别返回空内容"
        return text, ""
    except (aiohttp.ClientError, TimeoutError):
        return None, "图片识别服务连接失败"
    except (json.JSONDecodeError, ValueError, KeyError, IndexError):
        return None, "图片识别返回解析失败"


async def convert_images(images: list[ImageAttachment]) -> tuple[str | None, str]:
    """并发把一批图片转成文字描述,返回 (拼接文本, error)。

    error 非空时 text 为 None(任一张失败即整体失败,不让主模型对着残缺描述瞎猜)。
    拼接格式带"图片 N"序号,主模型能对应上"第几张图是什么"。
    """
    results = await asyncio.gather(
        *(_describe_one(img) for img in images),
        return_exceptions=True,
    )
    parts: list[str] = []
    for i, (img, res) in enumerate(zip(images, results), start=1):
        if isinstance(res, BaseException):
            return None, f"图片 {i} 读取失败: {res}"
        text, error = res
        if error:
            return None, f"图片 {i} 读取失败: {error}"
        parts.append(f"[图片附件: 图片{i}]\n{text}")
    return "\n\n".join(parts), ""
