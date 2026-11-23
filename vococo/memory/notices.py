"""待处理通知(铃铛)—— 所有「等你回答的提问」和「等你批的操作」(state.db notices 表)。

2026-09-28 主人反馈:提问/审批的选项经常超时,超时后按钮点了只回一句「已过期」,事情就断了。
这里把每一次弹出的选项都记下来,整个生命周期:

  pending   正在等你回答(gateway/clarify.register 带 notice 时创建,选项还能直接点)
  expired   超时 / 本轮结束 / 进程重启 / 后台任务夜间免打扰 —— 【仍然可以处理】:
            点选项 = 把你的选择补回原会话让它接着干(见 gateway/notice_actions.act)
  answered  已回答(按时点了,或事后补答)
  approved_unrun 已补批，但许可未被使用；verify_required 执行结果不明，须先核对
  replaying 正在尝试恢复或重跑；dismissed 你点了忽略

kind:ask(ask_user 提问)/ approval(危险操作审批,带 reason/detail/tool 与可存规则信息)/
decision(2026-10-10 待拍板事项:request_decision 工具登记,不阻塞任何一轮,直接以 expired 入库、
session_key 是所属 Agent 的主会话;点选项 = 把选择发回那个主会话接着执行)/ review(规则清理提醒)。
open = pending + expired + 待恢复项，就是铃铛上的数字。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Callable

from . import _db

RECOVERY_STATUSES = ("approved_unrun", "verify_required")
OPEN_STATUSES = ("pending", "expired", *RECOVERY_STATUSES)
_OPEN_SQL = "'pending','expired','approved_unrun','verify_required'"
_listener: Callable[[dict], None] | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notices(
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  updated_at REAL NOT NULL,
  session_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  prompt TEXT NOT NULL DEFAULT '',
  options TEXT NOT NULL DEFAULT '[]',
  clarify_id TEXT,
  status TEXT NOT NULL,
  answer TEXT,
  reason TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  tool TEXT NOT NULL DEFAULT '',
  rule_kind TEXT,
  rule_scope TEXT,
  rule_label TEXT
);
CREATE INDEX IF NOT EXISTS idx_notices_status ON notices(status, ts);
CREATE INDEX IF NOT EXISTS idx_notices_clarify ON notices(clarify_id);
"""
_ready_for = None  # 已建表的那条连接(测试会换库重连,换了就重新建表)


def _conn():
    global _ready_for
    c = _db.conn()
    if c is not _ready_for:
        c.executescript(_SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(notices)")}
        for name, kind in (("source_turn_id", "INTEGER"), ("operation_key", "TEXT"),
                           ("resume_model", "TEXT"), ("resume_sdk", "TEXT")):
            if name not in cols:
                c.execute(f"ALTER TABLE notices ADD COLUMN {name} {kind}")
        _ready_for = c
    return c


def set_listener(fn: Callable[[dict], None] | None) -> None:
    """通知有变化时的回调(Web 端据此刷新铃铛并收起已结束的审批)。"""
    global _listener
    _listener = fn


def _changed(change: dict | None = None) -> None:
    if _listener is not None:
        try:
            _listener(change or {})
        except Exception as exc:  # noqa: BLE001 —— 刷新推送失败不影响业务
            print(f"[notices] 变更通知失败:{exc}", flush=True)


_COLS = (
    "id, ts, updated_at, session_key, kind, prompt, options, clarify_id, status, answer, "
    "reason, detail, tool, rule_kind, rule_scope, rule_label, source_turn_id, operation_key, "
    "resume_model, resume_sdk"
)


def _row(r) -> dict:
    d = dict(zip([c.strip() for c in _COLS.split(",")], r))
    d["options"] = json.loads(d["options"] or "[]")
    return d


def add(
    *, session_key: str, kind: str, prompt: str, options: list[str] | None = None,
    clarify_id: str | None = None, status: str = "pending", reason: str = "",
    detail: str = "", tool: str = "", rule: tuple[str, str, str] | None = None,
    source_turn_id: int | None = None, operation_key: str | None = None,
    resume_model: str | None = None, resume_sdk: str | None = None,
) -> str:
    """登记一条通知。直接以 expired 登记的(后台免打扰/无通道)按 会话+原因+详情 去重。"""
    c = _conn()
    if status == "expired":
        row = c.execute(
            "SELECT id FROM notices WHERE session_key=? AND kind=? AND reason=? AND detail=? "
            "AND source_turn_id IS ? AND operation_key IS ? AND status IN ('pending','expired')",
            (session_key, kind, reason, detail[:500], source_turn_id, operation_key),
        ).fetchone()
        if row:
            return row[0]
    nid = uuid.uuid4().hex[:10]
    now = time.time()
    c.execute(
        f"INSERT INTO notices({_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            nid, now, now, session_key, kind, prompt[:2000], json.dumps(options or [], ensure_ascii=False),
            clarify_id, status, None, reason, detail[:500], tool,
            rule[0] if rule else None, rule[1] if rule else None, rule[2] if rule else None,
            source_turn_id, operation_key, resume_model, resume_sdk,
        ),
    )
    c.commit()
    _changed()
    return nid


