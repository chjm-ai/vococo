"""统一附件策略：格式识别、预算、视觉预览，不调用外部服务。"""
from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from vococo.core import agent
from vococo.core import attachment_policy as policy


def image(size=(40, 40), *, path="/tmp/original.png", fmt="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, fmt)
    return agent.ImageAttachment(base64.b64encode(buffer.getvalue()).decode(), "image/png", path)


@pytest.mark.parametrize("data,name,mime", [
    (b"PK\x03\x04binary", "sheet.xlsx", "application/octet-stream"),
    (b"%PDF-1.7", "renamed.txt", "text/plain"),
    (b"\x01\x02control", "unknown.xyz", "application/octet-stream"),
    (b"<svg><image href='data:image/png;base64,AAAA'/></svg>", "icon.svg", "image/svg+xml"),
])
@pytest.mark.anyio
async def test_non_text_documents_use_local_tools_not_document_blocks(data, name, mime):
    file = agent.FileAttachment(data, mime, name, "/tmp/attachment")
    message = await anext(agent._build_prompt([], "分析文件", [], [file]))
    blocks = message["message"]["content"]
    assert all(block["type"] == "text" for block in blocks)
    assert "/tmp/attachment" in blocks[1]["text"]
    assert "按需" in blocks[1]["text"]
    assert "base64," not in str(blocks)


@pytest.mark.anyio
async def test_utf16_code_is_decoded_as_text():
    file = agent.FileAttachment("print('你好')".encode("utf-16"), "application/octet-stream", "script.py")
    message = await anext(agent._build_prompt([], "分析代码", [], [file]))
    assert "print('你好')" in message["message"]["content"][1]["text"]


@pytest.mark.anyio
async def test_missing_binary_path_does_not_blindly_forward_bytes():
    file = agent.FileAttachment(b"\0\xff", "application/octet-stream", "unknown.bin")
    message = await anext(agent._build_prompt([], "分析文件", [], [file]))
    block = message["message"]["content"][1]
    assert block["type"] == "text"
    assert "无法读取" in block["text"]
    assert "重新上传" in block["text"]


def test_small_images_are_visual_and_unchanged():
    original = image()
    previews, notes = policy.prepare_images([original])
    assert len(previews) == 1 and previews[0].data == original.data
    assert previews[0].source_index == 1 and original.source_index == 0
    assert not notes


def test_large_image_preview_preserves_original():
    original = image((2000, 800))
    old_data = original.data
    previews, notes = policy.prepare_images([original])
    assert original.data == old_data
    assert previews[0].local_path == original.local_path
    assert "预览" in notes
    with Image.open(io.BytesIO(base64.b64decode(previews[0].data))) as preview:
        assert max(preview.size) <= 1568


def test_excess_images_are_explicit_path_references():
    previews, notes = policy.prepare_images([image(path=f"/tmp/{i}.png") for i in range(10)])
    assert len(previews) == policy.MAX_IMAGES
    assert "/tmp/9.png" in notes
    assert "未直传" in notes


def test_invalid_image_is_not_forwarded_as_vision():
    previews, notes = policy.prepare_images([agent.ImageAttachment("invalid", "image/png", "/tmp/bad.png")])
    assert not previews
    assert "/tmp/bad.png" in notes
    assert "未直传" in notes


def test_invalid_image_without_path_fails_explicitly():
    with pytest.raises(ValueError, match="重新上传"):
        policy.prepare_images([agent.ImageAttachment("invalid", "image/png")])


@pytest.mark.anyio
async def test_images_and_text_share_total_budget():
    images = [image() for _ in range(policy.MAX_IMAGES)]
    file = agent.FileAttachment(b"x" * 23_000, "text/plain", "notes.txt", "/tmp/notes.txt")
    message = await anext(agent._build_prompt([], "分析附件", images, [file]))
    blocks = message["message"]["content"]
    assert sum(block["type"] == "image" for block in blocks) == policy.MAX_IMAGES
    assert "x" * 100 not in str(blocks)
    assert "按需" in blocks[-1]["text"]


@pytest.mark.anyio
async def test_text_budget_counts_utf8_bytes_not_characters():
    file = agent.FileAttachment(("中" * 10_000).encode(), "text/plain", "notes.txt")
    message = await anext(agent._build_prompt([], "分析附件", [], [file]))
    text = message["message"]["content"][1]["text"]
    assert len(text.encode()) < 25_000
    assert "截断" in text


def test_history_restores_all_attachment_paths_without_bodies(isolated, monkeypatch):
    from vococo import config
    from vococo.memory import session_store

    for name in ("FILES_DIR", "AUDIO_DIR", "VIDEOS_DIR"):
        monkeypatch.setattr(config, name, isolated / name)
    key = "web:attachment-history"
    turn_id = session_store.start_turn(key, "帮我分析")
    session_store.save_turn_files(turn_id, [agent.FileAttachment(b"secret body", "text/plain", "code.py")])
    audio = agent.AudioAttachment(b"audio", "audio/wav", "memo.wav", "会议内容" * 10_000)
    session_store.save_turn_audio(turn_id, [audio])
    session_store.save_turn_videos(turn_id, [agent.VideoAttachment(b"video", "video/mp4", "demo.mp4")])
    session_store.finish_turn(turn_id, "已收到")
    history = session_store.load_recent(key)
    text = agent._compose_prompt(history, "继续")
    assert "code.py" in text and "memo.wav" in text and "demo.mp4" in text
    assert str(config.FILES_DIR) in text and str(config.AUDIO_DIR) in text
    assert str(config.VIDEOS_DIR) in text
    assert "secret body" not in text and "会议内容" not in text
    assert audio.transcript_path in text


def test_audio_transcript_sidecars_follow_clone_and_purge(isolated, monkeypatch):
    from pathlib import Path
    from vococo import config
    from vococo.memory import session_store

    monkeypatch.setattr(config, "AUDIO_DIR", isolated / "audio")
    source = session_store.start_turn("web:source", "录音")
    audio = agent.AudioAttachment(b"audio", "audio/wav", "memo.wav", "完整转写")
    entries = session_store.save_turn_audio(source, [audio])
    assert Path(audio.transcript_path).read_text() == "完整转写"
    target = session_store.start_turn("web:target", "副本")
    copied = session_store.clone_turn_audio(target, entries)
    assert copied[0]["transcript_file"] != entries[0]["transcript_file"]
    target_path = config.AUDIO_DIR / copied[0]["transcript_file"]
    assert target_path.read_text() == "完整转写"
    session_store.purge_session_audio(session_store._conn(), "web:source")
    assert not Path(audio.transcript_path).exists()
    assert target_path.exists()


@pytest.mark.anyio
async def test_long_audio_uses_same_budget_as_files(isolated, monkeypatch):
    from pathlib import Path
    from vococo import config
    from vococo.gateway import core
    from vococo.memory import session_store

    monkeypatch.setattr(config, "AUDIO_DIR", isolated / "audio")
    monkeypatch.setattr(config, "FILES_DIR", isolated / "files")
    captured = {}

    async def fake_stream(history, user_text, **kwargs):
        message = await anext(agent._build_prompt(history, user_text, kwargs["images"] or [], kwargs["files"]))
        captured["content"] = str(message["message"]["content"])
        yield agent.Done(agent.AgentReply("完成", [], None, False))

    monkeypatch.setattr(core, "stream_turn", fake_stream)
    audio = agent.AudioAttachment(b"audio", "audio/wav", "memo.wav", "长转写" * 20_000)
    await core.converse("web:audio-budget", "分析附件", None, core.Sink(), audios=[audio],
                        cwd_override=str(isolated), is_explicit_project_override=False)
    assert "长转写" * 100 not in captured["content"]
    assert audio.transcript_path in captured["content"]
    assert Path(audio.transcript_path).read_text() == audio.transcript
    assert session_store.load_recent("web:audio-budget")[0].user == "分析附件"


def test_frontend_thumbnail_keeps_original_upload_data():
    import subprocess
    from pathlib import Path

    source = (Path(__file__).parents[1] / "vococo/gateway/adapters/web_static/composer.js").read_text()
    function = source[source.index("async function compressImage(f){"):source.index("function loadImgEl(src){")]
    script = """
const assert = require('node:assert/strict');
const IMG_KEEP_BYTES=300*1024, IMG_MAX_EDGE=1568;
const raw='data:image/png;base64,'+'A'.repeat(100);
globalThis.FileReader=class { readAsDataURL(){ this.result=raw; this.onload(); } };
const loadImgEl=async()=>({naturalWidth:2000,naturalHeight:800});
globalThis.document={createElement:()=>({getContext:()=>({fillRect(){},drawImage(){}}),
  toDataURL:()=> 'data:image/jpeg;base64,PREVIEW'})};
""" + function + """
(async()=>{const result=await compressImage({size:400*1024,type:'image/png'});
assert.equal(result.data,raw.split(',')[1]);
assert.equal(result.media_type,'image/png');
assert.equal(result.url,'data:image/jpeg;base64,PREVIEW');})().catch(e=>{console.error(e);process.exit(1)});
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.anyio
async def test_skipped_images_keep_original_numbers():
    images = [agent.ImageAttachment("invalid", "image/png", "/tmp/bad.png"), image(path="/tmp/good.png")]
    message = await anext(agent._build_prompt([], "比较第二张图", images, []))
    blocks = message["message"]["content"]
    assert "图片 1 未直传" in blocks[0]["text"]
    assert "聊天图片附件 2" in blocks[1]["text"]


def test_history_handles_missing_and_malformed_attachment_metadata(isolated, monkeypatch):
    from vococo import config
    from vococo.memory import session_store

    monkeypatch.setattr(config, "FILES_DIR", isolated / "files")
    refs = session_store._user_attachment_refs('[null, 42, {"file":"../../secret", "name":"lost.bin"}]', "bad", "[]")
    assert len(refs) == 1 and "原文件缺失" in refs[0]
    assert "../../secret" not in refs[0]


def test_legacy_audio_transcript_survives_missing_original_and_purge(isolated, monkeypatch):
    import json
    from pathlib import Path
    from vococo import config
    from vococo.memory import session_store

    monkeypatch.setattr(config, "AUDIO_DIR", isolated / "audio")
    key = "web:legacy-audio"
    tid = session_store.start_turn(key, "旧录音")
    legacy = [{"file": f"{tid}_0.wav", "filename": "旧录音.wav", "text": "保留的文字" * 20_000}]
    db = session_store._conn()
    db.execute("UPDATE turns SET audios=? WHERE id=?", (json.dumps(legacy), tid))
    db.commit()
    session_store.finish_turn(tid, "收到")
    refs = session_store.load_recent(key)[0].attachment_refs
    info = json.loads(refs[0])
    transcript = Path(info["转写文字稿"])
    assert transcript.read_text() == legacy[0]["text"]
    assert "保留的文字" not in refs[0]
    session_store.purge_session_audio(db, key)
    assert not transcript.exists()


def test_image_descriptions_follow_history_clone_and_purge(isolated, monkeypatch):
    from pathlib import Path
    from vococo import config
    from vococo.memory import session_store
    from vococo.memory.images import save_image_description

    monkeypatch.setattr(config, "IMAGES_DIR", isolated / "images")
    key = "web:image-description"
    tid = session_store.start_turn(key, "图片")
    original = image()
    names = session_store.save_turn_images(tid, [original])
    description = Path(save_image_description([original], "完整识别文字" * 10_000))
    session_store.finish_turn(tid, "收到")
    assert str(description) in str(session_store.load_recent(key)[0].attachment_refs)
    target = session_store.start_turn("web:copy-image", "副本")
    copied = session_store.clone_turn_images(target, names)
    copied_description = config.IMAGES_DIR / (copied[0] + ".vision.txt")
    assert copied_description.read_text() == description.read_text()
    session_store.purge_session_images(session_store._conn(), key)
    assert not description.exists()
    assert copied_description.exists()
