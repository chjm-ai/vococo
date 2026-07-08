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
1. 上游 speech_started 立刻 cancel 掉正在跑的 turn task(省 token,不等它说完),
   但这时候还不知道用户听到了几句——服务端已发出的 sentence 可能比客户端已经
   播完的多——于是只记一个 PendingTruncation,不落库。
2. 随后上游给出 completed(带最终转写文本),才真正决定截到哪句、落库、
   还是判定是误触发(转写空/超时)整个撤销;played_sentences 用客户端持续
   上报的"最新播放进度"(见 on_played_progress),不用跟某次特定消息配对。

2026-07-08 连环打断内容累积(见 03-phase2-实现记录.md):真机连续快速追问
("今天天气怎么样"→打断→"有台风吗")之前会只回复最后一句,前面被打断的
问题因为对应的那次生成已经被 cancel、没能力"接着说",内容就丢了。现在每次
真打断确认(commit=True)都把那句话原文记进 `self._carried_text`,下一次
`_start_turn` 会把它拼在新说的话前面一起问模型,一次性给出结合了这几句的
回复——见 `_carried_text` 上的注释。
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
from . import prompts, session, task_tools, tts

_STATE_IDLE = "idle"
_STATE_CAPTURING = "capturing"
_STATE_THINKING = "thinking"
_STATE_SPEAKING = "speaking"

_DASHSCOPE_REALTIME_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
# capturing 状态的兜底超时:上游正常情况下要么给 completed、要么给下一次
# speech_started,万一它自己卡住/连接异常导致什么事件都不来,不能让界面
# 永远停在"聆听中"——这是当初客户端 VAD 不可靠踩过的坑,现在挪到服务端也要防一次。
_CAPTURING_STALL_MS = 30_000

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
    """打断后的"待定"状态:两阶段提交的中间态,confirm/rollback 共用同一份。"""

    user_text: str
    emitted_sentences: list[str]
    created_at: float
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
        # 回声兜底本来只在"正在打断一个还没说完的回答"(self._pending 非空)
        # 时生效——但 AI 说完最后一句、服务端状态已经翻回 idle 之后,客户端
        # 音箱可能还在播放最后一两句的尾音,这段声音漏回麦克风时 self._pending
        # 已经是 None,原来的回声/语气词判断整个被跳过,尾音就被当成一句全新的
        # 真话开了新的一轮。这里额外存一份"最近一轮说过的话 + 结束时刻",
        # 让回声判断在"刚说完话的一小段时间内"也能生效,不用非得在打断场景才查。
        self._recent_emitted_sentences: list[str] = []
        self._recent_emitted_at = 0.0

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
        """老按钮降级:等价"手动打断",同样走真取消,不弱于开口打断。"""
        if self.state in (_STATE_THINKING, _STATE_SPEAKING):
            await self._interrupt_current_turn()
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
            if self.state in (_STATE_THINKING, _STATE_SPEAKING):
                await self._interrupt_current_turn()
            await self._set_state(_STATE_CAPTURING)
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
            if not text or is_echo or is_filler:
                await self._resolve_pending(commit=False)
                await self._set_state(_STATE_IDLE)
                return
            # pending 还没超时就等到了新的有效转写 → 确认打断,提交截断
            await self._resolve_pending(commit=True, played_count=self._last_played_progress)
            await self._send("transcript", text=text)
            await self._start_turn(text)
        # speech_stopped/session.created/session.updated/增量 .text/
        # conversation.item.created/input_audio_buffer.committed/session.finished
        # 都只是过程性事件,不是决策点,忽略。

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
    async def _interrupt_current_turn(self) -> None:
        turn = self._current_turn
        if turn is None:
            return
        self._current_turn = None
        # 只发起取消、不在这里 await 它收尾——core/agent.py 的取消链路里
        # client.interrupt() 最多兜底等 5 秒,真 await 会把 WS 主收环卡住那么久,
        # 打断到静音的体验就废了。收尾丢给后台任务,不阻塞后续消息处理。
        turn.task.cancel()
        asyncio.ensure_future(_swallow_cancelled(turn.task))
        pending = PendingTruncation(
            user_text=self._pending_user_text or "",
            emitted_sentences=turn.emitted_sentences,
            created_at=time.monotonic(),
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
            truncated = build_truncated_text(pending.emitted_sentences, played_count or 0)
            session.append(pending.user_text, truncated)
            # 这句话被真的打断了、没问完整/没答完,留给下一轮 _start_turn 拼接,
            # 别让它就这么消失。
            self._carried_text = pending.user_text
            await self._send("interrupted", truncated_at_seq=played_count or 0)
            print(f"[voice/ws] 打断确认 elapsed={elapsed_ms:.0f}ms played={played_count}", flush=True)
        else:
            await self._send("resumed")
            reason = "超时" if timed_out else "转写为空"
            print(f"[voice/ws] 打断回滚({reason}) elapsed={elapsed_ms:.0f}ms", flush=True)

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
                    if isinstance(ev, ToolStarted):
                        if not filler_sent and ev.parent_id is None:
                            filler_sent = True
                            audio_bytes = await tts.filler_audio(config.VOICE_TTS_VOICE)
                            await self._send(
                                "filler", text=tts.FILLER_PHRASE,
                                audio_b64=_b64(audio_bytes),
                            )
                    elif isinstance(ev, TextDelta):
                        if not self._speaking_announced:
                            self._speaking_announced = True
                            await self._set_state(_STATE_SPEAKING)
                        await self._send("text_delta", text=ev.text)
                        for sentence in splitter.feed(ev.text):
                            await self._emit_sentence(turn, seq, sentence)
                            seq += 1
                    elif isinstance(ev, Done):
                        for sentence in splitter.flush():
                            await self._emit_sentence(turn, seq, sentence)
                            seq += 1
                        reply = ev.reply
                        session.append(user_text, reply.text)
                        if reply.sdk_session_id:
                            session.set_resume(reply.sdk_session_id)
                        self._current_turn = None
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
            await self._set_state(_STATE_IDLE)
            await self._send("error", message=str(exc))

    async def _emit_sentence(self, turn: TurnContext, seq: int, sentence: str) -> None:
        turn.emitted_sentences.append(sentence)
        audio = await tts.synthesize(sentence, config.VOICE_TTS_VOICE)
        await self._send("sentence", seq=seq, text=sentence, audio_b64=_b64(audio))

    async def close(self) -> None:
        """WS 连接收尾:关掉上游、取消看门狗,不留后台任务。"""
        self._clear_capturing_watchdog()
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
            await sess._interrupt_current_turn()
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