def get(nid: str) -> dict | None:
    r = _conn().execute(f"SELECT {_COLS} FROM notices WHERE id=?", (nid,)).fetchone()
    return _row(r) if r else None


def by_clarify(clarify_id: str) -> dict | None:
    r = _conn().execute(
        f"SELECT {_COLS} FROM notices WHERE clarify_id=?", (clarify_id,)
    ).fetchone()
    return _row(r) if r else None


def set_status(nid: str, status: str, answer: str | None = None, *, expected: str | None = None) -> bool:
    c = _conn()
    cur = c.execute(
        "UPDATE notices SET status=?, answer=COALESCE(?, answer), updated_at=? WHERE id=? AND (? IS NULL OR status=?)",
        (status, answer, time.time(), nid, expected, expected),
    )
    c.commit()
    if cur.rowcount:
        notice = get(nid)
        _changed({"clarify_id": notice["clarify_id"], "status": status})
    return cur.rowcount > 0


def list_open(limit: int = 100) -> list[dict]:
    rows = _conn().execute(
        f"SELECT {_COLS} FROM notices WHERE status IN ({_OPEN_SQL}) "
        "ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row(r) for r in rows]


def count_open_since(ts: float) -> int:
    return _conn().execute(
        f"SELECT COUNT(*) FROM notices WHERE status IN ({_OPEN_SQL}) AND ts >= ?", (ts,)
    ).fetchone()[0]


def expire_all_pending() -> int:
    """进程重启:之前在等的选项已经没人接了,统一改成 expired(仍可事后处理)。"""
    c = _conn()
    cur = c.execute(
        "UPDATE notices SET status='expired', updated_at=? WHERE status='pending'", (time.time(),)
    )
    c.execute("UPDATE notices SET status='verify_required', updated_at=? WHERE status='replaying'", (time.time(),))
    c.commit()
    return cur.rowcount


def recover_legacy_approvals() -> None:
    """仅恢复旧版已误关、且同一条补批明确因缺少原会话而未启动的记录。"""
    c = _conn()
    c.execute("""WITH failed AS MATERIALIZED (
          SELECT n.id, MAX(t.id) AS turn_id, n.session_key
          FROM notices n JOIN turns t ON t.session_key=n.session_key
          WHERE n.status='answered' AND n.kind='approval' AND n.resume_model IS NULL
            AND n.answer IN ('允许一次','本轮任务都允许')
            AND t.id>COALESCE((SELECT watermark_id FROM session_meta WHERE session_key=n.session_key),0)
            AND t.ts>=n.ts AND t.assistant_text='(执行失败:执行出错:原模型会话不可恢复，已停止续接)'
            AND t.user_text=('刚才等批准的操作,主人已批准(' || n.answer || '):' || n.reason
              || char(10) || n.detail || char(10) || '请重新执行这一步,并把之前因此没做完的部分补完。')
          GROUP BY n.id
        ) UPDATE notices SET resume_model='', updated_at=?, status=CASE WHEN EXISTS (
          SELECT 1 FROM turns t WHERE t.session_key=failed.session_key AND t.id>failed.turn_id
        ) THEN 'verify_required' ELSE 'approved_unrun' END
        FROM failed WHERE notices.id=failed.id
    """, (time.time(),))
    c.commit()


def close_asks_for_session(session_key: str) -> int:
    """你在这个会话里又发了新消息 → 之前超时没答的提问视为已翻篇(审批不动)。"""
    c = _conn()
    cur = c.execute(
        "UPDATE notices SET status='dismissed', updated_at=? "
        "WHERE session_key=? AND kind='ask' AND status='expired'",
        (time.time(), session_key),
    )
    c.commit()
    if cur.rowcount:
        _changed()
    return cur.rowcount


def dismiss_all() -> int:
    """「全部忽略」清掉所有已超时的项(含待拍板事项)。

    2026-10-12 主人反馈:原先跳过拍板项,点了全部忽略铃铛数字纹丝不动,像没生效。
    改为一起清,误清的风险交给前端确认框(会写明含几件拍板)。正在等回答的 pending 不动。"""
    c = _conn()
    cur = c.execute(
        "UPDATE notices SET status='dismissed', updated_at=? WHERE status='expired'",
        (time.time(),),
    )
    c.commit()
    if cur.rowcount:
        _changed()
    return cur.rowcount
