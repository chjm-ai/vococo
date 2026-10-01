"""统一后台任务引擎:派发一个任务 = 一个 asyncio task,跑独立的 stream_turn。

并发上限 TASK_MAX_CONCURRENCY,超出的排队(queued);任务终态时通过
core.task_events 广播并顺手拉起下一个排队任务。

2026-07-29 通用化(原 voice/executor.py):语音派发、cron 定时、普通会话发起
"独立新会话"三种触发方式共用这一个引擎——都是"没人盯着、后台自己跑一轮"这同一
件事,只是 dispatch()/append() 调用方不同(见 core.tasks.ORIGINS)。真正随触发方
而变的只有很薄的一层:cron 复用 job_id 当 task_id 实现"到点就 append 一轮"(见
cron/scheduler.py),语音/chat 每次 dispatch 随机生成新 task_id。执行、并发、
worktree、resume、通知这些主体逻辑完全共用,不因触发方分叉。

后台任务会话【不注入】voice/task_tools.py 的工具(不传 extra_mcp_servers)——防止
任务里的模型再调 dispatch_session 之类的工具派生任务、无限套娃(见 00-overview §4.2)。
"""
from __future__ import annotations

import asyncio
import contextvars
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from .. import config, providers
from ..memory import session_store
from ..tools import danger
from . import task_events, tasks, worktree
from .agent import (
    BudgetWarning,
    Done,
    SessionStarted,
    TextDelta,
    ToolFinished,
    ToolInput,
    ToolStarted,
    stream_turn,
)
from .timeline import Timeline

