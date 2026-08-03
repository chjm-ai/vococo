"""会议录音转写(voice/stt.py):时长分流、paraformer 结果聚合、失败降级。

真实 DashScope 调用不在这测(要 key+花钱),网络层全部 mock;ffprobe 探测用
真实 ffmpeg 造音频验证(本机装有 ffmpeg)。
"""
from __future__ import annotations

import json
import subprocess

import pytest

from claude_hermes.voice import stt


def _transcripts(sentences):
    """sentences: [(speaker_id, text), ...] → paraformer 结果 JSON 的 transcripts。"""
    return [{"channel_id": "0", "text": "x", "sentences": [
        {"speaker_id": sid, "text": t} for sid, t in sentences
    ]}]


async def _noop_cleanup(text: str) -> str:
    return text


@pytest.mark.anyio
async def test_format_meeting_groups_by_speaker(monkeypatch):
    """连续同一人合并、编号按首次开口顺序、跳号 speaker_id 也连续编号。"""
    trans = _transcripts([
        (2, "首先我们过一下进度。"),
        (2, "上周完成了两件事。"),
        (7, "好的收到。"),
        (2, "那接下来看预算。"),
        (7, "没问题。"),
    ])
    monkeypatch.setattr(stt, "_cleanup", _noop_cleanup)  # 不触发外网清洗
    out = await stt._format_meeting(trans)
    assert out == (
        "[说话人1] 首先我们过一下进度。上周完成了两件事。\n"
        "[说话人2] 好的收到。\n"
        "[说话人1] 那接下来看预算。\n"
        "[说话人2] 没问题。"
    )


@pytest.mark.anyio
async def test_format_meeting_no_speaker_uses_plain_text(monkeypatch):
    """没有 sentence 级输出(分离未生效)时退回整段全文。"""
    trans = [{"channel_id": "0", "text": "整段转写文本", "sentences": []}]
    monkeypatch.setattr(stt, "_cleanup", _noop_cleanup)
    assert await stt._format_meeting(trans) == "整段转写文本"


@pytest.mark.anyio
async def test_attachment_routes_by_duration(isolated, monkeypatch):
    """时长 ≥ 40 分钟走会议路径,否则走 qwen3-asr-flash;探测失败不阻塞。"""
    from claude_hermes import config

    monkeypatch.setattr(config, "AUDIO_DIR", isolated / "audio")
    monkeypatch.setattr(config, "PUBLISHED_DIR", isolated / "published")
    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "k")
    durations = [3600.0]

    async def fake_probe(audio, filename):
        return durations[0]

    async def fake_meeting(audio, filename, ctype, *, host):
        return ("[说话人1] 会议内容", "")

    async def fake_short(audio, filename, ctype, *, timeout_sec=180):
        return ("短录音", "")

    monkeypatch.setattr(stt, "probe_duration", fake_probe)
    monkeypatch.setattr(stt, "transcribe_meeting", fake_meeting)
    monkeypatch.setattr(stt, "transcribe", fake_short)

    durations[0] = 3600.0
    text, _ = await stt.transcribe_attachment(b"x", "m.m4a", "audio/m4a", host="h")
    assert text == "[说话人1] 会议内容"

    durations[0] = 60.0
    text, _ = await stt.transcribe_attachment(b"x", "m.m4a", "audio/m4a", host="h")
    assert text == "短录音"

    durations[0] = None  # ffprobe 失败
    text, _ = await stt.transcribe_attachment(b"x", "m.m4a", "audio/m4a", host="h")
    assert text == "短录音"


