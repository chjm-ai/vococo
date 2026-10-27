"""成果与静态预览共用的受控读取；调用方各自决定授权根目录与格式。"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat


def check_parts(parts: tuple[str, ...]) -> None:
    sensitive = re.compile(r"(?:^|[._-])(?:credentials?|secrets?|passwords?|tokens?|private[-_]keys?)(?:[._-]|$)", re.I)
    if not parts or any(part.startswith(".") or sensitive.search(part) for part in parts):
        raise ValueError("不支持隐藏目录、凭据目录或秘密文件。")


def read_regular(root: Path, parts: tuple[str, ...], max_bytes: int) -> bytes:
    # 从磁盘根逐级 NOFOLLOW，连授权根本身也不能在检查后被换成软链。
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
            if not 0 < info.st_size <= max_bytes:
                raise ValueError(f"文件不能为空，且不能超过 {max_bytes} 字节。")
            data = source.read(max_bytes + 1)
    finally:
        os.close(fd)
    if not 0 < len(data) <= max_bytes:
        raise ValueError(f"文件不能为空，且不能超过 {max_bytes} 字节。")
    return data
