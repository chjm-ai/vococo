"""两个派发入口共用的来源、关系和执行记录；旧任务不自动加入新链。"""
from __future__ import annotations

import json
import os
import secrets

from claude_agent_sdk import tool

from .. import config, providers
from ..memory import agents, session_store
from ..tools import danger
from . import delegation, tasks


def enabled() -> bool:
    return os.environ.get("VOCOCO_TASK_DISPATCH_V2", "").lower() in {"1", "true"}


def connection():
    c = tasks._conn()
    c.execute("""CREATE TABLE IF NOT EXISTS dispatch_executions(
        id TEXT PRIMARY KEY, session_key TEXT NOT NULL, turn_id INTEGER NOT NULL,
        root_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active',
        resumes INTEGER NOT NULL DEFAULT 0, access TEXT NOT NULL,
        claim TEXT, batch TEXT NOT NULL DEFAULT '[]')""")
    c.execute("""CREATE TABLE IF NOT EXISTS dispatch_receipts(
        task_id TEXT NOT NULL, run_version INTEGER NOT NULL, execution_id TEXT NOT NULL,
        text TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(task_id,run_version,execution_id))""")
    _usage_schema(c)
    c.commit()
    return c


def _usage_schema(c) -> None:
    schema = """CREATE TABLE IF NOT EXISTS dispatch_usage(
        turn_id INTEGER NOT NULL, execution_id TEXT NOT NULL, tokens INTEGER NOT NULL,
        PRIMARY KEY(execution_id,turn_id))"""
    c.execute(schema)
    primary = [row for row in c.execute("PRAGMA table_info(dispatch_usage)") if row["pk"]]
    if len(primary) == 2:
        return
    # 轮次编号可以被删除后复用，旧账必须保留但不能继续以轮次单列去重。
    with c:
        c.execute("SAVEPOINT dispatch_usage_migration")
        c.execute("ALTER TABLE dispatch_usage RENAME TO dispatch_usage_v1")
        c.execute(schema)
        c.execute("INSERT INTO dispatch_usage SELECT turn_id,execution_id,tokens FROM dispatch_usage_v1")
        c.execute("DROP TABLE dispatch_usage_v1")
        c.execute("RELEASE dispatch_usage_migration")


def record_usage(session: str, turn_id: int | None, tokens: int, *, execution_id: str | None = None) -> None:
    if turn_id is None or not tokens:
        return
    c = connection()
    row = c.execute("SELECT id FROM dispatch_executions WHERE session_key=? AND turn_id=? "
                    "AND (? IS NULL OR id=?) ORDER BY rowid DESC LIMIT 1",
                    (session, turn_id, execution_id, execution_id)).fetchone()
    if row:
        c.execute("INSERT INTO dispatch_usage(turn_id,execution_id,tokens) VALUES(?,?,?) "
                  "ON CONFLICT(execution_id,turn_id) DO UPDATE SET tokens=MAX(tokens,excluded.tokens)",
                  (turn_id, row[0], tokens))
        c.commit()


def usage_tokens(identifier: str) -> int:
    return connection().execute("SELECT COALESCE(SUM(u.tokens),0) FROM dispatch_usage u "
                                "JOIN dispatch_executions e ON e.id=u.execution_id WHERE e.root_id=?",
                                (identifier,)).fetchone()[0]


def limit_budget(session: str, budget: int, origin: str) -> tuple[int, str]:
    row = current(session)
    if not row:
        return budget, ""
    default = config.CHAT_TASK_TOKEN_BUDGET if origin == "chat" else config.TASK_TOKEN_BUDGET
    cap = max(0, int(os.environ.get("VOCOCO_TASK_CHAIN_TOKEN_BUDGET", str(default))))
    if not cap:
        return budget, ""
    remaining = cap - usage_tokens(row["root_id"])
    if remaining <= 0:
        return 0, "本任务链的 token 预算已用尽，未启动本轮；请核对结果后手动安排未完成项。"
    return min(budget, remaining) if budget else remaining, ""


def execution(execution_id: str) -> dict | None:
    row = connection().execute("SELECT * FROM dispatch_executions WHERE id=?", (execution_id,)).fetchone()
    return dict(row) if row else None


def current(session: str) -> dict | None:
    row = connection().execute(
        "SELECT * FROM dispatch_executions WHERE session_key=? ORDER BY rowid DESC LIMIT 1", (session,)
    ).fetchone()
    return dict(row) if row else None


