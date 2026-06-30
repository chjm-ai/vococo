"""Adapter 协议 —— 每个平台只需实现:收消息、发消息、给一个渲染用的 Sink。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol

from ..core import Choice, Sink


@dataclass
class Incoming:
    """归一化的入站消息。"""

    platform: str
    chat_id: int | str
    text: str

    @property
    def session_key(self) -> str:
        return f"{self.platform}:{self.chat_id}"


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
