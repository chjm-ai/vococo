"""过程时间线:converse/task_runner 共用录制(core.timeline.Timeline)+ 落库(events 列)+ Web 分段推送。

对应两条用户诉求:
① 工具卡与文字要按真实顺序交错(Web 端按 seg 分段推正文);
② 刷新页面后历史要能重建工具调用过程(events 随 turn 落库)。
"""
from __future__ import annotations

from vococo.core.timeline import Timeline


def test_timeline_interleaves_text_and_tools():
    tl = Timeline()
    tl.text("先说")
    tl.text("一句。")
    tl.tool_started("Bash", "t1", None)
    tl.tool_input("t1", {"command": "ls"}, None)
    tl.tool_finished("Bash", True, "ok", "t1", "a\nb", None)
    tl.text("跑完了,继续说。")
    assert [b["type"] for b in tl.blocks] == ["text", "tool", "text"]
    assert tl.blocks[0]["text"] == "先说一句。"
    tool = tl.blocks[1]
    assert tool["name"] == "Bash" and tool["input"] == {"command": "ls"}
    assert tool["ok"] is True and tool["preview"] == "ok" and tool["detail"] == "a\nb"


def test_timeline_subagent_steps_nest_under_parent():
    tl = Timeline()
    tl.tool_started("Agent", "task1", None)
    tl.tool_started("Read", "r1", "task1")   # 子代理内部工具
    tl.tool_finished("Read", True, "", "r1", "", "task1")
    tl.tool_started("Bash", "b1", "task1")
    tl.tool_finished("Bash", False, "boom", "b1", "", "task1")
    assert len(tl.blocks) == 1  # 子步不占顶层块
    subs = tl.blocks[0]["subs"]
    assert [(s["name"], s["ok"]) for s in subs] == [("Read", True), ("Bash", False)]


def test_timeline_caps_blocks():
    tl = Timeline()
    for i in range(Timeline.MAX_BLOCKS + 50):
        tl.tool_started("Read", f"t{i}", None)
    assert len(tl.blocks) == Timeline.MAX_BLOCKS


def test_finish_turn_persists_events_and_history_returns_them(isolated):
    from vococo.memory import session_store

    tid = session_store.start_turn("web:x", "问题")
    events = [
        {"type": "text", "text": "先说"},
        {"type": "tool", "name": "Bash", "id": "t1", "ok": True,
         "input": {"command": "ls"}, "preview": "ok", "detail": "a"},
        {"type": "text", "text": "说完"},
    ]
    session_store.finish_turn(tid, "先说说完", events=events)
    turns = session_store.load_history("web:x")
    assert len(turns) == 1
    assert turns[0]["assistant"] == "先说说完"
    assert turns[0]["id"] == tid
    # 默认懒加载:tool 块的 input/preview/detail 被砍掉只留空壳(见 _strip_tool_block)
    tool = turns[0]["events"][1]
    assert tool["name"] == "Bash" and tool["ok"] is True and tool["done"] is True
    assert "input" not in tool and "preview" not in tool and "detail" not in tool
    # full_events=True(懒加载点开时用)拿回完整版
    full_turns = session_store.load_history("web:x", full_events=True)
    assert full_turns[0]["events"] == events
    # /turn_events 路由背后的单轮查询,同样拿完整版
    assert session_store.load_turn_events("web:x", tid) == events
    assert session_store.load_turn_events("web:x", tid + 999) is None
    assert session_store.load_turn_events("web:other", tid) is None  # 越权查不到


def test_history_without_events_still_works(isolated):
    from vococo.memory import session_store

    tid = session_store.start_turn("web:x", "问题")
    session_store.finish_turn(tid, "纯文本回复")  # 不带 events(老路径)
    turns = session_store.load_history("web:x")
    assert turns[0]["events"] == []


def test_websink_splits_text_segments_on_tool_start(isolated):
    import asyncio

    from vococo.gateway.adapters.web import _WebSink

    class FakeAdapter:
        def __init__(self):
            self.sent = []

        def _emit(self, payload):
            self.sent.append(payload)

    a = FakeAdapter()
    sink = _WebSink(a, "main")

    async def run():
        await sink.text("第一段")
        await sink.tool_started("Bash", "t1")      # 顶层工具 → 切段
        await sink.tool_started("Read", "r1", "t1")  # 子代理内部工具 → 不切段
        await sink.text("第二段")

    asyncio.run(run())
    texts = [p for p in a.sent if p["type"] == "text"]
    assert [(p["seg"], p["text"]) for p in texts] == [(0, "第一段"), (1, "第二段")]
