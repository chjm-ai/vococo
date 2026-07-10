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
from claude_hermes.core.agent import AgentReply, Done, TextDelta
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


def test_build_prompt_adds_hint_for_long_transcripts(monkeypatch):
    """2026-07-09 事故复盘:派活规则第2条完全靠模型临场判断,没有代码兜底——一次
    7步骤的复杂任务险些没被当成后台任务处理。加一道低成本信号:识别文本超过字数
    阈值就多塞一句强提示,短句(日常聊天/问答)不受影响。"""
    from claude_hermes import config

    monkeypatch.setattr(config, "VOICE_LONG_TASK_CHARS", 10)
    short = prompts.build_prompt("今天天气怎么样")
    long = prompts.build_prompt("帮我查一下这个项目的代码然后总结一下最近的开发进度")
    assert "【输入较长提示】" not in short
    assert "【输入较长提示】" in long
    assert "voice_dispatch_task 就不要因为想" in long


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
async def test_voice_config_reports_enabled(voice_db, monkeypatch):
    # P1 起 register_routes() 会顺带摸 tasks._DB(F11 重启自愈),故也要 voice_db 隔离,
    # 否则会碰真实的 config.DATA_DIR。
    # VOICE_OMNI_ENABLED 显式钉死:这个测试只关心 /voice/config 的响应形状,不该
    # 被跑测试这台机器上真实设了什么环境变量(比如线上开着这个开关)带偏。
    monkeypatch.setattr(config, "VOICE_OMNI_ENABLED", False)
    app = web.Application()
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/voice/config")
        assert resp.status == 200
        # P3 阶段二加了 omni_enabled(见 test_voice_omni_realtime.py 的专门测试),
        # 这里只确认默认关着,不测那个开关本身的行为。
        # vad_threshold/vad_silence_ms:Omni WebRTC 链路的 turn_detection 用,跟
        # config.py 默认值对齐(见 index.html 的 session-init)。silence_ms 用
        # Omni 专属的 VOICE_OMNI_VAD_SILENCE_MS,跟旧 ws.py 链路的值分开调。
        assert (await resp.json()) == {
            "enabled": True,
            "omni_enabled": False,
            "vad_threshold": config.VOICE_VAD_THRESHOLD,
            "vad_silence_ms": config.VOICE_OMNI_VAD_SILENCE_MS,
            "omni_voice": config.VOICE_OMNI_VOICE,
        }


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
async def test_voice_send_tts_false_skips_synthesis_but_emits_sentences(voice_db, monkeypatch):
    """Omni 出声模式:body 带 tts:false,服务端不调 TTS(合成被 mock 成必炸),
    sentence 事件照发(前端拿文本做逐句朗读切分),只是 audio_b64 为空。"""

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        yield TextDelta("好的,收到。")
        yield Done(
            AgentReply(text="好的,收到。", tool_calls=[], cost_usd=None, is_error=False, sdk_session_id=None)
        )

    monkeypatch.setattr(session, "run_turn", fake_run_turn)

    def exploding_synthesize(text, voice):
        raise AssertionError("tts:false 时不该走到合成")

    monkeypatch.setattr(tts, "synthesize", exploding_synthesize)

    app = web.Application()
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/voice/send", json={"text": "在吗", "tts": False})
        assert resp.status == 200
        body = (await resp.read()).decode("utf-8")

    events = _parse_sse(body)
    sentence_events = [d for e, d in events if e == "sentence"]
    assert len(sentence_events) == 1
    assert sentence_events[0]["text"] == "好的,收到。"
    assert sentence_events[0]["audio_b64"] is None
    assert [e for e, _ in events][-1] == "done"


@pytest.mark.anyio
async def test_voice_debug_logs_and_returns_ok(voice_db, capsys):
    """前端调试信号上报:落服务器日志(stdout),响应 ok——见 routes._handle_debug。"""
    app = web.Application()
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/voice/debug", json={"tag": "dc:session.created", "state": "idle"})
        assert resp.status == 200
        assert (await resp.json()) == {"ok": True}
    assert "dc:session.created" in capsys.readouterr().out


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


@pytest.mark.anyio
async def test_voice_send_preempts_running_http_turn(voice_db, monkeypatch):
    """新一句到达时,持锁的上一轮 HTTP turn 该被抢占取消,而不是 409 拒答
    (2026-07-10 真机:一轮长任务持锁,用户连问几句全被"上一轮还没处理完"弹回)。
    被抢占的半轮要落库,用户说过的话不能凭空消失。"""
    import asyncio

    started = asyncio.Event()
    calls: list[str] = []

    async def fake_run_turn(prompt_text, extra_mcp_servers=None):
        calls.append(prompt_text)
        if len(calls) == 1:
            started.set()
            await asyncio.sleep(3600)  # 第一轮:装作一直在跑,等着被抢占取消
        yield Done(
            AgentReply(text="第二句的回答", tool_calls=[], cost_usd=None, is_error=False)
        )

    monkeypatch.setattr(session, "run_turn", fake_run_turn)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice: _none_coro())

    app = web.Application()
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        task1 = asyncio.ensure_future(client.post("/voice/send", json={"text": "第一句"}))
        await asyncio.wait_for(started.wait(), timeout=5)

        resp2 = await client.post("/voice/send", json={"text": "第二句"})
        assert resp2.status == 200
        body = await resp2.text()
        assert "第二句的回答" in body

        r1 = await task1  # 旧请求 SSE 已 prepare(200),被服务端取消后正常收尾
        assert r1.status == 200

    history = session.load_history()
    firsts = [t for t in history if t.user == "第一句"]
    assert firsts and "打断" in firsts[0].assistant
    assert any(t.user == "第二句" for t in history)


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
