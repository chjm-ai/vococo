"""待处理通知(铃铛)—— 所有「等你回答的提问」和「等你批的操作」(state.db notices 表)。

2026-09-28 主人反馈:提问/审批的选项经常超时,超时后按钮点了只回一句「已过期」,事情就断了。
这里把每一次弹出的选项都记下来,整个生命周期:

  pending   正在等你回答(gateway/clarify.register 带 notice 时创建,选项还能直接点)
  expired   超时 / 本轮结束 / 进程重启 / 后台任务夜间免打扰 —— 【仍然可以处理】:
            点选项 = 把你的选择补回原会话让它接着干(见 gateway/notice_actions.act)
  answered  已回答(按时点了,或事后补答)
  dismissed 你点了忽略

kind:ask(ask_user 提问)/ approval(危险操作审批,带 reason/detail/tool 与可存规则信息)/
decision(2026-10-10 待拍板事项:request_decision 工具登记,不阻塞任何一轮,直接以 expired 入库、
session_key 是所属 Agent 的主会话;点选项 = 把选择发回那个主会话接着执行)/ review(规则清理提醒)。
open = pending + expired,就是铃铛上的数字。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Callable

from . import _db

OPEN_STATUSES = ("pending", "expired")
_listener: Callable[[], None] | None = None

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
        _ready_for = c
    return c


def set_listener(fn: Callable[[], None] | None) -> None:
    """通知有变化时的回调(Web 端据此刷新铃铛数字)。"""
    global _listener
    _listener = fn


def _changed() -> None:
    if _listener is not None:
        try:
            _listener()
        except Exception as exc:  # noqa: BLE001 —— 刷新推送失败不影响业务
            print(f"[notices] 变更通知失败:{exc}", flush=True)


_COLS = (
    "id, ts, updated_at, session_key, kind, prompt, options, clarify_id, status, answer, "
    "reason, detail, tool, rule_kind, rule_scope, rule_label"
)


def _row(r) -> dict:
    d = dict(zip([c.strip() for c in _COLS.split(",")], r))
    d["options"] = json.loads(d["options"] or "[]")
    return d


def add(
    *, session_key: str, kind: str, prompt: str, options: list[str] | None = None,
    clarify_id: str | None = None, status: str = "pending", reason: str = "",
    detail: str = "", tool: str = "", rule: tuple[str, str, str] | None = None,
) -> str:
    """登记一条通知。直接以 expired 登记的(后台免打扰/无通道)按 会话+原因+详情 去重。"""
    c = _conn()
    if status == "expired":
        row = c.execute(
            "SELECT id FROM notices WHERE session_key=? AND kind=? AND reason=? AND detail=? "
            "AND status IN ('pending','expired')",
            (session_key, kind, reason, detail[:500]),
        ).fetchone()
        if row:
            return row[0]
    nid = uuid.uuid4().hex[:10]
    now = time.time()
    c.execute(
        f"INSERT INTO notices({_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            nid, now, now, session_key, kind, prompt[:2000], json.dumps(options or [], ensure_ascii=False),
            clarify_id, status, None, reason, detail[:500], tool,
            rule[0] if rule else None, rule[1] if rule else None, rule[2] if rule else None,
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


def set_status(nid: str, status: str, answer: str | None = None) -> bool:
    c = _conn()
    cur = c.execute(
        "UPDATE notices SET status=?, answer=COALESCE(?, answer), updated_at=? WHERE id=?",
        (status, answer, time.time(), nid),
    )
    c.commit()
    if cur.rowcount:
        _changed()
    return cur.rowcount > 0


def list_open(limit: int = 100) -> list[dict]:
    rows = _conn().execute(
        f"SELECT {_COLS} FROM notices WHERE status IN ('pending','expired') "
        "ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row(r) for r in rows]


def count_open_since(ts: float) -> int:
    return _conn().execute(
        "SELECT COUNT(*) FROM notices WHERE status IN ('pending','expired') AND ts >= ?", (ts,)
    ).fetchone()[0]


def expire_all_pending() -> int:
    """进程重启:之前在等的选项已经没人接了,统一改成 expired(仍可事后处理)。"""
    c = _conn()
    cur = c.execute(
        "UPDATE notices SET status='expired', updated_at=? WHERE status='pending'", (time.time(),)
    )
    c.commit()
    return cur.rowcount


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
    """「全部忽略」只清错过的提问/审批;待拍板的事得一件件选或单独忽略,免得误清。"""
    c = _conn()
    cur = c.execute(
        "UPDATE notices SET status='dismissed', updated_at=? WHERE status='expired' AND kind!='decision'",
        (time.time(),),
    )
    c.commit()
    if cur.rowcount:
        _changed()
    return cur.rowcount
