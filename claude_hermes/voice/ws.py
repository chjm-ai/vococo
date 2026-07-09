"""P2 全双工:/voice/ws——免提连续对话 + 开口即打断。

见 docs/design/voice-companion/03-phase2-experience.md §2-C/2-D,
以及 03-phase2-实现记录.md"识别慢的根因排查"一节(2026-07-08 改动记录)。

状态机:idle → capturing(上游 DashScope 判定开口)→ thinking(上游给出最终
转写)→ speaking → idle,thinking/speaking 任意时刻可因上游又检测到开口而
打断回 capturing。

2026-07-08 架构变更:识别/断句判断整体外包给 DashScope 的
qwen3-asr-flash-realtime 实时语音 WS(见 _connect_upstream)——真机实测客户端
那套 Silero VAD(ML 模型)经常判断不出"说完了",导致卡在"聆听中"不动。现在
客户端只管持续把 PCM 转发过来,本模块把它原样中继给 DashScope 的上游连接,
状态机完全由上游吐回来的事件驱动(speech_started/completed),不再有客户端
自己维护的音频缓冲区、WAV 包装、批量 stt.transcribe() 调用那一套——识别本身
已经在这条流式连接里做完了。

打断走两阶段提交(别拆成两套机制,细节见 build_truncated_text 和 PendingTruncation):
1. 上游 speech_started 只记一个 PendingTruncation(带上当前 turn 的引用),
   不落库——但 2026-07-09 起【不再立刻 cancel 掉正在跑的 turn task】,见下方
   "误触发不可逆"的教训,先让它继续在后台跑着。
2. 随后上游给出 completed(带最终转写文本):这时候才真正决定——是真打断
   (截到哪句、落库、cancel 掉 turn.task),还是判定误触发(转写空/回声/语气词/
   超时)整个撤销、什么都不用做,因为 turn 压根没被动过,会自己正常说完;
   played_sentences 用客户端持续上报的"最新播放进度"(见 on_played_progress),
   不用跟某次特定消息配对。

2026-07-09 误触发不可逆的教训(见 03-phase2-实现记录.md):原设计"speech_started
立刻 cancel,省 token、不等它说完"——省是省了,但真机连续测试 4 次打断里
2 次是背景噪音/回声触发的假警报,而 cancel 一旦发生就真的回不去了,"回滚"
充其量只能告诉客户端"接着播你手头缓存的部分",AI 那句话被腰斩的后半段
永远要不回来了。改成"先观察,真等到 completed 确认是人在说话才 cancel"——
假警报的代价从"答案缺一截"降级成"多花几秒 TTS 算力"(那几秒生成的音频如果
最终判定是假警报,不影响,turn 本来就该继续说完;只有真打断确认时才会用上
_resolve_pending 里存的 pending.turn 去真正 cancel)。唯一的例外是手动静音
按钮(on_mute)和 WS 断线清理——这两个是明确无歧义的信号,不用等谁确认,
直接真 cancel(见 _interrupt_current_turn 的 immediate 参数)。

2026-07-08 连环打断内容累积(见 03-phase2-实现记录.md):真机连续快速追问
("今天天气怎么样"→打断→"有台风吗")之前会只回复最后一句,前面被打断的
问题因为对应的那次生成已经被 cancel、没能力"接着说",内容就丢了。现在每次
真打断确认(commit=True)都把那句话原文记进 `self._carried_text`,下一次
`_start_turn` 会把它拼在新说的话前面一起问模型,一次性给出结合了这几句的
回复——见 `_carried_text` 上的注释。

2026-07-08 声纹识别(见 voiceprint.py、03-phase2-实现记录.md"声纹识别"一节):
免提场景背景有人说话时,降噪分不出"哪个人声是你",这里加了目标说话人识别
——异步、不卡对话速度(方案 B):转写完立刻正常起一轮回复,声纹比对
(`_voiceprint_gate`)在后台并行跑,判定"不像是本人"就把这一轮撤回。第一次
用没有声纹参照,前几句只建立参照不拦截;参照跨会话持久化,不用每次重新学。

2026-07-09 thinking/speaking 兜底看门狗:capturing 状态一直有 30 秒兜底
(_capturing_stall_guard),但 thinking/speaking 完全没有——真机复现过一次
session.run_turn() 卡住不吐事件,界面永远停在"思考中"转圈,没有报错也不会
自己恢复。现在对称地加了 _turn_stall_guard,原理见 _TURN_STALL_MS 的注释。
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import json
import re
import time
from dataclasses import dataclass, field

import aiohttp
from aiohttp import web

from .. import config
from ..core.agent import Done, TextDelta, ToolStarted
from . import prompts, session, task_tools, tts, voiceprint

_STATE_IDLE = "idle"
_STATE_CAPTURING = "capturing"
_STATE_THINKING = "thinking"
_STATE_SPEAKING = "speaking"

_DASHSCOPE_REALTIME_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
# capturing 状态的兜底超时:上游正常情况下要么给 completed、要么给下一次
# speech_started,万一它自己卡住/连接异常导致什么事件都不来,不能让界面
# 永远停在"聆听中"——这是当初客户端 VAD 不可靠踩过的坑,现在挪到服务端也要防一次。
_CAPTURING_STALL_MS = 30_000

# thinking/speaking 状态的兜底超时(对称于上面 capturing 那个):session.run_turn()
# 的 async for 一旦卡住不吐任何事件(模型调用挂住/工具调用卡死/未知异常被吞掉
# 却没抛出),之前完全没有保护,界面会永远停在"思考中"转圈,谁也叫不醒——2026-07-09
# 语音伴聊真机复现过一次。用"距离上一次收到任何事件已经过了多久"判断,而不是
# 给整轮对话设硬上限,因为工具调用(读代码/搜索)本身可能要花点时间,只要还在
# 吐事件就不算卡死。
_TURN_STALL_MS = 45_000

# 同一时刻只服务一个 WS 连接(单用户场景),新连接顶替旧的。
_active_ws: web.WebSocketResponse | None = None


def build_truncated_text(emitted_sentences: list[str], played_count: int) -> str:
    """打断截断:只算用户真正听完的句子,附加"被打断"标记。

    played_count 由客户端上报(source.onended 真正播完的句子数),可能大于服务端
    实际发出的句子数(防御性 clamp,不炸)。played_count<=0 时视为一句都没听到。
    """
    n = max(0, min(played_count, len(emitted_sentences)))
    heard = "".join(emitted_sentences[:n])
    return heard + "(此处被用户打断)"


# 真机实测(见 03-phase2-实现记录.md"连环打断"一节):VAD 灵敏度再怎么调,
# 环境噪音/呼吸声偶尔还是会被识别模型强行编成一个说得过去的短语气词,而不是
# 老实返回空——这类词本身从空闲状态触发没什么危害(AI 就回一句"在呢,你说"),
# 但如果拿去打断一个已经在跑的回答,会造成连环打断,真正的问题反而被吃掉。
_FILLER_WORDS = frozenset({
    "嗯", "呃", "啊", "哦", "噢", "诶", "欸", "唉", "哈", "呀",
    "是的", "知道", "好的", "好", "对", "对的", "行", "行吧", "稍等", "等等",
})
_PUNCT_RE = re.compile(r"[，。！？,.!?、\s]+")


def looks_like_filler_only(transcript: str) -> bool:
    """转写内容整段就是一个语气词/口头禅(去掉标点后跟已知词表精确匹配),
    大概率是噪音被硬凑出来的假触发——不用来判断"这是不是用户真开口",只用
    在"要不要让它打断一个已经在跑的回答"这个更谨慎的判断上,见调用处。
    """
    stripped = _PUNCT_RE.sub("", transcript)
    return not stripped or stripped in _FILLER_WORDS


def estimate_speech_too_short(
    speech_started_at: float,
    speech_stopped_at: float | None,
    *,
    silence_ms: int,
    min_speech_ms: int,
) -> bool:
    """时长兜底(见 config.VOICE_MIN_SPEECH_MS 的说明):不看转写文字,只看
    物理时长——呼吸声/麦克风杂音这类瞬时噪声通常撑不到一个字的时长,即便被
    识别模型硬编成语气词也该丢掉,所以在空闲状态也生效(不像 looks_like_filler_only
    只在打断场景生效)。

    2026-07-09 真机复现修正:原先假设 speech_started→speech_stopped 这段时间差
    里一定含有完整的 silence_ms(session.update 里配置的静音判停时长),减掉才是
    真正说话时长——但真机实测过好几次都是 raw_span < silence_ms(例如
    raw_span=985ms < silence_ms=1500ms),说明 DashScope 实际判"说完了"用的静音
    时长跟我们配置的对不上,减法算出来永远是负数,导致"你好呀""什么？"这类
    正常短句全部被误杀成噪声,免提模式变成几乎打不出一轮对话。改成直接用
    raw_span_ms(不做减法)判断——只挡真正瞬时的噪声(几十毫秒级别),
    宁可放过一些语气词误触发,也不能连正常说话都拦掉。silence_ms 形参保留
    但不再参与判断(签名不改,调用方不用跟着改;只是不再依赖这个不可靠的假设)。
    speech_stopped 还没来(比如被下一次打断打断)时不误伤,直接放行。
    """
    if speech_stopped_at is None:
        return False
    raw_span_ms = (speech_stopped_at - speech_started_at) * 1000
    return raw_span_ms < min_speech_ms


def looks_like_self_echo(
    transcript: str, emitted_sentences: list[str], threshold: float
) -> bool:
    """打断触发后转写出来的内容,是不是其实是 AI 自己声音漏进麦克风被听回去的。

    用"containment"而不是对称的相似度:回声通常只是 AI 那句话的一个片段
    (播放中途被打断/麦克风只收到一部分),transcript 一般比 emitted_sentences
    短很多,对称相似度会因为长度差太多而被稀释,量不出"这一小段几乎完全能在
    AI 刚说的话里找到"这件事。containment = transcript 里有多少字符能在
    emitted_sentences 拼起来的原文里找到匹配片段,除以 transcript 总长。
    """
    said = "".join(emitted_sentences)
    if not said or not transcript:
        return False
    matcher = difflib.SequenceMatcher(None, transcript, said)
    matched_chars = sum(block.size for block in matcher.get_matching_blocks())
    containment = matched_chars / len(transcript)
    return containment >= threshold


@dataclass
class PendingTruncation:
    """打断后的"待定"状态:两阶段提交的中间态,confirm/rollback 共用同一份。

    turn:待定期间 turn.task 还没被真 cancel(见模块顶部说明)时,存一份引用,
    真打断确认(_resolve_pending 的 commit 分支)才用它去真正 cancel;
    immediate=True(手动静音/断线)已经现场 cancel 过,这里存 None。
    """

    user_text: str
    emitted_sentences: list[str]
    created_at: float
    turn: "TurnContext | None" = None
    resolved: bool = False


@dataclass
class TurnContext:
    task: "asyncio.Task"
    emitted_sentences: list[str] = field(default_factory=list)


async def _connect_upstream(sample_rate: int) -> tuple[aiohttp.ClientSession, aiohttp.ClientWebSocketResponse]:
    """建到 DashScope 实时语音 WS 的上游连接,已发完 session.update。

    独立成函数是为了测试能整个 monkeypatch 掉(不能在单测里连真实 DashScope)。
    调用方负责在用完后依次关掉 ws、client_session(顺序不能反,client_session
    先关会把还没断开的 ws 连接一起杀掉)。
    """
    client_sess = aiohttp.ClientSession()
    url = f"{_DASHSCOPE_REALTIME_URL}?model={config.DASHSCOPE_REALTIME_MODEL}"
    try:
        ws = await client_sess.ws_connect(
            url, headers={"Authorization": f"Bearer {config.DASHSCOPE_API_KEY}"}
        )
    except Exception:
        await client_sess.close()
        raise
    await ws.send_str(json.dumps({
        "event_id": "session-init",
        "type": "session.update",
        "session": {
            "input_audio_format": "pcm",
            "sample_rate": sample_rate,
            "input_audio_transcription": {"language": "zh"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": config.VOICE_VAD_THRESHOLD,
                "silence_duration_ms": config.VOICE_VAD_SILENCE_MS,
            },
        },
    }))
    return client_sess, ws


class VoiceWsSession:
    """一条 WS 连接的完整状态机,供 handle() 驱动;逻辑抽成类方便单测直接构造。"""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self.state = _STATE_IDLE
        self._current_turn: TurnContext | None = None
        self._pending: PendingTruncation | None = None
        self._pending_user_text = ""
        # 连环打断累积:一轮被真打断确认(见 _resolve_pending 的 commit 分支)时,
        # 那句话原文存到这里,下一次 _start_turn 会把它拼在新说的话前面一起问
        # 模型——不然快速追问("今天天气怎么样"→打断→"有台风吗")会变成只回复
        # 最后一句,前面被打断的问题就石沉大海,见 03-phase2-实现记录.md。
        # 在 _start_turn 消费时立即清空,成功答完的轮次不会带着旧内容污染
        # 之后完全无关的新一轮。
        self._carried_text = ""
        self._speaking_announced = False
        self._last_played_progress = 0
        self._capturing_watchdog: "asyncio.Task | None" = None
        # thinking/speaking 兜底看门狗(见 _TURN_STALL_MS 的注释)+ 最近一次
        # 收到上游事件的时刻,_turn_stall_guard 用它判断"是不是真卡住了"。
        self._turn_watchdog: "asyncio.Task | None" = None
        self._turn_progress_at = 0.0
        # 回声兜底本来只在"正在打断一个还没说完的回答"(self._pending 非空)
        # 时生效——但 AI 说完最后一句、服务端状态已经翻回 idle 之后,客户端
        # 音箱可能还在播放最后一两句的尾音,这段声音漏回麦克风时 self._pending
        # 已经是 None,原来的回声/语气词判断整个被跳过,尾音就被当成一句全新的
        # 真话开了新的一轮。这里额外存一份"最近一轮说过的话 + 结束时刻",
        # 让回声判断在"刚说完话的一小段时间内"也能生效,不用非得在打断场景才查。
        self._recent_emitted_sentences: list[str] = []
        self._recent_emitted_at = 0.0
        # 声纹识别(见 voiceprint.py):区分"这是本人在说话"还是"背景里别人在
        # 说话"。参照跨会话持久化(load_profile 读磁盘上次存的),这里只是
        # 每条连接各自持有一份内存副本,更新后立刻存盘。
        self._voice_profile = voiceprint.load_profile()
        # 当前这一句正在被识别的原始 PCM——从进入 capturing 状态开始攒,
        # completed 事件到来时读出来做声纹提取,见 _set_state/on_audio_frame。
        self._capturing_pcm = bytearray()
        # 时长兜底(见 config.VOICE_MIN_SPEECH_MS 的说明):记录本句
        # speech_started/speech_stopped 的时刻,completed 到来时估算真正
        # 说话时长有多短。speech_stopped 迟迟不来(极端情况)时保持 None,
        # 时长检查直接跳过、不误伤。
        self._speech_started_at = 0.0
        self._speech_stopped_at: float | None = None

        self._upstream_sess: aiohttp.ClientSession | None = None
        self._upstream_ws: aiohttp.ClientWebSocketResponse | None = None
        self._upstream_consumer: "asyncio.Task | None" = None
        self._upstream_closing = False  # 我们主动关的,消费循环别当成异常重连

    async def _send(self, type_: str, **payload) -> None:
        try:
            await self.ws.send_json({"type": type_, **payload})
        except (ConnectionResetError, RuntimeError):
            pass

    async def _set_state(self, state: str) -> None:
        self.state = state
        await self._send("state", state=state)
        if state == _STATE_CAPTURING:
            self._arm_capturing_watchdog()
            self._capturing_pcm = bytearray()  # 新一句开始,清空上一句攒的音频
        else:
            self._clear_capturing_watchdog()

    def _arm_capturing_watchdog(self) -> None:
        self._clear_capturing_watchdog()
        self._capturing_watchdog = asyncio.ensure_future(self._capturing_stall_guard())

    def _clear_capturing_watchdog(self) -> None:
        if self._capturing_watchdog is not None:
            self._capturing_watchdog.cancel()
            self._capturing_watchdog = None

    async def _capturing_stall_guard(self) -> None:
        await asyncio.sleep(_CAPTURING_STALL_MS / 1000)
        if self.state == _STATE_CAPTURING:
            await self._resolve_pending(commit=False)
            await self._set_state(_STATE_IDLE)
            await self._send("error", message="识别一直没有响应,已重置")

    def _arm_turn_watchdog(self) -> None:
        self._clear_turn_watchdog()
        self._turn_progress_at = time.monotonic()
        self._turn_watchdog = asyncio.ensure_future(self._turn_stall_guard())

    def _clear_turn_watchdog(self) -> None:
        if self._turn_watchdog is not None:
            self._turn_watchdog.cancel()
            self._turn_watchdog = None

    def _kick_turn_watchdog(self) -> None:
        """收到上游任意事件就算"还活着",见 _TURN_STALL_MS 的注释。"""
        self._turn_progress_at = time.monotonic()

    async def _turn_stall_guard(self) -> None:
        while True:
            remaining = _TURN_STALL_MS / 1000 - (time.monotonic() - self._turn_progress_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            break
        turn = self._current_turn
        self._current_turn = None
        if turn is not None:
            turn.task.cancel()
            asyncio.ensure_future(_swallow_cancelled(turn.task))
        await self._set_state(_STATE_IDLE)
        await self._send("error", message="思考/回复一直没有进展,已重置")

    # ── 客户端→服务端事件入口 ─────────────────────────────────────────
    async def on_hello(self, sample_rate: int | None) -> None:
        """握手:拿到真实采样率后才知道怎么配置上游 session.update。"""
        rate = sample_rate or 16000
        try:
            self._upstream_sess, self._upstream_ws = await _connect_upstream(rate)
        except Exception as exc:  # noqa: BLE001 —— 连不上上游,直接告诉客户端
            await self._send("error", message=f"语音识别服务连接失败:{exc}")
            return
        self._upstream_consumer = asyncio.ensure_future(self._consume_upstream())

    async def on_audio_frame(self, data: bytes) -> None:
        if self.state == _STATE_CAPTURING:
            self._capturing_pcm.extend(data)  # 供声纹提取用,见 _voiceprint_gate
        if self._upstream_ws is None or self._upstream_ws.closed:
            return
        try:
            await self._upstream_ws.send_str(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(data).decode("ascii"),
            }))
        except (ConnectionResetError, RuntimeError):
            pass

    async def on_played_progress(self, played_sentences: int | None) -> None:
        if played_sentences is not None:
            self._last_played_progress = played_sentences

    async def on_mute(self) -> None:
        """老按钮降级:明确无歧义的手动打断信号,不用像开口打断那样等确认。"""
        if self.state in (_STATE_THINKING, _STATE_SPEAKING):
            await self._interrupt_current_turn(immediate=True)
            await self._resolve_pending(commit=False)  # 手动静音没有后续语音确认,直接当空
            await self._set_state(_STATE_IDLE)

    # ── 上游(DashScope)事件消费 ──────────────────────────────────────
    async def _consume_upstream(self) -> None:
        try:
            async for msg in self._upstream_ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                await self._handle_upstream_event(json.loads(msg.data))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 连接异常,走下面统一的重连/放弃逻辑
            pass
        if not self._upstream_closing:
            await self._on_upstream_lost()

    async def _handle_upstream_event(self, data: dict) -> None:
        type_ = data.get("type")
        if type_ == "input_audio_buffer.speech_started":
            self._speech_started_at = time.monotonic()
            self._speech_stopped_at = None
            if self.state in (_STATE_THINKING, _STATE_SPEAKING):
                await self._interrupt_current_turn(immediate=False)
            await self._set_state(_STATE_CAPTURING)
        elif type_ == "input_audio_buffer.speech_stopped":
            self._speech_stopped_at = time.monotonic()
        elif type_ == "conversation.item.input_audio_transcription.completed":
            text = (data.get("transcript") or "").strip()
            interrupting = self._pending is not None
            # 刚说完话(Done 或打断确认)之后的一小段时间内,客户端音箱可能还在
            # 播放尾巴,这段声音漏回麦克风时早就不在"打断"场景里了(self._pending
            # 已经是 None)——单独开一个时间窗兜底,不然这类尾音会绕开下面的
            # 回声判断,被当成一句全新的真话。
            within_post_done_guard = (
                not interrupting
                and bool(self._recent_emitted_sentences)
                and (time.monotonic() - self._recent_emitted_at) * 1000
                < config.VOICE_POST_DONE_ECHO_GUARD_MS
            )
            # 回声兜底:这次转写出来的内容,跟 AI 刚说的话(打断场景用 pending 里
            # 记的 emitted_sentences,尾音场景用 _recent_emitted_sentences)高度
            # 重合,大概率是它自己的声音漏回麦克风被听回去的,不是用户真开口——
            # 当成误触发处理,别真的打断/开新一轮拿这段话去问模型。
            is_echo = (interrupting or within_post_done_guard) and looks_like_self_echo(
                text,
                self._pending.emitted_sentences if interrupting else self._recent_emitted_sentences,
                config.VOICE_SELF_ECHO_THRESHOLD,
            )
            # 噪音兜底:只在"正要打断一个已经在跑的回答"这个场景生效——从空闲
            # 状态触发的语气词照常当一句真话处理(用户确实可能就是说了句"嗯"),
            # 危害只在"拿它去打断"这一步,见 looks_like_filler_only 的注释。
            is_filler = interrupting and looks_like_filler_only(text)
            # 时长兜底:不看文字看物理时长,任何状态下都生效(见 estimate_speech_too_short
            # 的说明)——跟上面 is_filler 是互补关系,不是互相替代。
            too_short = estimate_speech_too_short(
                self._speech_started_at,
                self._speech_stopped_at,
                silence_ms=config.VOICE_VAD_SILENCE_MS,
                min_speech_ms=config.VOICE_MIN_SPEECH_MS,
            )
            if not text or is_echo or is_filler or too_short:
                if too_short and not (is_echo or is_filler):
                    raw_span_ms = (
                        (self._speech_stopped_at - self._speech_started_at) * 1000
                        if self._speech_stopped_at is not None
                        else -1
                    )
                    print(
                        f"[voice/ws] 丢弃疑似噪声(时长不足) text={text!r} "
                        f"raw_span={raw_span_ms:.0f}ms silence_ms={config.VOICE_VAD_SILENCE_MS} "
                        f"min={config.VOICE_MIN_SPEECH_MS}",
                        flush=True,
                    )
                await self._resolve_pending(commit=False)
                if too_short and not is_echo and not interrupting:
                    # 只在"确实检测到一段声音、但时长兜底判定太短"这个具体场景补提示——
                    # 空转写(真的什么都没说)、自我回声(AI 尾音漏回麦克风)本来就该
                    # 悄无声息地过滤掉,不是用户体验问题;唯独这一种,用户是真开口了,
                    # 状态却从 capturing 悄悄退回 idle,体感上跟"卡死没反应"没区别
                    # (2026-07-09 真机复现:说"你好呀"被当噪声吃掉,界面毫无反应)。
                    await self._send("error", message="没听清,请再说一次")
                await self._set_state(_STATE_IDLE)
                return
            # pending 还没超时就等到了新的有效转写 → 确认打断,提交截断
            await self._resolve_pending(commit=True, played_count=self._last_played_progress)
            await self._send("transcript", text=text)
            # 声纹核验要用的原始音频,得在 _start_turn 把状态切走(_capturing_pcm
            # 会在下一次进 capturing 时被清空)之前先取一份快照。
            pcm_snapshot = bytes(self._capturing_pcm)
            await self._start_turn(text)
            # 方案 B(见 03-phase2-实现记录.md"声纹识别"一节):不卡这一轮的
            # 起步速度,正常立刻开始回复,声纹比对在后台并行跑,判定"不是
            # 本人"再把这一轮撤回——比对本身只要几十毫秒,但改成同步等待
            # 没必要,能异步就异步。
            if config.VOICE_VOICEPRINT_ENABLED and self._current_turn is not None:
                asyncio.ensure_future(self._voiceprint_gate(pcm_snapshot, self._current_turn))
        # session.created/session.updated/增量 .text/conversation.item.created/
        # input_audio_buffer.committed/session.finished 都只是过程性事件,
        # 不是决策点,忽略(speech_stopped 现在只记时间戳,见上面的分支)。

    async def _on_upstream_lost(self) -> None:
        """上游连接意外断开(不是我们主动关的):重连一次,再不行就放弃。"""
        await self._close_upstream()
        rate = 16000  # 断线重连场景拿不到最初的 hello 采样率,用默认兜底
        try:
            self._upstream_sess, self._upstream_ws = await _connect_upstream(rate)
        except Exception as exc:  # noqa: BLE001
            await self._resolve_pending(commit=False)
            await self._set_state(_STATE_IDLE)
            await self._send("error", message=f"语音识别连接断开且重连失败:{exc}")
            return
        self._upstream_consumer = asyncio.ensure_future(self._consume_upstream())

    async def _close_upstream(self) -> None:
        self._upstream_closing = True
        if self._upstream_ws is not None:
            try:
                await self._upstream_ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._upstream_ws = None
        if self._upstream_sess is not None:
            try:
                await self._upstream_sess.close()
            except Exception:  # noqa: BLE001
                pass
            self._upstream_sess = None
        self._upstream_closing = False

    # ── 打断:两阶段提交 ──────────────────────────────────────────────
    async def _interrupt_current_turn(self, *, immediate: bool) -> None:
        """immediate=True:手动静音/WS 断线,信号无歧义,现场真 cancel。
        immediate=False:VAD 猜测开口,先只记 pending、turn 继续在后台跑,
        真 cancel 延后到 _resolve_pending 的 commit 分支——见模块顶部说明。
        """
        turn = self._current_turn
        if turn is None:
            return
        if immediate:
            self._current_turn = None
            self._clear_turn_watchdog()  # 真人开口打断,不算"卡死",别让看门狗跟着凑热闹
            # 只发起取消、不在这里 await 它收尾——core/agent.py 的取消链路里
            # client.interrupt() 最多兜底等 5 秒,真 await 会把 WS 主收环卡住那么久,
            # 打断到静音的体验就废了。收尾丢给后台任务,不阻塞后续消息处理。
            turn.task.cancel()
            asyncio.ensure_future(_swallow_cancelled(turn.task))
        pending = PendingTruncation(
            user_text=self._pending_user_text or "",
            emitted_sentences=turn.emitted_sentences,
            created_at=time.monotonic(),
            turn=None if immediate else turn,
        )
        self._pending = pending
        # 兜底看门狗:万一后续 completed 迟迟不来(用户打断后干脆没再说话),
        # 超时后也要主动撤销打断、恢复播放——不能让 pending 悬空到 WS 断线才处理。
        asyncio.ensure_future(self._rollback_watchdog(pending))

    async def _rollback_watchdog(self, pending: PendingTruncation) -> None:
        await asyncio.sleep(config.VOICE_FALSE_POSITIVE_TIMEOUT_MS / 1000)
        if self._pending is pending and not pending.resolved:
            await self._resolve_pending(commit=False)

    async def _resolve_pending(self, commit: bool, played_count: int | None = None) -> None:
        pending = self._pending
        if pending is None or pending.resolved:
            return
        pending.resolved = True
        self._pending = None
        elapsed_ms = (time.monotonic() - pending.created_at) * 1000
        timed_out = elapsed_ms > config.VOICE_FALSE_POSITIVE_TIMEOUT_MS
        if commit and not timed_out:
            # 真打断确认:如果 turn 还没被现场 cancel 过(2026-07-09 起默认先
            # 观察,见模块顶部说明),现在才是真正动手的时候——晚 cancel 不会
            # 多花钱,只是多等这几秒的 TTS 合成,换来的是假警报不会腰斩 AI
            # 还没说完的话。self._current_turn is pending.turn 这个判断防的是
            # 罕见竞态:turn 可能已经自己正常跑完了(见 _run_turn 的 Done 分支
            # 开头那段兜底),这种情况下 pending 早就在那边被强制 commit=False
            # 处理掉了,不会走到这里来。
            if pending.turn is not None and self._current_turn is pending.turn:
                self._current_turn = None
                self._clear_turn_watchdog()
                pending.turn.task.cancel()
                asyncio.ensure_future(_swallow_cancelled(pending.turn.task))
            truncated = build_truncated_text(pending.emitted_sentences, played_count or 0)
            session.append(pending.user_text, truncated)
            # 这句话被真的打断了、没问完整/没答完,留给下一轮 _start_turn 拼接,
            # 别让它就这么消失。
            self._carried_text = pending.user_text
            await self._send("interrupted", truncated_at_seq=played_count or 0)
            print(f"[voice/ws] 打断确认 elapsed={elapsed_ms:.0f}ms played={played_count}", flush=True)
        else:
            # 假警报(超时/转写为空/回声/语气词):turn 如果一直没被现场 cancel
            # 过,此刻要么已经在后台正常跑完、要么还在跑——什么都不用做,该来的
            # text_delta/sentence/done 会按正常节奏继续发过来,这里只需要告诉
            # 客户端"可以继续放你手头缓存的音频了"。
            await self._send("resumed")
            reason = "超时" if timed_out else "转写为空"
            print(f"[voice/ws] 打断回滚({reason}) elapsed={elapsed_ms:.0f}ms", flush=True)

    # ── 声纹核验(异步,不卡对话速度,见 03-phase2-实现记录.md"声纹识别"一节)──
    async def _voiceprint_gate(self, pcm: bytes, turn: "TurnContext") -> None:
        """判断刚才那句话是不是本人说的;判定不是就把这一轮撤回。

        turn 是发起这次核验时的那个 TurnContext——比对跑完的时候这一轮可能
        已经被别的事情替换掉了(比如正常说完了、或者被真实的开口打断了),
        必须确认 self._current_turn 还是同一个对象才动手撤回,不然会误杀
        一轮完全不相关的、后来的对话。
        """
        loop = asyncio.get_event_loop()
        embedding = await loop.run_in_executor(None, voiceprint.extract_embedding, pcm)
        if embedding is None:
            return  # 音频太短提不出可靠声纹,不拦截也不纳入声纹参照
        profile = self._voice_profile
        score = voiceprint.match_score(embedding, profile)
        # 参照样本还不够(冷启动阶段)时不做拦截判定,只用来建立参照——第一次
        # 用这个功能时没有任何参照可比,不能拿一个空/单薄的参照去筛掉任何人。
        is_cold_start = score is None or len(profile) < config.VOICE_VOICEPRINT_MIN_SAMPLES
        if not is_cold_start and score < config.VOICE_VOICEPRINT_MATCH_THRESHOLD:
            if self._current_turn is turn:
                self._current_turn = None
                turn.task.cancel()
                asyncio.ensure_future(_swallow_cancelled(turn.task))
                await self._set_state(_STATE_IDLE)
                await self._send("error", message="没听清像是你的声音,请再说一次")
                print(f"[voice/ws] 声纹不匹配,撤回这一轮 score={score:.2f}", flush=True)
            return  # 不管撤没撤回,不匹配的样本都不该纳入声纹参照
        self._voice_profile = voiceprint.update_profile(
            profile, embedding, config.VOICE_VOICEPRINT_MATCH_THRESHOLD
        )
        voiceprint.save_profile(self._voice_profile)

    # ── 正常一轮:与 routes.py 的 _handle_send 同构,只是搬到 WS 上 ─────
    async def _start_turn(self, user_text: str) -> None:
        if self._carried_text:
            # 直接拼接:DashScope 转写自带句末标点,拼起来就是用户原本想连着
            # 说但被我们自己的打断机制切碎的完整一句话,不用额外加说明文字。
            user_text = f"{self._carried_text}{user_text}"
            self._carried_text = ""
        self._pending_user_text = user_text
        self._speaking_announced = False
        await self._set_state(_STATE_THINKING)
        turn = TurnContext(task=asyncio.ensure_future(self._run_turn(user_text)))
        self._current_turn = turn
        self._arm_turn_watchdog()

    async def _run_turn(self, user_text: str) -> None:
        # 延迟 import:routes.py 顶层已经 import 了本模块(挂路由用),这里反向
        # import routes 会形成循环——放函数体内、只在真正跑的时候才解析,两边都
        # 早已初始化完毕,不会有问题。复用同一把锁只是为了让 /voice/send(文字
        # 兜底输入)跟 WS 语音轮次互斥,不是两套各自的互斥逻辑。
        from . import routes

        splitter = tts.SentenceSplitter()
        turn = self._current_turn
        seq = 0
        filler_sent = False
        try:
            async with routes._lock:
                prompt_text = prompts.build_prompt(user_text)
                async for ev in session.run_turn(
                    prompt_text, extra_mcp_servers=task_tools.build_server()
                ):
                    self._kick_turn_watchdog()  # 只要还在吐事件就不算卡死,见 _TURN_STALL_MS
                    if isinstance(ev, ToolStarted):
                        if not filler_sent and ev.parent_id is None:
                            filler_sent = True
                            audio_bytes = await tts.filler_audio()
                            await self._send("filler", audio_b64=_b64(audio_bytes))
                    elif isinstance(ev, TextDelta):
                        if not self._speaking_announced:
                            self._speaking_announced = True
                            await self._set_state(_STATE_SPEAKING)
                        await self._send("text_delta", text=ev.text)
                        for sentence in splitter.feed(ev.text):
                            await self._emit_sentence(turn, seq, sentence)
                            seq += 1
                    elif isinstance(ev, Done):
                        if self._pending is not None and self._pending.turn is turn:
                            # 罕见竞态(见模块顶部"误触发不可逆的教训"):待定打断
                            # 还没等到 completed 确认/超时,turn 自己先正常说完
                            # 了——肯定不是真打断,强制判定回滚,别让下面的正常
                            # Done 流程再跟"打断确认"分支重复给这句话落两次库。
                            await self._resolve_pending(commit=False)
                        for sentence in splitter.flush():
                            await self._emit_sentence(turn, seq, sentence)
                            seq += 1
                        reply = ev.reply
                        session.append(user_text, reply.text)
                        if reply.sdk_session_id:
                            session.set_resume(reply.sdk_session_id)
                        self._current_turn = None
                        self._clear_turn_watchdog()
                        # 供"尾音回声"兜底用(见 __init__ 里 _recent_emitted_sentences
                        # 的注释):这一轮刚说完的话记下来,配合时间窗让 idle 之后
                        # 短时间内的回声也能被拦下来。
                        self._recent_emitted_sentences = list(turn.emitted_sentences)
                        self._recent_emitted_at = time.monotonic()
                        # 先广播回 idle 再发 done——客户端收到 done 时状态已经是准的,
                        # 不用再等下一条消息才知道"真的闲下来了"。
                        await self._set_state(_STATE_IDLE)
                        await self._send("done", full_text=reply.text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 兜底:出错也要给前端一个 done
            self._current_turn = None
            self._clear_turn_watchdog()
            await self._set_state(_STATE_IDLE)
            await self._send("error", message=str(exc))

    async def _emit_sentence(self, turn: TurnContext, seq: int, sentence: str) -> None:
        turn.emitted_sentences.append(sentence)
        audio = await tts.synthesize(sentence, config.VOICE_TTS_VOICE)
        await self._send("sentence", seq=seq, text=sentence, audio_b64=_b64(audio))

    async def close(self) -> None:
        """WS 连接收尾:关掉上游、取消看门狗,不留后台任务。"""
        self._clear_capturing_watchdog()
        self._clear_turn_watchdog()
        if self._upstream_consumer is not None:
            self._upstream_consumer.cancel()
        await self._close_upstream()


async def _swallow_cancelled(task: "asyncio.Task") -> None:
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 —— 收尾观察,不能让后台任务的异常变成日志噪音外的东西
        pass


def _b64(data: bytes | None) -> str | None:
    if not data:
        return None
    return base64.b64encode(data).decode("ascii")


def _ok_token(request: web.Request) -> bool:
    if not config.WEB_AUTH_TOKEN:
        return True
    import hmac

    tok = request.query.get("token") or ""
    return hmac.compare_digest(tok, config.WEB_AUTH_TOKEN)


async def handle(request: web.Request) -> web.WebSocketResponse:
    global _active_ws
    if not _ok_token(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    if _active_ws is not None:
        await _active_ws.close(code=4000, message=b"superseded by new connection")
    _active_ws = ws

    sess = VoiceWsSession(ws)
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                await sess.on_audio_frame(msg.data)
            elif msg.type == web.WSMsgType.TEXT:
                await _dispatch_control(sess, msg.json())
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        # 断线一律当作"当前轮被打断",不跨断线续播(见 03-phase2-experience.md 的取舍)。
        if sess._current_turn is not None:
            await sess._interrupt_current_turn(immediate=True)
        await sess.close()
        if _active_ws is ws:
            _active_ws = None
    return ws


async def _dispatch_control(sess: VoiceWsSession, data: dict) -> None:
    type_ = data.get("type")
    if type_ == "hello":
        await sess.on_hello(data.get("sample_rate"))
    elif type_ == "played_progress":
        await sess.on_played_progress(data.get("played_sentences"))
    elif type_ == "mute":
        await sess.on_mute()
