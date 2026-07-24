"""Gateway 核心 —— 平台无关。

所有入口(Telegram / 飞书 / TUI)共享:
- 命令注册表(handle_command):/new /clear /model /history /status /help …
- converse():消费 agent 事件流,喂给平台各自的 Sink(渲染层)
- 会话持久化

各平台只需实现 Sink(怎么把事件渲染出去)+ 收发 I/O。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..core.agent import (
    AgentReply,
    Compacted,
    Done,
    ImageAttachment,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolInput,
    ToolStarted,
    stream_turn,
)
import time

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

    async def compacted(self, trigger: str = "") -> None:
        """CLI 自动压缩了上下文。基类只刷新;Web 子类覆盖以推专门事件。"""
        await self.render()

    async def done(self, reply: AgentReply) -> None:
        if reply.text:
            self.answer = reply.text
        await self.render()

    async def cancelled(self) -> None:
        """用户手动取消当前轮。子类可覆盖以清理状态/通知前端。"""
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

    def compacted(self, trigger: str) -> None:
        """记一个压缩标记块;前端历史重放据此显示「上下文已自动压缩」系统条。"""
        if len(self.blocks) < self.MAX_BLOCKS:
            self.blocks.append({"type": "compact", "trigger": trigger})

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
    cwd_override: str | None = None,
) -> AgentReply | None:
    """跑一轮:载入历史 → 流式 → 喂 sink → 落库。

    store_user:入库用的替代 user 文本(默认与 user_text 相同)。系统注入的消息
    (如自我重启后的还魂指令)用它把「给模型的长指令」与「存进历史给人看的简短
    标记」分开 —— 否则长指令会被当成用户发的话显示、并污染后续上下文。

    cwd_override:显式指定这一轮的工作目录,跳过按 session_key 推导 worktree/项目根
    那一套(`project_cwd_for` 认不出非项目 session_key,如语音后台任务的
    `voice-task:{id}`)。语音任务续聊要延续任务派发时的 cwd,由调用方(web.py)从
    `voice.tasks` 查出来传进来。
    """
    from .. import config  # 懒加载,与本模块其余用法一致

    from ..tools import danger  # 懒加载,避免 config↔tools 循环

    from ..core import worktree  # 懒加载

    history = session_store.load_recent(session_key)
    root = config.project_root_for(session_key)  # 主仓库路径,供审批闸认项目文件为"内部"
    if cwd_override is not None:
        cwd = cwd_override
    else:
        # 项目会话首次干活时懒创建独立 worktree(每会话一分支,物理隔离);非项目会话直接跳过
        await worktree.ensure_worktree(session_key)
        cwd = config.project_cwd_for(session_key)  # 有 worktree→用它;否则项目根;再否则 None(进程默认)
    cwd_token = danger.set_cwd(cwd, project_root=root)  # 随 contextvar 传进审批闸,使「写 cwd 外文件」规则生效
    stored_user = store_user if store_user is not None else user_text
    turn_id = session_store.start_turn(session_key, stored_user)
    # 图片落盘 + 文件名入库:让刷新页面后历史里仍能显示这些图(仅落盘,不影响喂模型的 in-memory 版本)
    if images:
        session_store.save_turn_images(turn_id, images)
    # 上一轮的 SDK 会话 id:非空则本轮用 resume 让 SDK 重放真·多轮历史,不再拼历史大文本
    resume_sid = session_store.get_sdk_session_id(session_key)
    reply: AgentReply | None = None
    timeline = _Timeline()  # 录过程时间线,轮末随正文落库(刷新可重建工具卡)
    # 流式进行中:节流把当前全文写进 turns.draft_text,供前端刷新后兜底恢复
    _draft_full = ""       # 当前轮已输出的所有正文(每段全文覆盖)
    _draft_last_ts = 0.0   # 上次 flush 的时刻(0=还没写过)
    _err_msg = ""           # 流式期间抛出的异常消息
    try:
        async for ev in stream_turn(
            history, user_text, model=model, images=images, cwd=cwd, resume=resume_sid,
            session_key=session_key,  # 传给保温池:同会话下一轮复用活 client,零冷启动
        ):
            if isinstance(ev, TextDelta):
                # 输出侧敏感内容过滤(安全评估 P0-2)第一层:对单个 delta 扫一遍。
                # 已知密钥字面值通常是一个不含空格的连续 token,一次 delta 里出现
                # 的概率很高,这一层基本能兜住"自己的密钥被读出来又说出去"。
                # 多行的 PEM 私钥块大概率会被流式拆成好几个 delta、这里扫不全,
                # 靠下面 reply.text 落库前的第二层兜底(那时全文已完整)。
                delta_text = danger.redact_secrets(ev.text)
                timeline.text(delta_text)
                await sink.text(delta_text)
                # 节流:每 ~0.7s 把【当前累计全文】刷进 draft_text,供刷新兜底。
                # ev.text 是 token 增量 → 必须累加;直接覆盖会让 draft 只剩最后一个 token。
                _draft_full += delta_text
                now = time.monotonic()
                if now - _draft_last_ts > 0.7:
                    session_store.flush_draft(turn_id, _draft_full)
                    _draft_last_ts = now
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
            elif isinstance(ev, Compacted):
                timeline.compacted(ev.trigger)
                await sink.compacted(ev.trigger)
            elif isinstance(ev, Done):
                reply = ev.reply
    except asyncio.CancelledError:
        # 用户手动取消(/abort → scope.cancel())。CancelledError 继承 BaseException,
        # 上面的 except Exception 抓不到,若不在此拦下,后面的落库逻辑一行都不会跑 →
        # 这一轮的 turn 行永远停在 assistant_text='',被 load_recent 跳过,用户的提问
        # 凭空消失,下一轮完全没上下文(第一轮被取消时=彻底失忆)。
        # 这里把「提问 + 已产出的部分回复」补落库,再原样抛出让 CancelScope 正常收尾。
        partial = (_draft_full or "").strip()
        session_store.finish_turn(
            turn_id, partial or "(上一条回复已被取消)", events=timeline.blocks
        )
        # 通常取消发生在拿到 ResultMessage 前(reply 为 None、无 sdk_session_id);
        # 万一已拿到就存回,让下一轮仍能 resume 接上真·多轮历史。
        if reply is not None and reply.sdk_session_id:
            session_store.set_sdk_session_id(session_key, reply.sdk_session_id)
        # 通知渲染层本轮已被人为取消(Web 端可据此清理进行中快照,防止刷新后重放旧内容)
        await sink.cancelled()
        raise  # 原样抛出;cwd 由 finally 统一 reset
    except Exception as exc:
        _err_msg = str(exc)
    finally:
        danger.reset_cwd(cwd_token)
    # 流式异常(SDK/进程层面,连 ResultMessage 都没拿到)→ 构造一条错误回复,
    # 确保 sink.done() 始终能发(前端不会空泡)
    if reply is None and _err_msg:
        from ..core.agent import AgentReply as _AR  # 懒加载,避免循环引用
        from ..core.agent import describe_llm_error

        reply = _AR(
            text=describe_llm_error(None, _err_msg), tool_calls=[], cost_usd=None,
            is_error=True, error=_err_msg,
        )
    elif reply is not None and reply.is_error:
        # ResultMessage 报了模型层报错(如 429/529/撞 max_turns)。之前只在 reply.text
        # 完全空的时候才补提示——但撞 max_turns 时往往已经流出一大段正文,text 非空,
        # 导致这段说明被跳过,用户看到的是"说到一半突然没了",既不知道发生了什么,
        # 也看不到已经做到哪一步。改成:有正文就接在后面附加说明,而不是覆盖掉。
        from ..core.agent import describe_llm_error

        note = describe_llm_error(reply.api_error_status, reply.error)
        reply.text = f"{reply.text.rstrip()}\n\n{note}" if reply.text.strip() else note
    if reply is not None:
        # 输出侧敏感内容过滤(安全评估 P0-2)第二层:此时全文已经完整,能兜住
        # 上面逐 delta 扫描漏掉的、跨多个 delta 拼出来的多行私钥块。落库/sink.done
        # 用的都是这之后的 reply.text,保证「最终定格」的版本(历史记录 + TG 编辑
        # 后的最终消息)是干净的,即便直播过程中曾有一瞬间的原始分片。
        reply.text = danger.redact_secrets(reply.text)
        # 最后一刷:确保 refresh 前最后 0.7s 内输出的内容也进 draft
        if _draft_full:
            session_store.flush_draft(turn_id, _draft_full)
        session_store.finish_turn(turn_id, reply.text, events=timeline.blocks)
        # 存回本轮 SDK 会话 id,下一轮 resume 它接上真·多轮历史(每轮覆盖,链不断)
        if reply.sdk_session_id:
            session_store.set_sdk_session_id(session_key, reply.sdk_session_id)
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
    ("model", "查看或切换模型,如 /model claude-opus-5"),
    ("history", "看最近历史"),
    ("status", "会话信息"),
    ("suggest", "看/接受 Wazir 提的自动化建议"),
    ("help", "显示帮助"),
]
HELP_TEXT = "可用命令:\n" + "\n".join(f"/{n} — {d}" for n, d in COMMAND_LIST)

# 可选模型(/model 无参时弹这些供选择)。标签只留"名字（订阅）"——官方模型统一走订阅,
# 不加营销文案(如"最强/更均衡"),免得干扰用户判断。
MODEL_CHOICES: list[tuple[str, str]] = [
    ("claude-fable-5", "Fable 5（订阅）"),
    ("claude-opus-5", "Opus 5（订阅）"),
    ("claude-opus-4-6", "Opus 4.6（订阅）"),
    ("claude-sonnet-5", "Sonnet 5（订阅）"),
    ("claude-sonnet-4-6", "Sonnet 4.6（订阅）"),
    ("claude-haiku-4-5", "Haiku 4.5（订阅）"),
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
            return CommandOutcome(new_model=arg)
        # 候选 = 官方默认档 + cc-switch 里配好的 DeepSeek/Kimi 等供应商模型
        choices = providers.available_models(MODEL_CHOICES)
        opts = [
            (f"/model {v}", f"{label}{' ✓ 当前' if v == current_model else ''}")
            for v, label, _group in choices
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
    if cmd[1:] in _enabled_skill_names():
        # 不是系统命令,是「/skill 名」调用:不拦截、不回复,原样交给 agent。
        # SDK 的 claude_code 原生 system prompt 自带"看到 /xxx 就当 skill 调用"的指令,
        # 交给 agent 后它会自己识别并调 Skill 工具。
        return CommandOutcome(handled=False)
    return CommandOutcome(handled=False, reply=f"未知命令 {cmd} · /help 看命令")


def _enabled_skill_names() -> set[str]:
    """当前对 agent 可见(已启用)的 skill 名集合,小写。用于放行 "/skill名" 穿透到 agent。"""
    from . import settings_store

    return {s["name"].lower() for s in settings_store.list_skills() if s["enabled"]}


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
        # 展示【到点会真正跑的完整 prompt】,让「接受」是知情同意,而不是只看一个好听的标题——
        # 否则被劫持的 agent 能用人畜无害的 title 藏一条外传后门 prompt(审计口子 A / 2-2)。
        job_prompt = ((s.get("job_spec") or {}).get("prompt") or "").strip()
        if job_prompt:
            lines.append(f"   ↳ 到点会执行:{job_prompt}")
        opts.append((f"/suggest accept {s['id']}", f"✅ 接受: {s['title']}"))
        opts.append((f"/suggest dismiss {s['id']}", f"🗑 忽略: {s['title']}"))
    return CommandOutcome(choice=Choice(prompt="\n".join(lines), options=opts))
