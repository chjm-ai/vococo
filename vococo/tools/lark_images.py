"""复用成果文件授权，生成不含元数据的飞书图片上传快照。"""
from __future__ import annotations

import asyncio
import io
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .artifacts import snapshot

MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 25_000_000


def normalize_image(filename: str, data: bytes) -> bytes:
    if Path(filename).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError("图片仅支持 PNG、JPEG、WebP，不接受 SVG 或附件。")
    if not data or len(data) > MAX_BYTES:
        raise ValueError("图片必须非空且不超过20MiB。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > MAX_PIXELS:
                    raise ValueError("图片格式不支持或像素超过2500万。")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                # 不把 EXIF、尾随数据或任意嵌入文件传给宿主 CLI。
                oriented = ImageOps.exif_transpose(image)
                clean = Image.new("RGBA", oriented.size)
                clean.paste(oriented.convert("RGBA"))
                output = io.BytesIO()
                clean.save(output, format="PNG")
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValueError("图片无法安全解码。") from exc
    if output.tell() > MAX_BYTES:
        raise ValueError("规范化图片超过20MiB。")
    return output.getvalue()


async def image_snapshot(path: str) -> bytes:
    from ..gateway import clarify

    ctx = clarify.current()
    if ctx is None or not ctx.session_key:
        raise ValueError("缺少当前会话，不能读取图片。")
    filename, data = await snapshot(path, ctx.session_key)
    return await asyncio.to_thread(normalize_image, filename, data)