def begin(session: str, turn_id: int, access: dict | None = None) -> str:
    """真人新输入替代旧计划；只有服务端已领取的自动续跑可以复用父执行。"""
    c = connection()
    marker = (access or {}).get("dispatch_resume")
    previous = current(session)
    if marker and valid_resume(session, access, new_turn=turn_id):
        c.execute("UPDATE dispatch_executions SET turn_id=?,state='active' WHERE id=?",
                  (turn_id, previous["id"]))
        c.commit()
        return previous["id"]
    c.execute("UPDATE dispatch_executions SET state='superseded',claim=NULL "
              "WHERE session_key=? AND state IN ('active','waiting','claimed')", (session,))
    own_task = tasks.task_for_session(session)
    parent = execution(own_task["parent_execution_id"]) if own_task and own_task.get("parent_execution_id") else None
    identifier = secrets.token_hex(16)
    root = parent["root_id"] if parent else identifier
    source = tasks.capture_source_access(session)
    source.pop("dispatch_resume", None)
    c.execute("INSERT INTO dispatch_executions(id,session_key,turn_id,root_id,access) VALUES(?,?,?,?,?)",
              (identifier, session, turn_id, root, json.dumps(source)))
    c.commit()
    return identifier


def ensure_current(session: str) -> dict:
    pending = session_store._conn().execute(
        "SELECT id FROM turns WHERE session_key=? AND user_text!='' AND assistant_text='' ORDER BY id DESC LIMIT 1",
        (session,),
    ).fetchone()
    if not pending:
        raise ValueError("来源没有正在执行的轮次，不能派子任务")
    row = current(session)
    if not row or row["turn_id"] != pending[0]:
        row = execution(begin(session, pending[0]))
    if row["state"] != "active":
        raise ValueError("来源执行已经结束，不能派子任务")
    return row


def valid_resume(session: str, access: dict | None, *, new_turn: int | None = None) -> bool:
    marker = (access or {}).get("dispatch_resume") or {}
    row = current(session)
    if not (isinstance(marker, dict) and row and row["state"] == "claimed"
            and marker.get("id") == row["id"] and marker.get("claim") == row["claim"]):
        return False
    if new_turn is None:
        return _latest_turn(session) == row["turn_id"]
    previous = session_store._conn().execute(
        "SELECT MAX(id) FROM turns WHERE session_key=? AND user_text!='' AND id<?",
        (session, new_turn),
    ).fetchone()[0]
    return _latest_turn(session) == new_turn and previous == row["turn_id"]


def defer_resume(session: str, access: dict) -> None:
    if not valid_resume(session, access):
        return
    row = current(session)
    c = connection()
    c.execute("UPDATE dispatch_executions SET state='waiting',claim=NULL,resumes=MAX(0,resumes-1) WHERE id=?", (row["id"],))
    c.commit()
    wake()


def _check_denied(relation: str, denied: list[str]) -> None:
    names = ["mcp__vococo__dispatch_session"] if relation == "independent" else [
        "mcp__vococo__dispatch_task", "mcp__vococo__run_subagent", "mcp__vococo__delegate_to_agent", "Agent", "Task"]
    if any(delegation._blocked(name, denied) for name in names):
        raise ValueError("来源禁止此类派发")


def _target(session: str, reference: str | None) -> dict:
    target = delegation.resolve(reference) if reference else agents.agent_for_session(session)
    return target or agents.get(agents.GENERAL_ID)


def _model(session: str, selection: str | None, origin: str) -> str:
    if not selection:
        return (session_store.get_chosen_model(session) if origin != "voice" else "") or config.MODEL
    from ..gateway.core import MODEL_CHOICES

    hits = providers.match_models_by_text(selection, providers.available_models(MODEL_CHOICES))
    if len(hits) != 1:
        raise ValueError("模型未匹配或存在歧义，请指定完整模型 ID")
    return hits[0][0]


