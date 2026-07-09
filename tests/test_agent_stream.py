"""Phase 0 keystone:工具入参从流式 partial_json 拼装。

assemble_tool_input 把累积的 input_json_delta 片段解析成 dict;
空 / 半包 / 坏 JSON 都必须安全退化成 {},绝不抛异常(否则会炸整轮流式)。
"""
from __future__ import annotations

from claude_hermes.core.agent import (
    ToolInput,
    _compact_threshold,
    _turn_env,
    assemble_tool_input,
)


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


# === _compact_threshold:大窗口模型不能被 CLI 的旧窗口认知提前压缩 ===


def test_compact_threshold_ignores_stale_official_threshold():
    # sonnet-5 真实窗口 100万,但 CLI 注册表还认成 20万 → 官方阈值(16.7万)是按小窗口
    # 算出来的假象,不能再当"更保守的备份"采信,否则真实窗口两成不到就被砍。
    threshold = _compact_threshold(
        ctx_window_val=1_000_000,
        fallback_ratio=0.65,
        official_threshold=167_000,
        cli_window_stale=True,
    )
    assert threshold == 650_000


def test_compact_threshold_trusts_fresh_official_threshold():
    # CLI 认的窗口没有比我们权威表小(未过期)→ 官方阈值仍然是合法的更紧备份,照旧取更小值
    threshold = _compact_threshold(
        ctx_window_val=1_000_000,
        fallback_ratio=0.83,
        official_threshold=600_000,
        cli_window_stale=False,
    )
    assert threshold == 600_000


def test_compact_threshold_falls_back_to_ratio_when_no_official_value():
    threshold = _compact_threshold(
        ctx_window_val=200_000,
        fallback_ratio=0.83,
        official_threshold=0,
        cli_window_stale=False,
    )
    assert threshold == 166_000
