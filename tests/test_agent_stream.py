"""Phase 0 keystone:工具入参从流式 partial_json 拼装。

assemble_tool_input 把累积的 input_json_delta 片段解析成 dict;
空 / 半包 / 坏 JSON 都必须安全退化成 {},绝不抛异常(否则会炸整轮流式)。
"""
from __future__ import annotations

import anyio

from vococo import config
from vococo.core.agent import (
    ToolInput,
    _compact_threshold,
    _load_system_prompt,
    _query_context_usage,
    _turn_env,
    assemble_tool_input,
    context_window,
)


def test_load_system_prompt_times_out_without_blocking_turn(monkeypatch):
    """iCloud 卡在同步读文件时，必须放弃等待并让模型调用继续。"""
    import vococo.core.agent as agent

    async def stuck_reader(*_args, **_kwargs):
        await anyio.sleep(1)

    monkeypatch.setattr(config, "PROMPT_LOAD_TIMEOUT", 0.01)
    monkeypatch.setattr(agent.anyio.to_thread, "run_sync", stuck_reader)

    prompt = anyio.run(_load_system_prompt, "/tmp/project", "resume-id")

    assert prompt == {"type": "preset", "preset": "claude_code", "append": ""}


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
    provider = {"ANTHROPIC_BASE_URL": "https://api.deepseek.com", "ANTHROPIC_API_KEY": "sk-x"}
    env = _turn_env(provider)
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com"
    assert env["ANTHROPIC_API_KEY"] == "sk-x"
    assert env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_turn_env_does_not_mutate_input():
    # 纯函数:不能就地改传入的 provider_env(哈希/复用会依赖它的原值)
    provider = {"ANTHROPIC_BASE_URL": "https://x"}
    _turn_env(provider)
    assert "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" not in provider


def test_turn_env_injects_oauth_token_for_official(monkeypatch):
    # 官方模型必须显式带上 CLAUDE_CODE_OAUTH_TOKEN——config._scrub_env_secrets 启动时已把它从
    # 父进程 os.environ 里 pop 掉,指望"父进程 env 原样透传"是错的(2026-07-28 回归:本地登录态
    # 一旦掉线,父进程 env 和本地凭据都拿不到 token,官方模型每轮报 "Not logged in"),必须从
    # config.OAUTH_TOKEN(.env 里配置的长效订阅令牌)显式塞回去。
    monkeypatch.setattr(config, "OAUTH_TOKEN", "sk-ant-oat01-test")
    env = _turn_env({})
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test"


def test_turn_env_third_party_does_not_get_oauth_injection():
    # 第三方分支不该被官方分支的 OAuth 注入逻辑碰到——provider_env 已经是 providers.py
    # 算好的完整第三方 env(它自己会把 CLAUDE_CODE_OAUTH_TOKEN 设空,防 CLI 拿订阅 token
    # 打第三方端点),_turn_env 只应原样透传,不该额外注入官方 token。
    provider = {"ANTHROPIC_BASE_URL": "https://api.deepseek.com", "ANTHROPIC_API_KEY": "sk-x"}
    env = _turn_env(provider)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


# === context_window:DeepSeek V4 全系 1M ===


def test_context_window_deepseek_v4_is_1m():
    assert context_window("deepseek-v4-flash") == 1_000_000
    assert context_window("deepseek-v4-pro") == 1_000_000


def test_context_window_deepseek_old_names_fall_back():
    # 已停用的旧名(deepseek-chat/reasoner)不是 1M,走默认 200k 兜底
    assert context_window("deepseek-chat") == 200_000
    assert context_window("deepseek-reasoner") == 200_000


def test_context_window_unknown_falls_back():
    assert context_window("some-unknown-model") == 200_000


def test_context_window_codex_proxy_gpt_is_258k_for_all_tiers():
    # Codex 本机模型目录:272k × 95% = 258,400；三档窗口相同，不是 API 直连的 1.05M。
    for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        assert context_window(model) == 258_400


def test_context_window_error_explains_recovery():
    from vococo.core.agent import describe_llm_error

    text = describe_llm_error(
        400, "Your input exceeds the context window of this model."
    )

    assert "上下文" in text
    assert "自动压缩" in text


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


# === _query_context_usage:2026-07-23 从 stream_turn 内联代码拆出的独立 seam ===
# 拆出来之前,这段逻辑只能靠手搓完整 receive_messages 消息序列的 FakeClient 间接测到;
# 现在只需一个只实现 get_context_usage 的最小假对象就能直接验证。


class _StubClient:
    def __init__(self, usage: dict | None = None, *, raises: bool = False):
        self._usage = usage or {}
        self._raises = raises

    async def get_context_usage(self):
        if self._raises:
            raise RuntimeError("旧 CLI 不支持 /context")
        return self._usage


def test_query_context_usage_prefers_raw_max_when_larger():
    # CLI 认的窗口(rawMaxTokens)比我们权威表(sonnet-5=100万)大 → 采信 CLI 的更大值
    client = _StubClient({"totalTokens": 500, "rawMaxTokens": 2_000_000})
    cu, total, ctx_window_val, stale = anyio.run(
        _query_context_usage, client, "claude-sonnet-5"
    )
    assert total == 500
    assert ctx_window_val == 2_000_000
    assert stale is False


def test_query_context_usage_detects_stale_cli_window():
    # CLI 认的窗口(20万)明显小于权威表(sonnet-5=100万)→ 采信权威表,并标记 stale
    client = _StubClient({"totalTokens": 900_000, "rawMaxTokens": 200_000})
    cu, total, ctx_window_val, stale = anyio.run(
        _query_context_usage, client, "claude-sonnet-5"
    )
    assert ctx_window_val == 1_000_000
    assert stale is True


def test_query_context_usage_falls_back_on_error():
    # 旧 CLI 不支持 get_context_usage → 静默降级,按模型名估窗口,不抛异常
    client = _StubClient(raises=True)
    cu, total, ctx_window_val, stale = anyio.run(
        _query_context_usage, client, "claude-haiku-4-5"
    )
    assert cu is None
    assert total == 0
    assert ctx_window_val == 200_000
    assert stale is False
