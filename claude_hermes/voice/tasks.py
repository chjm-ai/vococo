"""P1 任务板:任务表 CRUD + 状态机(data/voice/voice.db 的 tasks 表)。

独立连接、独立表,不碰 session.py 的 turns/meta 表,同一 db 文件下互不干扰
(见 00-overview.md §2.4 的隔离约束)。

状态机:queued → running → {done, failed, cancelled};running → cancelled;
另有一条纠错通道 failed → done(见 _ALLOWED_TRANSITIONS 的注释)。
不允许的迁移(如 done → running)一律拒绝,由 set_status() 返回 False 体现。
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path

from .. import config

_DB: sqlite3.Connection | None = None

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"done", "failed", "cancelled"}),
    "done": frozenset(),
    # failed → done 是纠错通道:任务可能被外部误标失败(如另一进程的孤儿回收),
    # 而真正执行它的 executor 随后如实收尾——executor 是任务结局的唯一权威,
    # 它说 done 就允许把误标改回来。2026-07-12 事故:任务干完活提交了代码,
    # finish('done') 却被终态规则静默拒绝,任务板永远停在"失败"。
    "failed": frozenset({"done"}),
    "cancelled": frozenset(),
}


def _db_path() -> Path:
    return config.DATA_DIR / "voice" / "voice.db"


def _conn() -> sqlite3.Connection:
    global _DB
    if _DB is None:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _DB = sqlite3.connect(path, check_same_thread=False)
        _DB.row_factory = sqlite3.Row
        _DB.execute(
            "CREATE TABLE IF NOT EXISTS tasks("
            "id TEXT PRIMARY KEY,"
            "title TEXT NOT NULL,"
            "prompt TEXT NOT NULL,"
            "cwd TEXT,"
            "status TEXT NOT NULL,"
            "progress_note TEXT NOT NULL DEFAULT '',"
            "result_summary TEXT NOT NULL DEFAULT '',"
            "result_full TEXT NOT NULL DEFAULT '',"
            "dispatch_platform TEXT,"
            "dispatch_chat_id TEXT,"
            "parent_task_id TEXT,"
            "created_at REAL NOT NULL,"
            "updated_at REAL NOT NULL)"
        )
        # 向后兼容:为旧表加 dispatch_platform/dispatch_chat_id/parent_task_id(幂等)
        for col in ("dispatch_platform", "dispatch_chat_id", "parent_task_id"):
            try:
                _DB.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        _DB.commit()
    return _DB


def _row(r: sqlite3.Row) -> dict:
    return dict(r)


def create(
    title: str,
    prompt: str,
    cwd: str | None = None,
    dispatch_platform: str | None = None,
    dispatch_chat_id: str | None = None,
    parent_task_id: str | None = None,
) -> dict:
    """落库一条 queued 任务,返回完整行。id 是 8 位短随机串(碰撞概率可忽略)。

    dispatch_platform/dispatch_chat_id: 任务是从哪个平台(web/telegram)、
    哪个会话派来的——终态通知时靠它们回推该发给谁(见 notify.py)。
    parent_task_id: 本任务是对某任务的追加(voice_append_task),存父任务 id。
    """
    c = _conn()
    task_id = secrets.token_hex(4)
    now = time.time()
    c.execute(
        "INSERT INTO tasks(id,title,prompt,cwd,status,progress_note,result_summary,"
        "result_full,dispatch_platform,dispatch_chat_id,parent_task_id,created_at,updated_at) "
        "VALUES (?,?,?,?,'queued','','','',?,?,?,?,?)",
        (task_id, title, prompt, cwd, dispatch_platform, dispatch_chat_id,
         parent_task_id, now, now),
    )
    c.commit()
    return get(task_id)


def get(task_id: str) -> dict | None:
    row = _conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row(row) if row else None


def get_latest() -> dict | None:
    row = _conn().execute(
        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return _row(row) if row else None


def list_recent(limit: int = 20) -> list[dict]:
    rows = _conn().execute(
        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row(r) for r in rows]


def list_queued() -> list[dict]:
    rows = _conn().execute(
        "SELECT * FROM tasks WHERE status='queued' ORDER BY created_at ASC"
    ).fetchall()
    return [_row(r) for r in rows]


def count_running() -> int:
    row = _conn().execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE status='running'"
    ).fetchone()
    return int(row["n"])


def list_children(parent_task_id: str, status: str | None = None) -> list[dict]:
    """查某任务的所有子任务(追加链)。status 非空则进一步筛选(如 'queued')。"""
    if status:
        rows = _conn().execute(
            "SELECT * FROM tasks WHERE parent_task_id=? AND status=? ORDER BY created_at ASC",
            (parent_task_id, status),
        ).fetchall()
    else:
        rows = _conn().execute(
            "SELECT * FROM tasks WHERE parent_task_id=? ORDER BY created_at ASC",
            (parent_task_id,),
        ).fetchall()
    return [_row(r) for r in rows]


def set_status(task_id: str, status: str, progress_note: str | None = None) -> bool:
    """按状态机校验后迁移;非法迁移(如终态再迁移)返回 False,不改库。"""
    cur = get(task_id)
    if cur is None:
        return False
    if status not in _ALLOWED_TRANSITIONS.get(cur["status"], frozenset()):
        return False
    c = _conn()
    if progress_note is None:
        c.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
            (status, time.time(), task_id),
        )
    else:
        c.execute(
            "UPDATE tasks SET status=?, progress_note=?, updated_at=? WHERE id=?",
            (status, progress_note, time.time(), task_id),
        )
    c.commit()
    return True


def set_progress(task_id: str, note: str) -> None:
    """只更新 progress_note(不涉及状态迁移)。终态任务跳过不写——已结束的任务
    不该再有"进展",而且失败原因(如"服务重启,任务中断")就存在 progress_note 里,
    被迟到的进度更新覆盖掉会销毁排障线索(2026-07-12 事故教训)。"""
    c = _conn()
    c.execute(
        "UPDATE tasks SET progress_note=?, updated_at=? WHERE id=? "
        "AND status NOT IN ('done','failed','cancelled')",
        (note, time.time(), task_id),
    )
    c.commit()


def finish(task_id: str, status: str, result_full: str, result_summary: str) -> bool:
    """写终态 + 完整结果 + 摘要。status 必须是 done/failed/cancelled 之一。"""
    if status not in TERMINAL_STATUSES:
        return False
    cur = get(task_id)
    if cur is None or status not in _ALLOWED_TRANSITIONS.get(cur["status"], frozenset()):
        return False
    c = _conn()
    c.execute(
        "UPDATE tasks SET status=?, result_full=?, result_summary=?, updated_at=? WHERE id=?",
        (status, result_full, result_summary, time.time(), task_id),
    )
    c.commit()
    return True


def snapshot_for_prompt(done_limit: int = 5, done_window_sec: int = 1800) -> str:
    """把任务板此刻的真实状态压成几行人话,每轮注入语音会话的指令块(见 prompts.py)。

    2026-07-10 真机事故:任务 19:22:54 就完成了,19:23 模型还嘴硬"那个任务还在跑"
    ——它没调 voice_query_task,纯靠印象猜。模型的临场判断靠不住,就把事实每轮
    塞到它眼前:进行中/排队的全列,最近半小时内结束的带摘要列出来。
    """
    rows = list_recent(20)
    now = time.time()
    active = [r for r in rows if r["status"] in ("queued", "running")]
    recent_done = [
        r for r in rows
        if r["status"] in TERMINAL_STATUSES and now - r["updated_at"] <= done_window_sec
    ][:done_limit]
    if not active and not recent_done:
        return "(任务板是空的:没有在跑的任务,最近半小时也没有刚结束的任务)"
    lines: list[str] = []
    for r in active:
        if r["status"] == "queued":
            lines.append(f"-「{r['title']}」排队中,还没开始跑")
        else:
            mins = int((now - r["created_at"]) // 60)
            note = r["progress_note"] or "刚启动"
            lines.append(f"-「{r['title']}」进行中,已跑约 {mins} 分钟,最新动作:{note}")
    word = {"done": "已完成", "failed": "失败了", "cancelled": "已取消"}
    for r in recent_done:
        summary = r["result_summary"] or r["progress_note"] or "(没有摘要)"
        lines.append(f"-「{r['title']}」{word[r['status']]}:{summary}")
    return "\n".join(lines)


def mark_orphans_failed(exclude_ids: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """serve 重启后调用一次:把残留的 queued/running 任务标记失败(不续跑)。

    queued 一并处理,而不只是 running——本进程的执行器队列在内存里,重启后
    queued 任务永远不会被捡起,放着不管会变成"永远排队中"的僵尸记录。
    exclude_ids:本进程正在跑的任务 id,一律跳过——"孤儿"的定义是没有执行器
    在管的任务,活任务绝不能标死(2026-07-12 "假失败"事故的防线之一)。
    返回受影响的任务(供 executor 逐个走通知分发)。
    """
    c = _conn()
    rows = c.execute(
        "SELECT * FROM tasks WHERE status IN ('queued','running')"
    ).fetchall()
    orphans = [_row(r) for r in rows if r["id"] not in exclude_ids]
    if orphans:
        now = time.time()
        c.executemany(
            "UPDATE tasks SET status='failed', progress_note=?, updated_at=? WHERE id=?",
            [("服务重启,任务中断", now, o["id"]) for o in orphans],
        )
        c.commit()
    return orphans