@pytest.mark.anyio
async def test_meeting_success_cleans_public_file(isolated, monkeypatch):
    """成功路径:提交→轮询 SUCCEEDED→格式化返回,公网临时文件与本地残留都清掉。"""
    import aiohttp

    from claude_hermes import config

    monkeypatch.setattr(config, "AUDIO_DIR", isolated / "audio")
    monkeypatch.setattr(config, "PUBLISHED_DIR", isolated / "published")
    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "k")
    monkeypatch.setattr(stt, "_cleanup_stale_meeting_files", lambda: None)

    async def fake_channels(p):
        return 1  # 单声道,不转码

    monkeypatch.setattr(stt, "_meeting_channels", fake_channels)

    async def fake_format(trans):
        return "[说话人1] 你好"

    monkeypatch.setattr(stt, "_format_meeting", fake_format)

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _SubmitResp(_Resp):
        async def text(self):
            return json.dumps({"output": {"task_id": "t1"}})

    class _QueryResp(_Resp):
        async def text(self):
            return json.dumps({
                "output": {
                    "task_status": "SUCCEEDED",
                    "results": [{"transcription_url": "https://result.oss/1.json"}],
                }
            })

    class _DownloadResp(_Resp):
        async def text(self):
            # 真实结果 JSON:transcripts → sentences(带 speaker_id)
            return json.dumps({"transcripts": [{
                "channel_id": "0",
                "text": "你好",
                "sentences": [
                    {"speaker_id": 0, "text": "你好"},
                    {"speaker_id": 1, "text": "再见"},
                ],
            }]})

    class _Sess:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, **k):
            return _SubmitResp()

        def get(self, url, **k):
            return _DownloadResp() if "result.oss" in url else _QueryResp()

    monkeypatch.setattr(stt.aiohttp, "ClientSession", _Sess)
    text, err = await stt.transcribe_meeting(
        b"audio-bytes", "m.m4a", "audio/m4a", host="wazir.example.com"
    )
    assert text == "[说话人1] 你好"
    assert list(config.PUBLISHED_DIR.iterdir()) == []  # 公网临时文件已删
    # 本地探测/转码残留清空(.meeting 空目录留着无害)
    tmp_dir = config.AUDIO_DIR / ".meeting"
    assert not tmp_dir.exists() or list(tmp_dir.iterdir()) == []


@pytest.mark.anyio
async def test_meeting_falls_back_on_submit_error(isolated, monkeypatch):
    """提交任务网络失败 → 降级 qwen3-asr-flash,不把失败抛给用户,临时文件照删。"""
    import aiohttp

    from claude_hermes import config

    monkeypatch.setattr(config, "AUDIO_DIR", isolated / "audio")
    monkeypatch.setattr(config, "PUBLISHED_DIR", isolated / "published")
    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "k")
    monkeypatch.setattr(stt, "_cleanup_stale_meeting_files", lambda: None)

    async def fake_channels(p):
        return 1

    monkeypatch.setattr(stt, "_meeting_channels", fake_channels)

    seen = {}

    async def fake_short(audio, filename, ctype, *, timeout_sec=180):
        seen["timeout"] = timeout_sec
        return ("降级文本", "")

    monkeypatch.setattr(stt, "transcribe", fake_short)

    class _Sess:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, *a, **k):
            raise aiohttp.ClientError("connection refused")

    monkeypatch.setattr(stt.aiohttp, "ClientSession", _Sess)
    text, err = await stt.transcribe_meeting(
        b"audio-bytes", "m.m4a", "audio/m4a", host="wazir.example.com"
    )
    assert text == "降级文本"
    assert seen["timeout"] == 180
    assert list(config.PUBLISHED_DIR.iterdir()) == []


@pytest.mark.anyio
async def test_probe_duration_real_ffmpeg(isolated, monkeypatch):
    """真实 ffmpeg 造 2 秒音频,验证 ffprobe 探测链路(本机装有 ffmpeg)。"""
    from claude_hermes import config

    monkeypatch.setattr(config, "AUDIO_DIR", isolated / "audio")
    wav = isolated / "two_sec.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "anullsrc=r=16000:cl=mono", "-t", "2", str(wav)],
        check=True,
    )
    with open(wav, "rb") as f:
        audio = f.read()
    d = await stt.probe_duration(audio, "two_sec.wav")
    assert d is not None and abs(d - 2.0) < 0.2
