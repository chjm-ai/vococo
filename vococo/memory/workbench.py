"""工作台(GTD 待办看板)—— 项目/任务 CRUD,SQLite 存储。

工作台前端(gateway/adapters/web_static/workbench.js)最早是一份纯前端 demo,数据
写死在 WORKBENCH_DEMO 常量里。_seed_if_empty() 把那份已经维护好的真实任务清单原样
迁进 state.db,只在库是空的(首次启动)时插一次,不让切后端这一刀把内容丢了;
此后前端改走 /workbench/* 接口读写,不再改这份种子。

project_id/task_id 新建时用 uuid4 短串(种子数据例外,沿用 demo 里本来的可读 id,
方便肉眼核对迁移是否走样)。
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import time
import uuid

from .. import config
from . import _db

_STATUSES = {"todo", "done", "focus", "block"}
_ASSIGNEES = {"human", "ai"}
_IMG_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_EXT_STRIP_RE = re.compile(r"[^a-z0-9]")

_OBSIDIAN_ROOT = (
    "/Users/wesley/Library/Mobile Documents/iCloud~md~obsidian/Documents/Wesley notes"
)

_SEED_PROJECTS = [
    {"id": "consulting", "name": "AI 咨询"},
    {"id": "vocotrade", "name": "VocoTrade"},
    {"id": "fabric", "name": "面料外贸"},
    {"id": "transition", "name": "离职过渡"},
]
_SEED_TASKS = [
    {"id": "talk-script", "project": "consulting", "title": "约人话术准备", "status": "done", "month": "2026-08", "week": "2026-08-17", "date": "2026-08-18"},
    {"id": "meet-network", "project": "consulting", "title": "约见第一梯队：胜源、喆铭", "status": "focus", "month": "2026-08", "week": "2026-08-17", "date": "2026-08-20"},
    {"id": "case-page", "project": "consulting", "title": "案例单页初稿（vococo + VocoTrade）", "status": "todo", "month": "2026-08", "week": "2026-08-17", "date": None},
    {"id": "agent-wrap", "project": "vocotrade", "title": "邮件获客 agent 收口", "status": "focus", "month": "2026-08", "week": "2026-08-17", "date": "2026-08-20"},
    {"id": "video-path", "project": "vocotrade", "title": "研究 AI 剪辑宣传视频链路", "status": "todo", "month": "2026-08", "week": "2026-08-17", "date": None},
    {"id": "material-direction", "project": "vocotrade", "title": "确定宣传素材的第一版方向", "status": "todo", "month": "2026-08", "week": None, "date": None},
    {"id": "crawler-plan", "project": "fabric", "title": "确认 B12–B16 爬虫排产的下一步", "status": "block", "month": "2026-08", "week": "2026-08-17", "date": None},
    {"id": "difs", "project": "fabric", "title": "DIFS 展会预热：核对 B 批补发", "status": "todo", "month": "2026-08", "week": "2026-08-17", "date": "2026-08-21"},
    {"id": "lemlist", "project": "fabric", "title": "跟踪 BD-Knit-01 回复与退信", "status": "todo", "month": "2026-08", "week": "2026-08-17", "date": None},
    {"id": "contract", "project": "transition", "title": "确认竞业协议条款原文", "status": "focus", "month": "2026-08", "week": "2026-08-17", "date": "2026-08-20"},
    {"id": "family-talk", "project": "transition", "title": "跟小雯同步创业计划与对外说法", "status": "todo", "month": "2026-08", "week": "2026-08-17", "date": None},
    {"id": "mortgage", "project": "transition", "title": "房贷延期材料咨询", "status": "todo", "month": "2026-08", "week": "2026-08-17", "date": None},
]

def _seed_if_empty() -> None:
    """首次(workbench_projects 表为空)把 demo 内容当种子导入,此后不再触碰。

    按次查 COUNT 而非缓存一个「已跑过」标记:后者在 DB 被重置(如测试用例的
    isolated fixture、_db.reset())后仍会误判「已种过」从而漏种,COUNT 本身在
    这么小的表上代价可忽略,按次查更稳妥。
    """
    c = _db.conn()
    if c.execute("SELECT COUNT(*) FROM workbench_projects").fetchone()[0] > 0:
        return
    now = time.time()
    c.executemany(
        "INSERT INTO workbench_projects(id, name, sort_order, archived) VALUES (?,?,?,0)",
        [(p["id"], p["name"], i) for i, p in enumerate(_SEED_PROJECTS)],
    )
    c.executemany(
        "INSERT INTO workbench_tasks(id, project_id, title, detail, status, date, month, week, "
        "images, sort_order, created_at, updated_at, deleted_at, completed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (t["id"], t["project"], t["title"], "", t["status"], t["date"], t["month"], t["week"],
             "[]", i, now, now, None,
             now if t["status"] == "done" else None)
            for i, t in enumerate(_SEED_TASKS)
        ],
    )
    c.commit()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _row_to_project(row) -> dict:
    id_, name, sort_order = row
    return {"id": id_, "name": name, "sortOrder": sort_order}


def _row_to_task(row) -> dict:
    (id_, project_id, title, detail, status, date, month, week,
     images, sort_order, created_at, updated_at, deleted_at, completed_at,
     parent_id, assignee, session_ids) = row
    return {
        "id": id_,
        "project": project_id,
        "title": title,
        "detail": detail,
        "status": status,
        "date": date,
        "month": month,
        "week": week,
        "images": json.loads(images or "[]"),
        "sortOrder": sort_order,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "deletedAt": deleted_at,
        "completedAt": completed_at,
        "parentId": parent_id,
        "assignee": assignee or "human",
        "sessionIds": json.loads(session_ids or "[]"),
    }


_TASK_COLUMNS = (
    "id, project_id, title, detail, status, date, month, week, "
    "images, sort_order, created_at, updated_at, deleted_at, completed_at, "
    "parent_id, assignee, session_ids"
)


# ── 项目 ────────────────────────────────────────────────────────────────

def list_projects() -> list[dict]:
    _seed_if_empty()
    rows = _db.conn().execute(
        "SELECT id, name, sort_order FROM workbench_projects WHERE archived=0 ORDER BY sort_order ASC"
    ).fetchall()
    return [_row_to_project(r) for r in rows]


def create_project(name: str) -> dict | None:
    name = name.strip()
    if not name:
        return None
    _seed_if_empty()
    c = _db.conn()
    max_order = c.execute("SELECT COALESCE(MAX(sort_order), -1) FROM workbench_projects").fetchone()[0]
    project_id = _new_id("wp")
    c.execute(
        "INSERT INTO workbench_projects(id, name, sort_order, archived) VALUES (?,?,?,0)",
        (project_id, name, max_order + 1),
    )
    c.commit()
    return {"id": project_id, "name": name, "sortOrder": max_order + 1}


def rename_project(project_id: str, name: str) -> dict | None:
    name = name.strip()
    if not name:
        return None
    c = _db.conn()
    cur = c.execute(
        "UPDATE workbench_projects SET name=? WHERE id=? AND archived=0", (name, project_id)
    )
    c.commit()
    return {"id": project_id, "name": name} if cur.rowcount else None


def archive_project(project_id: str) -> None:
    """软删除:项目从列表隐藏,名下任务原样留在库里(不级联删除,可用 rename 反悔)。"""
    c = _db.conn()
    c.execute("UPDATE workbench_projects SET archived=1 WHERE id=?", (project_id,))
    c.commit()


def reorder_projects(order: list[str]) -> None:
    c = _db.conn()
    c.executemany(
        "UPDATE workbench_projects SET sort_order=? WHERE id=?",
        [(i, pid) for i, pid in enumerate(order)],
    )
    c.commit()


# ── 任务 ────────────────────────────────────────────────────────────────

def move_task(task_id: str, parent_id: str | None, order: list[str]) -> dict | None:
    """原子地更新任务父级与全局显示顺序，避免刷新后拖拽结果丢失。"""
    c = _db.conn()
    task = c.execute(
        "SELECT project_id FROM workbench_tasks WHERE id=? AND deleted_at IS NULL", (task_id,)
    ).fetchone()
    active_ids = [r[0] for r in c.execute(
        "SELECT id FROM workbench_tasks WHERE deleted_at IS NULL"
    ).fetchall()]
    if task is None or len(order) != len(active_ids) or set(order) != set(active_ids):
        return None

    if parent_id:
        seen = {task_id}
        ancestor_id = parent_id
        while ancestor_id:
            if ancestor_id in seen:
                return None
            seen.add(ancestor_id)
            parent = c.execute(
                "SELECT project_id, parent_id FROM workbench_tasks WHERE id=? AND deleted_at IS NULL",
                (ancestor_id,),
            ).fetchone()
            if parent is None or parent[0] != task[0]:
                return None
            ancestor_id = parent[1]

    c.execute(
        "UPDATE workbench_tasks SET parent_id=?, updated_at=? WHERE id=?",
        (parent_id, time.time(), task_id),
    )
    c.executemany(
        "UPDATE workbench_tasks SET sort_order=? WHERE id=?",
        [(i, item_id) for i, item_id in enumerate(order)],
    )
    c.commit()
    return get_task(task_id)



def list_tasks() -> list[dict]:
    """全量任务(个人规模全量拉取即可;按日/周/月分组、按项目筛选交给前端)。
    不含已软删除(回收站)的任务,那份数据走 list_deleted_tasks() 单独懒加载。
    """
    _seed_if_empty()
    rows = _db.conn().execute(
        f"SELECT {_TASK_COLUMNS} FROM workbench_tasks WHERE deleted_at IS NULL ORDER BY sort_order ASC"
    ).fetchall()
    return [_row_to_task(r) for r in rows]


def list_deleted_tasks() -> list[dict]:
    """回收站:按删除时间倒序,最近删的排最前。"""
    rows = _db.conn().execute(
        f"SELECT {_TASK_COLUMNS} FROM workbench_tasks WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
    ).fetchall()
    return [_row_to_task(r) for r in rows]


def get_task(task_id: str) -> dict | None:
    row = _db.conn().execute(
        f"SELECT {_TASK_COLUMNS} FROM workbench_tasks WHERE id=?", (task_id,)
    ).fetchone()
    return _row_to_task(row) if row else None


def create_task(
    project_id: str, title: str, *, detail: str = "", status: str = "todo",
    date: str | None = None, month: str | None = None, week: str | None = None,
    parent_id: str | None = None, assignee: str = "human",
) -> dict | None:
    title = title.strip()
    if not title or status not in _STATUSES:
        return None
    if assignee not in _ASSIGNEES:
        assignee = "human"
    now = time.time()
    c = _db.conn()
    max_order = c.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM workbench_tasks WHERE project_id=?", (project_id,)
    ).fetchone()[0]
    task_id = _new_id("wt")
    c.execute(
        "INSERT INTO workbench_tasks(id, project_id, title, detail, status, date, month, week, "
        "images, sort_order, created_at, updated_at, deleted_at, completed_at, "
        "parent_id, assignee, session_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, project_id, title, detail, status, date, month, week,
         "[]", max_order + 1, now, now, None,
         now if status == "done" else None,
         parent_id, assignee, "[]"),
    )
    c.commit()
    return get_task(task_id)


_UPDATABLE_FIELDS = {"title", "detail", "status", "date", "month", "week", "project", "assignee", "parentId"}


def update_task(task_id: str, **fields) -> dict | None:
    """部分更新;只接受白名单字段,status 校验枚举,project 映射到 project_id 列。"""
    sets, params = [], []
    for key, value in fields.items():
        if key not in _UPDATABLE_FIELDS:
            continue
        if key == "status" and value not in _STATUSES:
            continue
        if key == "assignee" and value not in _ASSIGNEES:
            continue
        if key == "title":
            value = value.strip()
            if not value:
                continue
        column = {"project": "project_id", "parentId": "parent_id"}.get(key, key)
        sets.append(f"{column}=?")
        params.append(value)
        # 完成时间跟着 status 走:切到 done 记下此刻,切回其它状态就清空——
        # 「已完成」按天分组要的是真实完成时刻,不能拿 updated_at(编辑标题/备注也会碰它)顶替。
        if key == "status":
            sets.append("completed_at=?")
            params.append(time.time() if value == "done" else None)
    if not sets:
        return get_task(task_id)
    sets.append("updated_at=?")
    params.append(time.time())
    params.append(task_id)
    c = _db.conn()
    cur = c.execute(f"UPDATE workbench_tasks SET {', '.join(sets)} WHERE id=?", params)
    c.commit()
    return get_task(task_id) if cur.rowcount else None


def link_session(task_id: str, session_id: str) -> dict | None:
    """往任务的 session_ids 数组追加一个会话 ID。"""
    task = get_task(task_id)
    if task is None:
        return None
    ids = task["sessionIds"]
    if session_id not in ids:
        ids.append(session_id)
    c = _db.conn()
    c.execute(
        "UPDATE workbench_tasks SET session_ids=?, updated_at=? WHERE id=?",
        (json.dumps(ids, ensure_ascii=False), time.time(), task_id),
    )
    c.commit()
    return get_task(task_id)


def list_children(parent_id: str) -> list[dict]:
    """列出某个父任务下的所有子任务。"""
    rows = _db.conn().execute(
        f"SELECT {_TASK_COLUMNS} FROM workbench_tasks WHERE parent_id=? AND deleted_at IS NULL ORDER BY sort_order ASC",
        (parent_id,),
    ).fetchall()
    return [_row_to_task(r) for r in rows]


def delete_task(task_id: str) -> bool:
    """软删除:移入回收站,图片原样留着(万一要恢复)。真正落盘删除见 purge_task。"""
    c = _db.conn()
    cur = c.execute(
        "UPDATE workbench_tasks SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
        (time.time(), task_id),
    )
    c.commit()
    return cur.rowcount > 0


def restore_task(task_id: str) -> dict | None:
    c = _db.conn()
    cur = c.execute(
        "UPDATE workbench_tasks SET deleted_at=NULL, updated_at=? WHERE id=? AND deleted_at IS NOT NULL",
        (time.time(), task_id),
    )
    c.commit()
    return get_task(task_id) if cur.rowcount else None


def purge_task(task_id: str) -> bool:
    """回收站里的「彻底删除」:只允许清已经软删除过的任务,防止误删还在用的任务。"""
    task = get_task(task_id)
    if task is None or task["deletedAt"] is None:
        return False
    c = _db.conn()
    c.execute("DELETE FROM workbench_tasks WHERE id=?", (task_id,))
    c.commit()
    for name in task["images"]:
        _unlink_image(name)
    return True


def empty_trash() -> int:
    """清空回收站:彻底删除所有已软删除的任务及其图片,返回删掉的条数。"""
    deleted = list_deleted_tasks()
    c = _db.conn()
    c.execute("DELETE FROM workbench_tasks WHERE deleted_at IS NOT NULL")
    c.commit()
    for task in deleted:
        for name in task["images"]:
            _unlink_image(name)
    return len(deleted)


# ── 任务图片(复用 config.IMAGES_DIR,文件名走既有 /image?name= 回显接口) ──────

def _img_ext(media_type: str) -> str:
    ext = (media_type or "").split("/")[-1].split(";")[0].strip().lower()
    ext = _EXT_STRIP_RE.sub("", ext)
    return ext or "png"


def _unlink_image(name: str) -> None:
    if _IMG_NAME_RE.match(name or ""):
        (config.IMAGES_DIR / name).unlink(missing_ok=True)


def add_task_image(task_id: str, data_b64: str, media_type: str) -> str | None:
    task = get_task(task_id)
    if task is None:
        return None
    try:
        raw = base64.b64decode(data_b64)
    except (binascii.Error, ValueError):
        return None
    config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    name = f"wb_{uuid.uuid4().hex[:16]}.{_img_ext(media_type)}"
    (config.IMAGES_DIR / name).write_bytes(raw)
    images = task["images"] + [name]
    c = _db.conn()
    c.execute(
        "UPDATE workbench_tasks SET images=?, updated_at=? WHERE id=?",
        (json.dumps(images, ensure_ascii=False), time.time(), task_id),
    )
    c.commit()
    return name


def remove_task_image(task_id: str, name: str) -> bool:
    task = get_task(task_id)
    if task is None or name not in task["images"]:
        return False
    images = [n for n in task["images"] if n != name]
    c = _db.conn()
    c.execute(
        "UPDATE workbench_tasks SET images=?, updated_at=? WHERE id=?",
        (json.dumps(images, ensure_ascii=False), time.time(), task_id),
    )
    c.commit()
    _unlink_image(name)
    return True
