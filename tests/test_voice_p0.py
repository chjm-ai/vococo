"""P0 语音入口/对话 MVP 的测试(见 docs/design/voice-companion/01-phase0-voice-entry.md §5)。

覆盖:句子切分器(标点/【屏幕】截断/超长兜底)、指令块拼接与落库剥离、
/voice/config 开关行为、/voice/send 的 SSE 事件序列(mock stream_turn 与 edge-tts)。
"""
from __future__ import annotations

import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_hermes import config
from claude_hermes.core.agent import AgentReply, Done, TextDelta, ToolStarted
from claude_hermes.memory import session_store
from claude_hermes.voice import executor, prompts, routes, session, stt, tasks, tts


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def voice_db(isolated, monkeypatch):
    """独立 voice.db,每个用例互不污染(仿 conftest 的 isolated 对 session_store 的处理)。

    显式清空 WEB_AUTH_TOKEN:测试不该依赖"本机是否恰好有 .env 配了口令"这种环境状态,
    否则本地开发机一旦配了真口令,_guard() 就会把测试请求当成未授权拦掉。

    P1 起 register_routes() 会顺带触发 executor.heal_after_restart()(F11),它会摸
    tasks._DB——同样要重置连接单例,否则会复用上一个用例已被 tmp_path 清理的旧连接。
    session 模块自 2026-07-08 起委托 session_store 存储,不再有自己的 _DB——`isolated`
    fixture 已经重置了 session_store._DB,这里不用再管。
    """
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")
    monkeypatch.setattr(tasks, "_DB", None)
    executor._running.clear()
    yield
    if tasks._DB is not None:
        tasks._DB.close()
        tasks._DB = None


# ── 句子切分器 ────────────────────────────────────────────────────────────
def test_sentence_splitter_splits_on_punctuation():
    sp = tts.SentenceSplitter()
    assert sp.feed("你好。") == ["你好。"]
    assert sp.feed("在吗") == []  # 没有标点收尾,先攒着
    assert sp.feed("?") == ["在吗?"]


def test_sentence_splitter_force_splits_overlong_buffer():
    sp = tts.SentenceSplitter()
    long_text = "字" * 70  # 超过 _MAX_BUFFER(60),且没有任何标点
    out = sp.feed(long_text)
    assert out == [long_text]


def test_sentence_splitter_flush_emits_residual_tail():
    sp = tts.SentenceSplitter()
    sp.feed("说到一半")
    assert sp.flush() == ["说到一半"]
    assert sp.flush() == []  # flush 后清空,再调不重复吐


def test_sentence_splitter_drops_punctuation_only_fragments():
    """2026-07-09 真机实测捕获:打断/衔接产生的残留 buffer 有时只剩一个"。",
    DashScope 对这种纯标点输入明确 400(InvalidParameter),源头过滤掉。"""
    sp = tts.SentenceSplitter()
    assert sp.feed("。") == []
    sp2 = tts.SentenceSplitter()
    sp2.feed("   ")
    assert sp2.flush() == []


def test_sentence_splitter_has_no_screen_only_suppression():
    """2026-07-07 去掉了"【屏幕】后的内容不朗读"这条机制——真机实测发现模型一遇到
    "内容有点多"就靠它把实质内容全丢进屏幕,用户根本不看屏幕,等于没回答。
    现在模型输出的一切文字都当普通文本正常朗读,哪怕字面上出现"【屏幕】"这几个字。"""
    sp = tts.SentenceSplitter()
    out = sp.feed("找到了一条笔记。【屏幕】完整内容在这里。")
    assert out == ["找到了一条笔记。", "【屏幕】完整内容在这里。"]


# ── 指令块拼接 ────────────────────────────────────────────────────────────
def test_build_prompt_wraps_instruction_block():
    out = prompts.build_prompt("明天天气怎么样")
    assert "【语音模式】" in out
    assert out.strip().endswith("用户说:明天天气怎么样")


def test_build_prompt_covers_delayed_reminders_within_timeout(monkeypatch):
    """2026-07-07 真机踩坑:AI 对"2分钟后提醒我"这类请求嘴上答应却没真调工具。
    修法是教它延时提醒也走 voice_dispatch_task(sleep 后回复即可,任务终态会
    自动触发通知),并写清楚仅限任务超时时长以内。"""
    from claude_hermes import config

    monkeypatch.setattr(config, "VOICE_TASK_TIMEOUT_MIN", 30)
    out = prompts.build_prompt("过2分钟提醒我")
    assert "延时提醒" in out
    assert "30 分钟以内" in out


