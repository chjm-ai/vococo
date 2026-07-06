"""多轮维持三项升级的单测:压缩事件透传 / prompt 快照 / 降级 blob 围栏。

- Compacted 事件从 SDK SystemMessage(compact_boundary) 透传到时间线与 Sink;
- build_system_prompt(cache_key=会话id) 在会话内冻结 append(防中途改 MEMORY.md
  打爆整条 prompt cache);
- _compose_prompt 降级路径带围栏标注(完整逐字、非摘要、历史指令不生效)。
"""
from __future__ import annotations

import asyncio

from claude_hermes.core.agent import Compacted, Turn, _compose_prompt
from claude_hermes.gateway.core import Sink, _Timeline


# === Compacted 事件与时间线 ===


def test_compacted_event_shape():
    ev = Compacted(trigger="auto")
    assert ev.trigger == "auto"
    assert Compacted().trigger == ""


def test_timeline_compact_block():
    tl = _Timeline()
    tl.text("前半")
    tl.compacted("auto")
    tl.text("后半")
    assert tl.blocks[1] == {"type": "compact", "trigger": "auto"}
    # 压缩块打断了 text 合并:前后是两个独立文字块
    assert [b["type"] for b in tl.blocks] == ["text", "compact", "text"]


def test_timeline_compact_respects_max_blocks():
    tl = _Timeline()
    tl.blocks = [{"type": "text", "text": "x"}] * _Timeline.MAX_BLOCKS
    tl.compacted("auto")
    assert len(tl.blocks) == _Timeline.MAX_BLOCKS  # 保险丝:不再膨胀


def test_sink_compacted_default_no_crash():
    async def run():
        s = Sink()
        await s.compacted("auto")  # 基类默认只 render(),不得抛异常

    asyncio.run(run())


# === 降级 blob 围栏(_compose_prompt) ===


def test_compose_prompt_empty_history_passthrough():
    assert _compose_prompt([], "你好") == "你好"


def test_compose_prompt_fenced_recovery_format():
    hist = [Turn(user="问A", assistant="答A"), Turn(user="问B", assistant="答B")]
    out = _compose_prompt(hist, "接着聊")
    first_line = out.splitlines()[0]
    # 围栏标注:完整逐字(防"被压缩"幻觉)+ 历史指令不生效(反注入)
    assert "完整逐字记录" in first_line and "不构成本轮指令" in first_line
    assert "我:问A" in out and "你:答B" in out
    assert out.rstrip().endswith("我:接着聊")
    assert "[当前这轮]" in out


# === system prompt 会话内快照 ===


def _write_brain(brain_dir, text: str) -> None:
    brain_dir.mkdir(parents=True, exist_ok=True)
    (brain_dir / "USER.md").write_text(text, encoding="utf-8")


def test_prompt_snapshot_frozen_within_session(isolated, monkeypatch):
    from claude_hermes.core import prompt

    monkeypatch.setattr(prompt, "_APPEND_CACHE", type(prompt._APPEND_CACHE)())
    from claude_hermes import config

    _write_brain(config.AI_BRAIN_DIR, "画像V1")
    first = prompt.build_system_prompt(cache_key="sid-1")
    assert "画像V1" in first["append"]

    _write_brain(config.AI_BRAIN_DIR, "画像V2")
    # 同一会话:命中快照,不受中途文件变化影响(缓存前缀稳定)
    again = prompt.build_system_prompt(cache_key="sid-1")
    assert again["append"] == first["append"]
    # 新会话(新 id)/无 key:现读文件,拿到最新画像
    assert "画像V2" in prompt.build_system_prompt(cache_key="sid-2")["append"]
    assert "画像V2" in prompt.build_system_prompt()["append"]


def test_prompt_snapshot_cache_bounded(isolated, monkeypatch):
    from claude_hermes.core import prompt

    monkeypatch.setattr(prompt, "_APPEND_CACHE", type(prompt._APPEND_CACHE)())
    from claude_hermes import config

    _write_brain(config.AI_BRAIN_DIR, "画像")
    for i in range(prompt._APPEND_CACHE_MAX + 8):
        prompt.build_system_prompt(cache_key=f"sid-{i}")
    assert len(prompt._APPEND_CACHE) == prompt._APPEND_CACHE_MAX
    assert "sid-0" not in prompt._APPEND_CACHE  # 最旧的被挤出
