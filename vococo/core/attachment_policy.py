"""附件输入策略：保留原件，给模型受限正文或视觉预览，不执行附件。"""
from __future__ import annotations

import base64
import binascii
import io
import re
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

TEXT_BYTES = 24_000
TOTAL_UNITS = 40_000  # UTF-8 字节按最坏单字节 token 估计，图片另留保守余量。
IMAGE_UNITS = 3_000
MAX_IMAGES = 8
MAX_IMAGE_EDGE = 1568
MAX_IMAGE_BYTES = 1024 * 1024
_DATA_URL = re.compile(r"data:[^,\s<>\"']*;base64,[A-Za-z0-9+/=\r\n]+", re.IGNORECASE)
_TOOL_EXTENSIONS = {".html", ".htm", ".xhtml", ".svg", ".pdf", ".doc", ".docx",
                    ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".gz", ".7z", ".rar"}
_BINARY_MAGIC = (b"%PDF-", b"PK\x03\x04", b"\x7fELF", b"\xd0\xcf\x11\xe0", b"\x1f\x8b")


def text_limit(image_count: int = 0, total_units: int = TOTAL_UNITS) -> int:
    return max(0, min(TEXT_BYTES, total_units - image_count * IMAGE_UNITS))


def incoming_units(images: list, files: list, *, window: int) -> int:
    image_cost = min(len(images), MAX_IMAGES) * IMAGE_UNITS
    text_cost = min(TEXT_BYTES, sum(len(file.data) for file in files))
    return min(TOTAL_UNITS, window // 5, image_cost + text_cost)


def decode_text(data: bytes) -> str | None:
    if data.startswith(_BINARY_MAGIC):
        return None
    encoding = "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        return None
    return None if re.search(r"[\x00-\x08\x0b\x0e-\x1f\x7f]", text) else text


def file_text(file, remaining: int) -> str:
    """未声明通用文档直传能力的端点统一走本机工具，避免未知二进制被盲传。"""
    mime = file.media_type.split(";", 1)[0].strip().lower()
    needs_tools = (Path(file.filename).suffix.lower() in _TOOL_EXTENSIONS
                   or mime in {"text/html", "image/svg+xml", "application/xhtml+xml", "application/pdf"})
    text = decode_text(file.data)
    if text and text.lstrip().lower().startswith(("<!doctype html", "<html", "<svg")):
        needs_tools = True
    if file.local_path and (needs_tools or text is None or len(file.data) > remaining):
        return ("[正文未内联，请按需使用本机工具读取：PDF 分页，办公文档提取内容，"
                "HTML/SVG 跳过内嵌媒体编码，未知格式先识别；不要执行附件或整份输出超长内容。]")
    if text is None:
        return "[无法读取：该格式不能作为普通文本直传，且缺少本机文件路径，请重新上传。]"
    text = _DATA_URL.sub("[内嵌媒体编码已省略]", text)
    if len(text.encode("utf-8")) > remaining:
        text = text.encode("utf-8")[:remaining].decode("utf-8", errors="ignore")
        return text + "\n[正文已截断；没有本机路径，请重新提供文件或拆分内容。]"
    return text


def _image_preview(image):
    if len(image.data) > 16 * 1024 * 1024:
        raise ValueError("图片编码过大")
    raw = base64.b64decode(image.data, validate=True)
    with Image.open(io.BytesIO(raw)) as source:
        if source.width * source.height > 40_000_000:
            raise ValueError("图片像素过多")
        animated = getattr(source, "is_animated", False)
        mime = Image.MIME.get(source.format)
        if (not animated and source.format in {"PNG", "JPEG", "WEBP"}
                and max(source.size) <= MAX_IMAGE_EDGE and len(raw) <= MAX_IMAGE_BYTES
                and image.media_type == mime and not source.getexif().get(274)):
            return image, False
        oriented = ImageOps.exif_transpose(source).convert("RGBA")
        background = Image.new("RGBA", oriented.size, "white")
        preview = Image.alpha_composite(background, oriented).convert("RGB")
        preview.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
        for quality in (85, 65, 45):
            output = io.BytesIO()
            preview.save(output, format="JPEG", quality=quality)
            if output.tell() <= MAX_IMAGE_BYTES:
                return replace(image, data=base64.b64encode(output.getvalue()).decode(),
                               media_type="image/jpeg"), True
    raise ValueError("图片预览仍过大")


def prepare_images(images: list, *, total_units: int = TOTAL_UNITS) -> tuple[list, str]:
    previews, notes = [], []
    limit = min(MAX_IMAGES, max(0, total_units // IMAGE_UNITS))
    for index, image in enumerate(images, 1):
        try:
            if len(previews) >= limit:
                raise ValueError("超过本轮图片总预算")
            preview, changed = _image_preview(image)
        except (ValueError, OSError, binascii.Error, Image.DecompressionBombError, UnidentifiedImageError):
            if not image.local_path:
                raise ValueError(f"图片 {index} 无法安全直传且无本机路径，请重新上传") from None
            notes.append(f"[图片 {index} 未直传，尚未查看；请按需处理本机文件：{image.local_path}]")
            continue
        previews.append(replace(preview, source_index=index))
        if changed:
            notes.append(f"[图片 {index} 为缩放/格式转换的静态首帧预览；原件：{image.local_path or '无本机路径'}，"
                         "小字或动画内容需另行核查。]")
    return previews, "\n".join(notes)