def test_build_prompt_forbids_claiming_success_without_tool_call():
    out = prompts.build_prompt("随便说点什么")
    assert "没有真的调用 voice_dispatch_task" in out


def test_build_prompt_requires_clarifying_ambiguous_task_before_dispatch():
    """2026-07-07 用户反馈:派活前该先把笼统的需求问清楚,不能自己脑补一个大任务就派出去。"""
    out = prompts.build_prompt("帮我查一下世界杯赛程")
    assert "都跟用户对齐" in out
    assert "脑补一个" in out


def test_build_prompt_requires_confirming_self_designed_breakdown_before_dispatch():
    """2026-07-07 用户反馈第二层:方向明确但模型自己设计了拆解方案(比如把"分析梅西"
    拆成俱乐部/国家队/荣誉等好几块)时,也要先说方案、等确认,不能连怎么拆都替用户定了。"""
    out = prompts.build_prompt("帮我分析一下梅西的职业生涯")
    assert "这个拆解方案" in out
    assert "不是用户说的" in out


# ── 存储迁移:主语音会话落进共享 session_store,不再有独立 voice.db ────────────
def test_voice_session_key_is_shared_store_prefix():
    assert session.SESSION_KEY == "voice-chat:main"


def test_voice_session_append_and_history_use_shared_session_store(voice_db):
    session.append("你好", "你好呀")
    assert session.load_history() == session_store.load_recent("voice-chat:main")
    assert session.load_history()[-1].user == "你好"
    assert session.load_history()[-1].assistant == "你好呀"


def test_voice_session_resume_id_round_trips_through_session_store(voice_db):
    assert session.get_resume() is None
    session.set_resume("sdk-sess-123")
    assert session.get_resume() == "sdk-sess-123"
    assert session_store.get_sdk_session_id("voice-chat:main") == "sdk-sess-123"


# ── /voice/config 开关 ───────────────────────────────────────────────────
@pytest.mark.anyio
async def test_voice_config_reports_enabled(voice_db):
    # P1 起 register_routes() 会顺带摸 tasks._DB(F11 重启自愈),故也要 voice_db 隔离,
    # 否则会碰真实的 config.DATA_DIR。
    app = web.Application()
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/voice/config")
        assert resp.status == 200
        assert (await resp.json()) == {"enabled": True}


# ── /voice/send SSE 事件序列 ─────────────────────────────────────────────
@pytest.mark.anyio
async def test_voice_send_streams_text_sentence_done_and_strips_instruction_on_store(
    voice_db, monkeypatch
):
    """mock stream_turn 吐两句话再收工;验证 SSE 事件、落库存的是原始 user_text
    (而不是拼了指令块的那一坨),且 assistant_text 是模型原始回复。"""

    captured_prompt = {}

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        captured_prompt["text"] = prompt_text
        yield TextDelta("好的,")
        yield TextDelta("明天多云二十八度。")
        yield Done(
            AgentReply(
                text="好的,明天多云二十八度。",
                tool_calls=[],
                cost_usd=None,
                is_error=False,
                sdk_session_id="sdk-123",
            )
        )

    monkeypatch.setattr(session, "run_turn", fake_run_turn)

    async def fake_synthesize(text, voice):
        return b"FAKE-MP3-BYTES"

    monkeypatch.setattr(tts, "synthesize", fake_synthesize)

    app = web.Application()
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/voice/send", json={"text": "明天天气怎么样"})
        assert resp.status == 200
        body = (await resp.read()).decode("utf-8")

    events = _parse_sse(body)
    kinds = [e for e, _ in events]
    assert kinds.count("text") == 2
    assert kinds[-1] == "done"
    sentence_events = [d for e, d in events if e == "sentence"]
    assert len(sentence_events) == 1
    assert sentence_events[0]["text"] == "好的,明天多云二十八度。"
    assert sentence_events[0]["audio_b64"]  # 有音频(base64 非空)

    # 拼进 stream_turn 的是指令块 + 原文;落库的是剥离指令块后的原文
    assert "明天天气怎么样" in captured_prompt["text"]
    assert "【语音模式】" in captured_prompt["text"]
    history = session.load_history()
    assert len(history) == 1
    assert history[0].user == "明天天气怎么样"
    assert history[0].assistant == "好的,明天多云二十八度。"
    assert session.get_resume() == "sdk-123"


