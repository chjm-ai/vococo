"""审批规则 + 审批记录(state.db 两张表)。

- approval_rules:「永远允许」存下的规则。kind 是规则类型,scope 是生效范围:
    write_dir  → 目录绝对路径(该目录及子目录下的写入免批)
    git_push   → 仓库根目录绝对路径(在该仓库里 git push 免批)
    mcp_tool   → 外部 MCP 写工具全名
  规则只收窄不放大:判定哪些操作能存成规则、范围怎么取,在 tools/danger.py 的
  _rule_target;这里只管存取和匹配。
- audit_log:每次需要审批 / 被直接拦下的操作记一笔(结果见 DECISIONS),保留 90 天。
- 等审批超时的操作不在这里:统一进铃铛(memory/notices.py),事后可补批。

2026-09-27 参照 Grok Bot / Meta Muse 的审批与 Activity log 设计新增。
"""
from __future__ import annotations

import os
import time
import uuid

from . import _db

AUDIT_KEEP_DAYS = 90
_PRUNE_EVERY = 200  # 每写这么多条审计记录顺手清一次过期的

# 审批结果取值(前端据此显示中文)
DECISIONS = {
    "rule": "命中永久规则",
    "session": "本轮任务/本会话已允许",
    "approved_once": "你批了一次",
    "approved_session": "你批了本轮任务/本会话",
    "approved_forever": "你批了永远允许",
    "full_access": "完全访问,自动放行",
    "denied": "你拒绝了",
    "timeout": "等审批超时",
    "deferred": "免打扰时段,进待批队列",
    "noninteractive_allow": "无人值守,本地操作放行",
    "noninteractive_deny": "无人值守,默认拒绝",
    "group_deny": "群聊不允许审批",
    "blocked": "直接拦截",
    "error": "审批出错,保守拒绝",
}


# ── 规则 ────────────────────────────────────────────────────────────────
def _norm_dir(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path)).rstrip("/") or "/"


def _path_under(path: str, base: str) -> bool:
    try:
        return os.path.commonpath([_norm_dir(path), _norm_dir(base)]) == _norm_dir(base)
    except ValueError:
        return False


def add_rule(kind: str, scope: str, label: str = "") -> dict:
    """新增一条永久规则;同 (kind, scope) 已存在则原样返回。"""
    if kind in ("write_dir", "git_push"):
        scope = _norm_dir(scope)
    c = _db.conn()
    row = c.execute(
        "SELECT id FROM approval_rules WHERE kind=? AND scope=?", (kind, scope)
    ).fetchone()
    if row is None:
        c.execute(
            "INSERT INTO approval_rules(id, kind, scope, label, created_at) VALUES(?,?,?,?,?)",
            (uuid.uuid4().hex[:10], kind, scope, label, time.time()),
        )
        c.commit()
    return find_rule_exact(kind, scope) or {}


def find_rule_exact(kind: str, scope: str) -> dict | None:
    row = _db.conn().execute(
        "SELECT id, kind, scope, label, created_at, last_used_at, hits "
        "FROM approval_rules WHERE kind=? AND scope=?",
        (kind, scope),
    ).fetchone()
    return _rule_dict(row) if row else None


def _rule_dict(row) -> dict:
    return {
        "id": row[0], "kind": row[1], "scope": row[2], "label": row[3],
        "created_at": row[4], "last_used_at": row[5], "hits": row[6],
    }


def match_rule(kind: str, target: str) -> dict | None:
    """找一条覆盖 target 的规则;命中则顺手记一次使用。

    write_dir / git_push 按目录包含关系匹配(target 在 scope 目录下即命中);
    mcp_tool 精确匹配工具名。
    """
    c = _db.conn()
    rows = c.execute(
        "SELECT id, kind, scope, label, created_at, last_used_at, hits "
        "FROM approval_rules WHERE kind=?",
        (kind,),
    ).fetchall()
    for row in rows:
        scope = row[2]
        hit = _path_under(target, scope) if kind in ("write_dir", "git_push") else target == scope
        if hit:
            c.execute(
                "UPDATE approval_rules SET hits=hits+1, last_used_at=? WHERE id=?",
                (time.time(), row[0]),
            )
            c.commit()
            return _rule_dict(row)
    return None


def list_rules() -> list[dict]:
    rows = _db.conn().execute(
        "SELECT id, kind, scope, label, created_at, last_used_at, hits "
        "FROM approval_rules ORDER BY created_at DESC"
    ).fetchall()
    return [_rule_dict(r) for r in rows]


def delete_rule(rule_id: str) -> bool:
    c = _db.conn()
    cur = c.execute("DELETE FROM approval_rules WHERE id=?", (rule_id,))
    c.commit()
    return cur.rowcount > 0


# ── 审批记录 ────────────────────────────────────────────────────────────
def log(
    *, session_key: str, tool: str, tier: str, reason: str, detail: str, decision: str
) -> None:
    """记一笔审批/拦截。写失败不影响主流程(调用方已 try 包住,这里不再兜)。"""
    c = _db.conn()
    cur = c.execute(
        "INSERT INTO audit_log(ts, session_key, tool, tier, reason, detail, decision) "
        "VALUES(?,?,?,?,?,?,?)",
        (time.time(), session_key or "", tool or "", tier, reason or "", (detail or "")[:500], decision),
    )
    if (cur.lastrowid or 0) % _PRUNE_EVERY == 0:
        c.execute(
            "DELETE FROM audit_log WHERE ts < ?", (time.time() - AUDIT_KEEP_DAYS * 86400,)
        )
    c.commit()


def list_audit(limit: int = 200, decision: str = "", session_key: str = "") -> list[dict]:
    sql = "SELECT id, ts, session_key, tool, tier, reason, detail, decision FROM audit_log"
    where, args = [], []
    if decision:
        where.append("decision=?")
        args.append(decision)
    if session_key:
        where.append("session_key=?")
        args.append(session_key)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    rows = _db.conn().execute(sql, args).fetchall()
    return [
        {
            "id": r[0], "ts": r[1], "session_key": r[2], "tool": r[3], "tier": r[4],
            "reason": r[5], "detail": r[6], "decision": r[7],
        }
        for r in rows
    ]
