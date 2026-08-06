"""用户音频落盘(Web 消息)。

音频本体写进 config.AUDIO_DIR,连同转写文字一起记进 turns.audios(JSON 列表,
元素 {"file":文件名,"text":转写文字})。与 images.py 的不同之处:图片直接喂给
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


def _audio_ext(media_type: str) -> str:
    """从 media_type(如 audio/mpeg)取一个安全的扩展名;取不到回落 bin。"""
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
        name = f"{turn_id}_{idx}.{_audio_ext(getattr(au, 'media_type', ''))}"
        (config.AUDIO_DIR / name).write_bytes(data)
        entries.append({"file": name, "text": getattr(au, "transcript", "") or ""})
    if entries:
        c = _db.conn()
        c.execute(
            "UPDATE turns SET audios=? WHERE id=?",
            (json.dumps(entries, ensure_ascii=False), turn_id),
        )
        c.commit()
    return entries


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
