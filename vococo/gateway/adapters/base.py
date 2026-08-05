"""Adapter 协议 —— 每个平台只需实现:收消息、发消息、给一个渲染用的 Sink。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol

from ... import config
from ...core.agent import AudioAttachment, ImageAttachment
from ..core import Choice, Sink


@dataclass
class Incoming:
    """归一化的入站消息。"""

    platform: str
    chat_id: int | str
    text: str
    images: list[ImageAttachment] = field(default_factory=list)
    audios: list[AudioAttachment] = field(default_factory=list)
    # 入库替代文本:系统注入的消息(如自我重启还魂)用它,让长指令不当用户话显示/存库
    store_text: str | None = None
    # server 模式:消息属于哪个租户。入站时(adapter 请求上下文里)盖章,
    # GatewayRunner._dispatch 用它注入租户上下文——消息经队列跨 task 传递,
    # ContextVar 不会自动跟过去,必须随消息本身走。personal 模式恒 "local"。
    tenant_id: str = "local"

    @property
    def session_key(self) -> str:
        # 统一开关下各入口共享同一会话(跨入口连续);否则按平台隔离
        return config.resolve_session_key(self.platform, self.chat_id)


class Adapter(Protocol):
    platform: str

    async def receive(self) -> AsyncIterator[Incoming]:
        """长连接/轮询,逐条 yield 入站消息(已做白名单过滤)。"""
        ...

    async def send(self, chat_id: int | str, text: str) -> None:
        """发一条完整消息(主动推送也走这)。"""
        ...

    def make_sink(self, chat_id: int | str) -> Sink:
        """给一轮对话造一个渲染 sink(流式更新)。"""
        ...

    async def present_choice(self, chat_id: int | str, choice: Choice) -> None:
        """渲染交互选项(各端原生:TG inline 按钮 / 飞书卡片)。选中后由该端把命令回灌 dispatch。"""
        ...
