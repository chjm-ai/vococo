"""统一后台任务表:任务 CRUD + 状态机(data/voice/voice.db 的 tasks 表)。

独立连接、独立表,不碰 session.py 的 turns/meta 表,同一 db 文件下互不干扰
(见 00-overview.md §2.4 的隔离约束)。

2026-07-29 通用化:原来叫 voice/tasks.py,只服务语音派发的后台任务。语音派发、
cron 定时、普通会话发起"独立新会话"三种触发方式本质是同一件事——都是"没人盯着、
后台自己跑一轮,需要并发上限/进度追踪/完成通知"——只是"谁触发的"不同,所以搬出
voice 包、session_key 前缀从 voice-task: 改成中性的 task:,并加 origin 字段
(voice/cron/chat)区分触发方,不再靠前缀名字猜。db 文件路径(data/voice/voice.db)
和表名(tasks)保留不动,只是历史遗留,不影响功能,避免无谓的文件搬迁风险。

状态机:queued → running → {done, failed, cancelled};running → cancelled;
另有两条纠错/续接通道:failed → done、{done,failed,cancelled} → running
(追问/cron 再次触发在原任务上原地重开一轮,见 task_runner.append,不再产生新任务行)。
不允许的迁移一律拒绝,由 set_status() 返回 False 体现。
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path

from .. import config
from .task_words import status_word

_DB: sqlite3.Connection | None = None

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})

# 后台任务的 session_key 就是这个前缀 + 任务 id(见 core/worktree.py、core/task_runner.py、
# voice/notify.py 的构造侧,gateway/run.py、gateway/adapters/web.py 的解析侧)——统一经
# session_key()/task_id_from_session_key() 两个函数读写,不再各处手写字符串拼接/split,
# 避免约定改了却漏改某个调用点(2026-07-23 架构复盘;2026-07-29 从 voice-task: 改名)。
SESSION_KEY_PREFIX = "task:"

# 触发方枚举:UI/通知按这个字段分组,不再靠 session_key 前缀猜(2026-07-29 起前缀
# 统一成 task:,以前 voice-task:/cron-task: 两个前缀各自为政的年代已经结束)。
ORIGINS = frozenset({"voice", "cron", "chat"})


def session_key(task_id: str) -> str:
    """任务 id → 该任务对应的会话 key。"""
    return f"{SESSION_KEY_PREFIX}{task_id}"


def task_id_from_session_key(key: str) -> str | None:
    """会话 key → 任务 id;不是本模块的 session_key() 前缀则返回 None。"""
    if not key or not key.startswith(SESSION_KEY_PREFIX):
        return None
    return key[len(SESSION_KEY_PREFIX):]

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"done", "failed", "cancelled"}),
    # done/cancelled → running:「追问」/cron 再次触发的重开通道——在同一个任务上
    # 原地续接一轮(resume 同一条 SDK 会话、复用同一个 worktree),不产生新任务行。
    "done": frozenset({"running"}),
    # failed → done 是纠错通道:任务可能被外部误标失败(如另一进程的孤儿回收),
    # 而真正执行它的 task_runner 随后如实收尾——task_runner 是任务结局的唯一权威,
    # 它说 done 就允许把误标改回来。2026-07-12 事故:任务干完活提交了代码,
    # finish('done') 却被终态规则静默拒绝,任务板永远停在"失败"。
    # failed → running 同样是追问/cron 重开通道。
    "failed": frozenset({"done", "running"}),
    "cancelled": frozenset({"running"}),
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
        # 向后兼容:为旧表加 dispatch_platform/dispatch_chat_id/parent_task_id/origin(幂等)
        for col, ddl in (
            ("dispatch_platform", "dispatch_platform TEXT"),
            ("dispatch_chat_id", "dispatch_chat_id TEXT"),
            ("parent_task_id", "parent_task_id TEXT"),
            # 老数据(改名前落库的行)一律是语音派发的,默认值 'voice' 准确反映历史事实。
            ("origin", "origin TEXT NOT NULL DEFAULT 'voice'"),
        ):
            try:
                _DB.execute(f"ALTER TABLE tasks ADD COLUMN {ddl}")
            except sqlite3.OperationalError:
                pass
        # 已移除 SDK 任务清单到任务表的镜像。历史镜像任务没有后台执行器,
        # 启动后必须静默冻结,避免被排队器误跑或被重启孤儿回收误报失败。
        _DB.execute(
            "UPDATE tasks SET status='cancelled', progress_note=?, updated_at=? "
            "WHERE origin='task' AND status IN ('queued','running')",
            ("SDK任务清单同步已停用", time.time()),
        )
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
    origin: str = "voice",
    task_id: str | None = None,
) -> dict:
    """落库一条 queued 任务,返回完整行。

    task_id 不传则随机生成 8 位短随机串(碰撞概率可忽略);cron 定时任务传显式
    task_id=job_id——定时任务是"反复触发同一个身份",直接复用 job 自己的 id 作为
    task id,让每次到点触发天然映射成"对同一个任务 append 一轮"(见 task_runner.append),
    不必每次触发都另开一行。

    dispatch_platform/dispatch_chat_id: 任务是从哪个平台、哪个会话派来的——终态
    通知时靠它们回推该发给谁(见 voice/notify.py)。origin 标记触发方,必须是
    ORIGINS 之一。
    """
    c = _conn()
    tid = task_id or secrets.token_hex(4)
    now = time.time()
    c.execute(
        "INSERT INTO tasks(id,title,prompt,cwd,status,progress_note,result_summary,"
        "result_full,dispatch_platform,dispatch_chat_id,origin,created_at,updated_at) "
        "VALUES (?,?,?,?,'queued','','','',?,?,?,?,?)",
        (tid, title, prompt, cwd, dispatch_platform, dispatch_chat_id, origin, now, now),
    )
    c.commit()
    return get(tid)


def update_prompt(task_id: str, new_prompt: str) -> None:
    """只在任务还没起跑(queued)时用:追问撞上还在排队的任务,把新指令并进
    待执行的 prompt——还没开始跑,不涉及 resume,直接改内容最简单。"""
    c = _conn()
    c.execute(
        "UPDATE tasks SET prompt=?, updated_at=? WHERE id=? AND status='queued'",
        (new_prompt, time.time(), task_id),
    )
    c.commit()


def get(task_id: str) -> dict | None:
    row = _conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row(row) if row else None


def get_latest() -> dict | None:
    row = _conn().execute(
        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return _row(row) if row else None


def list_recent(
    limit: int = 20,
    origin: str | None = None,
    dispatch_chat_id: str | None = None,
) -> list[dict]:
    """最近的任务,按 origin/dispatch_chat_id 可选过滤。"""
    c = _conn()
    params: list = []
    where: list[str] = []
    if origin is not None:
        where.append("origin=?")
        params.append(origin)
    if dispatch_chat_id is not None:
        where.append("dispatch_chat_id=?")
        params.append(dispatch_chat_id)
    sql = "SELECT * FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = c.execute(sql, params).fetchall()
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


def snapshot_for_prompt(done_limit: int = 5, done_window_sec: int = 86400, origin: str | None = None) -> str:
    """把任务板此刻的真实状态压成几行人话,每轮注入语音会话的指令块(见 voice/prompts.py)。

    origin='voice' 时只给语音看它自己派发的任务(2026-07-29:任务板现在也装着
    cron/chat 触发的任务,语音不该替用户口头汇报"你刚在网页上让我开的那个调研"
    这类它没有上下文的任务)。

    2026-07-10 真机事故:任务 19:22:54 就完成了,19:23 模型还嘴硬"那个任务还在跑"
    ——它没调 voice_query_session,纯靠印象猜。模型的临场判断靠不住,就把事实每轮
    塞到它眼前:进行中/排队的全列,24 小时内结束的带摘要列出来。

    2026-08-04:done 窗口从 30 分钟放宽到 24h,且每行带 session_id——续接决策
    (voice/prompts.py 规则8)需要"任务做完隔了一阵又说接着做"时快照里还能看到
    原任务,并能直接拿 id 调 voice_continue_session,不用再绕 voice_list_sessions。
    """
    rows = list_recent(20, origin=origin)
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
            lines.append(f"-「{r['title']}」(session_id={r['id']})排队中,还没开始跑")
        else:
            mins = int((now - r["created_at"]) // 60)
            note = r["progress_note"] or "刚启动"
            lines.append(f"-「{r['title']}」(session_id={r['id']})进行中,已跑约 {mins} 分钟,最新动作:{note}")
    for r in recent_done:
        summary = r["result_summary"] or r["progress_note"] or "(没有摘要)"
        lines.append(f"-「{r['title']}」(session_id={r['id']}){status_word(r['status'])}:{summary}")
    return "\n".join(lines)


def mark_orphans_failed(exclude_ids: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """serve 重启后调用一次:把残留的 queued/running 任务标记失败(不续跑)。

    queued 一并处理,而不只是 running——本进程的执行器队列在内存里,重启后
    queued 任务永远不会被捡起,放着不管会变成"永远排队中"的僵尸记录。
    exclude_ids:本进程正在跑的任务 id,一律跳过——"孤儿"的定义是没有执行器
    在管的任务,活任务绝不能标死(2026-07-12 "假失败"事故的防线之一)。
    返回受影响的任务(供 task_runner 逐个走通知分发)。
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
