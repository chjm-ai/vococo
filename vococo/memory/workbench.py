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
import datetime
import json
import re
import time
import uuid

from .. import config
from . import _db

_STATUSES = {"todo", "done", "focus", "block", "cancelled"}
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
     parent_id, assignee, session_ids, rolled) = row
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
        "rolled": rolled,
    }


_TASK_COLUMNS = (
    "id, project_id, title, detail, status, date, month, week, "
    "images, sort_order, created_at, updated_at, deleted_at, completed_at, "
    "parent_id, assignee, session_ids, rolled"
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

def move_task(task_id: str, parent_id: str | None, project_id: str, order: list[str]) -> dict | None:
    """原子地迁移任务树、更新父级,并只重排"目标层级"里的兄弟顺序。

    order 仍是浏览器当前内存里的全量任务 id(用来校验没有漏任务/多任务,防止
    拖拽发生时客户端数据已过期),但落库时只依据 order 里的相对顺序,重新编号
    task_id 所在这一层的兄弟(同 project + 同 parent_id),不再把 order 的顺序
    整体覆盖回全库——否则网页标签页只要没刷新到最新数据,随手拖一张卡片就会
    把跟这次拖拽无关的其它分支子任务顺序也打乱。
    """
    c = _db.conn()
    rows = c.execute(
        "SELECT id, project_id, parent_id FROM workbench_tasks WHERE deleted_at IS NULL"
    ).fetchall()
    tasks = {row[0]: {"project": row[1], "parentId": row[2]} for row in rows}
    if task_id not in tasks or len(order) != len(tasks) or set(order) != set(tasks):
        return None
    if project_id and c.execute(
        "SELECT 1 FROM workbench_projects WHERE id=? AND archived=0", (project_id,)
    ).fetchone() is None:
        return None

    if parent_id:
        seen = {task_id}
        ancestor_id = parent_id
        while ancestor_id:
            if ancestor_id in seen:
                return None
            seen.add(ancestor_id)
            parent = tasks.get(ancestor_id)
            if parent is None or parent["project"] != project_id:
                return None
            ancestor_id = parent["parentId"]

    children_by_parent: dict[str, list[str]] = {}
    for item_id, item in tasks.items():
        if item["parentId"]:
            children_by_parent.setdefault(item["parentId"], []).append(item_id)
    subtree, seen = [], set()
    pending = [task_id]
    while pending:
        item_id = pending.pop()
        if item_id in seen:
            continue
        seen.add(item_id)
        subtree.append(item_id)
        pending.extend(children_by_parent.get(item_id, []))

    now = time.time()
    c.executemany(
        "UPDATE workbench_tasks SET project_id=?, updated_at=? WHERE id=?",
        [(project_id, now, item_id) for item_id in subtree],
    )
    c.execute("UPDATE workbench_tasks SET parent_id=? WHERE id=?", (parent_id, task_id))

    # 只重排 task_id 落地后所在的那一层兄弟(同 project、同 parent_id),
    # 其它分支的 sort_order 原样不动。
    siblings = {
        item_id for item_id, item in tasks.items()
        if item_id != task_id and item["parentId"] == parent_id and item["project"] == project_id
    }
    siblings.add(task_id)
    group_order = [item_id for item_id in order if item_id in siblings]
    c.executemany(
        "UPDATE workbench_tasks SET sort_order=? WHERE id=?",
        [(i, item_id) for i, item_id in enumerate(group_order)],
    )
    c.commit()
    return get_task(task_id)



_ROLL_KINDS = {"date", "week", "month"}


def _roll_overdue_tasks() -> None:
    """惰性版 Things 的 Today:读取任务前,把过期未完成任务的 date/week/month 滚到当前周期
    (昨天→今天、上周→本周、上月→本月),已完成/已取消的保留原时间不动(日志要用)。
    date/week/month 三者互斥使用(建任务时只填其中一档更细的,粗档跟着派生),
    所以按 date > week > month 优先级只判一档就够,不会重复触发。

    滚动的同时把 rolled 标记成命中的那一档(date/week/month),供前端在对应的日/周/月
    视图顶部弹黄条提醒 + 任务名前点小黄点;点掉提醒走 dismiss_rollover() 清空。
    """
    today = datetime.date.today()
    today_str = today.isoformat()
    week_str = (today - datetime.timedelta(days=today.weekday())).isoformat()
    month_str = today_str[:7]
    c = _db.conn()
    rows = c.execute(
        "SELECT id, date, week, month FROM workbench_tasks "
        "WHERE deleted_at IS NULL AND status NOT IN ('done','cancelled')"
    ).fetchall()
    now = time.time()
    updates = []
    for task_id, date, week, month in rows:
        if date:
            if date < today_str:
                updates.append((today_str, month_str, week_str, "date", now, task_id))
        elif week:
            if week < week_str:
                updates.append((None, month_str, week_str, "week", now, task_id))
        elif month:
            if month < month_str:
                updates.append((None, month_str, None, "month", now, task_id))
    if not updates:
        return
    c.executemany(
        "UPDATE workbench_tasks SET date=?, month=?, week=?, rolled=?, updated_at=? WHERE id=?",
        updates,
    )
    c.commit()


def dismiss_rollover(kind: str) -> int:
    """清掉某一档(date/week/month,对应日/周/月视图)已读的滚动提醒,返回清掉的条数。"""
    if kind not in _ROLL_KINDS:
        return 0
    c = _db.conn()
    cur = c.execute("UPDATE workbench_tasks SET rolled=NULL WHERE rolled=?", (kind,))
    c.commit()
    return cur.rowcount


def list_tasks() -> list[dict]:
    """全量任务(个人规模全量拉取即可;按日/周/月分组、按项目筛选交给前端)。
    不含已软删除(回收站)的任务,那份数据走 list_deleted_tasks() 单独懒加载。
    """
    _seed_if_empty()
    _roll_overdue_tasks()
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
         now if status in ("done", "cancelled") else None,
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
        # 完成时间跟着 status 走:切到 done/cancelled 记下此刻,切回其它状态就清空——
        # 「日志」按天分组要的是真实完成/取消时刻,不能拿 updated_at(编辑标题/备注也会碰它)顶替。
        if key == "status":
            sets.append("completed_at=?")
            params.append(time.time() if value in ("done", "cancelled") else None)
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
