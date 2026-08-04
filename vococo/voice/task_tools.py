"""P1 任务板 + 网页跨端续聊的 MCP 工具:voice_dispatch_task / voice_cancel_task /
voice_list_sessions / voice_query_session / voice_continue_session。

只注入进语音前台会话(routes.py 调 session.run_turn 那次 stream_turn),后台任务
会话本身不挂这组工具——防止任务里的模型再派任务、无限套娃(见 00-overview.md §4.2)。

2026-07-29:原来是六个工具(voice_append_task/voice_query_task/voice_list_tasks 管
后台任务,voice_continue_web/voice_list_web_sessions 管网页对话),对模型而言是
"同一件事(续聊/查状态/列会话)在两种来源上各实现一遍"——用户明确要求把这套
读/续接接口收口:session_id 自动识别是后台任务 id 还是网页对话 conv,不用模型
自己先判断"这是哪种东西该调哪个工具"。

同一天进一步统一:后台任务本身也不再是语音专属——core/task_runner.py(原
voice/executor.py)现在是语音派发/cron 定时/普通会话发起"独立新会话"三种触发
方式共用的引擎(见 core/tasks.py 的 origin 字段)。voice_dispatch_task 派发时
固定传 origin="voice";列表/默认查询也只看 origin="voice" 的任务,不然语音会话
问"我那个任务怎么样了"时会被 cron 定时任务或网页发起的任务搅混——但显式给
session_id 查/续接不限制 origin,用户明确点名哪个就查哪个,不因为它是网页/cron
发起的就拒绝。
"""
from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server, tool

from .. import config, providers
from ..core import task_runner, task_words, tasks
from ..core.task_words import status_word
from ..gateway import clarify, web_bridge
from ..gateway.core import MODEL_CHOICES
from ..memory import session_store

# 可选模型走 available_models 合并官方档 + 设置页第三方供应商(DeepSeek/Kimi 等),
# 只拿裸 MODEL_CHOICES 会让模型以为只能选 6 个 Claude,看不到 kimi-k3 这类已配模型
_MODEL_EXAMPLES = "、".join(m for m, _, _ in providers.available_models(MODEL_CHOICES))

# 2026-08-04 续接优先:派发前强制关联检测的配套(见 voice_dispatch_task)。
# _hinted_candidates 记"已提示过可续接候选"的任务 id——同一候选再次命中就放行,
# 防"模型重调 dispatch 被同一候选永远拦死"的循环(用户确认新主题后重调即放行;
# 进程内生命周期足够,重启后最多再提示一次,可接受)。
_hinted_candidates: set[str] = set()


def _title_ngrams(title: str) -> set[str]:
    """标题切成 2 字滑动窗口(bigram)做关键词集:不依赖分词库,中文 2-4 字实词
    (查资料/写报告/加功能)重叠即可捕捉;先剔除空白/标点,避免产生无意义窗口。"""
    chars = [c for c in title if not c.isspace() and c not in "《》【】()（）,，。.、!?！？·—:：;；'\"“”‘’"]
    return {"".join(chars[i:i + 2]) for i in range(len(chars) - 1)}


def _find_related_candidate(title: str) -> dict | None:
    """派发前查最近语音任务里标题与本次相关的那个(先续接不新开,见
    voice/prompts.py 规则8)。只看 origin='voice'——cron/网页派的任务不在语音的
    "可续接面"内(语音会话没有它们的上下文)。命中返回任务 dict,未命中返回 None。"""
    ngrams = _title_ngrams(title)
    for t in tasks.list_recent(10, origin="voice"):
        if t["id"] in _hinted_candidates:
            continue  # 同一候选已提示过,放行(防死循环)
        if ngrams & _title_ngrams(t["title"]):
            return t
    return None


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _describe_task(task: dict) -> str:
    parts = [f"任务「{task['title']}」(session_id={task['id']}):{status_word(task['status'])}"]
    if task["status"] == "running" and task["progress_note"]:
        task_words.flag_if_reversed_direction(task["progress_note"], "task_tools._describe_task(running)")
        parts.append(f"当前进展:{task['progress_note']}")
    if task["status"] in tasks.TERMINAL_STATUSES and task["result_summary"]:
        task_words.flag_if_reversed_direction(task["result_summary"], "task_tools._describe_task(terminal)")
        parts.append(f"结果:{task['result_summary']}")
    return "。".join(parts)


