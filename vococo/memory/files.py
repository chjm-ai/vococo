"""用户通用文件附件落盘 + 历史元数据持久化。

文件本体写进 config.FILES_DIR,原始文件名和 MIME 类型记进 turns.files(JSON 列表,
元素 {"file":落盘名,"name":原始文件名,"media_type":MIME}),供刷新后的历史气泡继续
显示附件信息。

2026-08-31 起改为落盘(此前只存元数据、本体随轮次用完即丢):跟音频同一个毛病——
正文虽然塞进了当轮 content block,但模型没有任何本机路径,想用工具处理这个文件
(xlsx 读表、pdf 抽页、大文件 grep)就无从下手,只会去猜文件在哪。落盘的孤儿副本
问题由 purge_session_files 在删会话时一并清掉,跟 images/audio 同一套路。
"""
from __future__ import annotations

import json
import re
import sqlite3

from .. import config
from . import _db

_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")  # 落盘名白名单,挡路径穿越


def _file_ext(filename: str, media_type: str = "") -> str:
    """取一个安全的扩展名:原始文件名优先(扩展名才是工具链认的),取不到回落 bin。"""
    from_name = filename.rsplit(".", 1)[-1] if "." in (filename or "") else ""
    from_name = re.sub(r"[^a-z0-9]", "", from_name.strip().lower())
    if from_name and len(from_name) <= 8:
        return from_name
    ext = (media_type or "").split("/")[-1].split(";")[0].strip().lower()
    ext = re.sub(r"[^a-z0-9]", "", ext)
    return ext or "bin"


def save_turn_files(turn_id: int, files: list) -> list[dict]:
    """把该轮附件写盘,并把 {file,name,media_type} 记进 turns.files;返回写入的列表。

    同时把落盘的本机绝对路径回填到附件对象的 .local_path —— 模型靠它用工具直接
    处理原始文件(见 core/agent.py 拼 prompt 的地方)。
    """
    if not files:
        return []
    config.FILES_DIR.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    for idx, file in enumerate(files):
        filename = str(getattr(file, "filename", "") or "").strip()
        if not filename:
            continue
        media_type = str(getattr(file, "media_type", "") or "")
        entry = {"name": filename, "media_type": media_type}
        data = getattr(file, "data", None)
        if data:
            name = f"{turn_id}_{idx}.{_file_ext(filename, media_type)}"
            path = config.FILES_DIR / name
            path.write_bytes(data)
            entry["file"] = name
            try:
                file.local_path = str(path)
            except AttributeError:
                pass
        entries.append(entry)
    if entries:
        c = _db.conn()
        c.execute(
            "UPDATE turns SET files=? WHERE id=?",
            (json.dumps(entries, ensure_ascii=False), turn_id),
        )
        c.commit()
    return entries


def purge_session_files(c: sqlite3.Connection, session_key: str) -> None:
    """删会话前把它名下所有附件文件从磁盘清掉,避免孤儿文件堆积。用法同
    purge_session_audio:传入同一条连接,同一事务里先清文件再删行。"""
    rows = c.execute(
        "SELECT files FROM turns WHERE session_key=? AND files IS NOT NULL",
        (session_key,),
    ).fetchall()
    for (raw,) in rows:
        try:
            entries = json.loads(raw) if raw else []
        except (json.JSONDecodeError, ValueError):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            name = e.get("file") or ""
            if name and _FILE_NAME_RE.match(name):
                (config.FILES_DIR / name).unlink(missing_ok=True)
