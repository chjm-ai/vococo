"""GatewayRunner —— 一个常驻进程跑所有 adapter 收发 + 调度器。

- 每个 adapter 的 receive() 入站消息 → _dispatch(命令 or converse)
- 主动推送(cron)经 push(platform, chat_id) 走对应 adapter.send
- 这是 launchd 常驻的入口(claude-hermes serve)
"""
from __future__ import annotations

import anyio

from .. import config
from ..cron.scheduler import run_scheduler
from ..memory import session_store
from ..tools import selfops
from . import clarify, core
from .adapters.base import Adapter, Incoming


class GatewayRunner:
    def __init__(self, adapters: list[Adapter]):
        self.adapters: dict[str, Adapter] = {a.platform: a for a in adapters}
        self.models: dict[str, str] = {}  # 每会话模型覆盖(/model 切换)
        self._locks: dict[str, anyio.Lock] = {}  # 每会话一把锁:同会话串行
        self._tg: anyio.abc.TaskGroup | None = None  # nursery,用于并发派发

    async def push(self, platform: str, chat_id, text: str) -> None:
        adapter = self.adapters.get(platform)
        if adapter is not None:
            await adapter.send(chat_id, text)

    def _lock_for(self, key: str) -> anyio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = anyio.Lock()
            self._locks[key] = lock
        return lock

    async def _dispatch(self, adapter: Adapter, inc: Incoming) -> None:
        # clarify 回复必须在【拿锁前】拦截:发起 ask_user 的那一轮还占着会话锁、
        # 阻塞等回答,若这里再去抢同一把锁就死锁。
        if await self._try_clarify(adapter, inc):
            return
        # 同一会话排队串行(防并发写库/串消息),不同会话并发互不阻塞。
        # 整体 try 兜底:start_soon 的子任务若抛异常会炸掉 nursery。
        async with self._lock_for(inc.session_key):
            try:
                await self._handle(adapter, inc)
            except Exception as e:
                try:
                    await adapter.send(inc.chat_id, f"⚠️ 出错了:{e}")
                except Exception:
                    pass
        # 自我重启安排在【本轮完整结束、历史已落库】之后:不杀半条消息,
        # 也不自己 spawn 新进程 —— 退出即可,拉起交给 run.sh 守护循环(单实例)
        # 消费 pop 以确保只退出一次(即使有多条后续消息进来也不重复)
        if selfops.pop_restart_pending(inc.session_key) is not None:
            await selfops.exit_for_restart(adapter, inc.chat_id)

    async def _try_clarify(self, adapter: Adapter, inc: Incoming) -> bool:
        """把入站消息当作对某个待答 clarify 的回答来消费;消费了返回 True。"""
        text = (inc.text or "").strip()
        key = inc.session_key
        if text.startswith("/clarify "):
            parts = text.split()
            if len(parts) >= 3:
                cid, tok = parts[1], parts[2]
                if tok == "other":
                    clarify.mark_awaiting_text(cid)
                    await adapter.send(inc.chat_id, "好,直接打字回答就行。")
                elif not clarify.resolve_button(cid, tok):
                    await adapter.send(inc.chat_id, "(这个选择已过期)")
            return True
        if not text.startswith("/") and clarify.has_pending(key):
            clarify.resolve_text_for_session(key, text)
            return True
        return False

    async def _handle(self, adapter: Adapter, inc: Incoming) -> None:
        key = inc.session_key
        # 优先内存里本次会话的切换,其次库里持久化的选定(重启后仍在),最后默认
        model = self.models.get(key) or session_store.get_chosen_model(key) or config.MODEL
        if core.is_command(inc.text):
            outcome = core.handle_command(inc.text, key, model)
            if outcome.new_model:
                self.models[key] = outcome.new_model
            if outcome.choice is not None:
                await adapter.present_choice(inc.chat_id, outcome.choice)
            elif outcome.reply:
                await adapter.send(inc.chat_id, outcome.reply)
            return
        # 设置本轮路由上下文(供 ask_user 工具反问时找到该发给谁),随 contextvar 传入工具
        token = clarify.set_current(key, adapter, inc.chat_id)
        try:
            with anyio.fail_after(config.AGENT_TURN_TIMEOUT):  # 单轮硬超时(含等 clarify)
                await core.converse(
                    key, inc.text, model, adapter.make_sink(inc.chat_id),
                    images=inc.images, store_user=inc.store_text,
                )
        except TimeoutError:
            pass  # 超时静默处理,不向用户发送错误消息
        finally:
            clarify.reset_current(token)
            clarify.clear_session(key)  # 轮结束,取消任何还挂着的 clarify

    async def _serve(self, adapter: Adapter) -> None:
        # 监督:adapter 整体挂了也只重启它自己,不连累调度器/其它 adapter
        while True:
            try:
                async for inc in adapter.receive():
                    # 并发派发:慢会话不再卡住同平台其它会话(同会话串行仍由 per-session 锁保证)
                    if self._tg is not None:
                        self._tg.start_soon(self._dispatch, adapter, inc)
                    else:
                        await self._dispatch(adapter, inc)
            except Exception as e:
                print(f"[gateway] adapter {adapter.platform} 异常,5s 后重启: {e}")
                await anyio.sleep(5)

    async def _resume_after_restart(self) -> None:
        """自我重启后的「还魂」:读遗书(读完即删,至多一次)→ 回原对话注入验证指令。

        agent 改完自身代码用 restart_self 重启后,新进程从这里接上:
        往原对话派发一条系统消息,走和真实用户消息完全相同的 _dispatch 流水线
        (锁/clarify 上下文/流式 sink/历史落库全都一致)。
        """
        task = selfops.consume_resume()
        rolled_back = selfops.consume_rollback_flag()  # 无遗书也消费,清掉陈旧标记
        if task is None:
            return
        await anyio.sleep(3)  # 等 adapter 起好(web 端口绑定 / TG 轮询就绪)
        adapter = self.adapters.get(str(task.get("platform") or ""))
        if adapter is None:
            print(f"[selfops] 还魂失败:入口 {task.get('platform')} 未启用,验证计划已丢弃")
            return
        inc = Incoming(
            platform=task["platform"],
            chat_id=task["chat_id"],
            text=selfops.build_resume_prompt(task, rolled_back),      # 给模型的完整指令
            store_text=selfops.build_resume_store_text(task, rolled_back),  # 入库/显示的简短系统条
        )
        await self._dispatch(adapter, inc)

    async def run(self) -> None:
        clarify.register_push(self.push)  # 让 send_message 等工具能主动发消息
        async with anyio.create_task_group() as tg:
            self._tg = tg
            for adapter in self.adapters.values():
                tg.start_soon(self._serve, adapter)
            tg.start_soon(run_scheduler, self.push)
            tg.start_soon(self._resume_after_restart)


async def run_serve() -> None:
    """组装并启动 gateway(Telegram + Web + 调度器,按配置挂载)。"""
    from .adapters.telegram import TelegramAdapter

    adapters: list[Adapter] = []
    if config.TELEGRAM_BOT_TOKEN:
        adapters.append(TelegramAdapter())
    if config.WEB_ENABLED:
        from .adapters.web import WebAdapter

        adapters.append(WebAdapter())
    if not adapters:
        print("⚠️  没启用任何入口(Telegram/Web),本次只跑调度器。")

    await GatewayRunner(adapters).run()
