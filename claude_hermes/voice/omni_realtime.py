"""Qwen-Omni-Realtime(阿里云百炼)语音进语音出会话——P3 阶段一后端骨架,2026-07-10。

跟现有 voice/ws.py 的关系:ws.py 是"客户端录音→自己判断打断/回声→丢文字给 Claude
对话→自己切句子合成 TTS→前端自己拼播放队列"这条全自建链路,2026-07-09 这条链路在
播放/回声消除环节连炸三次故障(哑巴→双音→再哑巴,见 memory
hermes-voice-echo-loopback-removed),而且 ws.py 里 looks_like_self_echo /
VOICE_SELF_ECHO_THRESHOLD 这类"靠转写文字相似度猜是不是回声"的兜底,本质上是在
软件层山寨浏览器/模型本该内置的回声消除。

本模块改用 Qwen-Omni-Realtime 的语音进语音出会话替代识别+生成+合成+VAD+打断这
一整条链路,只保留任务派发这层业务逻辑(executor.py/tasks.py/task_tools.py)——那套
已经很成熟,直接复用,不重写。

阶段一(本文件):只搭后端会话骨架 + function calling 桥接,不接入现有 /voice/ws
路由,不影响现在能用的语音功能。阶段二:前端换真 WebRTC 音频轨道(拿它内置的回声
消除/降噪,不像 ws.py 现在这样自己维护打断/回声兜底),需要真机测,单独一轮做,
见 2026-07-10 与 Wesley 的讨论。

协议细节(session.update 字段、function_call 系列事件名、function_call_output
回传格式)已用真实 DashScope 账号跑通过完整一轮 dispatch_task 调用+回传+模型接续
说话,不是照着文档猜的。
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import aiohttp

from .. import config
from ..core import prompt as core_prompt
from . import prompts, task_tools

_DASHSCOPE_REALTIME_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
# WebRTC 的 SDP 信令交换端点(阶段二用)——注意这不是上面那个全局域名,必须是
# "{WorkspaceId}.cn-beijing.maas.aliyuncs.com"这种工作区专属域名,2026-07-10
# 真机连线验证过全局域名对这个路径直接 404。
_WEBRTC_URL_TMPL = "https://{workspace}.cn-beijing.maas.aliyuncs.com/api/v1/webrtc/realtime"

# 复用 task_tools.py 里已经在真机反复调过、行为稳定的三个工具——claude_agent_sdk
# 的 @tool 装饰器把 name/description/input_schema/handler 都挂在函数对象上,这里
# 直接读,不手抄一份措辞,不会跟 MCP 那边的 schema 跑偏。
_TASK_TOOL_FNS = (
    task_tools.voice_dispatch_task,
    task_tools.voice_query_task,
    task_tools.voice_list_tasks,
)


def _build_tools() -> list[dict]:
    return [
        {"type": "function", "name": fn.name, "description": fn.description, "parameters": fn.input_schema}
        for fn in _TASK_TOOL_FNS
    ]


def _build_instructions() -> str:
    """复用 prompts.py 里已经真机调了四轮的语音人设规则,砍掉这里用不到的
    {long_task_hint}/{user_text} 动态拼接段——Omni 会话里用户说的话走会话本身的
    语音输入,不需要像 ws.py 那样手工拼一份 prompt 文本喂给 stream_turn。

    2026-07-10 真机反馈:Omni 是完全独立于 Claude 的模型会话,不会像
    core/prompt.py build_system_prompt 那样自动拿到 PERSONA/USER.md/MEMORY.md——
    表现为"不知道用户在哪个城市"这类基础画像缺失。这里只补 USER.md 画像(带
    跟 Claude 那边同一套反注入围栏 _MEMORY_FENCE),PERSONA 里大部分是 Claude
    Code 工具/子代理执行模型相关的规则,Qwen 没有那些工具,搬过来它也用不上;
    MEMORY.md 索引同理(那是给 recall_past/save_memory 这两个 Qwen 没有的工具用的)。
    """
    body = prompts._INSTRUCTION_BLOCK.split("{long_task_hint}")[0]
    voice_rules = body.format(timeout_min=config.VOICE_TASK_TIMEOUT_MIN)
    profile = core_prompt._load_user_profile()
    if not profile:
        return voice_rules
    return voice_rules + f"\n\n=== 参考数据围栏 ===\n{core_prompt._MEMORY_FENCE}" + profile


@dataclass
class OmniAudioDelta:
    """服务端合成语音的一个音频分片(已 base64 解码成 PCM 字节,24kHz/16bit/单声道)。"""

    pcm: bytes


@dataclass
class OmniTranscript:
    """用户说的一句话识别完成(最终结果,非流式增量)。"""

    text: str


@dataclass
class OmniTextDelta:
    """模型输出文字的增量,给屏幕字幕用(语音本体走 OmniAudioDelta)。"""

    text: str


@dataclass
class OmniFunctionCall:
    """模型这一轮触发了一次工具调用,等上层执行完拿结果回传(见 handle_function_call)。"""

    call_id: str
    name: str
    arguments: dict


@dataclass
class OmniSpeechStarted:
    """服务端 VAD 检测到用户开始说话。turn_detection.interrupt_response=true 时
    服务端会自动打断自己正在合成的回复,这里只是把事件透出去给前端做打断动画用,
    不需要再像 ws.py 那样自己维护一套打断状态机。"""


@dataclass
class OmniTurnDone:
    """这一轮 response 完全结束(对应 response.done)。"""


@dataclass
class OmniError:
    message: str


class OmniRealtimeSession:
    """一条到 Qwen-Omni-Realtime 的 WS 连接。"""

    def __init__(self) -> None:
        self._client_sess: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    async def connect(self) -> None:
        self._client_sess = aiohttp.ClientSession()
        url = f"{_DASHSCOPE_REALTIME_URL}?model={config.VOICE_OMNI_REALTIME_MODEL}"
        try:
            self._ws = await self._client_sess.ws_connect(
                url, headers={"Authorization": f"Bearer {config.DASHSCOPE_API_KEY}"}, timeout=15,
            )
        except Exception:
            await self._client_sess.close()
            raise
        await self._ws.send_str(json.dumps({
            "event_id": "session-init",
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": config.VOICE_TTS_VOICE,
                "input_audio_format": "pcm",
                "output_audio_format": "pcm",
                "instructions": _build_instructions(),
                "tools": _build_tools(),
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": config.VOICE_VAD_THRESHOLD,
                    "silence_duration_ms": config.VOICE_VAD_SILENCE_MS,
                },
            },
        }))

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._client_sess is not None:
            try:
                await self._client_sess.close()
            except Exception:
                pass

    async def send_audio(self, pcm16_bytes: bytes) -> None:
        """转发一帧客户端麦克风 PCM(16kHz/16bit/单声道)给上游。"""
        await self._ws.send_str(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm16_bytes).decode("ascii"),
        }))

    async def submit_function_result(self, call_id: str, output_text: str) -> None:
        """执行完一次工具调用后,把结果回传给模型并触发它继续说话。"""
        await self._ws.send_str(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id, "output": output_text},
        }))
        await self._ws.send_str(json.dumps({"type": "response.create"}))

    async def events(self):
        """驱动这条连接,把底层事件类型收成上面几种高层事件吐出去;不认识的事件
        类型直接跳过——协议还有很多字段这个阶段用不上,没必要照单全收。"""
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            t = data.get("type")
            if t == "response.audio.delta":
                yield OmniAudioDelta(base64.b64decode(data["delta"]))
            elif t == "conversation.item.input_audio_transcription.completed":
                yield OmniTranscript(data.get("transcript", ""))
            elif t == "response.text.delta":
                yield OmniTextDelta(data.get("delta", ""))
            elif t == "response.function_call_arguments.done":
                try:
                    args = json.loads(data.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                yield OmniFunctionCall(data["call_id"], data["name"], args)
            elif t == "input_audio_buffer.speech_started":
                yield OmniSpeechStarted()
            elif t == "response.done":
                yield OmniTurnDone()
            elif t == "error":
                yield OmniError((data.get("error") or {}).get("message", "未知错误"))


async def exchange_webrtc_sdp(offer_sdp: str) -> str:
    """把浏览器生成的 SDP offer 转发给 Qwen-Omni-Realtime,换回 SDP answer。

    必须由后端代理这一步,浏览器自己发不了:一是跨域限制(阿里云文档原话是
    "浏览器无法直接向服务端发起建立连接的请求"),二是这一步要带真实
    DASHSCOPE_API_KEY,不能下发到前端。信令换完之后,实际的音频/DataChannel
    流量是浏览器跟阿里云服务器直连(WebRTC 走 P2P/UDP),不再经过我们的服务器。

    2026-07-10 用 aiortc 模拟浏览器连线验证过:ICE/DTLS 能完整握手到
    connected,服务端会推一个 label="txt" 的 DataChannel 并主动发
    session.created,音频 m-line 也正确协商——这一步(信令代理本身)是可靠的。
    真实浏览器的 WebRTC/SCTP 实现比 aiortc 成熟,真机测的重点是"数据通道
    收发事件+音频轨道播放"这一层,不是这个代理。
    """
    if not config.VOICE_OMNI_WORKSPACE_ID:
        raise RuntimeError("未配置 VOICE_OMNI_WORKSPACE_ID(去百炼控制台复制业务空间ID)")
    url = (
        _WEBRTC_URL_TMPL.format(workspace=config.VOICE_OMNI_WORKSPACE_ID)
        + f"?model={config.VOICE_OMNI_REALTIME_MODEL}"
    )
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            url, data=offer_sdp,
            headers={
                "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
                "Content-Type": "application/sdp",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"WebRTC 信令交换失败 status={resp.status} body={body[:300]!r}")
            return body


async def handle_function_call(call: OmniFunctionCall) -> str:
    """执行一次工具调用,返回要回传给模型的文本结果。找不到对应工具时返回错误话术
    而不是抛异常——这是语音对话的一环,炸了不该把整条连接搞断。"""
    for fn in _TASK_TOOL_FNS:
        if fn.name == call.name:
            result = await fn.handler(call.arguments)
            parts = result.get("content") or []
            text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
            return text or "(无结果)"
    return f"没有名为 {call.name} 的工具。"
