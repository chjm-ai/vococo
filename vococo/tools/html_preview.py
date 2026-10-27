"""显式 HTML 预览的文件快照；不提供任意路径的 HTTP 访问入口。"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from . import danger
from .file_snapshot import check_parts as _check_parts, read_regular

MAX_BYTES = 200_000


def snapshot_id(filename: str, data: bytes) -> str:
    import hashlib

    return hashlib.sha256(filename.encode("utf-8") + b"\0" + data).hexdigest()


def validate_source(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise ValueError("HTML 超过 200KB 上限。")
    text = data.decode("utf-8-sig")
    if not text.strip() or "\x00" in text:
        raise ValueError("HTML 为空或不是有效文本。")
    if danger.redact_secrets(text) != text:
        raise ValueError("HTML 含疑似密钥，拒绝展示。")
    return text


async def read_file_snapshot(path: str, *, cwd: str | None = None, allow_external: bool = True) -> tuple[str, bytes]:
    cwd = cwd or danger.current_cwd()
    if not cwd:
        raise ValueError("当前任务没有工作目录，无法安全展示 HTML。")
    if not isinstance(path, str) or not path.strip() or "://" in path or "\x00" in path:
        raise ValueError("path 必须是本地 HTML 文件路径，不支持 URL。")
    root = Path(cwd).resolve(strict=True)
    candidate = Path(path).expanduser()
    candidate = candidate if candidate.is_absolute() else root / candidate
    # 不先 resolve 用户路径，否则软链和 .. 的证据会丢失。
    if ".." in candidate.parts or candidate.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("仅支持 .html/.htm 文件，不接受目录逃逸。")
    try:
        parts = candidate.relative_to(root).parts
    except ValueError:
        if not allow_external:
            raise PermissionError("无权读取会话工作目录外的 HTML；请让 AI 显式调用 show_html 并完成审批。")
        root, parts = Path(candidate.anchor), candidate.parts[1:]
        _check_parts(parts)
        if not await danger.require_approval("展示工作目录外的 HTML", str(candidate)):
            raise ValueError("未获批准，未读取或展示工作目录外的文件。")
    _check_parts(parts)
    data = await asyncio.to_thread(read_regular, root, parts, MAX_BYTES)
    validate_source(data)
    return candidate.name, data


async def read_snapshot(path: str, *, cwd: str | None = None, allow_external: bool = True) -> str:
    _, data = await read_file_snapshot(path, cwd=cwd, allow_external=allow_external)
    return validate_source(data)


def local_html_path(value: str) -> str:
    """只解码无主机的 file URI；绝不把 URL/UNC 当成服务器本地路径。"""
    from urllib.parse import unquote, urlsplit

    if not isinstance(value, str) or not value.strip():
        raise ValueError("需要本地 HTML 路径。")
    path = value.strip()
    if path.lower().startswith("file:"):
        uri = urlsplit(path)
        if uri.netloc or uri.query or uri.fragment or not uri.path.startswith("/"):
            raise ValueError("只支持无主机的本地 file:/// 路径。")
        path = unquote(uri.path, errors="strict")
    if re.match(r"^[a-z][a-z0-9+.-]*:", path, re.I) or path.startswith("//") or "\\" in path:
        raise ValueError("不支持 URL 或远程共享路径。")
    return path