_PROGRESS_THROTTLE_SEC = 5
_SUMMARY_MAX = 50
# 让任务在收尾时"顺手"吐出播报用的一句话摘要,不必为了压缩摘要另开一次完整
# agent 会话(那样要重付一整套 skills/MCP 工具表的打包成本,见 2026-07-28 token
# 审计:单次最高 8.8 万 fresh token,只为了输出一句 ≤50 字的话)。做法:在发给
# 模型的任务文本后面追加这条指令,让它在同一次已经要花的对话里带出摘要;
# 收尾时用 _SUMMARY_TAG_RE 从回复里抠出来,抠不到就退化成朴素截断(兜底路径
# 本来就有,不是新增风险)。
_SUMMARY_TAG_INSTRUCTION = (
    "\n\n(完成后,在回复最后另起一行,格式:[[SUMMARY: 一句不超过50字的中文口语总结,"
    "不要markdown/代码块/星号/反引号,这句话会被语音朗读和推送通知用户——你是被派来"
    "干这件事的后台任务,这句话是在向用户汇报你自己办完的结果,主语是'我',不是在等"
    "用户做什么;如果活确实没干完,就如实说卡在哪一步,不要说成'等你XX'这种把动手的"
    "人说成用户的话]])"
)
_SUMMARY_TAG_RE = re.compile(r"\[\[SUMMARY:\s*(.*?)\s*\]\]", re.DOTALL)
# 后台任务跑的是原始 stream_turn(没有 P0 语音人设那套"禁止 markdown"规则),模型
# 回复里常带 `代码` / **加粗** 这类符号——result_summary 可能被朗读,先摘掉。
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
    (追问/cron 再次触发打断续接 / 原地续聊)。turn_text 非空时作为这一轮实际发给
    模型的文本(而不是 row['prompt']),配合 resume 续同一条 SDK 会话。

    不走 _maybe_start_next 的正常排队逻辑,直接设 running + create_task。
    若 set_status 失败(状态已被别处改了),什么都不做,返回 False。
    """
    task_id = task["id"]
    if not tasks.set_status(task_id, "running"):
        return False
    # 任务重新激活(追问/续接/cron 再次触发)时自动取消归档——归档只在任务静默时
    # 才有意义,重新开跑必须回到侧边栏可见,否则前端「工作中」过滤会藏掉它
    # (见 web_static/sidebar.js buildVoiceTaskRow 的 convFilter 判定)。
    session_store.set_conv_archived(tasks.session_key(task_id), False)
    _notify_activity(task_id)
    _running[task_id] = _spawn(task_id, turn_text)
    return True


def _spawn(task_id: str, turn_text: str | None = None) -> asyncio.Task:
    """起跑一轮任务,用【全新的】contextvars 上下文。

    asyncio.create_task 默认复制调用方的上下文:网页会话里调 dispatch_session 派出的任务,
    会把派发它那一轮的 clarify 路由(会话 key / 网页通道)也继承过去,于是任务里的审批弹窗、
    铃铛通知、审批记录全记到【派发方会话】名下,批准后续跑的也是派发方,任务本身收不到
    (2026-10-01 小红书审稿台任务实测)。任务要的上下文(任务会话 / cwd)由 _run 自己登记。"""
    return asyncio.create_task(_run(task_id, turn_text), context=contextvars.Context())


def _notify_activity(task_id: str) -> None:
    """任务非终态变化后,把最新行广播给在线页面。"""
    from . import task_events

    row = tasks.get(task_id)
    if row is not None:
        task_events.on_task_activity(row)


def progress_text(name: str, tool_input: dict) -> str:
    """把一次顶层工具调用模板化成人话短句,不必额外调 LLM(见 F6)。

    routes.py/ws.py 也用它生成前台轮次的 activity 事件(通话视图的动作行),
    所以是公开函数;task_tools 的 MCP 工具名(mcp__xxx__yyy)单独映射,
    别把内部命名念给用户听。"""
    ti = tool_input or {}
    if "dispatch_session" in name or "voice_dispatch_task" in name:
        return "正在安排后台任务"
    if "continue_session" in name:
        return "正在接续会话"
    if "query_session" in name or "list_sessions" in name:
        return "正在查会话状态"
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


def _split_summary_tag(result_text: str) -> tuple[str, str | None]:
    """从任务回复里拆出 [[SUMMARY: ...]] 标记(见 _SUMMARY_TAG_INSTRUCTION)。
    返回 (去掉标记后的正文, 摘要或 None)——标记缺失时摘要为 None,由调用方
    自己决定怎么兜底,不在这里悄悄降级。"""
    text = result_text or ""
    m = _SUMMARY_TAG_RE.search(text)
    if not m:
        return text, None
    summary = _MD_STRIP_RE.sub("", m.group(1)).strip()
    clean = (text[: m.start()] + text[m.end() :]).rstrip()
    return clean, (summary[:_SUMMARY_MAX] if summary else None)


def _summarize(result_text: str) -> str:
    """结果压成 ≤50 字一句话(F7)的兜底:朴素截断,不再为了压摘要单独开一次
    LLM 会话——那样每次都要重付一整套 skills/MCP 工具表的打包成本(2026-07-28
    token 审计:单次最高 8.8 万 fresh token,只为了一句 ≤50 字的话)。正常路径
    走 _split_summary_tag 拿模型顺手带出的标记,只有标记缺失才落到这里。"""
    text = _MD_STRIP_RE.sub("", (result_text or "").strip())
    if not text:
        return "(没有产出内容)"
    if len(text) <= _SUMMARY_MAX:
        return text
    return text[: _SUMMARY_MAX - 1] + "…"


async def _push_budget_warning(row: dict, session_key: str, note: str) -> None:
    """80% 提醒推到手机(Web Push)。推送不可用/失败都不影响任务继续跑。"""
    try:
        from ..gateway.adapters.web_push import PUSH  # 懒加载:core 不在导入期依赖网关

        await PUSH.notify(
            title=f"后台任务快到 token 上限:{row.get('title') or '任务'}",
            body=note, conv=session_key, kind="proactive",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[task_runner] 预算提醒推送失败:{exc}", flush=True)


def _cron_job_budget(task_id: str) -> int | None:
    """cron 任务的 job_id 就是 task_id;job 里填了 budget_tokens 就用它覆盖全局单次上限。"""
    try:
        from ..cron import scheduler  # 懒加载:cron 包依赖本模块,顶部 import 会循环

        for job in scheduler.load_jobs():
            if job.get("id") == task_id and job.get("budget_tokens"):
                return int(job["budget_tokens"])
    except Exception:  # noqa: BLE001 —— 读不到 job 就用全局默认
        pass
    return None


def _counts_daily(row: dict) -> bool:
    """哪些任务计入每日总量:无人值守的 cron / 语音派活;网页派出的独立会话不算。"""
    return row.get("origin") != "chat"


def _token_budget(row: dict) -> tuple[int, str]:
    """本轮 token 上限 + 超每日总量时的拒跑原因(空串=可以跑)。0=不限。"""
    if row.get("origin") == "chat":
        return config.CHAT_TASK_TOKEN_BUDGET, ""
    per_run = _cron_job_budget(row["id"]) or config.TASK_TOKEN_BUDGET
    cap = config.BG_DAILY_TOKEN_BUDGET
    if not cap:
        return per_run, ""
    remaining = cap - tasks.daily_usage()
    if remaining <= 0:
        return 0, (
            f"今天后台任务的 token 总量已达上限({cap // 10000} 万),这一轮没有启动;"
            "明天自动恢复,急用可在 .env 调大 BG_DAILY_TOKEN_BUDGET"
        )
    return (min(per_run, remaining) if per_run else remaining), ""


def _timeout_min(row: dict) -> int:
    """网页独立会话默认不限时，其他无人值守任务保留兜底超时。"""
    if row["origin"] == "chat":
        return config.CHAT_TASK_TIMEOUT_MIN
    return config.TASK_TIMEOUT_MIN


async def _run(task_id: str, turn_text: str | None = None) -> None:
    """跑一个任务的一轮对话。turn_text 为 None(首次派发)用 row['prompt'];
    非 None(追问/cron 再次触发)则是这一轮实际发给模型的文本——历史靠 resume 接,
    不用每次都把全部历史重新拼进 prompt(见 append())。"""
    from . import task_events

    row = tasks.get(task_id)
    if row is None:
        return
    prompt_text = turn_text if turn_text is not None else row["prompt"]
    # 派发的这一轮对话也落进跟普通文字对话共用的 session_store(不只是 voice.db 里的
    # result_full 摘要)——这样任务完成后用户能在侧边栏"后台任务"分组里看到完整对话,
    # 并且能继续用文字追问(续聊直接走 web 发消息路径的 converse(),见 web.py)。
    session_key = tasks.session_key(task_id)
    turn_id = session_store.start_turn(session_key, prompt_text)
    sdk_session_id: str | None = None
    # Git 项目任务必须在专属 worktree + 分支(vococo/<task_id>)执行;创建失败就让任务失败,
    # 绝不能回退 row["cwd"] 的主检出目录。非 Git 目录仍可原样执行资料类任务。
    effective_cwd = row["cwd"]
    cwd_token = None
    last_progress_ts = 0.0
    result_text = ""
    status = "failed"
    error_note = ""
    used_tokens = 0  # 本轮新鲜 token,收尾计入每日总量
    tool_calls = 0  # 本轮顶层工具调用次数(子代理内部的不算),记进运行指标
    started_at = time.monotonic()
    token_budget, over_daily = _token_budget(row)
    # 录过程时间线(工具调用 + 正文交错),跟普通文字对话(gateway/core.py converse())
    # 对齐——否则任务跑完侧边栏只看得到最后一句摘要,回溯不了 AI 到底做了什么。
    timeline = Timeline()

    async def _drive() -> None:
        nonlocal result_text, last_progress_ts, error_note, sdk_session_id, used_tokens, tool_calls
        if over_daily:
            error_note = over_daily
            return
        resume_sid = session_store.get_sdk_session_id(session_key)
        # 会话选定的模型——派发/追问时(dispatch 的 model 参数)已经提前写进
        # session_meta(见 dispatch()),这里读出来传给 stream_turn;没设过就是
        # 空串,stream_turn 内部 providers.resolve(None,...) 自动落到全局默认。
        model = session_store.get_chosen_model(session_key) or None
        # 没显式指定模型 → 默认回退到已配置的第三方供应商,不再走官方订阅:
        # 订阅 token 被封(401 OAuth access token has been revoked)时,没设
        # model 的后台任务会一启动就失败。sidecar_env 按供应商名取 (model, env),
        # 未配置/缺 key/是官方端点时返回 None → 保持原样落 config.MODEL 官方默认。
        # 优先 DeepSeek(便宜且稳定);没配 DeepSeek 就兜任意已配置第三方
        # (如 Codex OAuth 代理的 GPT 系),仍不落官方订阅。
        if model is None:
            ds = providers.sidecar_env("deepseek")
            if ds is None:
                ds = providers.sidecar_env("")
            if ds is not None:
                model = ds[0]
        # 追加的标记指令只喂给模型,不进 turns 表(session_key.start_turn 存的是
        # 上面干净的 prompt_text)——收尾时从回复里抠出来,见 _split_summary_tag。
        from ..memory import agents

        async for ev in stream_turn(
            [], prompt_text + _SUMMARY_TAG_INSTRUCTION, model=model, cwd=effective_cwd,
            is_explicit_project=bool(row.get("cwd_explicit")), session_key=session_key,
            resume=resume_sid, max_turns=config.TASK_MAX_TURNS, token_budget=token_budget,
            system_prompt_extra=agents.prompt_extra_for_session(session_key),
        ):
            if isinstance(ev, SessionStarted):
                # 尽早存回 session_store,而不是等整轮跑完的 Done——这样哪怕这一轮
                # 之后被 append 打断(CancelledError,永远走不到 Done),下一轮追问
                # 依然能 resume 回同一条 SDK 会话,不会丢上下文重开对话。
                sdk_session_id = ev.session_id
                session_store.set_sdk_session_id(session_key, sdk_session_id)
            elif isinstance(ev, TextDelta):
                timeline.text(danger.redact_secrets(ev.text))
            elif isinstance(ev, ToolStarted):
                timeline.tool_started(ev.name, ev.tool_id, ev.parent_id)
                if ev.parent_id is None:
                    tool_calls += 1
            elif isinstance(ev, ToolFinished):
                timeline.tool_finished(
                    ev.name, ev.ok, ev.preview, ev.tool_id, ev.detail, ev.parent_id
                )
            elif isinstance(ev, ToolInput):
                timeline.tool_input(ev.tool_id, ev.tool_input, ev.parent_id)
                if ev.parent_id is None:
                    now = time.monotonic()
                    if now - last_progress_ts >= _PROGRESS_THROTTLE_SEC:
                        last_progress_ts = now
                        tasks.set_progress(task_id, progress_text(ev.name, ev.tool_input))
                        _notify_activity(task_id)
            elif isinstance(ev, BudgetWarning):
                note = (
                    f"⚠️ token 已用 {ev.used * 100 // ev.budget}%"
                    f"(约 {ev.used // 10000} 万 / 上限 {ev.budget // 10000} 万),超过上限会自动停止"
                )
                tasks.set_progress(task_id, note)
                _notify_activity(task_id)
                await _push_budget_warning(row, session_key, note)
            elif isinstance(ev, Done):
                result_text = ev.reply.text
                sdk_session_id = ev.reply.sdk_session_id
                used_tokens = max(ev.reply.stream_tokens, ev.reply.turn_tokens)
                if ev.reply.is_error:
                    error_note = ev.reply.error or "模型返回了错误"
                # 跟普通文字对话(gateway/core.py converse())对齐:记一笔 token 用量,
                # 否则 session_meta 的 ctx_tokens/total_tokens 永远是 0,Web UI 右上角
                # 的上下文用量图标会把这条会话误判成"全新会话"而一直不显示。
                if ev.reply.context_tokens or ev.reply.turn_tokens:
                    session_store.record_usage(
                        session_key,
                        ev.reply.context_tokens,
                        ev.reply.turn_tokens,
                        window=ev.reply.context_window,
                        last_in=ev.reply.input_fresh,
                        last_cache=ev.reply.cache_read,
                        last_out=ev.reply.output_tokens,
                        model=ev.reply.model,
                    )

    # 登记本轮属于哪个任务会话:审批闸据此把要批的操作推到 Web 任务会话、记审批记录/待批队列
    task_session_token = danger.set_task_session(session_key)
    try:
        effective_cwd = await worktree.execution_cwd_for_task(row["cwd"], task_id)
        cwd_token = danger.set_cwd(
            effective_cwd,
            project_root=row["cwd"] if effective_cwd != row["cwd"] else None,
        )
        timeout_min = _timeout_min(row)
        if timeout_min > 0:
            await asyncio.wait_for(_drive(), timeout=timeout_min * 60)
        else:
            await _drive()
        status = "failed" if error_note else "done"
    except asyncio.CancelledError:
        status = "cancelled"
    except asyncio.TimeoutError:
        error_note = f"超时(超过 {timeout_min} 分钟)"
    except Exception as exc:  # noqa: BLE001 —— 兜底:任何异常都要走到终态收尾,不留 running 僵尸
        error_note = f"执行出错:{exc}"
    finally:
        if cwd_token is not None:
            danger.reset_cwd(cwd_token)
        danger.reset_task_session(task_session_token)
        _running.pop(task_id, None)
        if used_tokens and _counts_daily(row):
            try:
                tasks.add_daily_usage(used_tokens)
            except Exception as exc:  # noqa: BLE001 —— 记账失败不影响任务收尾
                print(f"[task_runner] 每日用量记账失败:{exc}", flush=True)

    try:
        tasks.set_run_metrics(task_id, used_tokens, time.monotonic() - started_at, tool_calls)
    except Exception as exc:  # noqa: BLE001 —— 指标只是统计,写失败不影响任务收尾
        print(f"[task_runner] 运行指标记账失败:{exc}", flush=True)

    if status == "cancelled":
        session_store.finish_turn(turn_id, "(任务已取消)", events=timeline.blocks, session_key=session_key)
        ok = tasks.set_status(task_id, "cancelled", progress_note="已取消")
    elif status == "done":
        clean_text, tag_summary = _split_summary_tag(result_text)
        session_store.finish_turn(turn_id, clean_text, events=timeline.blocks, session_key=session_key)
        if sdk_session_id:
            session_store.set_sdk_session_id(session_key, sdk_session_id)
        ok = tasks.finish(task_id, "done", clean_text, tag_summary or _summarize(clean_text))
    else:
        session_store.finish_turn(
            turn_id, result_text or f"(执行失败:{error_note})", events=timeline.blocks,
            session_key=session_key,
        )
        if sdk_session_id:
            session_store.set_sdk_session_id(session_key, sdk_session_id)
        ok = tasks.finish(task_id, "failed", result_text, _humanize_error(error_note) if error_note else "执行失败")
    if not ok:
        # 收尾写库被状态机拒绝 = 任务状态被别处改过(误标/竞态)。响亮记录,
        # 绝不静默吞掉——2026-07-12 事故里 finish('done') 被拒后无声无息,
        # 任务板停在假"失败",排障多绕了一大圈。
        row_now = tasks.get(task_id)
        print(
            f"[task_runner] ⚠️ 任务 {task_id} 收尾写 {status} 被拒,"
            f"库里当前状态是 {row_now['status'] if row_now else '(已不存在)'}"
            f"——它的状态被别的进程/路径改过,任务板显示的可能不是真实结局",
            flush=True,
        )

    _record_agent_run(task_id, status)
    await task_events.emit_terminal(task_id)

    _maybe_start_next()


def _record_agent_run(task_id: str, status: str) -> None:
    """从 Agent 会话派出的后台任务(非定时)跑完 → 记进该 Agent 的运行记录。

    定时任务不在这里记:它们由 cron/scheduler 的终态钩子记,还会复制一份到主会话。
    用户取消的不记——那不是一次真正的运行结果。"""
    row = tasks.get(task_id)
    if not row or not row.get("agent_id") or row.get("origin") == "cron" or status == "cancelled":
        return
    from ..memory import agents

    try:
        agents.record_run(
            row["agent_id"], {"id": task_id, "name": row["title"]},
            "success" if status == "done" else "error",
            row.get("result_summary") or row.get("result_full") or "(无输出)",
            metrics=agents.metrics_from_task(row), to_main=False,
        )
    except Exception as exc:  # noqa: BLE001 —— 记录失败不影响任务本身
        print(f"[task_runner] 写 Agent 运行记录失败:{exc}", flush=True)


def _maybe_start_next() -> None:
    """并发有空位就从队首拉一个 queued 任务起跑;没有空位/没有排队任务则什么都不做。"""
    while tasks.count_running() < config.TASK_MAX_CONCURRENCY:
        queued = tasks.list_queued()
        if not queued:
            return
        nxt = queued[0]
        if not tasks.set_status(nxt["id"], "running"):
            continue  # 状态已被别处改了(如取消排队中的任务),跳过看下一个
        _notify_activity(nxt["id"])
        _running[nxt["id"]] = _spawn(nxt["id"])


def _source_agent_id(context_session_key: str | None) -> str | None:
    """派发来源会话属于哪个项目 Agent → 任务也归它(拿它的设定、结果记进它的运行记录)。

    通用 Agent 不算:语音/全局主会话派的活一向是「无主」的,不给它们套通用设定。"""
    if not context_session_key:
        return None
    from ..memory import agents

    try:
        a = agents.agent_for_session(context_session_key)
    except Exception:  # noqa: BLE001 —— 认不出归属就当无主,不影响派发
        return None
    return a["id"] if a and a["id"] != agents.GENERAL_ID else None


def dispatch(
    title: str,
    prompt: str,
    cwd: str | None = None,
    dispatch_platform: str | None = None,
    dispatch_chat_id: str | None = None,
    model: str | None = None,
    origin: str = "voice",
    task_id: str | None = None,
    context_session_key: str | None = None,
) -> dict:
    """落库 + 尝试立即起跑(并发满则排队)。立即返回任务行,不等待执行。

    dispatch_platform/dispatch_chat_id: 任务是从哪个平台哪个会话派来的,
    终态通知时靠它们回推该发给谁(见 voice/notify.py)。
    model:指定要用哪个模型跑这个任务(如 claude-opus-5-5),不传就用当前全局默认——
    必须在 _maybe_start_next() 真正起跑前写进 session_meta.chosen_model,
    _run()/_drive() 才能读到(见 _drive 里的 get_chosen_model)。
    origin/task_id 透传给 tasks.create()(见其文档:cron 复用 job_id 当 task_id)。
    context_session_key:当前对话 key;cwd 未指定时按该会话匹配具体项目,匹配不到回落
    默认项目。最终落库的是项目根,执行时再由 _run() 创建任务专属 worktree。
    """
    cwd_explicit = bool((cwd or "").strip())
    cwd = config.resolve_execution_root(session_key=context_session_key, cwd=cwd)
    task = tasks.create(title=title, prompt=prompt, cwd=cwd, cwd_explicit=cwd_explicit,
                        dispatch_platform=dispatch_platform,
                        dispatch_chat_id=dispatch_chat_id,
                        origin=origin, task_id=task_id,
                        agent_id=_source_agent_id(context_session_key))
    if model:
        session_store.set_chosen_model(tasks.session_key(task["id"]), model)
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
      同一条 SDK 会话继续聊,不用重新交代前情。cron 任务每次到点触发都走这条
      分支(job_id 复用为 task_id,上次运行必是终态)。

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
                asyncio.create_task(task_events.emit_terminal(task_id))
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
    """serve 重启后调用一次:把残留 queued/running 标失败并广播终态。"""
    for row in tasks.mark_orphans_failed(exclude_ids=set(_running)):
        await task_events.emit_terminal(row["id"])
