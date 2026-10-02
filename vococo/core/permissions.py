"""权限档位:标准 / 完全访问(2026-10-02)。

审批闸(tools/danger.py)把「危险但非灾难」的操作判成 escalate 请你批准;完全访问档下
这些操作改为自动放行,仍记审批记录(decision=full_access)。两级设置,会话覆盖 Agent:

    会话设置(session_meta.perm_mode/perm_until,可限时) → Agent 设置(agent.json permission)
    → 默认标准

完全访问【不】放开的:
- 灾难级 block(删根/格式化/fork 炸弹)与常开正确性防线(_hard_guard),它们不走审批闸;
- KEEP_ASKING 里的类别(疑似密钥外带):一旦被网页/邮件内容注入,后果是凭据泄露,
  这一类照旧弹窗;
- 群聊会话:批准权不能落在群成员手里,永远标准档。
"""
from __future__ import annotations

import time

FULL = "full"
STANDARD = "standard"
MODES = (FULL, STANDARD)

# 完全访问下仍要请你批准的 escalate 类别(按 danger._category 归一后的原因匹配)
KEEP_ASKING = frozenset({"疑似把密钥/令牌通过网络外带"})


def _is_group(session_key: str) -> bool:
    return bool(session_key) and session_key.startswith("tg:")


def _session_override(session_key: str, now: float) -> tuple[str, float]:
    """会话自己的覆盖设置;限时的完全访问过期后当没设过(回到跟随 Agent)。"""
    from ..memory import session_store

    mode, until = session_store.get_permission(session_key)
    if mode not in MODES:
        return "", 0.0
    if mode == FULL and until and until <= now:
        return "", 0.0
    return mode, until


def state(session_key: str, now: float | None = None) -> dict:
    """会话当前的权限状态,给前端胶囊和审批闸共用。

    返回 {mode, source, session_mode, until, agent_mode, agent_name, allowed}:
    mode=最终生效档位;source=session/agent/default;allowed=False 表示此会话不能开(群聊)。
    """
    now = time.time() if now is None else now
    out = {"mode": STANDARD, "source": "default", "session_mode": "", "until": 0.0,
           "agent_mode": STANDARD, "agent_name": "", "allowed": not _is_group(session_key)}
    if not session_key or not out["allowed"]:
        return out
    try:
        from ..memory import agents

        a = agents.agent_for_session(session_key)
    except Exception as exc:  # noqa: BLE001 —— 认不出 Agent 就按没有 Agent
        print(f"[permissions] 读取 Agent 失败:{exc}", flush=True)
        a = None
    if a:
        out["agent_name"] = a["name"]
        out["agent_mode"] = a.get("permission") or STANDARD
        if out["agent_mode"] == FULL:
            out.update(mode=FULL, source="agent")
    mode, until = _session_override(session_key, now)
    if mode:
        out.update(mode=mode, source="session", session_mode=mode, until=until)
    return out


def is_full(session_key: str) -> bool:
    """该会话是否处于完全访问。任何异常都按标准档(fail-closed)。"""
    try:
        return state(session_key)["mode"] == FULL
    except Exception as exc:  # noqa: BLE001
        print(f"[permissions] 判定失败,按标准档处理:{exc}", flush=True)
        return False


def auto_allows(session_key: str, category: str) -> bool:
    """审批闸入口:这个会话的这类 escalate 操作要不要免批放行。"""
    return category not in KEEP_ASKING and is_full(session_key)


def set_session(session_key: str, mode: str, hours: float = 0) -> dict:
    """设会话覆盖。mode 空串=跟随 Agent;hours>0 只对完全访问有效,到点自动回到跟随 Agent。"""
    from ..memory import session_store

    if mode and mode not in MODES:
        raise ValueError("权限只能是 full / standard / 空(跟随 Agent)")
    if mode == FULL and _is_group(session_key):
        raise ValueError("群聊会话不能开完全访问")
    until = time.time() + hours * 3600 if mode == FULL and hours and hours > 0 else 0
    session_store.set_permission(session_key, mode, until)
    return state(session_key)
