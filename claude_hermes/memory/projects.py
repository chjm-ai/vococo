"""项目(Web 端)—— 哈希 ↔ 路径。

项目 = 用户选的一个文件夹当 agent 的 cwd,身份即其规范化绝对路径。会话 key 里以
路径短哈希编码(web:p<hash>:<conv>);这张表存 哈希→路径 供 UI 显示。「移除项目」
是软移除(hidden=1):记录与其会话历史都留库,再加回同一文件夹即复活。2026-07-23
从 session_store.py 拆出(见 images.py 顶部说明)。
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from . import _db


def project_hash(path: str) -> str:
    """规范化绝对路径 → 定长短哈希(同文件夹恒得同哈希,天然去重)。"""
    norm = normalize_project_path(path)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def normalize_project_path(path: str) -> str:
    """展开 ~、转绝对路径并消解 .. ——「路径即身份」的规范化基准。"""
    return str(Path(os.path.expanduser(path)).resolve())


def upsert_project(path: str) -> dict:
    """按文件夹路径建/复活项目;返回 {hash, path, name, last_used}。

    同一文件夹已存在则复活(hidden=0)并刷新 last_used;不存在则新建。
    新建/复活都把 sort_order 顶到最前(-now),侧边栏习惯是"新项目出现在最上面"。
    """
    norm = normalize_project_path(path)
    h = project_hash(norm)
    now = time.time()
    c = _db.conn()
    c.execute(
        "INSERT INTO projects(hash, path, last_used, hidden, sort_order) VALUES (?,?,?,0,?) "
        "ON CONFLICT(hash) DO UPDATE SET last_used=excluded.last_used, hidden=0, sort_order=excluded.sort_order",
        (h, norm, now, -now),
    )
    c.commit()
    return {"hash": h, "path": norm, "name": os.path.basename(norm) or norm, "last_used": now}


def list_projects() -> list[dict]:
    """未隐藏的项目,置顶(pinned)优先,组内再按 sort_order 升序。名 = 文件夹名(basename)。"""
    rows = _db.conn().execute(
        "SELECT hash, path, last_used, pinned FROM projects WHERE hidden=0 "
        "ORDER BY pinned DESC, sort_order ASC, last_used DESC"
    ).fetchall()
    return [
        {"hash": h, "path": p, "name": os.path.basename(p) or p, "last_used": ts, "pinned": bool(pin)}
        for h, p, ts, pin in rows
    ]


def reorder_projects(order: list[str]) -> None:
    """侧边栏拖拽落地后整体覆盖排序:sort_order = 数组下标。"""
    c = _db.conn()
    c.executemany(
        "UPDATE projects SET sort_order=? WHERE hash=?",
        [(i, h) for i, h in enumerate(order)],
    )
    c.commit()


def path_for_hash(h: str) -> str | None:
    """按哈希反查文件夹路径(隐藏的也返回,好让在跑的会话仍有 cwd);找不到返回 None。"""
    row = _db.conn().execute(
        "SELECT path FROM projects WHERE hash=?", (h,)
    ).fetchone()
    return row[0] if row else None


def hide_project(h: str) -> None:
    """软移除:仅从列表隐藏,项目记录与其会话历史都保留(可复活)。"""
    c = _db.conn()
    c.execute("UPDATE projects SET hidden=1 WHERE hash=?", (h,))
    c.commit()


def set_project_pinned(h: str, pinned: bool) -> None:
    """置顶/取消置顶:侧边栏「置顶」分组的开关。"""
    c = _db.conn()
    c.execute("UPDATE projects SET pinned=? WHERE hash=?", (1 if pinned else 0, h))
    c.commit()


def touch_project(h: str) -> None:
    """标记项目最近被使用(刷新侧边栏排序)。"""
    c = _db.conn()
    c.execute("UPDATE projects SET last_used=? WHERE hash=?", (time.time(), h))
    c.commit()
