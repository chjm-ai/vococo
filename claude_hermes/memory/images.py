"""用户图片落盘(Web 消息)。

图片本体写进 config.IMAGES_DIR,文件名记进 turns.images(JSON 列表);只有当轮
喂模型的 in-memory base64 会被清掉,落盘的这份让刷新后仍能显示。2026-07-23 从
session_store.py 拆出(该文件当时把图片 blob、项目路径、worktree 绑定、搜索、
偏好设置六个不相关关注点全挤在一份 45 函数的文件里,只共享一个连接)。
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3

from .. import config
from . import _db

_IMG_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")  # 文件名白名单,挡路径穿越


def _img_ext(media_type: str) -> str:
    """从 media_type(如 image/png)取一个安全的扩展名;取不到回落 png。"""
    ext = (media_type or "").split("/")[-1].split(";")[0].strip().lower()
    ext = re.sub(r"[^a-z0-9]", "", ext)
    return ext or "png"


def save_turn_images(turn_id: int, images: list) -> list[str]:
    """把某轮用户图片写盘并把文件名记进 turns.images;返回文件名列表。

    images 元素需有 .data(base64 字符串)和 .media_type;解码失败的单张跳过,
    不影响其余图片与正文落库。
    """
    if not images:
        return []
    config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for idx, im in enumerate(images):
        data = getattr(im, "data", None)
        if not data:
            continue
        try:
            raw = base64.b64decode(data)
        except (binascii.Error, ValueError):
            continue  # 坏 base64:跳过这张
        name = f"{turn_id}_{idx}.{_img_ext(getattr(im, 'media_type', ''))}"
        (config.IMAGES_DIR / name).write_bytes(raw)
        names.append(name)
    if names:
        c = _db.conn()
        c.execute(
            "UPDATE turns SET images=? WHERE id=?",
            (json.dumps(names, ensure_ascii=False), turn_id),
        )
        c.commit()
    return names


def image_path(name: str):
    """按文件名返回图片的磁盘路径(Path);非法名/不存在返回 None —— 供 HTTP 取图时校验。"""
    if not name or not _IMG_NAME_RE.match(name):
        return None  # 挡 ../ 等路径穿越
    p = config.IMAGES_DIR / name
    return p if p.is_file() else None


def purge_session_images(c: sqlite3.Connection, session_key: str) -> None:
    """删会话前把它名下所有图片文件从磁盘清掉,避免孤儿文件堆积。供 session_store 的
    clear()/delete_session() 调用(传入同一条连接,同一事务里先清文件再删行)。"""
    rows = c.execute(
        "SELECT images FROM turns WHERE session_key=? AND images IS NOT NULL",
        (session_key,),
    ).fetchall()
    for (imgs,) in rows:
        try:
            names = json.loads(imgs) if imgs else []
        except (json.JSONDecodeError, ValueError):
            continue
        for n in names:
            if _IMG_NAME_RE.match(n or ""):
                (config.IMAGES_DIR / n).unlink(missing_ok=True)
