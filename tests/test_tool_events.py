"""工具事件改造(复刻 Claude Code 体验)的单元测试。

覆盖三块:
- _detail / _preview:结果全文截断(保留换行) vs 单行预览
- Sink 按 tool_id 配对:并行同名工具不再错标
- 子代理(parent_id)事件:计入所属 Task 的步数,不混进主工具列表
"""
from __future__ import annotations

import asyncio

from vococo.core.agent import ToolFinished, ToolInput, ToolStarted, _detail, _preview
from vococo.gateway.core import Sink


# ── 结果文本处理 ──────────────────────────────────────────────────────────
def test_detail_preserves_newlines():
    got = _detail("line1\nline2\nline3")
    assert got == "line1\nline2\nline3"


def test_detail_truncates_long_output():
    got = _detail("x" * 5000)
    assert got.startswith("x" * 4000) and got.endswith("…(已截断)")
    assert len(got) < 5000


def test_detail_joins_content_blocks():
    blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert _detail(blocks) == "a\nb"


def test_preview_is_single_line():
    assert "\n" not in _preview("line1\nline2")


# ── 事件默认值:老调用方(不传新字段)不受影响 ────────────────────────────────
def test_event_defaults_backwards_compatible():
    st = ToolStarted("Bash")
    fin = ToolFinished("Bash", ok=True, preview="ok")
    inp = ToolInput(name="Edit", tool_id="t1", tool_input={})
    assert st.tool_id == "" and st.parent_id is None
    assert fin.tool_id == "" and fin.detail == "" and fin.parent_id is None
    assert inp.parent_id is None


# ── Sink:按 id 配对 ───────────────────────────────────────────────────────
def test_sink_pairs_parallel_same_name_tools_by_id():
    async def t():
        s = Sink()
        await s.tool_started("Bash", "id_a")
        await s.tool_started("Bash", "id_b")
        # 后启动的先完成(并行常见)→ 按 id 应标记 id_b,而不是列表里第一个
        await s.tool_finished("Bash", ok=False, preview="err", tool_id="id_b")
        assert [x["done"] for x in s.tools] == [False, True]
        assert s.tools[1]["ok"] is False
    asyncio.run(t())


def test_sink_falls_back_to_name_without_id():
    async def t():
        s = Sink()
        await s.tool_started("Read", "id_r")
        await s.tool_finished("Read", ok=True, preview="ok")  # 无 id → 按名字
        assert s.tools[0]["done"] is True
    asyncio.run(t())


def test_sink_input_pairs_by_id():
    async def t():
        s = Sink()
        await s.tool_started("Edit", "id_1")
        await s.tool_started("Edit", "id_2")
        await s.tool_input("Edit", "id_2", {"file_path": "b.py"})
        assert s.tools[0]["input"] is None
        assert s.tools[1]["input"] == {"file_path": "b.py"}
    asyncio.run(t())


# ── Sink:子代理事件 ──────────────────────────────────────────────────────
def test_sink_subagent_steps_counted_not_listed():
    async def t():
        s = Sink()
        await s.tool_started("Task", "task_1")
        await s.tool_started("Bash", "sub_1", parent_id="task_1")
        await s.tool_started("Read", "sub_2", parent_id="task_1")
        await s.tool_finished("Bash", True, "ok", tool_id="sub_1", parent_id="task_1")
        # 子代理工具不进主列表,只累计步数
        assert len(s.tools) == 1
        assert s.tools[0]["sub_calls"] == 2
        assert "Task(2步)" in s.tools_summary()
        # Task 本身完成
        await s.tool_finished("Task", True, "done", tool_id="task_1")
        assert s.tools[0]["done"] is True
    asyncio.run(t())