def _check_chain(parent: dict, target: dict) -> None:
    c = connection()
    count = c.execute("SELECT COUNT(*) FROM tasks t JOIN dispatch_executions e "
                      "ON e.id=t.parent_execution_id WHERE e.root_id=?", (parent["root_id"],)).fetchone()[0]
    if count >= 5:
        raise ValueError("同链最多五个任务")
    depth, cursor = 0, parent
    source_agent = _target(parent["session_key"], None)["id"]
    while cursor:
        owner = _target(cursor["session_key"], None)["id"]
        if owner == target["id"] and owner != source_agent:
            raise ValueError("不能委托给本链祖先")
        own = tasks.task_for_session(cursor["session_key"])
        if own and own.get("parent_execution_id"):
            depth += 1
            cursor = execution(own["parent_execution_id"])
        else:
            break
    if depth >= 2:
        raise ValueError("超过两层子任务")


def dispatch(session: str, relation: str, title: str, prompt: str, *, agent: str | None = None,
             model: str | None = None, cwd: str | None = None, origin: str = "chat",
             denied=(), capabilities: dict | None = None, platform=None, chat_id=None,
             review_cwd: str | None = None) -> dict:
    from . import task_runner

    _validate(relation, title, prompt)
    parent, target, access, origin = _source(session, relation, denied, agent, origin, capabilities)
    selected = _model(session, model, origin)
    root = config.resolve_execution_root(session_key=session, cwd=cwd or target.get("workdir"))
    row = _persist_task(relation, parent, access, review_cwd, selected,
                        title=title.strip(), prompt=prompt, cwd=root, cwd_explicit=True, agent_id=target["id"],
                        source_session_key=session, origin=origin,
                        dispatch_platform=platform, dispatch_chat_id=chat_id)
    task_runner._maybe_start_next()
    task_runner._notify_activity(row["id"])
    return tasks.get(row["id"])


def _validate(relation: str, title: str, prompt: str) -> None:
    if relation not in {"child", "independent"}:
        raise ValueError("未知任务关系")
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        raise ValueError("任务标题不能为空或超过 100 字")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 60000:
        raise ValueError("任务背景不能为空或超过 60000 字")


def _source(session, relation, denied, agent, origin, capabilities):
    access = tasks.capture_source_access(session, denied)
    _check_denied(relation, access["denied"])
    if access.get("remote_brain"):
        raise ValueError("外部 MCP 自动来源不能再次派发")
    # 独立入口也不能被自动链用来重置深度和任务数。
    own = tasks.task_for_session(session)
    in_chain = access.get("chain_session") or (own and own.get("parent_execution_id"))
    parent = ensure_current(session) if relation == "child" or in_chain else None
    if own:
        origin = own.get("origin") or origin
    target = _target(session, agent)
    if parent:
        _check_chain(parent, target)
    access["denied"] = sorted(set(access["denied"] + list(target.get("disallowed_tools") or [])))
    access.pop("dispatch_resume", None)
    access["capabilities"] = capabilities or access.get("capabilities") or {"skills": [], "mcp": []}
    return parent, target, access, origin


def _persist_task(relation, parent, access, review_cwd, selected, **fields):
    c = connection()
    with c:
        row = tasks.create(**fields, source_access=access, commit=False)
        if parent:
            access["chain_session"] = row["session_key"]
        if review_cwd:
            access["denied"] = sorted(set(access["denied"] + ["Bash", "Write", "Edit", "NotebookEdit", "Skill", "mcp__*"]))
            access["capabilities"] = {"skills": [], "mcp": []}
        c.execute("UPDATE tasks SET relation=?,parent_execution_id=?,execution_profile=?,review_cwd=?,source_access=? WHERE id=?",
                  (relation, parent["id"] if parent else None, "review" if review_cwd else "", review_cwd,
                   json.dumps(access), row["id"]))
        # 模型配置失败时回滚任务，不能让队列以后以错误模型执行。
        session_store.set_chosen_model(row["session_key"], selected)
    return row


def _reply(value: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}


async def _review_prompt(cwd: str, prompt: str) -> str:
    from . import reviewer, worktree

    code, base, _ = await worktree._git(cwd, "merge-base", "HEAD", "main")
    if code:
        raise ValueError("无法确认评审分支基线")
    code, diff, _ = await worktree._git(cwd, "diff", base.strip())
    if code:
        raise ValueError("无法读取待评审差异")
    diff += await _untracked_diff(cwd)
    if len(diff) > 150000:
        raise ValueError("评审差异超过单次范围，请分批评审，未截断内容")
    return reviewer.PROMPT + "\n本次评审无 Bash/写入/MCP 权限，以下是服务端提供的含未提交改动差异；按路径读取上下文。\n" + prompt + "\n" + danger.redact_secrets(diff)


