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
import os
import re
import sqlite3
import threading

from .. import config
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
        path = config.IMAGES_DIR / name
        path.write_bytes(raw)
        # 这张图会在本轮原样喂给模型；同时把受控落盘路径回填，模型需要将图片
        # 用作网站素材等文件操作时可直接读取，不必再猜临时目录。
        im.local_path = str(path)
        names.append(name)
    if names:
        c = _db.conn()
        c.execute(
            "UPDATE turns SET images=? WHERE id=?",
            (json.dumps(names, ensure_ascii=False), turn_id),
        )
        c.commit()
    return names


def clone_turn_images(turn_id: int, names: list) -> list[str]:
    """把一批已落盘的图片复制成属于 turn_id 的新副本,返回新文件名列表。

    供 duplicate_session 用:副本会话必须持有自己的文件,否则两个会话共享同一批
    磁盘文件,删掉任一个都会把另一个的图片一起清空(purge_session_images 按文件名删)。
    源文件缺失/文件名非法的单张跳过,不影响其余。
    """
    out: list[str] = []
    config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    for idx, old in enumerate(names):
        if not isinstance(old, str) or not _IMG_NAME_RE.match(old):
            continue
        src = config.IMAGES_DIR / old
        if not src.is_file():
            continue
        ext = old.rsplit(".", 1)[-1] if "." in old else "png"
        # AI 主动发的图必须保住 ai_ 前缀:历史渲染靠它区分贴用户气泡还是 AI 气泡
        prefix = AI_IMAGE_PREFIX if old.startswith(AI_IMAGE_PREFIX) else ""
        new = f"{prefix}{turn_id}_{idx}.{ext}"
        (config.IMAGES_DIR / new).write_bytes(src.read_bytes())
        out.append(new)
    return out


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
    p = config.IMAGES_DIR / name
    return p if p.is_file() else None


_THUMB_MAX = 320  # 缩略图最长边(px);历史消息一屏可能同时挂几十张图,拖慢首屏
_THUMBS_DIR_NAME = "_thumbs"
# 缩略图统一存 WebP(带透明通道也能存):实测比原格式小约 60%,国内中转线路带宽有限。
# 文件名 = 原图名 + ".webp";旧版按原格式存的同名缩略图由 backfill_thumbs 顺手清掉。
_THUMB_SUFFIX = ".webp"


def _thumb_file(name: str):
    return config.IMAGES_DIR / _THUMBS_DIR_NAME / (name + _THUMB_SUFFIX)


def _make_thumb(orig, thumb) -> None:
    """用 Pillow 生成缩略图。CPU 活(大图一张 150~330ms),调用方别放在事件循环里跑。

    先写临时文件再改名:请求线程和后台补齐可能同时生成同一张,改名是原子的,
    读的一方不会读到写了一半的文件。
    """
    from PIL import Image

    thumb.parent.mkdir(parents=True, exist_ok=True)
    tmp = thumb.with_name(f"{thumb.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with Image.open(orig) as im:
        im.thumbnail((_THUMB_MAX, _THUMB_MAX))
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if im.mode in ("P", "LA", "PA") else "RGB")
        im.save(tmp, "WEBP", quality=80, method=4)
    tmp.replace(thumb)


def thumb_path(name: str):
    """按文件名返回缩略图路径(Path);没有就现生成并落盘缓存,此后直接命中。

    内容寻址(原图文件名不变→缩略图文件名跟着不变),同一张图只生成一次。
    生成失败(损坏文件/PIL 不支持的格式)时回落到原图路径,保证至少能显示原图。
    非法名/原图不存在时同 image_path 返回 None。
    现生成要占 CPU,Web 端经 asyncio.to_thread 调用;平时由 backfill_thumbs 提前备好。
    """
    orig = image_path(name)
    if orig is None:
        return None
    thumb = _thumb_file(name)
    if thumb.is_file():
        return thumb
    try:
        _make_thumb(orig, thumb)
        return thumb
    except Exception:
        return orig


def backfill_thumbs() -> int:
    """给还没有缩略图的原图补生成,返回新生成的张数。服务启动后在后台线程跑一遍。

    不补的话,第一次打开图多的旧会话要现场生成,整页图片迟迟出不来(2026-09-27 实测
    626 张原图里有 312 张没缩略图)。新图优先:最近的会话最可能被打开。
    """
    d = config.IMAGES_DIR
    if not d.is_dir():
        return 0
    thumbs_dir = d / _THUMBS_DIR_NAME
    if thumbs_dir.is_dir():  # 清掉旧版按原格式存的缩略图和中断留下的临时文件
        for f in thumbs_dir.iterdir():
            if not f.name.endswith(_THUMB_SUFFIX):
                f.unlink(missing_ok=True)
    origs = []
    for p in d.iterdir():
        try:
            if p.is_file() and _IMG_NAME_RE.match(p.name):
                origs.append((p.stat().st_mtime, p))
        except OSError:
            continue
    made = 0
    for _, p in sorted(origs, key=lambda x: x[0], reverse=True):
        thumb = _thumb_file(p.name)
        if thumb.is_file():
            continue
        try:
            _make_thumb(p, thumb)
            made += 1
        except Exception:
            continue
    return made


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
                _thumb_file(n).unlink(missing_ok=True)
