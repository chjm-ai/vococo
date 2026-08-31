"""用户音频落盘(Web 消息)。

音频本体写进 config.AUDIO_DIR,连同原始文件名和转写文字一起记进 turns.audios(JSON 列表,
元素 {"file":落盘名,"filename":原始文件名,"text":转写文字})。与 images.py 的不同之处:图片直接喂给
模型的多模态 block,音频协议层压根没有对应的 block 类型(见 core/agent.py
AudioAttachment 的说明),AI"解读"的其实是转写文字——所以这里连转写文字一起
存,不然刷新页面后历史轮次就只剩一个能回放但没人"读懂"过的音频文件。
"""
from __future__ import annotations

import json
import re
import sqlite3

from .. import config
from . import _db

_AUDIO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")  # 文件名白名单,挡路径穿越


def _audio_ext(media_type: str, filename: str = "") -> str:
    """取一个安全的扩展名:原始文件名优先,其次 media_type,都取不到回落 bin。

    优先用原始文件名,是因为浏览器给 m4a 之类的 type 常常为空或不准(回落成
    audio/mpeg),落成 .mpeg 会让后续 ffmpeg/ASR 按错误容器解析;文件名里的
    .m4a 才是真的。
    """
    from_name = (filename or "").rsplit(".", 1)[-1] if "." in (filename or "") else ""
    from_name = re.sub(r"[^a-z0-9]", "", from_name.strip().lower())
    if from_name and len(from_name) <= 5:
        return from_name
    ext = (media_type or "").split("/")[-1].split(";")[0].strip().lower()
    ext = re.sub(r"[^a-z0-9]", "", ext)
    return ext or "bin"


def save_turn_audio(turn_id: int, audios: list) -> list[dict]:
    """把某轮用户音频写盘并把 {file,text} 记进 turns.audios;返回写入的列表。

    audios 元素需有 .data(原始字节,来自 multipart 上传,不是 base64)、
    .media_type、.filename、.transcript(上传时已转写好,这里不重跑一遍 ASR)。
    """
    if not audios:
        return []
    config.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    for idx, au in enumerate(audios):
        data = getattr(au, "data", None)
        if not data:
            continue
        ext = _audio_ext(getattr(au, "media_type", ""), str(getattr(au, "filename", "") or ""))
        name = f"{turn_id}_{idx}.{ext}"
        path = config.AUDIO_DIR / name
        path.write_bytes(data)
        # 回填本机路径:模型要能拿原始音频再处理(转写失败/长音频重跑)就靠这个,同图片
        try:
            au.local_path = str(path)
        except AttributeError:
            pass
        entries.append({
            "file": name,
            "filename": str(getattr(au, "filename", "") or ""),
            "text": getattr(au, "transcript", "") or "",
        })
    if entries:
        c = _db.conn()
        c.execute(
            "UPDATE turns SET audios=? WHERE id=?",
            (json.dumps(entries, ensure_ascii=False), turn_id),
        )
        c.commit()
    return entries


def clone_turn_audio(turn_id: int, entries: list) -> list[dict]:
    """把一批已落盘的音频复制成属于 turn_id 的新副本,返回新的 entries。

    同 images.clone_turn_images:副本会话要持有自己的文件,否则删原会话会连副本的
    音频一起清掉。源文件缺失的条目保留转写文字、去掉 file 字段(至少不丢文字稿)。
    """
    out: list[dict] = []
    config.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    for idx, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        old = e.get("file") or ""
        new_entry = dict(e)
        src = config.AUDIO_DIR / old if _AUDIO_NAME_RE.match(old) else None
        if src is not None and src.is_file():
            ext = old.rsplit(".", 1)[-1] if "." in old else "bin"
            new = f"{turn_id}_{idx}.{ext}"
            (config.AUDIO_DIR / new).write_bytes(src.read_bytes())
            new_entry["file"] = new
        else:
            new_entry.pop("file", None)
        out.append(new_entry)
    return out


def audio_path(name: str):
    """按文件名返回音频的磁盘路径(Path);非法名/不存在返回 None —— 供 HTTP 取音频时校验。"""
    if not name or not _AUDIO_NAME_RE.match(name):
        return None  # 挡 ../ 等路径穿越
    p = config.AUDIO_DIR / name
    return p if p.is_file() else None


def purge_session_audio(c: sqlite3.Connection, session_key: str) -> None:
    """删会话前把它名下所有音频文件从磁盘清掉,避免孤儿文件堆积。供 session_store 的
    clear()/delete_session() 调用(传入同一条连接,同一事务里先清文件再删行)。"""
    rows = c.execute(
        "SELECT audios FROM turns WHERE session_key=? AND audios IS NOT NULL",
        (session_key,),
    ).fetchall()
    for (auds,) in rows:
        try:
            entries = json.loads(auds) if auds else []
        except (json.JSONDecodeError, ValueError):
            continue
        for e in entries:
            name = e.get("file") or ""
            if _AUDIO_NAME_RE.match(name):
                (config.AUDIO_DIR / name).unlink(missing_ok=True)
