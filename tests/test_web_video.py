"""Web 视频链路:上传 → 发送 → 落盘/入库 → 历史回显,以及 AI 主动发视频。

盯死两条容易回退的红线:
1. 视频【绝不能】进模型请求体(没有 video block,几十 MB base64 塞进去必炸);
2. 落盘/历史拆分靠 "ai_" 前缀,AI 发的视频不能贴到用户气泡上。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.core import agent
from vococo.gateway.adapters.web import WebAdapter
from vococo.memory import session_store, videos


@pytest.fixture
def adapter():
    return WebAdapter()


@pytest.fixture(autouse=True)
def _no_web_auth(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "")


@pytest.fixture
def video_app(adapter):
    app = web.Application()
    app.add_routes([
        web.post("/upload_video", adapter._handle_upload_video),
        web.post("/send", adapter._handle_send),
        web.get("/video", adapter._handle_video),
    ])
    return app


@pytest.fixture
def video_dir(isolated, monkeypatch, tmp_path):
    d = tmp_path / "videos"
    monkeypatch.setattr(config, "VIDEOS_DIR", d)
    return d


@pytest.mark.anyio
async def test_upload_then_send_carries_video(video_app, adapter):
    async with TestClient(TestServer(video_app)) as client:
        form = FormData()
        form.add_field("video", b"mp4 bytes", filename="clip.mp4", content_type="video/mp4")
        resp = await client.post("/upload_video", data=form, headers={"X-Auth-Token": ""})
        assert resp.status == 200
        vid = (await resp.json())["id"]

        send = await client.post(
            "/send",
            json={"conv": "main", "text": "", "videos": [{"id": vid}]},
            headers={"X-Auth-Token": ""},
        )
        assert send.status == 200

    inc = await adapter._inbox.get()
    assert [(v.data, v.filename, v.media_type) for v in inc.videos] == [
        (b"mp4 bytes", "clip.mp4", "video/mp4")
    ]
    assert vid not in adapter._pending_videos  # 已消费,不能留着被二次发送


@pytest.mark.anyio
async def test_video_never_enters_model_request_body(video_app, adapter):
    """视频只能以路径出现在正文里;原始字节一个都不许进 content block。"""
    adapter._pending_videos["v1"] = (b"HUGE-VIDEO-BYTES", "big.mp4", "video/mp4", 0.0)
    async with TestClient(TestServer(video_app)) as client:
        await client.post(
            "/send",
            json={"conv": "main", "text": "存一下", "videos": [{"id": "v1"}]},
            headers={"X-Auth-Token": ""},
        )
    inc = await adapter._inbox.get()
    # _build_prompt 只认 images/files 两种附件,视频压根没有入口
    prompt = agent._build_prompt([], inc.text, inc.images, inc.files)
    assert isinstance(prompt, str)  # 无多模态附件 → 退化成纯文本 prompt
    assert "HUGE-VIDEO-BYTES" not in prompt


@pytest.mark.anyio
async def test_upload_video_over_limit_rejected(video_app, adapter, monkeypatch):
    monkeypatch.setattr(config, "VIDEO_MAX_BYTES", 4)
    async with TestClient(TestServer(video_app)) as client:
        form = FormData()
        form.add_field("video", b"way-too-big", filename="big.mp4")
        resp = await client.post("/upload_video", data=form, headers={"X-Auth-Token": ""})
        data = await resp.json()
    assert resp.status == 400
    assert "MB 上限" in data["error"]
    assert not adapter._pending_videos


def test_save_and_history_splits_user_and_ai_videos(video_dir):
    key = "web:v1"
    turn_id = session_store.start_turn(key, "看看这个")

    class _V:
        data = b"bytes"
        media_type = "video/mp4"
        filename = "手持拍摄.mp4"
        local_path = ""

    v = _V()
    session_store.save_turn_videos(turn_id, [v])
    assert v.local_path  # 路径必须回填,否则模型拿不到文件
    session_store.append_turn_video(
        key, {"file": "ai_abc.mp4", "filename": "render.mp4", "media_type": "video/mp4"}
    )
    (video_dir / "ai_abc.mp4").write_bytes(b"ai bytes")

    turns = session_store.load_history(key)
    assert [x["filename"] for x in turns[-1]["videos"]] == ["手持拍摄.mp4"]
    assert turns[-1]["ai_videos"][0]["url"] == "/video?name=ai_abc.mp4"
    # 用户上传那条落盘名以 turn_id 开头(数字),跟 ai_ 前缀互不相交
    assert turns[-1]["videos"][0]["url"].endswith(f"={turn_id}_0.mp4")


def test_delete_session_purges_video_files(video_dir):
    key = "web:v2"
    turn_id = session_store.start_turn(key, "带视频的一轮")

    class _V:
        data = b"bytes"
        media_type = "video/mp4"
        filename = "a.mp4"
        local_path = ""

    session_store.save_turn_videos(turn_id, [_V()])
    assert list(video_dir.glob("*.mp4"))
    session_store.delete_session(key)
    assert not list(video_dir.glob("*.mp4"))  # 视频最占地方,不能留孤儿文件


@pytest.mark.anyio
async def test_send_video_rejects_unplayable_format(adapter, video_dir, tmp_path):
    src = tmp_path / "x.mkv"
    src.write_bytes(b"mkv")
    err = await adapter.send_video("main", src)
    assert err and "ffmpeg" in err  # 直说该怎么转,而不是发出去一个黑框


@pytest.mark.anyio
async def test_send_video_emits_mid_turn_and_persists(adapter, video_dir, tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(adapter, "_emit", events.append)
    session_store.start_turn(config.resolve_session_key("web", "main"), "帮我渲染")
    src = tmp_path / "out.mp4"
    src.write_bytes(b"rendered")

    assert await adapter.send_video("main", src, "渲染好了") is None
    assert events[0]["mid_turn"] is True  # 不能当"回合结束",后面还有正文
    name = events[0]["videos"][0]["url"].removeprefix("/video?name=")
    assert name.startswith(videos.AI_VIDEO_PREFIX)
    assert (video_dir / name).read_bytes() == b"rendered"
    # 落库:只推 SSE 不落库的话刷新后视频会永久消失
    turns = session_store.load_history(config.resolve_session_key("web", "main"))
    assert turns[-1]["ai_videos"][0]["url"] == f"/video?name={name}"


def test_video_path_blocks_traversal(video_dir):
    video_dir.mkdir(parents=True, exist_ok=True)
    assert session_store.video_path("../../etc/passwd") is None
    assert session_store.video_path("nope.mp4") is None


def test_frontend_routes_video_to_its_own_upload_endpoint():
    js = (Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/composer.js").read_text(
        encoding="utf-8"
    )
    assert "/upload_video?client_id=" in js
    assert 'videos:sendFiles.filter(x=>x.kind==="video")' in js
    # audio/webm 不能被视频扩展名兜底抢走(两边都认 webm)
    assert 'if(type.startsWith("audio/")) return false;' in js
