"""委托仅记录普通会话的来源关系；回执永不进入用户输入通道。"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import secrets

from claude_agent_sdk import tool

from .. import config
from ..gateway import clarify
from ..memory import agents, session_store
from ..tools import danger
from . import tasks

_pump: asyncio.Task | None = None
_draining = False


def _version(task_id: str) -> int:
    row = tasks.get(task_id)
    return row["run_version"] if row else 0


def _conn():
    c = tasks._conn()
    c.execute("""CREATE TABLE IF NOT EXISTS agent_delegations(
        task_id TEXT PRIMARY KEY, chain_id TEXT NOT NULL, parent_id TEXT,
        source_session TEXT NOT NULL, source_agent TEXT NOT NULL, target_agent TEXT NOT NULL,
        ancestors TEXT NOT NULL, depth INTEGER NOT NULL, tokens INTEGER NOT NULL,
        seconds INTEGER NOT NULL, reserved_tokens INTEGER NOT NULL DEFAULT 0,
        reserved_seconds INTEGER NOT NULL DEFAULT 0, used INTEGER NOT NULL DEFAULT 0,
        denied TEXT NOT NULL, deadline REAL NOT NULL, receipt TEXT,
        summary_started INTEGER NOT NULL DEFAULT 0, summary_used INTEGER NOT NULL DEFAULT 0,
        delivered INTEGER NOT NULL DEFAULT 0)
    """)
    if "policy" not in {r[1] for r in c.execute("PRAGMA table_info(agent_delegations)")}:
        c.execute("ALTER TABLE agent_delegations ADD COLUMN policy TEXT")
        c.commit()
    columns = {r[1] for r in c.execute("PRAGMA table_info(agent_delegations)")}
    for name in ("delivered_turn", "receipt_turn"):
        if name not in columns:
            c.execute(f"ALTER TABLE agent_delegations ADD COLUMN {name} INTEGER NOT NULL DEFAULT -1")
            if name == "delivered_turn":
                for row in c.execute("SELECT task_id FROM agent_delegations WHERE delivered=1").fetchall():
                    c.execute("UPDATE agent_delegations SET delivered_turn=? WHERE task_id=?",
                              (_version(row[0]), row[0]))
    c.commit()
    return c


def get(task_id: str) -> dict | None:
    row = _conn().execute("SELECT * FROM agent_delegations WHERE task_id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def discover() -> list[dict]:
    return [{"id": a["id"], "name": a["name"]} for a in agents.list_agents()]


def resolve(reference: str) -> dict:
    all_agents = agents.list_agents()
    exact = [a for a in all_agents if a["id"] == reference]
    matches = exact or [a for a in all_agents if a["name"] == reference]
    if len(matches) != 1:
        raise ValueError("Agent 不存在或名称重名，请用 list_delegate_agents 返回的唯一 ID")
    return matches[0]


def _blocked(name: str, denied: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern.split("(", 1)[0])
               or (pattern.startswith("mcp__") and name.startswith(pattern + "__"))
               for pattern in denied)


def _source(session: str) -> tuple[str, str]:
    owner = agents.agent_for_session(session)
    if not owner:
        # 普通 Web 会话归总助理；无法确认的任意路由不默认为总助理。
        if (config.project_hash_from_key(session)
                or not (session == config.SESSION_KEY or session.startswith("web:"))):
            raise ValueError("无法确认来源 Agent")
        owner = {"id": agents.GENERAL_ID}
    row = session_store._conn().execute(
        "SELECT id FROM turns WHERE session_key=? AND user_text!='' AND assistant_text='' ORDER BY id DESC LIMIT 1",
        (session,),
    ).fetchone()
    if not row:
        raise ValueError("来源没有正在执行的用户轮次，不能新建委托链")
    return owner["id"], f"{session}:{row[0]}"


def delegate(session: str, denied: list[str], reference: str, title: str, prompt: str,
             tokens=None, seconds=None) -> dict:
    """旧预算参数仅兼容调用；任务使用普通执行器的统一设置。"""
    from . import task_runner

    if _blocked("mcp__vococo__delegate_to_agent", denied):
        raise ValueError("来源禁止 Agent 委托")
    if not all(isinstance(value, str) for value in (reference, title, prompt)):
        raise ValueError("目标、标题和背景必须是文本")
    if not title.strip() or not prompt.strip() or len(prompt) > 60_000 or len(title) > 100:
        raise ValueError("标题/背景不能为空或过长")
    target = resolve(reference)
    record = _lineage(session, denied, target)
    access = tasks.capture_source_access(session, json.loads(record["denied"]))
    task = _create_task(target, record, title, prompt, access)
    model = session_store.get_chosen_model(session) or config.MODEL
    session_store.set_chosen_model(tasks.session_key(task["id"]), model)
    task_runner._maybe_start_next()
    task_runner._notify_activity(task["id"])
    return task


def _lineage(session: str, denied: list[str], target: dict) -> dict:
    source, chain = _source(session)
    denied = tasks.capture_source_access(session, denied)["denied"]
    access = session_store.active_source_access(session)
    parent_id = tasks.task_id_from_session_key(access.get("chain_session", session)) if access else None
    parent = get(parent_id) if parent_id else None
    if parent_id and not parent:
        raise ValueError("来源自动链关系缺失，不能重建委托链")
    if parent:
        chain = parent["chain_id"]
        ancestors = list(dict.fromkeys(json.loads(parent["ancestors"]) + [parent["target_agent"], source]))
        denied = list(set(denied + access.get("denied", []) + json.loads(parent["denied"])))
        depth = parent["depth"] + 1
    else:
        ancestors, depth = [source], 1
    if depth > 2 or target["id"] in ancestors:
        raise ValueError("超过两层委托或目标是本链祖先")
    if _blocked("mcp__vococo__delegate_to_agent", denied):
        raise ValueError("来源禁止 Agent 委托")
    return {"task_id": secrets.token_hex(8), "chain_id": chain, "parent_id": parent_id,
            "source_session": session, "source_agent": source, "target_agent": target["id"],
            "ancestors": json.dumps(ancestors), "depth": depth, "tokens": 0, "seconds": 0,
            "denied": json.dumps(sorted(set(denied))), "deadline": 0}


def create_related_task(source: str, title: str, prompt: str, **options) -> dict:
    """其他自动派发入口沿用同一来源账本，不能换入口重置链路。"""
    target = agents.get(options.get("agent_id") or agents.GENERAL_ID)
    if not target:
        raise ValueError("无法确认目标 Agent")
    record = _lineage(source, [], target)
    record["task_id"] = options.pop("task_id", None) or record["task_id"]
    access = tasks.capture_source_access(source, json.loads(record["denied"]))
    return _create_task(target, record, title, prompt, access, **options)


def _create_task(target: dict, record: dict, title: str, prompt: str, access: dict, **task_options) -> dict:
    key = task_options.get("session_key") or config.new_session_key()
    task_options["session_key"] = key
    access["chain_session"] = key
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT COUNT(*) FROM agent_delegations WHERE chain_id=?",
                     (record["chain_id"],)).fetchone()[0] >= 5:
            raise ValueError("同链最多五个子任务")
        options = {"cwd": str(target.get("workdir") or config.ROOT_DIR), "cwd_explicit": True,
                   "agent_id": target["id"], "origin": "chat", **task_options}
        task = tasks.create(title, prompt, task_id=record["task_id"],
                            source_session_key=record["source_session"], source_access=access, commit=False, **options)
        columns = ",".join(record)
        placeholders = ",".join(":" + key for key in record)
        c.execute(f"INSERT INTO agent_delegations({columns}) VALUES ({placeholders})", record)
        c.commit()
        return task
    except Exception:
        c.rollback()
        raise


def query(session: str, task_id: str) -> dict:
    row = get(task_id)
    parent = get(tasks.task_id_from_session_key(session) or "")
    owns_chain = row and _conn().execute(
        "SELECT 1 FROM agent_delegations WHERE chain_id=? AND source_session=? LIMIT 1",
        (row["chain_id"], session),
    ).fetchone()
    if not row or not (owns_chain or (parent and parent["chain_id"] == row["chain_id"])):
        raise ValueError("委托不存在或不属于当前来源")
    task = _task_state(task_id)
    return {key: task[key] for key in ("id", "title", "status", "result_full", "result_summary")} | {
        "parent_id": row["parent_id"], "depth": row["depth"],
        "receipt_delivered": row["delivered_turn"] == _version(task_id)}


def build_tools(session: str, denied: list[str]):
    # 工具闭包只接受目标/任务；来源及权限不在模型可写参数里。
    @tool("list_delegate_agents", "列出可委托的持久 Agent（非同步子代理），名称重名请用 ID。", {})
    async def list_agents(_args):
        return _ok(discover())

    @tool("delegate_to_agent", "给持久 Agent 创建普通独立会话，支持用户继续和追问。必须提供完整背景；自动派单最多两层、同链五任务。旧预算参数兼容接收但忽略，执行遵循普通任务设置。", {
        "type": "object", "properties": {
            "agent": {"type": "string"}, "title": {"type": "string"}, "prompt": {"type": "string"},
            "token_budget": {"type": "integer", "description": "已废弃，兼容接收但忽略"},
            "timeout_seconds": {"type": "integer", "description": "已废弃，兼容接收但忽略"}},
        "required": ["agent", "title", "prompt"],
    })
    async def dispatch(args):
        try:
            row = delegate(session, denied, args["agent"], args["title"], args["prompt"],
                           args.get("token_budget"), args.get("timeout_seconds"))
            return _ok({"task_id": row["id"], "message": "已派发，完成/失败/取消后自动回执"})
        except (ValueError, KeyError) as exc:
            return _ok({"error": str(exc)})

    @tool("get_agent_delegation", "查询当前来源派出的 Agent 委托状态与结果。", {"task_id": str})
    async def status(args):
        try:
            return _ok(query(session, args["task_id"]))
        except ValueError as exc:
            return _ok({"error": str(exc)})

    return [list_agents, dispatch, status]


def _ok(value):
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}


def _busy(session: str) -> bool:
    task_id = tasks.task_id_from_session_key(session)
    row = tasks.get(task_id) if task_id else None
    return session in clarify._active_sessions or clarify.has_pending(session) or bool(
        row and row["status"] in {"running", "queued"})


def _receipt(row: dict, task: dict) -> str:
    prefix = f"【Agent 回执 · {row['target_agent']} · {task['id']} · {task['status']}】\n"
    children = _conn().execute(
        "SELECT receipt FROM agent_delegations WHERE parent_id=? ORDER BY rowid", (task["id"],)
    ).fetchall()
    text = prefix + danger.redact_secrets(task["result_summary"] or task["progress_note"] or "无结果")
    if children:
        text += "\n下级回执（全文可按任务 ID 查询）：\n" + "\n".join((r[0] or "")[:6000] for r in children)
    return text


async def drain() -> None:
    global _draining
    if _draining:
        return
    _draining = True
    try:
        await _drain_once()
    finally:
        _draining = False


async def _drain_once() -> None:
    rows = _conn().execute("SELECT * FROM agent_delegations ORDER BY depth DESC, rowid").fetchall()
    for raw in rows:
        try:
            await _process_receipt(dict(raw))
        except Exception as exc:
            print(danger.redact_secrets(f"[delegation] {raw['task_id']} 回执待重试：{exc}"), flush=True)


def _task_state(task_id: str) -> dict:
    return tasks.get(task_id) or {
        "id": task_id, "title": "已删除的委托", "status": "cancelled",
        "result_summary": "任务已删除，结果不可用", "result_full": "", "progress_note": "已取消",
    }


async def _process_receipt(row: dict) -> None:
    task = _task_state(row["task_id"])
    version = _version(task["id"])
    if row["delivered_turn"] == version:
        return
    children = _conn().execute("SELECT task_id, delivered_turn FROM agent_delegations WHERE parent_id=?",
                               (row["task_id"],)).fetchall()
    if (task["status"] not in tasks.TERMINAL_STATUSES
            or any(child["delivered_turn"] != _version(child["task_id"]) for child in children)
            or _busy(row["source_session"])):
        return
    text = row["receipt"] if row["receipt_turn"] == version else _receipt(row, task)
    c = _conn()
    c.execute("UPDATE agent_delegations SET receipt=?, receipt_turn=? WHERE task_id=?",
              (text, version, task["id"]))
    c.commit()
    _deliver(row, text, version)


def _deliver(row: dict, text: str, version: int) -> None:
    from . import task_events

    source = row["source_session"]
    source_task = tasks.task_id_from_session_key(source)
    turn_id = None
    if not source_task or tasks.get(source_task):
        turn_id = session_store.append_external(source, text, f"agent-receipt:{row['task_id']}:{version}")
        session_store.set_pending_review(source, True)
    # 已删除的上级任务不复活会话；回执仍存账本，供更上级汇总。
    c = _conn()
    c.execute("UPDATE agent_delegations SET delivered=1, delivered_turn=? WHERE task_id=?",
              (version, row["task_id"]))
    c.commit()
    if turn_id is not None:
        conv = "main" if source == config.SESSION_KEY else config.conversation_id_for_key(source)
        # 复用现有 Agent 结果通知，不参与用户输入/start/done 状态机。
        task_events._bridge_event({"conv": conv, "type": "agent_run", "turn_id": turn_id, "text": text})


async def _run_pump() -> None:
    while True:
        try:
            await drain()
        except Exception as exc:
            print(f"[delegation] 回执待重试：{exc}", flush=True)
        if not any(row["delivered_turn"] != _version(row["task_id"])
                   for row in _conn().execute("SELECT task_id, delivered_turn FROM agent_delegations")):
            return
        await asyncio.sleep(1)


def wake() -> None:
    global _pump
    if _pump is None or _pump.done():
        _pump = asyncio.create_task(_run_pump())


async def on_terminal(_task_id: str) -> None:
    wake()
