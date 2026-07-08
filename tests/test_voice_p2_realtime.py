"""P2 全双工的测试(见 docs/design/voice-companion/03-phase2-experience.md §2-C/2-D,
以及 03-phase2-实现记录.md"识别慢的根因排查"一节)。

2026-07-08 起识别/断句判断整体交给中继的 DashScope 实时语音 WS 上游连接
(见 voice/ws.py 的 _connect_upstream),不再是客户端 speech_start/speech_end
消息直接驱动状态机——测试相应地把上游连接换成可控的假对象(FakeUpstreamWs),
不连真实 DashScope,通过它推送 speech_started/completed 等事件来驱动状态机。

覆盖:打断截断纯函数(含防御性 clamp)、WS 状态机迁移(由上游事件驱动)、
PCM 转发给上游、打断确认/假打断回滚两条路径(含看门狗超时)、二次打断边界、
capturing 卡死兜底看门狗、与 /voice/send 的互斥。

不覆盖(得真机测):DashScope 服务本身的准确率/延迟、真实网络下的上游重连、
隧道下 WS 稳定性、真实 SDK 的取消行为。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_hermes import config
from claude_hermes.core.agent import AgentReply, Done, TextDelta
from claude_hermes.voice import executor, routes, session, tasks, tts, ws


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def voice_db(isolated, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(session, "_DB", None)
    monkeypatch.setattr(tasks, "_DB", None)
    monkeypatch.setattr(ws, "_active_ws", None)
    executor._running.clear()
    yield
    if session._DB is not None:
        session._DB.close()
        session._DB = None
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


async def _none_coro(*_a, **_k):
    return None


def _app() -> web.Application:
    app = web.Application()
    routes.register_routes(app)
    return app


class FakeUpstreamWs:
    """假的 DashScope 上游 WS 连接:记录发出去的帧,让测试按需推事件进来。"""

    def __init__(self):
        self.sent: list[dict] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send_str(self, s: str) -> None:
        self.sent.append(json.loads(s))

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._queue.put(None)

    def push_event(self, type_: str, **payload) -> None:
        data = json.dumps({"type": type_, **payload})
        self._queue.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=data))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


class FakeClientSession:
    async def close(self) -> None:
        pass


def _patch_upstream(monkeypatch, fake_ws: FakeUpstreamWs, connected: asyncio.Event):
    """把 ws._connect_upstream 换成返回固定的假连接,记录"已连上"的时机
    (测试要在这之后才能开始往 fake_ws 里推事件,不然会推早了没人收)。
    """

    async def fake_connect_upstream(sample_rate):
        connected.set()
        return FakeClientSession(), fake_ws

    monkeypatch.setattr(ws, "_connect_upstream", fake_connect_upstream)


async def _hello_and_wait_connected(wsc, connected: asyncio.Event) -> None:
    await wsc.send_json({"type": "hello", "sample_rate": 16000})
    await connected.wait()


async def _drain_until_sentence(wsc) -> list[dict]:
    """读消息直到收到一条 sentence(seq=0)——这是"这一句已经真正发出去"的
    确定性信号,WS 是单连接有序的,收到 sentence 时它之前的 state/transcript/
    text_delta 消息必然已经发生过,用它来同步"该模拟打断了"。
    """
    out = []
    while True:
        msg = await wsc.receive_json()
        out.append(msg)
        if msg["type"] == "sentence":
            return out


# ── build_truncated_text:纯函数,不需要 WS ────────────────────────────────
def test_build_truncated_text_keeps_only_played_sentences():
    out = ws.build_truncated_text(["第一句。", "第二句。", "第三句。"], 2)
    assert out == "第一句。第二句。(此处被用户打断)"


def test_build_truncated_text_zero_played_is_marker_only():
    out = ws.build_truncated_text(["第一句。"], 0)
    assert out == "(此处被用户打断)"


def test_build_truncated_text_clamps_overcount_defensively():
    # 客户端上报的播完数比服务端实际发出的还多(竞态/防御性场景),不能越界拼接。
    out = ws.build_truncated_text(["仅一句。"], 99)
    assert out == "仅一句。(此处被用户打断)"


def test_build_truncated_text_clamps_negative():
    out = ws.build_truncated_text(["一句。"], -1)
    assert out == "(此处被用户打断)"


# ── looks_like_self_echo:纯函数,回声兜底 ─────────────────────────────────
def test_looks_like_self_echo_detects_substring_of_ai_speech():
    said = ["好主意，今天天气适合走走，呼吸一下新鲜空气。"]
    assert ws.looks_like_self_echo("呼吸一下新鲜空气", said, 0.6) is True


def test_looks_like_self_echo_false_for_unrelated_text():
    said = ["好主意，今天天气适合走走，呼吸一下新鲜空气。"]
    assert ws.looks_like_self_echo("明天股票会涨吗", said, 0.6) is False


def test_looks_like_self_echo_false_when_nothing_was_said_yet():
    # 打断发生在 AI 一个字都还没说的时候(emitted_sentences 空)——没什么可比对的,
    # 不该被误判成回声(这也是真实一轮"完全没来得及说话就被打断"的常见场景)。
    assert ws.looks_like_self_echo("随便说点什么", [], 0.6) is False


def test_looks_like_self_echo_false_for_empty_transcript():
    assert ws.looks_like_self_echo("", ["随便说点什么。"], 0.6) is False


def test_looks_like_self_echo_respects_threshold():
    said = ["今天天气不错。"]
    # "今天" 只占 transcript 一半,containment 在 0.5 上下——阈值调高/调低应该
    # 分别判定为不是/是回声,验证阈值真的在生效,不是写死的常量。
    assert ws.looks_like_self_echo("今天出去玩", said, 0.9) is False
    assert ws.looks_like_self_echo("今天出去玩", said, 0.1) is True


# ── looks_like_filler_only:纯函数,连环打断兜底(真机原样例子) ───────────────
@pytest.mark.parametrize(
    "transcript",
    ["嗯。", "哦。", "是的。", "知道。", "稍等。", "好的", "对的。", "嗯", ""],
)
def test_looks_like_filler_only_true_for_known_fillers(transcript):
    assert ws.looks_like_filler_only(transcript) is True


@pytest.mark.parametrize(
    "transcript", ["今天天气怎么样？", "有台风吗？", "很奇特。", "哈喽哈喽，听到吗？"]
)
def test_looks_like_filler_only_false_for_real_content(transcript):
    assert ws.looks_like_filler_only(transcript) is False


# ── WS 状态机:正常一轮 ───────────────────────────────────────────────────
@pytest.mark.anyio
async def test_ws_normal_turn_flow_reaches_idle_and_appends_history(voice_db, monkeypatch):
    fake_ws = FakeUpstreamWs()
    connected = asyncio.Event()
    _patch_upstream(monkeypatch, fake_ws, connected)

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        yield TextDelta("好的,天气不错。")
        yield Done(AgentReply(text="好的,天气不错。", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "run_turn", fake_run_turn)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice: _none_coro())

    async with TestClient(TestServer(_app())) as client:
        async with client.ws_connect("/voice/ws") as wsc:
            await _hello_and_wait_connected(wsc, connected)

            fake_ws.push_event("input_audio_buffer.speech_started")
            assert (await wsc.receive_json()) == {"type": "state", "state": "capturing"}

            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed",
                transcript="今天天气怎么样",
            )

            states = []
            transcript = None
            done = None
            while done is None:
                msg = await wsc.receive_json()
                if msg["type"] == "state":
                    states.append(msg["state"])
                elif msg["type"] == "transcript":
                    transcript = msg["text"]
                elif msg["type"] == "done":
                    done = msg

            assert transcript == "今天天气怎么样"
            assert "thinking" in states and "speaking" in states
            assert states[-1] == "idle"
            assert done["full_text"] == "好的,天气不错。"

    history = session.load_history()
    assert history[-1].user == "今天天气怎么样"
    assert history[-1].assistant == "好的,天气不错。"


@pytest.mark.anyio
async def test_post_done_tail_echo_within_guard_window_is_discarded(voice_db, monkeypatch):
    """真机反馈:偶发录到 AI 自己说的话的尾音——AI 说完最后一句、状态已经
    翻回 idle,但客户端音箱可能还在播最后一两句,这段尾音漏回麦克风时不在
    "打断"场景里(self._pending 已是 None),原来的回声判断整个被跳过。
    现在应该在"刚说完话"的短时间窗口内,即便已经是 idle 状态,内容跟刚说过的
    话高度重合也要当回声discard,不能又起一轮新对话。
    """
    fake_ws = FakeUpstreamWs()
    connected = asyncio.Event()
    _patch_upstream(monkeypatch, fake_ws, connected)

    run_turn_calls = []

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        run_turn_calls.append(prompt_text)
        yield TextDelta("今天天气晴朗，适合出门散步。")
        yield Done(AgentReply(text="今天天气晴朗，适合出门散步。", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "run_turn", fake_run_turn)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice: _none_coro())

    async with TestClient(TestServer(_app())) as client:
        async with client.ws_connect("/voice/ws") as wsc:
            await _hello_and_wait_connected(wsc, connected)

            fake_ws.push_event("input_audio_buffer.speech_started")
            await wsc.receive_json()  # state: capturing
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed",
                transcript="今天天气怎么样",
            )
            done = None
            while done is None:
                msg = await wsc.receive_json()
                if msg["type"] == "done":
                    done = msg
            assert len(run_turn_calls) == 1

            # AI 刚说完("适合出门散步"跟刚才那句高度重合),状态已经是 idle,
            # 模拟音箱尾音漏回麦克风。
            fake_ws.push_event("input_audio_buffer.speech_started")
            await wsc.receive_json()  # state: capturing(会先短暂翻过去)
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed",
                transcript="适合出门散步",
            )
            # 判定为回声后同步发一条 state:idle,不需要等待更多消息。
            msg = await wsc.receive_json()
            assert msg == {"type": "state", "state": "idle"}

    # 尾音回声不该起新一轮:run_turn 仍然只被调用过一次。
    assert len(run_turn_calls) == 1
    history = session.load_history()
    assert not any(h.user == "适合出门散步" for h in history)


@pytest.mark.anyio
async def test_post_done_unrelated_utterance_still_starts_new_turn(voice_db, monkeypatch):
    """回声兜底是"内容比对",不是"这段时间内一律不理你"——刚说完话之后
    马上问一个完全不相关的新问题,得正常起新一轮,不能被误伤。
    """
    fake_ws = FakeUpstreamWs()
    connected = asyncio.Event()
    _patch_upstream(monkeypatch, fake_ws, connected)

    run_turn_calls = []

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        run_turn_calls.append(prompt_text)
        if len(run_turn_calls) == 1:
            yield TextDelta("今天天气晴朗，适合出门散步。")
            yield Done(AgentReply(text="今天天气晴朗，适合出门散步。", tool_calls=[], cost_usd=None, is_error=False))
        else:
            yield Done(AgentReply(text="末班车是23点。", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "run_turn", fake_run_turn)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice: _none_coro())

    async with TestClient(TestServer(_app())) as client:
        async with client.ws_connect("/voice/ws") as wsc:
            await _hello_and_wait_connected(wsc, connected)

            fake_ws.push_event("input_audio_buffer.speech_started")
            await wsc.receive_json()  # state: capturing
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed",
                transcript="今天天气怎么样",
            )
            done = None
            while done is None:
                msg = await wsc.receive_json()
                if msg["type"] == "done":
                    done = msg

            fake_ws.push_event("input_audio_buffer.speech_started")
            await wsc.receive_json()  # state: capturing
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed",
                transcript="帮我查一下地铁末班车几点",
            )
            done2 = None
            while done2 is None:
                msg = await wsc.receive_json()
                if msg["type"] == "done":
                    done2 = msg

    assert len(run_turn_calls) == 2
    assert done2["full_text"] == "末班车是23点。"


@pytest.mark.anyio
async def test_empty_completed_transcript_resets_to_idle_without_starting_turn(
    voice_db, monkeypatch
):
    fake_ws = FakeUpstreamWs()
    connected = asyncio.Event()
    _patch_upstream(monkeypatch, fake_ws, connected)

    called = {"n": 0}

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        called["n"] += 1
        yield Done(AgentReply(text="不该被调用", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "run_turn", fake_run_turn)

    async with TestClient(TestServer(_app())) as client:
        async with client.ws_connect("/voice/ws") as wsc:
            await _hello_and_wait_connected(wsc, connected)
            fake_ws.push_event("input_audio_buffer.speech_started")
            await wsc.receive_json()  # state: capturing
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed", transcript=""
            )
            msg = await wsc.receive_json()
            assert msg == {"type": "state", "state": "idle"}

    assert called["n"] == 0


@pytest.mark.anyio
async def test_audio_frames_relayed_to_upstream_as_append_events(voice_db, monkeypatch):
    fake_ws = FakeUpstreamWs()
    connected = asyncio.Event()
    _patch_upstream(monkeypatch, fake_ws, connected)

    async with TestClient(TestServer(_app())) as client:
        async with client.ws_connect("/voice/ws") as wsc:
            await _hello_and_wait_connected(wsc, connected)
            await wsc.send_bytes(b"\x01\x02\x03\x04")
            # 二进制帧转发是 fire-and-forget,给一点时间跑到 fake_ws.sent 里。
            for _ in range(50):
                if fake_ws.sent:
                    break
                await asyncio.sleep(0.02)

    assert len(fake_ws.sent) == 1
    assert fake_ws.sent[0]["type"] == "input_audio_buffer.append"
    import base64
    assert base64.b64decode(fake_ws.sent[0]["audio"]) == b"\x01\x02\x03\x04"


# ── 打断:两阶段提交 ──────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_barge_in_interrupt_truncates_and_commits_on_confirmed_new_speech(
    voice_db, monkeypatch
):
    fake_ws = FakeUpstreamWs()
    connected = asyncio.Event()
    _patch_upstream(monkeypatch, fake_ws, connected)

    started = asyncio.Event()
    gate = asyncio.Event()

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        # 打断后新一轮的 prompt 会把"第一句话"拼在前面一起问模型(见
        # _carried_text),所以不能再靠"是否包含第一句话"来区分两轮——改成
        # 靠只在第二轮才出现的那句话判断。
        if "打断后我说的话" not in prompt_text:
            yield TextDelta("这是被打断前说的第一句。")
            started.set()
            await gate.wait()
            yield TextDelta("不该说到这里")
            yield Done(AgentReply(text="不该说到这里", tool_calls=[], cost_usd=None, is_error=False))
        else:
            yield Done(AgentReply(text="打断后的新回复", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "run_turn", fake_run_turn)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice: _none_coro())

    async with TestClient(TestServer(_app())) as client:
        async with client.ws_connect("/voice/ws") as wsc:
            await _hello_and_wait_connected(wsc, connected)

            fake_ws.push_event("input_audio_buffer.speech_started")
            await wsc.receive_json()  # state: capturing
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed", transcript="第一句话"
            )
            # 先把第一轮已经发出的 sentence 读掉,确认"已经说了一句、正卡在中途"
            # 再打断——不然队列里堆着的旧消息会被误当成打断后的响应读到。
            await _drain_until_sentence(wsc)
            await started.wait()

            # 客户端上报"播完了 1 句",服务端记住这个值,等 completed 时用它截断。
            await wsc.send_json({"type": "played_progress", "played_sentences": 1})
            fake_ws.push_event("input_audio_buffer.speech_started")  # 打断
            msg = await wsc.receive_json()
            assert msg == {"type": "state", "state": "capturing"}

            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed",
                transcript="打断后我说的话",
            )

            events = []
            done = None
            while done is None:
                msg = await wsc.receive_json()
                events.append(msg)
                if msg["type"] == "done":
                    done = msg

            types = [e["type"] for e in events]
            assert "interrupted" in types
            assert done["full_text"] == "打断后的新回复"

    history = session.load_history()
    # 第一轮只落了被截断的那句 + 标记,不是完整未打断的文本。
    interrupted_turn = next(h for h in history if h.user == "第一句话")
    assert interrupted_turn.assistant == "这是被打断前说的第一句。(此处被用户打断)"
    # 第二轮的 user_text 是"被打断的第一句"拼上"新说的话"——不能让第一句
    # 问的内容因为被打断就彻底消失、只回复最后一句。
    new_turn = next(h for h in history if h.user == "第一句话打断后我说的话")
    assert new_turn.assistant == "打断后的新回复"

    gate.set()  # 收尾:放开被取消前挂起的协程,避免测试进程留下悬空 task


@pytest.mark.anyio
async def test_cascading_real_interrupts_combine_into_one_prompt(voice_db, monkeypatch):
    """真机原样场景(见 03-phase2-实现记录.md):"今天天气怎么样"被打断,追问
    "有台风吗"又被打断——两句都是真实内容(不是语气词),之前的行为是只回复
    最后一句、前面被打断的问题石沉大海。现在两句都要拼进最终问模型的那句话里,
    一次性回复,不能丢。
    """
    fake_ws = FakeUpstreamWs()
    connected = asyncio.Event()
    _patch_upstream(monkeypatch, fake_ws, connected)

    gate = asyncio.Event()
    run_turn_calls = []

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        run_turn_calls.append(prompt_text)
        if len(run_turn_calls) < 3:
            yield TextDelta("正在回答。")
            await gate.wait()  # 一直不会被 set,靠打断 cancel 掉,不会真跑到 Done
            yield Done(AgentReply(text="不该跑到这", tool_calls=[], cost_usd=None, is_error=False))
        else:
            yield Done(AgentReply(text="天气晴朗,没有台风", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "run_turn", fake_run_turn)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice: _none_coro())

    async with TestClient(TestServer(_app())) as client:
        async with client.ws_connect("/voice/ws") as wsc:
            await _hello_and_wait_connected(wsc, connected)

            # 第一轮:"今天天气怎么样？"
            fake_ws.push_event("input_audio_buffer.speech_started")
            await wsc.receive_json()  # state: capturing
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed",
                transcript="今天天气怎么样？",
            )
            await _drain_until_sentence(wsc)

            # 打断:"有台风吗？"——同样是真实内容,不是语气词。
            fake_ws.push_event("input_audio_buffer.speech_started")
            await wsc.receive_json()  # state: capturing
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed",
                transcript="有台风吗？",
            )
            await _drain_until_sentence(wsc)

            # 再打断一次,这次不再追问,让第三轮跑完。
            fake_ws.push_event("input_audio_buffer.speech_started")
            await wsc.receive_json()  # state: capturing
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed",
                transcript="谢谢。",
            )

            done = None
            while done is None:
                msg = await wsc.receive_json()
                if msg["type"] == "done":
                    done = msg

    assert len(run_turn_calls) == 3
    # 最终真正跑完的那一轮,prompt 里三句话都在,不是只有最后一句"谢谢"。
    final_prompt = run_turn_calls[-1]
    assert "今天天气怎么样？" in final_prompt
    assert "有台风吗？" in final_prompt
    assert "谢谢。" in final_prompt
    assert done["full_text"] == "天气晴朗,没有台风"

    history = session.load_history()
    combined_turn = next(
        h for h in history if h.user == "今天天气怎么样？有台风吗？谢谢。"
    )
    assert combined_turn.assistant == "天气晴朗,没有台风"


@pytest.mark.anyio
async def test_false_positive_interrupt_rolls_back_without_appending(voice_db, monkeypatch):
    fake_ws = FakeUpstreamWs()
    connected = asyncio.Event()
    _patch_upstream(monkeypatch, fake_ws, connected)

    started = asyncio.Event()
    gate = asyncio.Event()

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        yield TextDelta("正常回答的第一句。")
        started.set()
        await gate.wait()
        yield Done(AgentReply(text="不该跑到这", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "run_turn", fake_run_turn)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice: _none_coro())

    async with TestClient(TestServer(_app())) as client:
        async with client.ws_connect("/voice/ws") as wsc:
            await _hello_and_wait_connected(wsc, connected)

            fake_ws.push_event("input_audio_buffer.speech_started")
            await wsc.receive_json()  # state: capturing
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed",
                transcript="正常回答的第一句",
            )
            await _drain_until_sentence(wsc)
            await started.wait()

            fake_ws.push_event("input_audio_buffer.speech_started")  # 疑似打断
            msg = await wsc.receive_json()
            assert msg == {"type": "state", "state": "capturing"}

            # 转写为空 → 判定误触发
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed", transcript=""
            )

            events = []
            resumed = None
            while resumed is None:
                msg = await wsc.receive_json()
                events.append(msg)
                if msg["type"] == "resumed":
                    resumed = msg

    history = session.load_history()
    assert not any("被用户打断" in h.assistant for h in history)

    gate.set()


@pytest.mark.anyio
async def test_self_echo_interrupt_rolls_back_without_starting_new_turn(voice_db, monkeypatch):
    """AI 自己的声音漏回麦克风,被 DashScope 当成用户开口识别出来——转写内容
    跟 AI 刚说的话高度重合,该判定成回声误触发,不能真拿这段话去问模型。
    """
    fake_ws = FakeUpstreamWs()
    connected = asyncio.Event()
    _patch_upstream(monkeypatch, fake_ws, connected)

    started = asyncio.Event()
    gate = asyncio.Event()
    run_turn_calls = []

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        run_turn_calls.append(prompt_text)
        yield TextDelta("好主意，今天天气适合走走，呼吸一下新鲜空气。")
        started.set()
        await gate.wait()
        yield Done(AgentReply(text="不该跑到这", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "run_turn", fake_run_turn)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice: _none_coro())

    async with TestClient(TestServer(_app())) as client:
        async with client.ws_connect("/voice/ws") as wsc:
            await _hello_and_wait_connected(wsc, connected)

            fake_ws.push_event("input_audio_buffer.speech_started")
            await wsc.receive_json()  # state: capturing
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed",
                transcript="今天出去玩玩吧",
            )
            await _drain_until_sentence(wsc)
            await started.wait()
            assert len(run_turn_calls) == 1

            fake_ws.push_event("input_audio_buffer.speech_started")  # AI 自己的声音漏回麦克风
            await wsc.receive_json()  # state: capturing

            # 转写内容是 AI 刚说的那句话的一个片段——回声,不是真开口。
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed",
                transcript="呼吸一下新鲜空气",
            )

            events = []
            resumed = None
            while resumed is None:
                msg = await wsc.receive_json()
                events.append(msg)
                if msg["type"] == "resumed":
                    resumed = msg

    # 回声判定生效:没有为它新开一轮(run_turn 只被调用过一次,就是最初那句)。
    assert len(run_turn_calls) == 1
    history = session.load_history()
    assert not any("被用户打断" in h.assistant for h in history)
    assert not any(h.user == "呼吸一下新鲜空气" for h in history)

    gate.set()


@pytest.mark.anyio
async def test_filler_word_interrupt_does_not_cascade_or_start_new_turn(voice_db, monkeypatch):
    """真机原样场景(见 03-phase2-实现记录.md"连环打断"一节):AI 正在回答时,
    环境噪音被识别成"稍等。"这类语气词——不该拿它去打断+开新一轮,不然会
    连环打断,后面真正的问题反而被吃掉。
    """
    fake_ws = FakeUpstreamWs()
    connected = asyncio.Event()
    _patch_upstream(monkeypatch, fake_ws, connected)

    started = asyncio.Event()
    gate = asyncio.Event()
    run_turn_calls = []

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        run_turn_calls.append(prompt_text)
        yield TextDelta("今天天气晴朗，适合出门。")
        started.set()
        await gate.wait()
        yield Done(AgentReply(text="不该跑到这", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "run_turn", fake_run_turn)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice: _none_coro())

    async with TestClient(TestServer(_app())) as client:
        async with client.ws_connect("/voice/ws") as wsc:
            await _hello_and_wait_connected(wsc, connected)

            fake_ws.push_event("input_audio_buffer.speech_started")
            await wsc.receive_json()  # state: capturing
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed",
                transcript="今天天气怎么样？",
            )
            await _drain_until_sentence(wsc)
            await started.wait()
            assert len(run_turn_calls) == 1

            fake_ws.push_event("input_audio_buffer.speech_started")  # 环境噪音
            await wsc.receive_json()  # state: capturing

            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed", transcript="稍等。"
            )

            events = []
            resumed = None
            while resumed is None:
                msg = await wsc.receive_json()
                events.append(msg)
                if msg["type"] == "resumed":
                    resumed = msg

    # 语气词判定生效:没有为"稍等。"新开一轮(run_turn 只被调用过一次)。
    assert len(run_turn_calls) == 1
    history = session.load_history()
    assert not any(h.user == "稍等" for h in history)
    assert not any(h.user == "稍等。" for h in history)

    gate.set()


@pytest.mark.anyio
async def test_false_positive_rollback_via_watchdog_timeout(voice_db, monkeypatch):
    """打断后上游迟迟不给 completed(用户没再说话):超时后也要主动撤销、发 resumed。"""
    monkeypatch.setattr(config, "VOICE_FALSE_POSITIVE_TIMEOUT_MS", 50)
    fake_ws = FakeUpstreamWs()
    connected = asyncio.Event()
    _patch_upstream(monkeypatch, fake_ws, connected)

    started = asyncio.Event()
    gate = asyncio.Event()

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        yield TextDelta("第一句。")
        started.set()
        await gate.wait()
        yield Done(AgentReply(text="不该跑到这", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "run_turn", fake_run_turn)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice: _none_coro())

    async with TestClient(TestServer(_app())) as client:
        async with client.ws_connect("/voice/ws") as wsc:
            await _hello_and_wait_connected(wsc, connected)

            fake_ws.push_event("input_audio_buffer.speech_started")
            await wsc.receive_json()  # state: capturing
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed", transcript="第一句"
            )
            await _drain_until_sentence(wsc)
            await started.wait()

            fake_ws.push_event("input_audio_buffer.speech_started")  # 打断,之后再也不给 completed
            msg = await wsc.receive_json()
            assert msg == {"type": "state", "state": "capturing"}

            msg = await wsc.receive_json()  # 看门狗超时后应主动补发 resumed
            assert msg == {"type": "resumed"}

    gate.set()


# ── 边界情况 ──────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_capturing_stall_watchdog_resets_to_idle(voice_db, monkeypatch):
    """idle → capturing 后上游一直没有任何后续事件(非打断场景,没有 pending
    可用的看门狗覆盖不到):兜底的 capturing 专用看门狗要顶上,不能永远卡住。
    """
    monkeypatch.setattr(ws, "_CAPTURING_STALL_MS", 50)
    fake_ws = FakeUpstreamWs()
    connected = asyncio.Event()
    _patch_upstream(monkeypatch, fake_ws, connected)

    async with TestClient(TestServer(_app())) as client:
        async with client.ws_connect("/voice/ws") as wsc:
            await _hello_and_wait_connected(wsc, connected)
            fake_ws.push_event("input_audio_buffer.speech_started")
            await wsc.receive_json()  # state: capturing

            msg = await wsc.receive_json()
            assert msg == {"type": "state", "state": "idle"}
            msg = await wsc.receive_json()
            assert msg["type"] == "error"


@pytest.mark.anyio
async def test_upstream_connect_failure_reports_error(voice_db, monkeypatch):
    async def fake_connect_upstream(sample_rate):
        raise RuntimeError("boom")

    monkeypatch.setattr(ws, "_connect_upstream", fake_connect_upstream)

    async with TestClient(TestServer(_app())) as client:
        async with client.ws_connect("/voice/ws") as wsc:
            await wsc.send_json({"type": "hello", "sample_rate": 16000})
            msg = await wsc.receive_json()
            assert msg["type"] == "error"


# ── 与 /voice/send 互斥 ───────────────────────────────────────────────────
@pytest.mark.anyio
async def test_ws_turn_blocks_concurrent_voice_send(voice_db, monkeypatch):
    fake_ws = FakeUpstreamWs()
    connected = asyncio.Event()
    _patch_upstream(monkeypatch, fake_ws, connected)

    started = asyncio.Event()
    gate = asyncio.Event()

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        started.set()
        await gate.wait()
        yield Done(AgentReply(text="ok", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "run_turn", fake_run_turn)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice: _none_coro())

    async with TestClient(TestServer(_app())) as client:
        async with client.ws_connect("/voice/ws") as wsc:
            await _hello_and_wait_connected(wsc, connected)
            fake_ws.push_event("input_audio_buffer.speech_started")
            await wsc.receive_json()  # state: capturing
            fake_ws.push_event(
                "conversation.item.input_audio_transcription.completed", transcript="语音里说的话"
            )
            await started.wait()  # 此时 WS 那一轮已经拿到 routes._lock

            resp = await client.post("/voice/send", json={"text": "文字兜底输入"})
            assert resp.status == 409

            gate.set()
