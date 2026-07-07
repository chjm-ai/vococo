"""Phase 0 keystone:工具入参从流式 partial_json 拼装。

assemble_tool_input 把累积的 input_json_delta 片段解析成 dict;
空 / 半包 / 坏 JSON 都必须安全退化成 {},绝不抛异常(否则会炸整轮流式)。
"""
from __future__ import annotations

from claude_hermes.core.agent import ToolInput, _turn_env, assemble_tool_input


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


def test_turn_env_forces_foreground_for_official():
    # 官方订阅场景 provider_env 为空 → 仍必须带强制前台开关,否则 Agent 被 CLI 异步化成后台
    env = _turn_env({})
    assert env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_turn_env_keeps_provider_keys():
    # 第三方(cc-switch)场景:供应商 base_url+key 要保留,同时叠加前台开关
    provider = {"ANTHROPIC_BASE_URL": "https://api.deepseek.com", "ANTHROPIC_AUTH_TOKEN": "sk-x"}
    env = _turn_env(provider)
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-x"
    assert env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_turn_env_does_not_mutate_input():
    # 纯函数:不能就地改传入的 provider_env(哈希/复用会依赖它的原值)
    provider = {"ANTHROPIC_BASE_URL": "https://x"}
    _turn_env(provider)
    assert "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" not in provider
