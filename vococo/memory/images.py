"""用户图片落盘(Web 消息)。

图片本体写进当前租户图片目录(tenancy.paths.images_dir),文件名记进 turns.images(JSON 列表);只有当轮
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

from ..tenancy import paths as tenant_paths
from . import _db

_IMG_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")  # 文件名白名单,挡路径穿越

# AI 主动发的图(append_turn_image)统一用这个前缀命名,用户上传图是 "{turn_id}_{idx}.ext"
# (数字开头)——两种命名互不相交,靠这个前缀就能从 turns.images 里把两类图拆开,
# 分别贴回"用户"气泡和"AI"气泡(同一轮里可能两种都有,不能混在一起显示)。
AI_IMAGE_PREFIX = "ai_"


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
    tenant_paths.images_dir().mkdir(parents=True, exist_ok=True)
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
        (tenant_paths.images_dir() / name).write_bytes(raw)
        names.append(name)
    if names:
        c = _db.conn()
        c.execute(
            "UPDATE turns SET images=? WHERE id=?",
            (json.dumps(names, ensure_ascii=False), turn_id),
        )
        c.commit()
    return names


def append_turn_image(session_key: str, name: str) -> None:
    """AI 主动发的一张图(send_image 工具)追加进当前(最新)一轮的 turns.images。

    与 save_turn_images 不同:那是用户上传图片时【整轮一次性写入】;这里是模型在
    轮次进行中途主动补发一张,要在已有列表基础上追加而不是覆盖,否则会连带把
    这一轮用户上传的图片记录冲掉。找不到该会话的轮次(理论上不会,调用时轮次
    必然已 start_turn)则静默跳过。
    """
    c = _db.conn()
    row = c.execute(
        "SELECT id, images FROM turns WHERE session_key=? ORDER BY id DESC LIMIT 1",
        (session_key,),
    ).fetchone()
    if not row:
        return
    turn_id, imgs = row
    try:
        names = json.loads(imgs) if imgs else []
    except (json.JSONDecodeError, ValueError):
        names = []
    names.append(name)
    c.execute(
        "UPDATE turns SET images=? WHERE id=?",
        (json.dumps(names, ensure_ascii=False), turn_id),
    )
    c.commit()


def image_path(name: str):
    """按文件名返回图片的磁盘路径(Path);非法名/不存在返回 None —— 供 HTTP 取图时校验。"""
    if not name or not _IMG_NAME_RE.match(name):
        return None  # 挡 ../ 等路径穿越
    p = tenant_paths.images_dir() / name
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
                (tenant_paths.images_dir() / n).unlink(missing_ok=True)