async def _untracked_diff(cwd: str) -> str:
    from pathlib import Path
    from . import worktree

    code, names, _ = await worktree._git(cwd, "ls-files", "--others", "--exclude-standard", "-z")
    if code:
        raise ValueError("无法读取未跟踪文件清单")
    parts, size = [], 0
    for name in filter(None, names.split("\0")):
        path = Path(cwd) / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("未跟踪文件含软链或非常规文件，请先明确评审范围")
        size += path.stat().st_size
        if size > 150000:
            raise ValueError("未跟踪文件超过单次评审范围，请分批评审")
        code, text, _ = await worktree._git(cwd, "diff", "--no-index", "--", "/dev/null", name)
        if code not in (0, 1):
            raise ValueError("无法读取未跟踪文件差异")
        parts.append(text)
    return "\n".join(parts)


def build_tools(session: str, denied=(), capabilities: dict | None = None, *, parent_cwd: str | None = None):
    def create(name, relation, description):
        @tool(name, description, {"type": "object", "properties": {
            key: {"type": "string"} for key in ("title", "prompt", "agent", "model", "cwd")},
            "required": ["title", "prompt"], "additionalProperties": False})
        async def invoke(args):
            from ..gateway import clarify

            ctx = clarify.current()
            try:
                reference, prompt, review = args.get("agent"), args["prompt"], None
                if reference == "code-reviewer":
                    if relation != "child" or not parent_cwd:
                        raise ValueError("代码评审只能作为当前工作目录的子任务")
                    review = parent_cwd
                    prompt = await _review_prompt(review, prompt)
                    reference = None
                row = dispatch(session, relation, args["title"], prompt, agent=reference,
                               model=args.get("model"), cwd=args.get("cwd"), denied=list(denied),
                               capabilities=capabilities, platform=ctx.adapter.platform if ctx and ctx.adapter else None,
                               chat_id=str(ctx.chat_id) if ctx else None, review_cwd=review)
                return _reply({"task_id": row["id"], "session_id": row["session_key"], "status": row["status"],
                               "message": "已派发，结果自动回传；本轮结束后系统等待子任务，不要轮询" if relation == "child" else "已另开独立会话，完成后通知"})
            except (ValueError, TypeError) as exc:
                return _reply({"error": str(exc)})
        return invoke
    return [create("dispatch_task", "child", "为当前任务派子任务。保存子会话、可查看进度与续聊，不占侧栏；完成后主任务自动继续。完整背景放 prompt；Agent/模型默认继承。派发后可继续其他工作，本轮结束由系统等待并汇总，不要轮询或重复派发。"),
            create("dispatch_session", "independent", "另开独立会话推进新主题，显示在侧栏；完成通知，不自动续跑当前对话。仅用户要求独立推进时使用；当前任务分工请用 dispatch_task。")]


def metadata(session: str) -> dict:
    row = tasks.task_for_session(session)
    if not row:
        return {}
    return {"relation": row.get("relation", "independent"), "parent_session_key": row.get("source_session_key")
            if row.get("relation") == "child" else None}


def tree(session: str) -> dict:
    c = connection()
    def children(source: str, seen: set[str]):
        if source in seen:
            return []
        rows = c.execute("SELECT * FROM tasks WHERE source_session_key=? AND relation='child' ORDER BY created_at,id",
                         (source,)).fetchall()
        result = []
        for raw in rows:
            row = dict(raw)
            receipt = c.execute("SELECT processed FROM dispatch_receipts WHERE task_id=? AND run_version=? "
                                "ORDER BY rowid DESC LIMIT 1", (row["id"], row["run_version"])).fetchone()
            state = current(row["session_key"])
            result.append({**{k: row[k] for k in ("id", "title", "status", "progress_note", "session_key", "agent_id")},
                           "status": "waiting_children" if state and state["state"] == "waiting" else row["status"],
                           "model": session_store.get_chosen_model(row["session_key"]),
                           "receipt_state": "processed" if receipt and receipt[0] else "delivered" if receipt else "pending",
                           "children": children(row["session_key"], seen | {source})})
        return result
    own = current(session)
    return {"session_key": session, "parent_session_key": metadata(session).get("parent_session_key"),
            "state": own["state"] if own else None,
            "usage_tokens": usage_tokens(own["root_id"]) if own else 0, "children": children(session, set())}


