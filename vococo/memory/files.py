"""用户通用文件附件的历史元数据持久化。

文件正文只在当前轮次内送进模型,这里仅保存文件名和 MIME 类型,供刷新后的
历史气泡继续显示附件信息。文件本体不落盘,避免会话删除时留下无法管理的副本。
"""
from __future__ import annotations

import json

from . import _db


def save_turn_files(turn_id: int, files: list) -> list[dict]:
    """把该轮附件的显示元数据写进 turns.files。"""
    if not files:
        return []
    entries: list[dict] = []
    for file in files:
        filename = str(getattr(file, "filename", "") or "").strip()
        if not filename:
            continue
        entries.append({
            "name": filename,
            "media_type": str(getattr(file, "media_type", "") or ""),
        })
    if entries:
        c = _db.conn()
        c.execute(
            "UPDATE turns SET files=? WHERE id=?",
            (json.dumps(entries, ensure_ascii=False), turn_id),
        )
        c.commit()
    return entries
