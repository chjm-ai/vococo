"""P1 后台执行器:派发一个任务 = 一个 asyncio task,跑独立的 stream_turn。

并发上限 VOICE_TASK_MAX_CONCURRENCY,超出的排队(queued);任务终态时触发
notify.on_task_terminal 并顺手拉起下一个排队任务。

后台任务会话【不注入】task_tools 的四个工具(不传 extra_mcp_servers)——防止
任务里的模型再调 voice_dispatch_task 派生任务、无限套娃(见 00-overview §4.2)。
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from .. import config
from ..core import worktree
from ..core.agent import Done, SessionStarted, ToolInput, run_turn, stream_turn
from ..memory import session_store
from ..tools import danger
from . import notify, tasks

_PROGRESS_THROTTLE_SEC = 5
_SUMMARY_MAX = 50
_SUMMARY_PROMPT = (
    "把下面这段内容压成一句不超过 {n} 字的中文口语摘要,直接给结果,不要加任何解释或前缀;"
    "这句话会被语音朗读出来,禁止 markdown/代码块/星号/反引号这类没法读的符号:\n\n{text}"
)
# 后台任务跑的是原始 stream_turn(没有 P0 语音人设那套"禁止 markdown"规则),模型
# 回复里常带 `代码` / **加粗** 这类符号——result_summary 最终要被朗读,先摘掉。
_MD_STRIP_RE = re.compile(r"[`*_#]+")


def _humanize_error(error_note: str) -> str:
    """SDK 报错原文常是英文(如 "Reached maximum number of turns (40)"),而失败任务的
    error_note 会被 notify 直接朗读/推送——先翻译成中文人话,翻不出来的保留原文截断。"""
    low = (error_note or "").lower()
    if "maximum number of turns" in low or "error_max_turns" in low:
        return "步骤太多,超过单次任务的工具调用轮数上限,没能跑完"
    if any(kw in low for kw in ("rate", "429", "quota", "overloaded", "529")):
        return "模型限流或过载了,过一会儿重新派一次就行"
    if any(kw in low for kw in ("timeout", "timed out")):
        return "执行超时了"
    return error_note[:120]

# task_id -> 对应的 asyncio task,供 cancel() 取消一个正在跑的任务
_running: dict[str, asyncio.Task] = {}


def _start_one(task: dict, turn_text: str | None = None) -> bool:
    """内部:立刻起跑一条任务(跳过排队队列),用于首次派发之外的重开场景
    (追问打断续接 / 追问原地续聊)。turn_text 非空时作为这一轮实际发给模型的
    文本(而不是 row['prompt']),配合 resume 续同一条 SDK 会话。

    不走 _maybe_start_next 的正常排队逻辑,直接设 running + create_task。
    若 set_status 失败(状态已被别处改了),什么都不做,返回 False。
    """
    task_id = task["id"]
    if not tasks.set_status(task_id, "running"):
        return False
    _notify_activity(task_id)
    _running[task_id] = asyncio.create_task(_run(task_id, turn_text))
    return True


def _notify_activity(task_id: str) -> None:
    """任务非终态变化后,把最新行广播给在线页面(通话视图任务状态条实时刷新)。"""
    row = tasks.get(task_id)
    if row is not None:
        notify.on_task_activity(row)


def progress_text(name: str, tool_input: dict) -> str:
    """把一次顶层工具调用模板化成人话短句,不必额外调 LLM(见 F6)。

    routes.py/ws.py 也用它生成前台轮次的 activity 事件(通话视图的动作行),
    所以是公开函数;task_tools 的 MCP 工具名(mcp__xxx__yyy)单独映射,
    别把内部命名念给用户听。"""
    ti = tool_input or {}
    if "voice_dispatch_task" in name:
        return "正在安排后台任务"
    if "voice_query_task" in name or "voice_list_tasks" in name:
        return "正在查任务进度"
    if name.startswith("mcp__"):
        return "正在使用工具"
    if name == "Bash":
        cmd = (ti.get("command") or "").strip()
        return f"正在执行:{cmd[:40]}" if cmd else "正在执行命令"
    if name == "Read":
        p = ti.get("file_path") or ""
        return f"正在读取 {Path(p).name}" if p else "正在读取文件"
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        p = ti.get("file_path") or ti.get("notebook_path") or ""
        return f"正在写入 {Path(p).name}" if p else "正在写入文件"
    if name in ("Grep", "Glob"):
        pat = ti.get("pattern") or ""
        return f"正在搜索「{pat[:20]}」" if pat else "正在搜索"
    if name == "WebSearch":
        q = (ti.get("query") or "").strip()
        return f"正在搜索:{q[:24]}" if q else "正在搜网页"
    if name == "WebFetch":
        host = urlparse(ti.get("url") or "").netloc
        return f"正在读网页:{host}" if host else "正在读网页"
    if name in ("Agent", "Task"):
        desc = (ti.get("description") or "").strip()
        return f"正在派子任务:{desc[:20]}" if desc else "正在派子任务"
    return f"正在使用 {name}"


async def _summarize(result_text: str) -> str:
    """结果压成 ≤50 字一句话(F7):短结果直接用;长结果尝试一次轻量 LLM 压缩,
    失败(异常/空/报错)一律降级为截断,不让摘要失败拖垮任务收尾。"""
    text = _MD_STRIP_RE.sub("", (result_text or "").strip())
    if not text:
        return "(没有产出内容)"
    if len(text) <= _SUMMARY_MAX:
        return text
    try:
        reply = await run_turn([], _SUMMARY_PROMPT.format(n=_SUMMARY_MAX, text=text[:2000]))
        summary = (reply.text or "").strip()
        if summary and not reply.is_error:
            return summary[:_SUMMARY_MAX]
    except Exception:
        pass
    return text[: _SUMMARY_MAX - 1] + "…"


async def _run(task_id: str, turn_text: str | None = None) -> None:
    """跑一个任务的一轮对话。turn_text 为 None(首次派发)用 row['prompt'];
    非 None(追问重开)则是这一轮实际发给模型的文本——历史靠 resume 接,
    不用每次都把全部历史重新拼进 prompt(见 append())。"""
    row = tasks.get(task_id)
    if row is None:
        return
    prompt_text = turn_text if turn_text is not None else row["prompt"]
    # 派发的这一轮对话也落进跟普通文字对话共用的 session_store(不只是 voice.db 里的
    # result_full 摘要)——这样任务完成后用户能在侧边栏"语音任务"分组里看到完整对话,
    # 并且能继续用文字追问(续聊直接走 web 发消息路径的 converse(),见 web.py)。
    session_key = f"voice-task:{task_id}"
    turn_id = session_store.start_turn(session_key, prompt_text)
    sdk_session_id: str | None = None
    # 任务 cwd 指到一个 git 仓库就给这次任务开专属 worktree + 分支(hermes/<task_id>),
    # 跟 Web/CLI「一会话一分支」看齐——语音这边真要动代码,必须走这条派后台任务的路径
    # 才能拿到工具(前台会话已代码层禁掉 Edit/Write,见 voice/session.py),而这条路径
    # 现在也不再直接在原 cwd 上改,落地的是隔离分支,不是主目录/主分支。非 git 仓库
    # (或没给 cwd)拿到 None,原样回退到 row["cwd"],不阻塞非代码类任务(查资料等)。
    worktree_dir = await worktree.ensure_worktree_for_task(row["cwd"], task_id)
    effective_cwd = worktree_dir or row["cwd"]
    cwd_token = danger.set_cwd(effective_cwd, project_root=row["cwd"] if worktree_dir else None)
    last_progress_ts = 0.0
    result_text = ""
    status = "failed"
    error_note = ""

    async def _drive() -> None:
        nonlocal result_text, last_progress_ts, error_note, sdk_session_id
        resume_sid = session_store.get_sdk_session_id(session_key)
        async for ev in stream_turn(
            [], prompt_text, cwd=effective_cwd, session_key=session_key, resume=resume_sid,
            max_turns=config.VOICE_TASK_MAX_TURNS,
        ):
            if isinstance(ev, SessionStarted):
                # 尽早存回 session_store,而不是等整轮跑完的 Done——这样哪怕这一轮
                # 之后被 voice_append_task 打断(CancelledError,永远走不到 Done),
                # 下一轮追问依然能 resume 回同一条 SDK 会话,不会丢上下文重开对话。
                sdk_session_id = ev.session_id
                session_store.set_sdk_session_id(session_key, sdk_session_id)
            elif isinstance(ev, ToolInput) and ev.parent_id is None:
                now = time.monotonic()
                if now - last_progress_ts >= _PROGRESS_THROTTLE_SEC:
                    last_progress_ts = now
                    tasks.set_progress(task_id, progress_text(ev.name, ev.tool_input))
                    _notify_activity(task_id)
            elif isinstance(ev, Done):
                result_text = ev.reply.text
                sdk_session_id = ev.reply.sdk_session_id
                if ev.reply.is_error:
                    error_note = ev.reply.error or "模型返回了错误"

    try:
        await asyncio.wait_for(_drive(), timeout=config.VOICE_TASK_TIMEOUT_MIN * 60)
        status = "failed" if error_note else "done"
    except asyncio.CancelledError:
        status = "cancelled"
    except asyncio.TimeoutError:
        error_note = f"超时(超过 {config.VOICE_TASK_TIMEOUT_MIN} 分钟)"
    except Exception as exc:  # noqa: BLE001 —— 兜底:任何异常都要走到终态收尾,不留 running 僵尸
        error_note = f"执行出错:{exc}"
    finally:
        danger.reset_cwd(cwd_token)
        _running.pop(task_id, None)

    if status == "cancelled":
        session_store.finish_turn(turn_id, "(任务已取消)")
        ok = tasks.set_status(task_id, "cancelled", progress_note="已取消")
    elif status == "done":
        session_store.finish_turn(turn_id, result_text)
        if sdk_session_id:
            session_store.set_sdk_session_id(session_key, sdk_session_id)
        ok = tasks.finish(task_id, "done", result_text, await _summarize(result_text))
    else:
        session_store.finish_turn(turn_id, result_text or f"(执行失败:{error_note})")
        if sdk_session_id:
            session_store.set_sdk_session_id(session_key, sdk_session_id)
        ok = tasks.finish(task_id, "failed", result_text, _humanize_error(error_note) if error_note else "执行失败")
    if not ok:
        # 收尾写库被状态机拒绝 = 任务状态被别处改过(误标/竞态)。响亮记录,
        # 绝不静默吞掉——2026-07-12 事故里 finish('done') 被拒后无声无息,
        # 任务板停在假"失败",排障多绕了一大圈。
        row_now = tasks.get(task_id)
        print(
            f"[voice/task] ⚠️ 任务 {task_id} 收尾写 {status} 被拒,"
            f"库里当前状态是 {row_now['status'] if row_now else '(已不存在)'}"
            f"——它的状态被别的进程/路径改过,任务板显示的可能不是真实结局",
            flush=True,
        )

    await notify.on_task_terminal(task_id)

    _maybe_start_next()


def _maybe_start_next() -> None:
    """并发有空位就从队首拉一个 queued 任务起跑;没有空位/没有排队任务则什么都不做。"""
    while tasks.count_running() < config.VOICE_TASK_MAX_CONCURRENCY:
        queued = tasks.list_queued()
        if not queued:
            return
        nxt = queued[0]
        if not tasks.set_status(nxt["id"], "running"):
            continue  # 状态已被别处改了(如取消排队中的任务),跳过看下一个
        _notify_activity(nxt["id"])
        _running[nxt["id"]] = asyncio.create_task(_run(nxt["id"]))


def dispatch(
    title: str,
    prompt: str,
    cwd: str | None = None,
    dispatch_platform: str | None = None,
    dispatch_chat_id: str | None = None,
) -> dict:
    """落库 + 尝试立即起跑(并发满则排队)。立即返回任务行,不等待执行。

    dispatch_platform/dispatch_chat_id: 任务是从哪个平台哪个会话派来的,
    终态通知时靠它们回推该发给谁(见 notify.py)。"""
    task = tasks.create(title=title, prompt=prompt, cwd=cwd,
                        dispatch_platform=dispatch_platform,
                        dispatch_chat_id=dispatch_chat_id)
    _maybe_start_next()
    # 派发瞬间就推给在线页面(状态条立刻出现);重新 get 拿最新状态——
    # 上一行可能已把它从 queued 拉成 running
    _notify_activity(task["id"])
    return task


async def append(task_id: str, instruction: str) -> dict:
    """给一个已有后台任务追加一轮指令——原地续在同一个 task_id 上,自始至终只有
    一条任务/一条 SDK 会话/一个 worktree,不再派生新任务:

    - queued(还没轮到):新指令直接并进待执行的 prompt。
    - running(正在跑):打断当前这轮,resume 回打断前已经存好的 SDK 会话 id
      (见 _run 里 SessionStarted 的早捕获),带着已经做的工作接上新指令重跑。
    - 终态(done/failed/cancelled):直接把 instruction 当这一轮的话,resume 回
      同一条 SDK 会话继续聊,不用重新交代前情。

    返回 {"ok": bool, "message": str, "task": dict|None}。
    """
    row = tasks.get(task_id)
    if row is None:
        return {"ok": False, "message": f"任务不存在(id={task_id})", "task": None}

    if row["status"] == "queued":
        merged = f"{row['prompt']}\n\n--- 追加指令 ---\n{instruction}"
        tasks.update_prompt(task_id, merged)
        return {"ok": True,
                "message": "还没开始跑,新指令已并入待执行内容(仍是原任务)",
                "task": tasks.get(task_id)}

    if row["status"] == "running":
        await cancel_and_wait(task_id)  # 等 _run 真正收尾(sdk_session_id 已提前存好)
        started = _start_one(tasks.get(task_id), turn_text=instruction)
        if not started:
            return {"ok": False, "message": "任务状态被别处改过,追加失败",
                    "task": tasks.get(task_id)}
        return {"ok": True,
                "message": "已打断原任务,带着已完成的进度接上新指令继续跑",
                "task": tasks.get(task_id)}

    # 终态:done / failed / cancelled —— 原地续聊
    started = _start_one(row, turn_text=instruction)
    if not started:
        return {"ok": False, "message": "任务状态被别处改过,追加失败",
                "task": tasks.get(task_id)}
    return {"ok": True, "message": "原任务已结束,已在原任务上继续跑新指令",
            "task": tasks.get(task_id)}


def cancel(task_id: str) -> bool:
    """取消一个任务:排队中直接置 cancelled;运行中 cancel 对应 asyncio task
    (由 _run 的 except CancelledError 分支落终态)。"""
    row = tasks.get(task_id)
    if row is None:
        return False
    if row["status"] == "queued":
        # 排队中取消不会走 _run 的终态收尾(没有 on_task_terminal),
        # 但已是一个终态——需要通知离线用户(Web Push / 平台推送),
        # 不只是推 SSE 给在线页面。SSE 通知走 _notify_activity,
        # Web Push/平台推送走 on_task_terminal(异步,create_task 不阻塞)。
        ok = tasks.set_status(task_id, "cancelled", progress_note="已取消(未开始)")
        if ok:
            _notify_activity(task_id)
            try:
                asyncio.get_running_loop()  # 只在有 event loop 时才发异步通知
            except RuntimeError:
                pass
            else:
                asyncio.create_task(notify.on_task_terminal(task_id))
        return ok
    if row["status"] == "running":
        t = _running.get(task_id)
        if t is not None:
            t.cancel()
            return True
    return False


async def cancel_and_wait(task_id: str, timeout: float = 10.0) -> None:
    """取消并等运行中的任务收尾完成。

    删除任务会话前必须走这个而不是 cancel():cancel 只是发出取消请求,
    _run 的收尾还会 finish_turn 写回会话——先删会话再等它写,会话就"复活"了。
    asyncio.wait 超时不抛也不重复 cancel,超时就放弃等待(收尾极端卡住时删除照常进行)。
    """
    cancel(task_id)
    t = _running.get(task_id)
    if t is not None:
        await asyncio.wait({t}, timeout=timeout)


async def heal_after_restart() -> None:
    """serve 重启后调用一次(见 F11):把残留 queued/running 标失败,并按通知规则分发。

    只在 web.py 的 serve 启动路径调用(不要挂到 register_routes 之类会被测试/脚本
    顺带执行的地方);并排除本进程 _running 里的活任务——刚启动时它本来就是空的,
    这层排除是防线,保证本函数无论被谁在什么时机调用都杀不了活人。
    """
    for row in tasks.mark_orphans_failed(exclude_ids=set(_running)):
        await notify.on_task_terminal(row["id"])
