"""Hermes 原生工具 —— 接通"记忆"这条灵魂线。

两个工具,挂成一个 SDK MCP server(名字 hermes),全入口共用:

- recall_past:检索跨会话的历史对话(agent 自己读不到 SQLite,必须给工具)。
- save_memory:把一条值得长期记的【新主题】记忆写入 ~/AI_BRAIN/memory/<topic>.md,
  并在 MEMORY.md 末尾登记索引。只负责"新建独立主题文件"这种安全情形;
  要往已有分类文件(lessons/preferences/tech-decisions)追加,交给 agent 用文件
  编辑工具按其现有格式做 —— 避免硬编码格式覆坏既有 Obsidian 记忆。

工具暴露名为 mcp__hermes__recall_past / mcp__hermes__save_memory。
"""
from __future__ import annotations

import datetime
import functools
import re

import anyio
from claude_agent_sdk import create_sdk_mcp_server, tool

from .. import config
from ..cron import suggestions
from ..memory import session_store

_TOPIC_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_DEFAULT_CATEGORY = "其他主题"
_SUMMARY_MAX = 120  # summary 会写进索引,须一句话;详细内容放 body


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "recall_past",
    "检索跨会话的历史对话记录(当前会话之外、可能已被 /new 归档的旧对话)。"
    "当用户提到「上次/之前我们聊过/我记得说过」之类、而当前上下文里找不到时,用它召回。"
    "传入关键词(中文友好,子串匹配)。",
    {"query": str},
)
async def recall_past(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return _ok("recall_past 需要一个非空的检索关键词。")
    rows = session_store.search(query, limit=8)
    if not rows:
        return _ok(f"没有找到与「{query}」相关的历史对话。")
    parts = []
    for session_key, user_text, assistant_text in rows:
        block = f"[来源会话 {session_key}]\n我:{user_text}"
        if assistant_text:
            block += f"\n你:{assistant_text}"
        parts.append(block)
    return _ok(f"与「{query}」相关的历史片段(最多 8 条):\n\n" + "\n\n---\n\n".join(parts))


@tool(
    "save_memory",
    "把一条值得【长期记住】的新主题记忆写进 Wesley 的 AI_BRAIN(~/AI_BRAIN/memory/<topic>.md),"
    "并自动登记到 MEMORY.md 索引。仅用于【新建独立主题文件】(如某服务器/工具的关键路径与踩坑)。"
    "若该 topic 文件已存在,本工具会拒绝(不覆盖);要往已有记忆或 lessons/preferences/"
    "tech-decisions 等分类文件追加,请改用 Read+Edit 文件工具按其现有格式追加。\n"
    "topic:英文短横线 slug(如 vpn-speeedai-server);title:中文标题;"
    "summary:一句话摘要(也会写进索引);body:markdown 正文;"
    "category:索引分节名(不带 ##,如「服务器 / 基础设施」「工作偏好 / 设置」),"
    "登记到该分节;省略则进「其他主题」。",
    {
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "body": {"type": "string"},
            "category": {"type": "string"},
        },
        "required": ["topic", "title", "summary", "body"],
    },
)
async def save_memory(args: dict) -> dict:
    topic = (args.get("topic") or "").strip()
    title = (args.get("title") or "").strip()
    summary = (args.get("summary") or "").strip()
    body = (args.get("body") or "").strip()
    category = (args.get("category") or "").strip() or _DEFAULT_CATEGORY

    if not (topic and title and summary and body):
        return _ok("save_memory 需要 topic / title / summary / body 四项都非空。")
    if not _TOPIC_RE.match(topic):
        return _ok(f"topic「{topic}」非法:只允许字母、数字、下划线、短横线(防路径穿越)。")
    if len(summary) > _SUMMARY_MAX:
        return _ok(
            f"summary 太长了({len(summary)} 字)。它会写进索引,请压到一句话"
            f"(≤{_SUMMARY_MAX} 字),详细内容放进 body。"
        )

    mem_dir = config.AI_BRAIN_DIR / "memory"
    path = mem_dir / f"{topic}.md"
    if path.exists():
        return _ok(
            f"⚠️ memory/{topic}.md 已存在,未改动。要追加内容请用 Read+Edit 打开它,"
            "按其现有格式追加,避免覆盖。"
        )

    today = datetime.date.today().isoformat()
    content = f"---\ncreated: {today}\n---\n# {title}\n\n> {summary}\n\n{body}\n"
    mem_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    _append_index(topic, summary, category)
    return _ok(f"✅ 已写入 memory/{topic}.md 并登记到索引「{category}」。")


