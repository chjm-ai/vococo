"""私有文件成果快照；来源边界与文档预览完全独立。"""
from __future__ import annotations

import asyncio
import io
import os
import stat
import zipfile
from pathlib import Path

from . import danger
from .html_preview import _check_parts

MAX_BYTES = 64 * 1024 * 1024
# 只交付成果格式，不把代码、数据库、密钥容器当成通用下载入口。
EXTENSIONS = {".pptx", ".docx", ".xlsx", ".pdf", ".txt", ".md", ".csv", ".tsv",
              ".json", ".html", ".htm", ".png", ".jpg", ".jpeg", ".webp", ".svg",
              ".mp4", ".mp3", ".wav", ".zip"}
ARCHIVES = {".zip", ".pptx", ".docx", ".xlsx"}


def _roots(session_key: str, cwd: str) -> tuple[Path, ...]:
    from ..core.execution import policy_for_session

    policy = policy_for_session(session_key, cwd)
    roots = (policy.workspace, policy.output_dir) if policy else (Path(cwd),)
    safe = []
    for root in roots:
        root = root.expanduser().resolve()
        # HOME 及其祖先不是项目；即使错误配置也不能变成任意文件下载。
        if Path.home().resolve().is_relative_to(root) or root == Path(root.anchor):
            raise ValueError("工作目录过宽，不能安全交付文件。")
        parts = root.parts[1:]
        # SDK 开发 worktree 位于 .claude/worktrees；这不是读取 .claude 凭据的授权。
        _check_parts(tuple(part for i, part in enumerate(parts)
                           if not (part == ".claude" and parts[i + 1:i + 2] == ("worktrees",))))
        safe.append(root)
    return tuple(safe)


def _read_source(root: Path, parts: tuple[str, ...]) -> bytes:
    # 从磁盘根逐级 NOFOLLOW，拒绝中间目录换链；FIFO 不得挂死工具。
    fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (*root.parts[1:], *parts[:-1]):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("仅支持普通文件，不接受软链或硬链接。")
            if not 0 < info.st_size <= MAX_BYTES:
                raise ValueError("成果不能为空，且单个不能超过 64MB。")
            data = source.read(MAX_BYTES + 1)
    finally:
        os.close(fd)
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError("成果不能为空，且单个不能超过 64MB。")
    return data


def _check_secrets(data: bytes) -> None:
    text = data.decode("utf-8", errors="ignore")
    if danger.redact_secrets(text) != text or "-----BEGIN " in text and "PRIVATE KEY-----" in text:
        raise ValueError("成果含疑似密钥，拒绝交付。")


def _archive_parts(entry: zipfile.ZipInfo) -> None:
    parts = Path(entry.filename).parts
    # OOXML 的标准关系文件 _rels/.rels 不是隐藏凭据；其他隐藏项仍拒绝。
    checked = (*parts[:-1], "relationships.rels") if parts and parts[-1] == ".rels" else parts
    _check_parts(checked)
    if entry.filename.startswith("/") or ".." in parts or "\\" in entry.filename:
        raise ValueError("压缩包含越界路径。")
    if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
        raise ValueError("不接受软链或加密压缩包。")
    suffix = ".rels" if parts and parts[-1] == ".rels" else Path(entry.filename).suffix.lower()
    office_assets = {".xml", ".rels", ".bin", ".emf", ".wmf", ".vml", ".tiff", ".gif", ".bmp", ".odttf"}
    if not entry.is_dir() and suffix not in EXTENSIONS | office_assets:
        raise ValueError("压缩包含非成果文件。")


def _check_archive(data: bytes, depth: int = 0, budget: list[int] | None = None) -> None:
    budget = budget if budget is not None else [0, 0]
    if depth > 2:
        raise ValueError("压缩嵌套过深，无法安全检查。")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            budget[0] += len(entries)
            budget[1] += sum(e.file_size for e in entries)
            if budget[0] > 5000 or budget[1] > MAX_BYTES * 2:
                raise ValueError("压缩内容过大，无法安全检查。")
            for entry in entries:
                _archive_parts(entry)
                if entry.is_dir():
                    continue
                payload = archive.read(entry)
                _check_secrets(payload)
                # PPT 图表嵌入 XLSX 是正常成果，同样递归检查并共用解压预算。
                if payload.startswith(b"PK\x03\x04"):
                    _check_archive(payload, depth + 1, budget)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError("压缩包无法安全检查。") from exc


def _snapshot(path: str, session_key: str, cwd: str) -> tuple[str, bytes]:
    if not isinstance(path, str) or not path.strip() or "://" in path or "\x00" in path or "\\" in path:
        raise ValueError("path 必须是本地成果文件路径。")
    candidate = Path(path).expanduser()
    candidate = candidate if candidate.is_absolute() else Path(cwd).resolve() / candidate
    if ".." in candidate.parts or candidate.suffix.lower() not in EXTENSIONS:
        raise ValueError("不接受目录逃逸或非成果格式。")
    from .. import config

    runtime = config.DATA_DIR.resolve()
    for root in _roots(session_key, cwd):
        if candidate.is_relative_to(root):
            parts = candidate.relative_to(root).parts
            _check_parts(parts)
            if candidate.is_relative_to(runtime) and not root.is_relative_to(runtime):
                raise ValueError("运行数据目录不是当前会话的成果来源。")
            data = _read_source(root, parts)
            _check_secrets(data)
            if candidate.suffix.lower() in ARCHIVES:
                _check_archive(data)
            return candidate.name, data
    raise ValueError("文件不在当前会话工作目录或正式成果目录内，未读取。")


async def snapshot(path: str, session_key: str) -> tuple[str, bytes]:
    cwd = danger.current_cwd()
    if not cwd:
        raise ValueError("当前会话没有工作目录，无法安全交付。")
    return await asyncio.to_thread(_snapshot, path, session_key, cwd)