@pytest.mark.anyio
async def test_voice_send_plays_filler_once_on_first_top_level_tool(voice_db, monkeypatch):
    """本轮第一次顶层工具调用要垫一句"稍等";子代理内部的工具(parent_id 非空)
    和同一轮里后续的工具调用都不应该再触发第二次。"""

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        yield ToolStarted("Read", tool_id="t0", parent_id="agent-1")  # 子代理内部,不算
        yield ToolStarted("Bash", tool_id="t1", parent_id=None)  # 第一次顶层,触发垫话
        yield ToolStarted("Read", tool_id="t2", parent_id=None)  # 第二次顶层,不应重复垫话
        yield TextDelta("查完了,是这样的。")
        yield Done(AgentReply(text="查完了,是这样的。", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "run_turn", fake_run_turn)

    filler_calls = []

    async def fake_filler_audio(voice):
        filler_calls.append(voice)
        return b"FILLER-BYTES"

    monkeypatch.setattr(tts, "filler_audio", fake_filler_audio)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice: _bytes_coro(b"REPLY-BYTES"))

    app = web.Application()
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/voice/send", json={"text": "帮我查一下"})
        body = (await resp.read()).decode("utf-8")

    assert len(filler_calls) == 1  # 只垫一次
    events = _parse_sse(body)
    filler_events = [d for e, d in events if e == "filler"]
    assert len(filler_events) == 1  # event:filler 是独立事件,和正式回复的 event:sentence 分开
    assert filler_events[0]["text"] == tts.FILLER_PHRASE
    assert filler_events[0]["audio_b64"]
    sentence_events = [d for e, d in events if e == "sentence"]
    assert sentence_events == [{"text": "查完了,是这样的。", "audio_b64": "UkVQTFktQllURVM="}]
    # filler 必须先于正式回复,前端才能先把"稍等"念出来再接正文
    assert [e for e, _ in events].index("filler") < [e for e, _ in events].index("sentence")


async def _bytes_coro(b):
    return b


@pytest.mark.anyio
async def test_voice_send_accepts_audio_and_emits_transcript_first(voice_db, monkeypatch):
    """合并了 /voice/stt 的路径:直接传 multipart 音频,服务端转写完接着续跑同一条
    SSE 流,event:transcript 必须先于 text/sentence/done——省掉客户端那一整趟
    「先拿文字再单独发一次」的网络往返。"""

    async def fake_transcribe(audio, filename, ctype):
        assert audio == b"FAKE-AUDIO-BYTES"
        return "识别出来的话", ""

    monkeypatch.setattr(stt, "transcribe", fake_transcribe)

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        assert "识别出来的话" in prompt_text
        yield TextDelta("收到。")
        yield Done(AgentReply(text="收到。", tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(session, "run_turn", fake_run_turn)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice: _none_coro())

    app = web.Application()
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        form = aiohttp.FormData()
        form.add_field("audio", b"FAKE-AUDIO-BYTES", filename="voice.webm", content_type="audio/webm")
        resp = await client.post("/voice/send", data=form)
        assert resp.status == 200
        body = (await resp.read()).decode("utf-8")

    events = _parse_sse(body)
    assert events[0] == ("transcript", {"text": "识别出来的话"})
    assert [e for e, _ in events[1:]].count("text") == 1
    assert events[-1][0] == "done"
    history = session.load_history()
    assert history[0].user == "识别出来的话"


@pytest.mark.anyio
async def test_voice_send_rejects_concurrent_turn(voice_db, monkeypatch):
    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        yield Done(
            AgentReply(text="ok", tool_calls=[], cost_usd=None, is_error=False)
        )

    monkeypatch.setattr(session, "run_turn", fake_run_turn)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice: _none_coro())

    app = web.Application()
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        routes._lock._locked = True  # 模拟上一轮还在跑(直接摆弄锁状态,免去真并发时序)
        try:
            resp = await client.post("/voice/send", json={"text": "在吗"})
            assert resp.status == 409
        finally:
            routes._lock._locked = False


async def _none_coro():
    return None


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event, data = "message", None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if data is not None:
            out.append((event, json.loads(data)))
    return out