def _append_index(topic: str, summary: str, category: str) -> None:
    """把一行索引登记到 MEMORY.md 的「## <category>」分节末尾。

    分节存在 → 追加到该节最后一条之后;不存在 → 在文件末尾新建该分节;
    索引文件不存在 → 新建。
    """
    index = config.AI_BRAIN_DIR / "MEMORY.md"
    header = f"## {category}"
    line = f"→ memory/{topic}.md — {summary}"
    try:
        text = index.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        index.write_text(f"# 记忆索引\n\n{header}\n{line}\n", encoding="utf-8")
        return

    lines = text.splitlines()
    try:
        hi = next(i for i, ln in enumerate(lines) if ln.strip() == header)
    except StopIteration:
        # 没有该分节 → 末尾新建
        body = "\n".join(lines).rstrip("\n")
        index.write_text(f"{body}\n\n{header}\n{line}\n", encoding="utf-8")
        return

    # 找该分节的结束(下一个 ## 标题处),把新行插在分节末尾
    end = next(
        (i for i in range(hi + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    insert_at = end
    while insert_at > hi + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1  # 跳过分节末尾空行,插在最后一条之后
    lines.insert(insert_at, line)
    index.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


@tool(
    "suggest_automation",
    "给 Wesley 提一条【定时自动化建议】(不会自动开跑,等他用 /建议 一键接受)。"
    "当你发现他反复问/做同一件事、适合排成定时任务时用。\n"
    "title:简短名;description:一句话说明做什么;cron:5 段 cron 表达式"
    "(如 '0 8 * * *' = 每天早 8 点);prompt:到点时你要执行的任务指令;"
    "dedup_key:去重键(同键被忽略后不再提,省略则用 title)。",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "cron": {"type": "string"},
            "prompt": {"type": "string"},
            "dedup_key": {"type": "string"},
        },
        "required": ["title", "description", "cron", "prompt"],
    },
)
async def suggest_automation(args: dict) -> dict:
    title = (args.get("title") or "").strip()
    description = (args.get("description") or "").strip()
    cron = (args.get("cron") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    dedup_key = (args.get("dedup_key") or "").strip() or f"usage:{title}"
    if not (title and cron and prompt):
        return _ok("suggest_automation 需要 title / cron / prompt 都非空。")
    try:
        from croniter import croniter

        croniter(cron)
    except Exception:
        return _ok(f"cron 表达式「{cron}」不合法(要 5 段,如 '0 8 * * *')。")
    rec = suggestions.add_suggestion(
        title=title,
        description=description,
        source="usage",
        job_spec={
            "name": title,
            "prompt": prompt,
            "schedule": {"kind": "cron", "expr": cron},
        },
        dedup_key=dedup_key,
    )
    if rec is None:
        return _ok(f"「{title}」这类建议已提过或建议已满,跳过(不重复打扰)。")
    return _ok(f"✅ 已提建议「{title}」。用 /建议 查看并一键接受(接受后才会真正定时跑)。")


# === cron 任务管理(查看/停用/删除;创建仍走建议 consent-first)===
def _sched_desc(sch: dict) -> str:
    kind = sch.get("kind")
    if kind == "cron":
        return sch.get("expr", "?")
    if kind == "interval":
        return f"每{sch.get('minutes', 60)}分钟"
    if kind == "once":
        return "一次性"
    return kind or "?"


def _resolve_job(ref: str, jobs: list[dict]) -> dict | None:
    """按 id / 1-based 序号 / 名字(不分大小写)解析一个任务。"""
    ref = ref.strip()
    for j in jobs:
        if j.get("id") == ref:
            return j
    if ref.isdigit():
        i = int(ref) - 1
        if 0 <= i < len(jobs):
            return jobs[i]
    for j in jobs:
        if j.get("name", "").lower() == ref.lower():
            return j
    return None


@tool(
    "list_cron_jobs",
    "列出当前所有定时任务(序号/id/名字/计划/是否启用/上次状态)。"
    "用户问「有哪些定时任务/自动化在跑」时用。",
    {"type": "object", "properties": {}},
)
async def list_cron_jobs(args: dict) -> dict:
    from ..cron import scheduler

    jobs = scheduler.load_jobs()
    if not jobs:
        return _ok("当前没有定时任务。(建议用 /建议 或 suggest_automation 提议后接受)")
    lines = []
    for i, j in enumerate(jobs, 1):
        state = "✅启用" if j.get("enabled") else "⏸停用"
        lines.append(
            f"{i}. [{j.get('id')}] {j.get('name')} — {_sched_desc(j.get('schedule', {}))}"
            f" — {state} — 上次:{j.get('last_status') or '未跑'}"
        )
    return _ok("定时任务:\n" + "\n".join(lines))


@tool(
    "set_cron_job_enabled",
    "启用或停用一个定时任务(按 序号/id/名字)。用户说「把 X 关掉/开启」时用。",
    {
        "type": "object",
        "properties": {"ref": {"type": "string"}, "enabled": {"type": "boolean"}},
        "required": ["ref", "enabled"],
    },
)
async def set_cron_job_enabled(args: dict) -> dict:
    from ..cron import scheduler

    ref = (args.get("ref") or "").strip()
    enabled = bool(args.get("enabled"))
    jobs = scheduler.load_jobs()
    j = _resolve_job(ref, jobs)
    if not j:
        return _ok(f"没找到任务「{ref}」。用 list_cron_jobs 看列表。")
    j["enabled"] = enabled
    if not enabled:
        j["next_run_at"] = None
    scheduler.save_jobs(jobs)
    return _ok(f"{'✅ 已启用' if enabled else '⏸ 已停用'}任务「{j.get('name')}」。")


@tool(
    "delete_cron_job",
    "删除一个定时任务(按 序号/id/名字)。不可恢复,删前最好先向用户确认。",
    {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]},
)
async def delete_cron_job(args: dict) -> dict:
    from ..cron import scheduler

    ref = (args.get("ref") or "").strip()
    jobs = scheduler.load_jobs()
    j = _resolve_job(ref, jobs)
    if not j:
        return _ok(f"没找到任务「{ref}」。用 list_cron_jobs 看列表。")
    jobs.remove(j)
    scheduler.save_jobs(jobs)
    return _ok(f"🗑 已删除任务「{j.get('name')}」。")


@tool(
    "ask_user",
    "需要用户拍板/补充信息、你不该瞎猜时,用它问一个问题并【阻塞等回答】再继续本轮。"
    "question:问题;options:可选的选项列表(给了就渲染成按钮,没给就开放式等用户打字)。"
    "仅在 TG/Web 等消息渠道生效;拿到答案(字符串)后接着往下做。",
    {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["question"],
    },
)
async def ask_user(args: dict) -> dict:
    from ..gateway import clarify

    question = (args.get("question") or "").strip()
    options = [str(o).strip() for o in (args.get("options") or []) if str(o).strip()]
    if not question:
        return _ok("ask_user 需要一个非空 question。")
    ctx = clarify.current()
    if ctx is None:
        return _ok("(当前环境不支持交互提问,请直接在回复正文里问用户。)")

    p = clarify.register(ctx.session_key, options)
    try:
        if options:
            from ..gateway.core import Choice

            opts = [(f"/clarify {p.clarify_id} {i}", o) for i, o in enumerate(options)]
            opts.append((f"/clarify {p.clarify_id} other", "✍️ 其他(直接打字回答)"))
            await ctx.adapter.present_choice(
                ctx.chat_id, Choice(prompt=f"❓ {question}", options=opts)
            )
        else:
            await ctx.adapter.send(ctx.chat_id, f"❓ {question}\n(直接回复即可)")
    except Exception as e:
        clarify.resolve(p.clarify_id, "")
        return _ok(f"(提问没能发出去:{e};请直接在正文里问。)")

    answer = await clarify.wait(p.clarify_id, config.CLARIFY_TIMEOUT)
    if not answer:
        return _ok("(用户未在时限内回答;请基于已有信息继续,或稍后再问。)")
    return _ok(f"用户回答:{answer}")


@tool(
    "send_message",
    "主动给用户发一条【独立消息】(不是本轮回复正文)。用于:单独发长内容、发进度提醒、"
    "或从后台任务 ping 用户。to:'current'(默认,当前聊天)或 'platform:chat_id'(如 telegram:123)。",
    {
        "type": "object",
        "properties": {"text": {"type": "string"}, "to": {"type": "string"}},
        "required": ["text"],
    },
)
async def send_message(args: dict) -> dict:
    from ..gateway import clarify

    text = (args.get("text") or "").strip()
    to = (args.get("to") or "current").strip() or "current"
    if not text:
        return _ok("send_message 需要非空 text。")
    if to == "current":
        ctx = clarify.current()
        if ctx is None:
            return _ok("(当前无聊天上下文,无法主动发;请直接在正文回复。)")
        await ctx.adapter.send(ctx.chat_id, text)
        return _ok("已发送到当前聊天。")
    if ":" not in to:
        return _ok("to 应为 'current' 或 'platform:chat_id'(如 telegram:123)。")
    platform, _, cid = to.partition(":")
    cid = cid.strip()
    target = int(cid) if cid.lstrip("-").isdigit() else cid
    ok = await clarify.push(platform.strip(), target, text)
    return _ok(f"已发送到 {to}。" if ok else "发送失败(网关未就绪或平台不存在)。")


@tool(
    "restart_self",
    "修改完 claude-hermes【自身代码】后,重启进程加载新代码,重启完自动回到当前对话"
    "继续执行你的验证计划(对话历史在 SQLite 里,不会丢)。调用前必须:"
    "1)已把代码改动 git commit(该 commit 即回滚锚点,工作区脏会被拒);"
    "2)想好 verify_plan —— 重启后你会收到一条系统消息,照着它验证并把结果告诉用户。"
    "重启发生在【本轮回复完整结束之后】,所以调用本工具后直接在正文告诉用户改了什么即可,"
    "不要等待。预检失败/15分钟内重启超过3次会被拒绝(进程不会退出)。\n"
    "reason:为什么改代码(一句话);verify_plan:重启后的验证步骤(具体到命令/接口);"
    "allow_dirty:true 才允许带未提交改动重启(回滚会不可靠,慎用)。",
    {
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
            "verify_plan": {"type": "string"},
            "allow_dirty": {"type": "boolean"},
        },
        "required": ["reason", "verify_plan"],
    },
)
async def restart_self(args: dict) -> dict:
    from ..gateway import clarify
    from . import selfops

    reason = (args.get("reason") or "").strip()
    verify_plan = (args.get("verify_plan") or "").strip()
    if not (reason and verify_plan):
        return _ok("restart_self 需要 reason 和 verify_plan 都非空。")
    ctx = clarify.current()
    if ctx is None:
        return _ok("(当前无聊天上下文(CLI/定时任务?),重启后无处回归对话,已取消。)")
    platform = getattr(ctx.adapter, "platform", "")
    if not platform:
        return _ok("(拿不到当前入口平台,无法安排重启后还魂,已取消。)")
    # 预检含 compileall/git 子进程,扔线程池免得卡住事件循环里别的会话
    msg = await anyio.to_thread.run_sync(
        functools.partial(
            selfops.request_restart,
            platform=platform,
            chat_id=ctx.chat_id,
            session_key=ctx.session_key,
            reason=reason,
            verify_plan=verify_plan,
            allow_dirty=bool(args.get("allow_dirty")),
        )
    )
    return _ok(msg)


def build_mcp_servers() -> dict:
    """返回挂给 ClaudeAgentOptions.mcp_servers 的 server 表。"""
    return {
        "hermes": create_sdk_mcp_server(
            "hermes",
            tools=[
                recall_past,
                save_memory,
                suggest_automation,
                list_cron_jobs,
                set_cron_job_enabled,
                delete_cron_job,
                ask_user,
                send_message,
                restart_self,
            ],
        )
    }
