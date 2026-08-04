"""语音模式的轻量 Adapter, 让 restart_self 工具能找到 clarify 上下文。

语音模式不走 GatewayRunner, 所以没有现成的 Adapter 实例。
这个最小实现只提供 restart_self/exit_for_restart 需要的 send() 和 platform 属性。
"""
from __future__ import annotations

import json
from aiohttp import web


class VoiceAdapter:
    """语音模式的 Adapter 最小可用子集:

    - platform="voice" → 遗书里标记, 重启后能识别
    - send(chat_id, text) → 通过当前 SSE 响应推送给前端
    - make_sink / present_choice → stub (语音不走这套)
    """

    platform = "voice"

    def __init__(self, sse_response: web.StreamResponse) -> None:
        self._resp = sse_response

    async def send(self, chat_id: int | str, text: str) -> None:
        """通过 SSE system_message 事件推送消息给前端。"""
        data = json.dumps({"text": text}, ensure_ascii=False)
        await self._resp.write(
            f"event: system_message\ndata: {data}\n\n".encode("utf-8")
        )

    def make_sink(self, chat_id: int | str) -> None:
        raise NotImplementedError("VoiceAdapter: 语音用直接 SSE, 不走 Sink")

    async def present_choice(self, chat_id: int | str, choice) -> None:
        raise NotImplementedError("VoiceAdapter: 语音暂不支持交互选项")