def finish(session: str, status: str) -> bool:
    """模型一轮结束不等于主任务结束；返回是否仍需等待后代。"""
    row = current(session)
    if not row or row["state"] not in {"active", "claimed"}:
        return False
    c = connection()
    if status == "done":
        for task_id, version in json.loads(row["batch"]):
            c.execute("UPDATE dispatch_receipts SET processed=1 WHERE execution_id=? AND task_id=? AND run_version=?",
                      (row["id"], task_id, version))
    outstanding = c.execute("SELECT 1 FROM tasks t WHERE parent_execution_id=? AND relation='child' "
                            "AND NOT EXISTS (SELECT 1 FROM dispatch_receipts r WHERE r.execution_id=? "
                            "AND r.task_id=t.id AND r.run_version=t.run_version AND r.processed=1)",
                            (row["id"], row["id"])).fetchone()
    waiting = status == "done" and bool(outstanding)
    state = "waiting" if waiting else "completed" if status == "done" else status
    c.execute("UPDATE dispatch_executions SET state=?,claim=NULL,batch='[]' WHERE id=?", (state, row["id"]))
    c.commit()
    if status == "cancelled":
        cancel_children(row["id"])
    wake()
    return waiting


def cancel_session(session: str) -> bool:
    row = current(session)
    if not row or row["state"] not in {"active", "waiting", "claimed"}:
        return False
    cancel_children(row["id"])
    return True


def cancel_children(identifier: str) -> None:
    from . import task_runner

    c = connection()
    c.execute("UPDATE dispatch_executions SET state='cancelled',claim=NULL WHERE id=?", (identifier,))
    c.commit()
    for raw in c.execute("SELECT id,session_key,status FROM tasks WHERE parent_execution_id=? AND relation='child'",
                         (identifier,)).fetchall():
        own = current(raw["session_key"])
        if own and own["state"] in {"active", "waiting", "claimed"}:
            cancel_children(own["id"])
        if raw["status"] not in tasks.TERMINAL_STATUSES:
            task_runner.cancel(raw["id"])


def _store_receipts(row: dict) -> bool:
    from . import task_runner

    c = connection()
    children = c.execute("SELECT * FROM tasks WHERE parent_execution_id=? AND relation='child'",
                         (row["id"],)).fetchall()
    for raw in children:
        child = dict(raw)
        own = current(child["session_key"])
        if child["status"] not in tasks.TERMINAL_STATUSES or task_runner.session_busy(child["session_key"]):
            return False
        if own and own["state"] in {"active", "waiting", "claimed"}:
            return False
    for raw in children:
        child = dict(raw)
        if c.execute("SELECT 1 FROM dispatch_receipts WHERE task_id=? AND execution_id=? AND run_version=?",
                     (child["id"], row["id"], child["run_version"])).fetchone():
            continue
        text = danger.redact_secrets(f"【子任务结果 · {child['id']} · {child['status']}】\n"
                                    f"{child['title']}\n{child['result_full'] or child['result_summary'] or child['progress_note'] or '无结果'}")
        session_store.append_external(row["session_key"], text,
                                      f"dispatch:{row['id']}:{child['id']}:{child['run_version']}")
        c.execute("INSERT OR IGNORE INTO dispatch_receipts(task_id,run_version,execution_id,text) VALUES(?,?,?,?)",
                  (child["id"], child["run_version"], row["id"], text))
        c.commit()
    return True


def _resume_access(row: dict, claim: str) -> dict:
    access = json.loads(row["access"])
    access["dispatch_resume"] = {"id": row["id"], "claim": claim}
    return access


def _latest_turn(session: str) -> int | None:
    raw = session_store._conn().execute("SELECT MAX(id) FROM turns WHERE session_key=? AND user_text!=''",
                                       (session,)).fetchone()
    return raw[0]


def fail_resume(session: str, access: dict | None) -> None:
    marker = (access or {}).get("dispatch_resume") or {}
    if not marker:
        return
    row = current(session)
    if not (isinstance(marker, dict) and row and row["state"] in {"claimed", "active"}
            and marker.get("id") == row["id"] and marker.get("claim") == row["claim"]):
        return
    _pause(row, "自动续跑启动或执行中断，已释放并发名额。结果已保存，请核对后手动继续。")
    from . import task_runner

    task_runner._maybe_start_next()


