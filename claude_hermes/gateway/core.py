"""Gateway 核心 —— 平台无关。

所有入口(Telegram / 飞书 / TUI)共享:
- 命令注册表(handle_command):/new /clear /model /history /status /help …
- converse():消费 agent 事件流,喂给平台各自的 Sink(渲染层)
- 会话持久化

各平台只需实现 Sink(怎么把事件渲染出去)+ 收发 I/O。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.agent import (
    AgentReply,
    Done,
    ImageAttachment,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolInput,
    ToolStarted,
    stream_turn,
)
from ..memory import session_store
from .. import providers


class Sink:
    """渲染层接口 + 状态聚合(平台无关)。

    基类把事件流聚合成可显示状态:思考缓冲、工具进度、正文增量,
    并生成单行状态串(💭 思考中 / 🔧 Read✓ · Bash⏳)。
    子类通常只需实现 render()(把当前状态显示出去),
    各端(TG editMessageText / TUI rich.Live / 将来飞书卡片)复用同一套聚合。
    """

    def __init__(self) -> None:
        self.thinking_buf = ""
        self.answer = ""
        # 每项 {name, id, done, ok, preview, sub_calls};done=False 即进行中
        self.tools: list[dict] = []

    # --- 事件入口(子类一般不覆盖,只覆盖 render)---
    async def thinking(self, text: str) -> None:
        self.thinking_buf += text
        await self.render()

    async def text(self, text: str) -> None:
        self.answer += text
        await self.render()

    async def tool_started(
        self, name: str, tool_id: str = "", parent_id: str | None = None
    ) -> None:
        if parent_id:
            # 子代理内部的工具:计入所属 Task 项的步数,不单独占一行
            for t in self.tools:
                if t.get("id") == parent_id:
                    t["sub_calls"] = t.get("sub_calls", 0) + 1
                    break
            await self.render()
            return
        self.tools.append(
            {
                "name": name,
                "id": tool_id,
                "done": False,
                "ok": True,
                "preview": "",
                "input": None,
                "sub_calls": 0,
            }
        )
        await self.render()

    async def tool_input(
        self,
        name: str,
        tool_id: str,
        tool_input: dict,
        parent_id: str | None = None,
    ) -> None:
        """收到某工具的完整入参 → 挂到对应工具项上(优先按 id 配对)。

        基类默认只做聚合(供 TUI/TG 复用状态);富渲染(diff/todo/计划卡)由
        Web Sink 覆盖本方法自行推事件。子代理内部工具的入参不聚合。
        """
        if parent_id:
            return
        for t in reversed(self.tools):
            if (tool_id and t.get("id") == tool_id) or (
                not tool_id and t["name"] == name and t.get("input") is None
            ):
                t["input"] = tool_input
                break
        await self.render()

    async def tool_finished(
        self,
        name: str,
        ok: bool,
        preview: str,
        tool_id: str = "",
        detail: str = "",
        parent_id: str | None = None,
    ) -> None:
        if parent_id:  # 子代理内部工具完成:基类不逐项跟踪,只触发刷新
            await self.render()
            return
        # 优先按 tool_id 配对(并行同名工具不会错标),找不到再回退按名字
        target = None
        if tool_id:
            target = next(
                (t for t in self.tools if t.get("id") == tool_id and not t["done"]),
                None,
            )
        if target is None:
            target = next(
                (t for t in self.tools if t["name"] == name and not t["done"]), None
            )
        if target is not None:
            target.update(done=True, ok=ok, preview=preview)
        else:
            self.tools.append(
                {"name": name, "id": tool_id, "done": True, "ok": ok, "preview": preview}
            )
        await self.render()

    async def done(self, reply: AgentReply) -> None:
        if reply.text:
            self.answer = reply.text
        await self.render()

    # --- 渲染(子类实现:把当前聚合状态显示出去)---
    async def render(self) -> None: ...

    # --- 状态串 helper(供子类拼装显示)---
    def tools_summary(self) -> str:
        marks = {"pending": "⏳", "ok": "✓", "err": "✗"}
        bits = []
        for t in self.tools:
            key = "pending" if not t["done"] else ("ok" if t["ok"] else "err")
            n = t.get("sub_calls") or 0
            label = f"{t['name']}({n}步)" if n else t["name"]
            bits.append(f"{label}{marks[key]}")
        return " · ".join(bits)

    def status_line(self) -> str:
        """单行状态。有工具→显示工具进度;否则正文未开始时→💭 思考中。"""
        if self.tools:
            return "🔧 " + self.tools_summary()
        if self.thinking_buf and not self.answer:
            return "💭 思考中…"
        return ""


class _Timeline:
    """把一轮事件流录成可落库的时间线:文字段与工具调用按真实顺序交错。

    结构(JSON 可序列化):
    [{"type":"text","text":...},
     {"type":"tool","name":...,"id":...,"input":{...},"ok":...,"preview":...,
      "detail":...,"subs":[{"name","ok"},...]}]
    刷新页面时 /history 带回这份时间线,前端据此原样重建工具卡与文字的交错画面。
    """

    MAX_BLOCKS = 400  # 极端长轮次的保险丝:超出只记数,不再膨胀

    def __init__(self) -> None:
        self.blocks: list[dict] = []
        self._by_id: dict[str, dict] = {}  # 顶层工具 id → block(配对 input/结果)

    def text(self, t: str) -> None:
        if self.blocks and self.blocks[-1]["type"] == "text":
            self.blocks[-1]["text"] += t
        elif len(self.blocks) < self.MAX_BLOCKS:
            self.blocks.append({"type": "text", "text": t})

    def tool_started(self, name: str, tool_id: str, parent_id: str | None) -> None:
        if parent_id:  # 子代理内部工具:挂进所属 Task 块的 subs,不占顶层块
            parent = self._by_id.get(parent_id)
            if parent is not None:
                parent.setdefault("subs", []).append({"name": name, "ok": True})
            return
        if len(self.blocks) >= self.MAX_BLOCKS:
            return
        block = {"type": "tool", "name": name, "id": tool_id, "ok": True}
        self.blocks.append(block)
        if tool_id:
            self._by_id[tool_id] = block

    def tool_input(self, tool_id: str, tool_input: dict, parent_id: str | None) -> None:
        if parent_id:
            return
        block = self._by_id.get(tool_id)
        if block is not None:
            block["input"] = tool_input

    def tool_finished(
        self, name: str, ok: bool, preview: str, tool_id: str,
        detail: str, parent_id: str | None,
    ) -> None:
        if parent_id:
            parent = self._by_id.get(parent_id)
            if parent is not None:  # 子步只标最后一个未完成的同名项
                for sub in reversed(parent.get("subs", [])):
                    if sub["name"] == name and "done" not in sub:
                        sub.update(done=True, ok=ok)
                        break
            return
        block = self._by_id.get(tool_id)
        if block is None:  # 没配上 id(如 hook 拦截):回退找最后一个同名未完成块
            block = next(
                (b for b in reversed(self.blocks)
                 if b["type"] == "tool" and b["name"] == name and "preview" not in b),
                None,
            )
        if block is not None:
            block.update(ok=ok, preview=preview, detail=detail)


async def converse(
    session_key: str,
    user_text: str,
    model: str | None,
    sink: Sink,
    images: list[ImageAttachment] | None = None,
    store_user: str | None = None,
) -> AgentReply | None:
    """跑一轮:载入历史 → 流式 → 喂 sink → 落库。

    store_user:入库用的替代 user 文本(默认与 user_text 相同)。系统注入的消息
    (如自我重启后的还魂指令)用它把「给模型的长指令」与「存进历史给人看的简短
    标记」分开 —— 否则长指令会被当成用户发的话显示、并污染后续上下文。
    """
    from .. import config  # 懒加载,与本模块其余用法一致

    from ..tools import danger  # 懒加载,避免 config↔tools 循环

    from ..core import worktree  # 懒加载

    history = session_store.load_recent(session_key)
    # 项目会话首次干活时懒创建独立 worktree(每会话一分支,物理隔离);非项目会话直接跳过
    await worktree.ensure_worktree(session_key)
    cwd = config.project_cwd_for(session_key)  # 有 worktree→用它;否则项目根;再否则 None(进程默认)
    cwd_token = danger.set_cwd(cwd)  # 随 contextvar 传进审批闸,使「写 cwd 外文件」规则生效
    stored_user = store_user if store_user is not None else user_text
    turn_id = session_store.start_turn(session_key, stored_user)
    reply: AgentReply | None = None
    timeline = _Timeline()  # 录过程时间线,轮末随正文落库(刷新可重建工具卡)
    try:
        async for ev in stream_turn(history, user_text, model=model, images=images, cwd=cwd):
            if isinstance(ev, TextDelta):
                timeline.text(ev.text)
                await sink.text(ev.text)
            elif isinstance(ev, ThinkingDelta):
                await sink.thinking(ev.text)
            elif isinstance(ev, ToolStarted):
                timeline.tool_started(ev.name, ev.tool_id, ev.parent_id)
                await sink.tool_started(ev.name, ev.tool_id, ev.parent_id)
            elif isinstance(ev, ToolInput):
                timeline.tool_input(ev.tool_id, ev.tool_input, ev.parent_id)
                await sink.tool_input(ev.name, ev.tool_id, ev.tool_input, ev.parent_id)
            elif isinstance(ev, ToolFinished):
                timeline.tool_finished(
                    ev.name, ev.ok, ev.preview, ev.tool_id, ev.detail, ev.parent_id
                )
                await sink.tool_finished(
                    ev.name, ev.ok, ev.preview, ev.tool_id, ev.detail, ev.parent_id
                )
            elif isinstance(ev, Done):
                reply = ev.reply
    finally:
        danger.reset_cwd(cwd_token)
    if reply is not None:
        session_store.finish_turn(turn_id, reply.text, events=timeline.blocks)
        if reply.context_tokens or reply.turn_tokens:
            session_store.record_usage(
                session_key,
                reply.context_tokens,
                reply.turn_tokens,
                window=reply.context_window,
                last_in=reply.input_fresh,
                last_cache=reply.cache_read,
                last_out=reply.output_tokens,
                model=reply.model,
            )
        await sink.done(reply)
    else:
        session_store.cancel_turn(turn_id)
    return reply


# === 命令注册表(单一来源:TG 菜单 setMyCommands 和 /help 都从这生成)===
COMMAND_LIST: list[tuple[str, str]] = [
    ("new", "开新会话(旧历史保留)"),
    ("clear", "清屏并开新会话"),
    ("model", "查看或切换模型,如 /model claude-opus-4-8"),
    ("history", "看最近历史"),
    ("status", "会话信息"),
    ("suggest", "看/接受 Hermes 提的自动化建议"),
    ("help", "显示帮助"),
]
HELP_TEXT = "可用命令:\n" + "\n".join(f"/{n} — {d}" for n, d in COMMAND_LIST)

# 可选模型(/model 无参时弹这些供选择)
MODEL_CHOICES: list[tuple[str, str]] = [
    ("claude-opus-4-8", "Opus 4.8 · 最强(吃周限额)"),
    ("claude-sonnet-4-6", "Sonnet 4.6 · 日常均衡"),
    ("claude-haiku-4-5", "Haiku 4.5 · 最快最省"),
]


@dataclass
class Choice:
    """通用交互选项。每个 option 是 (选中后执行的命令, 显示文字)。

    各端各自渲染(TUI 选择框 / TG inline 按钮 / 飞书卡片),选中=执行那条命令。
    """

    prompt: str
    options: list[tuple[str, str]]


@dataclass
class CommandOutcome:
    handled: bool = True
    reply: str | None = None
    exit: bool = False
    clear_screen: bool = False
    reset_history: bool = False
    new_model: str | None = None
    choice: Choice | None = None


def is_command(text: str) -> bool:
    return text.startswith("/")


def handle_command(text: str, session_key: str, current_model: str) -> CommandOutcome:
    """处理斜杠命令。平台无关的部分在这;UI 副作用(清屏等)由前端按 outcome 标志执行。"""
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.lower(), arg.strip()

    if cmd in ("/exit", "/quit"):
        return CommandOutcome(reply="再见。", exit=True)
    if cmd in ("/new", "/reset"):
        session_store.new_session(session_key)
        return CommandOutcome(reply="🆕 已开新会话(旧历史已保留)。", reset_history=True)
    if cmd == "/clear":
        session_store.new_session(session_key)
        return CommandOutcome(
            reply="🧹 已清空当前上下文(旧历史保留)。", reset_history=True, clear_screen=True
        )
    if cmd in ("/start", "/help"):
        return CommandOutcome(reply=HELP_TEXT)
    if cmd == "/model":
        if arg:
            session_store.set_chosen_model(session_key, arg)  # 持久化,刷新/重启不丢
            return CommandOutcome(reply=f"已切换模型 → {arg}", new_model=arg)
        # 候选 = 官方默认档 + cc-switch 里配好的 DeepSeek/Kimi 等供应商模型
        choices = providers.available_models(MODEL_CHOICES)
        opts = [
            (f"/model {v}", f"{label}{' ✓ 当前' if v == current_model else ''}")
            for v, label in choices
        ]
        return CommandOutcome(choice=Choice(prompt="选择模型:", options=opts))
    if cmd == "/history":
        h = session_store.load_recent(session_key, limit=10)
        if not h:
            return CommandOutcome(reply="(当前会话还没有历史)")
        lines = [f"最近 {len(h)} 轮:"]
        for t in h:
            lines.append(f"· 我:{t.user[:50]}")
        return CommandOutcome(reply="\n".join(lines))
    if cmd == "/status":
        n = len(session_store.load_recent(session_key))
        active = providers.load_active()
        prov_line = (
            f"\n供应商:{active.name}(cc-switch)" if active and not active.is_official else ""
        )
        return CommandOutcome(
            reply=f"会话:{session_key}\n模型:{current_model}{prov_line}\n本会话轮数:{n}"
        )
    if cmd in ("/suggest", "/建议"):
        return _handle_suggest(arg, session_key)
    return CommandOutcome(handled=False, reply=f"未知命令 {cmd} · /help 看命令")


def _origin_from_session_key(session_key: str) -> dict | None:
    """从会话键推导接受任务的推送目标(结果推回用户接受时所在的聊天)。"""
    if session_key.startswith("tg:"):
        try:
            return {"platform": "telegram", "chat_id": int(session_key[3:])}
        except ValueError:
            return None
    if ":" in session_key:  # 非统一模式 platform:chat_id
        platform, _, cid = session_key.partition(":")
        try:
            return {"platform": platform, "chat_id": int(cid)}
        except ValueError:
            return {"platform": platform, "chat_id": cid}
    # 统一主会话拿不到 chat_id → 回退到 REFLECT_TARGET
    from .. import config

    if ":" in config.REFLECT_TARGET:
        platform, _, cid = config.REFLECT_TARGET.partition(":")
        try:
            return {"platform": platform.strip(), "chat_id": int(cid.strip())}
        except ValueError:
            return {"platform": platform.strip(), "chat_id": cid.strip()}
    return None


def _handle_suggest(arg: str, session_key: str) -> CommandOutcome:
    """/suggest:无参列出待定建议(带接受/忽略按钮);accept/dismiss <ref> 处理单条。"""
    from ..cron import suggestions

    action, _, ref = arg.partition(" ")
    action, ref = action.strip().lower(), ref.strip()

    if action in ("accept", "接受") and ref:
        job = suggestions.accept_suggestion(ref, origin=_origin_from_session_key(session_key))
        if job is None:
            return CommandOutcome(reply="没找到这条待定建议(可能已处理)。/suggest 看列表。")
        return CommandOutcome(reply=f"✅ 已接受,建好定时任务「{job.get('name')}」。")
    if action in ("dismiss", "忽略") and ref:
        ok = suggestions.dismiss_suggestion(ref)
        return CommandOutcome(reply="🗑 已忽略,同类不再提。" if ok else "没找到这条建议。")

    pending = suggestions.list_pending()
    if not pending:
        return CommandOutcome(reply="✨ 暂无待定的自动化建议。")
    lines = ["💡 待定的自动化建议(点按钮接受/忽略):"]
    opts: list[tuple[str, str]] = []
    for i, s in enumerate(pending, 1):
        lines.append(f"{i}. {s['title']} — {s.get('description', '')}")
        opts.append((f"/suggest accept {s['id']}", f"✅ 接受: {s['title']}"))
        opts.append((f"/suggest dismiss {s['id']}", f"🗑 忽略: {s['title']}"))
    return CommandOutcome(choice=Choice(prompt="\n".join(lines), options=opts))
