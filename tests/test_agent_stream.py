"""Phase 0 keystone:工具入参从流式 partial_json 拼装。

assemble_tool_input 把累积的 input_json_delta 片段解析成 dict;
空 / 半包 / 坏 JSON 都必须安全退化成 {},绝不抛异常(否则会炸整轮流式)。
"""
from __future__ import annotations

from claude_hermes.core.agent import ToolInput, assemble_tool_input


def test_assemble_full_json():
    got = assemble_tool_input('{"file_path": "a.py", "old_string": "x"}')
    assert got == {"file_path": "a.py", "old_string": "x"}


def test_assemble_chunked_like_stream():
    # 模拟分片到达后拼接的最终串
    parts = ['{"todos": [', '{"content": "改 bug", ', '"status": "pending"}]}']
    got = assemble_tool_input("".join(parts))
    assert got["todos"][0]["status"] == "pending"


def test_assemble_empty_is_dict():
    assert assemble_tool_input("") == {}
    assert assemble_tool_input("   ") == {}


def test_assemble_broken_json_is_dict():
    assert assemble_tool_input('{"a": ') == {}  # 半包
    assert assemble_tool_input("not json") == {}


def test_assemble_non_object_is_dict():
    assert assemble_tool_input("[1, 2, 3]") == {}  # 顶层不是对象 → {}
    assert assemble_tool_input('"str"') == {}


def test_tool_input_event_shape():
    ev = ToolInput(name="Edit", tool_id="tu_1", tool_input={"file_path": "a.py"})
    assert ev.name == "Edit" and ev.tool_input["file_path"] == "a.py"