def running_resumes() -> int:
    return connection().execute(
        "SELECT COUNT(*) FROM dispatch_executions e WHERE e.state IN ('claimed','active') "
        "AND e.resumes>0 AND NOT EXISTS (SELECT 1 FROM tasks t "
        "WHERE t.session_key=e.session_key AND t.status='running')"
    ).fetchone()[0]


def _pause(row: dict, reason: str) -> None:
    c = connection()
    c.execute("UPDATE dispatch_executions SET state='failed',claim=NULL WHERE id=?", (row["id"],))
    c.commit()
    session_store.append_external(row["session_key"], reason, f"dispatch-paused:{row['id']}")
    session_store.set_pending_review(row["session_key"], True)


def _ready(row: dict) -> bool:
    from ..gateway import clarify, web_bridge
    from ..tools import sessions
    from . import task_runner

    session = row["session_key"]
    if _latest_turn(session) != row["turn_id"]:
        c = connection()
        c.execute("UPDATE dispatch_executions SET state='superseded' WHERE id=?", (row["id"],))
        c.commit()
        return False
    if not _store_receipts(row):
        return False
    if (session in clarify._active_sessions or clarify.has_pending(session)
            or task_runner.session_busy(session) or sessions.claim_pending(session)
            or web_bridge.session_busy(session) or not web_bridge.keyed_available()):
        return False
    if not session_store.get_sdk_session_id(session):
        _pause(row, "自动续跑已暂停：原模型会话无法恢复。子任务结果已保存，请核对后手动继续。")
        return False
    limit = max(1, int(os.environ.get("VOCOCO_TASK_RESUME_LIMIT", "5")))
    if row["resumes"] >= limit:
        _pause(row, "自动续跑已达次数上限。子任务结果已保存，请核对后手动继续。")
        return False
    return tasks.count_running() + running_resumes() < config.TASK_MAX_CONCURRENCY


def _enqueue_resume(row: dict) -> None:
    from ..tools import sessions

    c = connection()
    batch = [(r[0], r[1]) for r in c.execute("SELECT task_id,run_version FROM dispatch_receipts "
                                             "WHERE execution_id=? AND processed=0", (row["id"],))]
    if not batch:
        return
    claim = secrets.token_hex(16)
    changed = c.execute("UPDATE dispatch_executions SET state='claimed',claim=?,batch=?,resumes=resumes+1 "
                        "WHERE id=? AND state='waiting'", (claim, json.dumps(batch), row["id"])).rowcount
    c.commit()
    if not changed:
        return
    text = "【子任务自动续跑】子任务均已结束，结果已放入上下文。请处理成功、失败及取消结果，继续完成原任务。不是用户新授权，不要重复已完成的外部操作。"
    try:
        sessions._enqueue(row["session_key"], text, _resume_access(row, claim))
    except Exception:
        c.execute("UPDATE dispatch_executions SET state='waiting',claim=NULL,resumes=MAX(0,resumes-1) WHERE id=? AND claim=?",
                  (row["id"], claim))
        c.commit()


async def drain() -> None:
    if not enabled():
        return
    for raw in connection().execute("SELECT * FROM dispatch_executions WHERE state='waiting'").fetchall():
        row = dict(raw)
        if _ready(row):
            _enqueue_resume(row)


_pump = None


def wake() -> None:
    import asyncio

    global _pump
    if _pump is None or _pump.done():
        _pump = asyncio.create_task(_run_pump())


async def _run_pump() -> None:
    import asyncio

    while enabled() and connection().execute("SELECT 1 FROM dispatch_executions WHERE state='waiting'").fetchone():
        try:
            await drain()
        except Exception as exc:
            print(f"[dispatch] 回传待重试：{type(exc).__name__}", flush=True)
        await asyncio.sleep(1)


def recover() -> None:
    c = connection()
    # 已领取但进程未开始的轮次可重新投递；已经开始的轮次不自动重做外部操作。
    c.execute("UPDATE dispatch_executions SET state='waiting',claim=NULL WHERE state='claimed'")
    c.execute("UPDATE dispatch_executions SET state='failed' WHERE state='active'")
    c.commit()
    wake()