def _web_conv(session_key: str) -> str:
    """web: 前缀的 session_key → 纯 conv id(去掉前缀)。"""
    prefix = "web:"
    return session_key[len(prefix):] if session_key.startswith(prefix) else session_key


def _find_web_session(session_id: str) -> dict | None:
    """按 conv id 在网页会话列表里找一条;找不到返回 None。"""
    key = config.resolve_session_key("web", session_id)
    return next((r for r in session_store.list_sessions("web:") if r["key"] == key), None)


@tool(
    "voice_dispatch_task",
    "把一件重活派给后台独立会话去干:要写改代码/文件、要查多处资料、要拆成多步才能"
    "做完、或要跑命令/脚本的事都算(按工作量信号判断,不要靠猜耗时),立即返回不等它跑完;"
    "派发前系统会先检查最近派过的任务:如果发现标题与本次可能有承接关系的既有任务,"
    "会返回候选提示让你改用 voice_continue_session 续接(先续接不新开,见【派活规则】8)"
    "——是延续就改调 voice_continue_session,确实跟它没有承接关系、是全新主题时再重调"
    "本工具派发;你应该同时口头告诉用户「好,我去办,好了叫你」"
    "这类话。title:6 字以内短名(会出现在播报/任务卡片里);prompt:完整任务描述(后台会话"
    "看不到当前对话上下文,必须把要做的事说完整);cwd:任务要在哪个项目目录下干活——"
    "涉及改代码/改仓库文件/查项目代码的任务【必须】传该项目根目录的绝对路径,"
    "是 git 仓库会自动开独立 worktree+分支,绝不会动主目录;不传则默认落到"
    "vococo 自己的仓库(同样走 worktree 隔离)。model:用户明确指定要用哪个"
    f"模型跑这个任务时才传(如「用 opus 跑」),可选值:{_MODEL_EXAMPLES};"
    "没听到用户点名要哪个模型就不要传,默认跟当前对话同一个模型。",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "prompt": {"type": "string"},
            "cwd": {"type": "string"},
            "model": {"type": "string"},
        },
        "required": ["title", "prompt"],
    },
)
async def voice_dispatch_task(args: dict) -> dict:
    title = (args.get("title") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    # 不传 cwd 就默认本项目根:2026-07-12 事故里模型从没传过 cwd,worktree 隔离
    # 形同虚设,子代理直接在 serve 的主检出目录上改代码拉分支——代码层兜底,
    # 让"忘了传"也走 worktree,而不是落到主目录。
    cwd = (args.get("cwd") or "").strip() or str(config.ROOT_DIR)
    model = (args.get("model") or "").strip()
    if not (title and prompt):
        return _ok("voice_dispatch_task 需要 title 和 prompt 都非空。")
    # 2026-08-04 续接优先(见 voice/prompts.py 规则8):派发前先检测可能承接的
    # 既有语音任务,命中返回候选提示、不派发——把"该不该续接"从模型自觉变成
    # 系统强制过一遍,堵住"规则只是软建议、模型直接新开"的走样。
    candidate = _find_related_candidate(title)
    if candidate is not None:
        _hinted_candidates.add(candidate["id"])
        return _ok(
            f"⚠️ 检测到可能有承接关系的既有任务:{_describe_task(candidate)}。"
            "如果这次要干的是它的延续(补充/改需求/接着做),请改用 "
            "voice_continue_session(session_id, instruction) 续接,不要新开;"
            "确实是全新主题、与它没有承接关系,再调本工具派发。"
        )
    # 捕获派发时的平台上下文:任务完成后需要知道通知该发给谁(见 notify.py)。
    # clarify.current() 由 run.py 在每轮对话开始前设置(含 adapter+chat_id);
    # 不在网关上下文里(如测试)返回 None,不阻塞任务派发。
    ctx = clarify.current()
    dispatch_platform = ctx.adapter.platform if (ctx and ctx.adapter) else None
    dispatch_chat_id = str(ctx.chat_id) if (ctx and ctx.chat_id is not None) else None
    task = task_runner.dispatch(
        title=title, prompt=prompt, cwd=cwd, model=model or None,
        dispatch_platform=dispatch_platform, dispatch_chat_id=dispatch_chat_id,
        origin="voice",
    )
    model_note = f",模型:{model}" if model else ""
    return _ok(f"已派发,session_id={task['id']},标题「{title}」{model_note},状态:{status_word(task['status'])}。")


@tool(
    "voice_list_sessions",
    "列出最近的会话——origin='task' 查后台任务(voice_dispatch_task 派的独立后台"
    "活),origin='web' 查网页端对话(浏览器打开的 Web UI 里的对话)。两者是完全"
    "不同的东西,用于先确认用户说的「刚才那个/网页那个」具体是哪一类里的哪一个,"
    "拿到 session_id 后配合 voice_query_session/voice_continue_session 使用。",
    {
        "type": "object",
        "properties": {"origin": {"type": "string", "enum": ["task", "web"]}},
        "required": ["origin"],
    },
)
async def voice_list_sessions(args: dict) -> dict:
    origin = (args.get("origin") or "").strip()
    if origin == "task":
        # 只看语音自己派发的(origin="voice")——cron 定时任务/网页发起的任务
        # 不该混进这份"我最近派了什么"的清单,会跟用户对不上号。要查某个具体的
        # 非语音任务,用 voice_query_session 给明确的 session_id。
        rows = tasks.list_recent(limit=10, origin="voice")
        if not rows:
            return _ok("当前没有任何后台任务。")
        return _ok("\n".join(_describe_task(t) for t in rows))
    if origin == "web":
        rows = session_store.list_sessions("web:")[:10]
        if not rows:
            return _ok("当前没有任何网页端对话。")
        lines = []
        for r in rows:
            flag = "⚠️最后一轮报错/卡住,等续聊" if r.get("last_error") else "正常"
            lines.append(f"session_id={_web_conv(r['key'])},标题「{r['title']}」,{flag}")
        return _ok("\n".join(lines))
    return _ok("voice_list_sessions 的 origin 只能是 'task' 或 'web'。")


@tool(
    "voice_query_session",
    "查一个会话(后台任务或网页端对话都可以,自动识别是哪一种,不用先判断)的"
    "当前状态。session_id 从 voice_list_sessions 拿;留空则查最近一次派发的"
    "后台任务。返回原始字段拼的一句话,你要把它压成更口语的转述再讲给用户,"
    "不要念「状态/进展」这类字段名。",
    {"type": "object", "properties": {"session_id": {"type": "string"}}},
)
async def voice_query_session(args: dict) -> dict:
    session_id = (args.get("session_id") or "").strip()
    if not session_id:
        # 留空 = 查"我(语音)最近派发的那个",只看 origin="voice",理由同
        # voice_list_sessions 的 origin='task' 分支。
        latest = tasks.list_recent(limit=1, origin="voice")
        if not latest:
            return _ok("没有找到任务(还从没派发过任务)。")
        return _ok(_describe_task(latest[0]))
    task = tasks.get(session_id)
    if task is not None:
        return _ok(_describe_task(task))
    web_row = _find_web_session(session_id)
    if web_row is not None:
        flag = "最后一轮报错/卡住,等续聊" if web_row.get("last_error") else "正常,空闲等下一句话"
        return _ok(f"网页对话「{web_row['title']}」(session_id={session_id}):{flag}")
    return _ok(f"没有找到 session_id={session_id} 对应的任务或网页对话。")


@tool(
    "voice_cancel_task",
    "喊停一个后台任务:排队中直接取消;运行中会打断当前这轮并标记已取消(已做完的"
    "部分不会丢,只是不再继续);已结束的任务不用停,会如实告诉你它已经结束了。"
    "session_id 从 voice_list_sessions 或 voice_query_session 拿,留空则停最近派发的"
    "那个后台任务。用户说「停掉/别跑了/取消那个任务」时用这个工具真正去停,并把"
    "停没停掉、任务现在的状态转述给用户——不要只嘴上答应。",
    {"type": "object", "properties": {"session_id": {"type": "string"}}},
)
async def voice_cancel_task(args: dict) -> dict:
    session_id = (args.get("session_id") or "").strip()
    if not session_id:
        # 留空 = 停"我(语音)最近派发的那个",只看 origin="voice",理由同
        # voice_list_sessions 的 origin='task' 分支。
        latest = tasks.list_recent(limit=1, origin="voice")
        if not latest:
            return _ok("没有找到任务(还从没派发过任务)。")
        session_id = latest[0]["id"]
    task = tasks.get(session_id)
    if task is None:
        # 不是后台任务:可能是网页对话 conv,那边有自己的 abort 机制,不归这里管。
        return _ok(f"没有找到 session_id={session_id} 对应的后台任务。")
    if task["status"] in tasks.TERMINAL_STATUSES:
        return _ok(f"任务「{task['title']}」已经结束了({status_word(task['status'])}),不用再停。")
    if task["status"] == "running":
        if not task_runner.cancel(session_id):
            cur = tasks.get(session_id)
            return _ok(f"任务「{task['title']}」显示运行中但没找到执行器,可能刚结束,"
                       f"当前状态:{status_word(cur['status']) if cur else '已不存在'}。")
        # 等 _run 的收尾落库(置 cancelled + 通知),结果才是准的,不会报"已停"结果还在跑
        await task_runner.cancel_and_wait(session_id)
        cur = tasks.get(session_id)
        status = status_word(cur["status"]) if cur else "已结束"
        return _ok(f"已停止正在运行的任务「{task['title']}」(session_id={session_id}),现在状态:{status}。")
    # queued:还没轮到它跑,直接置 cancelled 就行(不用等,没有执行器在管)
    if task_runner.cancel(session_id):
        return _ok(f"已取消排队中的任务「{task['title']}」(session_id={session_id}),还没开始跑。")
    cur = tasks.get(session_id)
    return _ok(f"取消失败,任务「{task['title']}」当前状态:"
               f"{status_word(cur['status']) if cur else '已不存在'}。")


@tool(
    "voice_continue_session",
    "给一个已有会话——后台任务或网页端对话都可以,自动识别是哪一种——续接一句"
    "指令/追加需求,原地续在同一个会话上,绝不会产生新会话。session_id 从"
    "voice_list_sessions 或 voice_query_session 拿(后台任务传它的 session_id,"
    "网页对话传它的 conv,两者格式不同但本工具自动识别,不用你先判断)。"
    "系统会自动判断怎么接:目标空闲(任务已跑完,或网页对话撞限流/报错后其实是"
    "空闲等续聊)就直接把这句话当下一轮内容;目标还在跑就先打断当前这轮,带着"
    "已完成的进度接上新指令重新跑。本工具不等新一轮跑完。",
    {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "instruction": {"type": "string"},
        },
        "required": ["session_id", "instruction"],
    },
)
async def voice_continue_session(args: dict) -> dict:
    session_id = (args.get("session_id") or "").strip()
    instruction = (args.get("instruction") or "").strip()
    if not (session_id and instruction):
        return _ok("voice_continue_session 需要 session_id 和 instruction 都非空。")

    task = tasks.get(session_id)
    if task is not None:
        result = await task_runner.append(session_id, instruction)
        if not result["ok"]:
            return _ok(f"追加失败:{result['message']}")
        parts = [f"✅ {result['message']}"]
        if result.get("task"):
            t = result["task"]
            parts.append(f"session_id={t['id']},标题「{t['title']}」,状态:{status_word(t['status'])}")
        return _ok("。".join(parts))

    # 不是后台任务的 session_id,当成网页对话 conv 处理。
    if not web_bridge.available():
        return _ok("当前不是常驻服务模式(没有开启网页入口),没法操作网页端对话。")
    if _find_web_session(session_id) is None:
        return _ok(f"没有找到 session_id={session_id} 对应的任务或网页对话。")
    key = config.resolve_session_key("web", session_id)
    interrupted = web_bridge.cancel_if_running(key)
    await web_bridge.continue_session(session_id, instruction)
    note = "(先打断了正在跑的那一轮)" if interrupted else "(原本是空闲的,直接续上)"
    return _ok(f"已经把这句话发进网页对话 session_id={session_id}{note},它会自动接着跑。")


def build_server() -> dict:
    """返回可直接塞进 stream_turn(extra_mcp_servers=...) 的 server 表。"""
    return {
        "voice_tasks": create_sdk_mcp_server(
            "voice_tasks",
            tools=[
                voice_dispatch_task, voice_list_sessions, voice_query_session,
                voice_cancel_task, voice_continue_session,
            ],
        )
    }
