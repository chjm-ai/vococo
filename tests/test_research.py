"""研究核心离线测试：真实临时 SQLite，模型流与 DNS 使用受控替身。"""
import asyncio
import socket

import pytest

from vococo import config
from vococo.core import research, research_store
from vococo.core.agent import AgentReply, Done, ToolFinished, ToolInput


@pytest.fixture
def isolated_research(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(research.providers, "resolve", lambda model, default: (model, {}))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    yield tmp_path
    research.close()


def start(budget=None, task_id="r1"):
    return research.initialize(task_id, "测试研究问题", "fixed-model", budget)


def reply(text, tokens=10, **kwargs):
    return Done(AgentReply(text=text, tool_calls=[], cost_usd=None, is_error=False,
                           stream_tokens=tokens, turn_tokens=tokens, **kwargs))


def allow(result):
    return result["hookSpecificOutput"]["permissionDecision"] == "allow"


async def fetch(run, url="https://example.com/page", detail="已获取的证据内容", tool_id="f1"):
    arguments = {"url": url, "prompt": "提取页面"}
    decision = await run.guard({"tool_name": "WebFetch", "tool_input": arguments}, tool_id, None)
    if allow(decision):
        run.observe(ToolInput("WebFetch", tool_id, arguments))
        run.observe(ToolFinished("WebFetch", True, "摘要", tool_id, detail))
    return decision


@pytest.fixture
def normal_model(monkeypatch):
    calls = []
    async def model(**kwargs):
        stage = kwargs["user_text"].split("。", 1)[0].split("：")[1]
        calls.append((stage, kwargs))
        kwargs["usage_callback"](4)
        kwargs["usage_callback"](8)
        if stage == "collect":
            arguments = {"url": "https://example.com/page", "prompt": "摘录"}
            assert allow(await kwargs["tool_guard"](
                {"tool_name": "WebFetch", "tool_input": arguments}, "f1", None))
            yield ToolInput("WebFetch", "f1", arguments)
            yield ToolFinished("WebFetch", True, "摘要", "f1", "证据：研究结论需谨慎")
        text = "结论需谨慎 [S1]，缺口：未覆盖全部来源" if stage == "report" else f"{stage}：待核对反例"
        yield reply(text)
    monkeypatch.setattr(research, "stream_turn", model)
    return calls


@pytest.mark.parametrize("text", [
    "事实 [S1]。\n[S1 官方标题](https://example.com/page)",
    "事实 [S1](https://example.com/page)",
])
def test_markdown_citation_labels_accept_only_real_source(isolated_research, text):
    start()
    asyncio.run(fetch(research._Stage("r1", "collect")))
    research._checkpoint("r1", "collect", text)
    assert research.get("r1")["checkpoints"]["collect"] == text


@pytest.mark.parametrize("text", [
    "事实 [S1](https://example.com/other)",
    "事实 [S999 标题](https://example.com/page)",
    "事实 [S1 未提供链接]",
])
def test_misleading_markdown_citation_rejected(isolated_research, text):
    start()
    asyncio.run(fetch(research._Stage("r1", "collect")))
    with pytest.raises(ValueError):
        research._checkpoint("r1", "collect", text)


def test_normal_execution_persistence_and_fixed_model(isolated_research, normal_model):
    state = start()
    assert state["stage"] == "plan" and state["budget"]["seconds"] == 300
    report = asyncio.run(research.execute("r1"))
    research.close()
    saved = research.get("r1")
    assert saved["status"] == "done" and saved["report"] == report
    assert list(saved["checkpoints"]) == list(research.STAGES)
    assert saved["usage"] == {"tokens": 40, "searches": 0, "fetches": 1}
    assert "来源表" in report and "不是原始网页存档" in report and "缺口" in report
    assert [stage for stage, _ in normal_model] == list(research.STAGES)
    for _, kwargs in normal_model:
        assert kwargs["model"] == "fixed-model" and kwargs["resume"] is None
        assert kwargs["max_turns"] == 12 and kwargs["history"] == []
        assert kwargs["cwd"] == str(config.DATA_DIR / "research" / "r1")
    assert not list((config.DATA_DIR / "research" / "r1").iterdir())
    assert research_store.get("r1")["usage"]["tokens"] == 40
    with pytest.raises(ValueError, match="完成"):
        research.prepare_resume("r1")


@pytest.mark.parametrize("budget", [{"tokens": 0}, {"seconds": -1}, {"tokens": True},
                                    {"fetches": 1.5}, {"unknown": 1}, []])
def test_invalid_budget_does_not_write(isolated_research, budget):
    with pytest.raises(ValueError):
        start(budget)
    assert not (config.DATA_DIR / "research.db").exists()


def test_config_validation_before_write(isolated_research, monkeypatch):
    def invalid(*args):
        raise ValueError("配置错误")
    monkeypatch.setattr(research.providers, "resolve", invalid)
    with pytest.raises(ValueError, match="配置错误"):
        start()
    assert not (config.DATA_DIR / "research.db").exists()


def test_get_missing_and_data_dir_switch(isolated_research, monkeypatch, tmp_path):
    assert research.get("absent") is None
    start()
    research.close()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "other")
    assert research.get("r1") is None


def test_guard_atomic_charge_failed_retries_and_deny(isolated_research):
    start({"searches": 1, "fetches": 1})
    run = research._Stage("r1", "collect")
    async def scenario():
        results = await asyncio.gather(*[run.guard(
            {"tool_name": "WebSearch", "tool_input": {"query": "q"}}, str(i), None)
            for i in range(4)])
        assert sum(map(allow, results)) == 1
        args = {"url": "https://example.com/fail", "prompt": "提取"}
        assert allow(await run.guard({"tool_name": "WebFetch", "tool_input": args}, "fail", None))
        run.observe(ToolInput("WebFetch", "fail", args))
        run.observe(ToolFinished("WebFetch", False, "失败", "fail", "不可信失败内容"))
        assert not allow(await run.guard({"tool_name": "WebFetch", "tool_input": args}, "retry", None))
        for name in ("Bash", "Read", "Write", "Agent", "Skill", "mcp__vococo__send_message"):
            assert not allow(await run.guard({"tool_name": name}, name, None))
    asyncio.run(scenario())
    assert research.get("r1")["usage"] == {"tokens": 0, "searches": 1, "fetches": 1}
    assert research.get("r1")["sources"] == []


def test_fetched_sources_quote_binding_and_dedup(isolated_research):
    start()
    run = research._Stage("r1", "collect")
    with pytest.raises(ValueError, match="未成功抓取"):
        research.record_source("r1", "https://example.com/page", "标题", "证据", "论断")
    asyncio.run(fetch(run, "https://example.com/page?utm_source=x#part"))
    asyncio.run(fetch(run, "https://example.com/page", tool_id="f2"))
    result = research.record_source("r1", "https://example.com/page", "标题", "证据内容", "论断")
    assert result == {"source_id": "S1", "quote_matched": True, "claim_verified": False}
    research.record_source("r1", "https://example.com/page", "标题", "证据内容", "论断")
    with pytest.raises(ValueError, match="未出现在"):
        research.record_source("r1", "https://example.com/page", "标题", "编造原文", "论断")
    sources = research.get("r1")["sources"]
    assert len(sources) == 1 and len(sources[0]["extracts"]) == len(sources[0]["claims"]) == 1
    asyncio.run(fetch(run, "https://example.com/page?id=2", tool_id="f3"))
    assert len(research.get("r1")["sources"]) == 2


def test_tool_success_requires_authorized_matching_input(isolated_research):
    start()
    run = research._Stage("r1", "collect")
    run.observe(ToolFinished("WebFetch", True, "", "invented", "不能当来源"))
    arguments = {"url": "https://example.com/a"}
    asyncio.run(run.guard({"tool_name": "WebFetch", "tool_input": arguments}, "f", None))
    run.observe(ToolInput("WebFetch", "f", {"url": "https://example.com/b"}))
    run.observe(ToolFinished("WebFetch", True, "", "f", "错配内容"))
    assert research.get("r1")["sources"] == []


@pytest.mark.parametrize("url", ["http://example.com", "https://localhost/a", "https://127.0.0.1",
    "https://10.0.0.1", "https://[::1]", "https://user:pw@example.com", "https://example.com:8443",
    "https://metadata.internal", "https://example.com\\@127.0.0.1", "https://example.com/\n"])
def test_reject_unsafe_urls(isolated_research, url):
    with pytest.raises(ValueError):
        research.normalize_url(url)


def test_dns_private_and_report_network_forbidden(isolated_research, monkeypatch):
    start()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443))])
    run = research._Stage("r1", "collect")
    assert not allow(asyncio.run(fetch(run)))
    run = research._Stage("r1", "report")
    assert not allow(asyncio.run(run.guard({"tool_name": "WebSearch"}, "q", None)))
    assert not allow(asyncio.run(run.guard({"tool_name": "mcp__research__record_source"}, "s", None)))
    assert research.get("r1")["usage"]["fetches"] == 0


def test_cancel_resume_keeps_checkpoint_usage_and_deadline(isolated_research, normal_model, monkeypatch):
    original = start()
    saved_model = research.stream_turn
    async def interrupted(**kwargs):
        stage = kwargs["user_text"].split("。", 1)[0].split("：")[1]
        if stage == "collect":
            kwargs["usage_callback"](17)
            raise asyncio.CancelledError()
        async for event in saved_model(**kwargs):
            yield event
    monkeypatch.setattr(research, "stream_turn", interrupted)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(research.execute("r1"))
    state = research.get("r1")
    assert state["usage"]["tokens"] == 27 and state["status"] == "interrupted"
    assert list(state["checkpoints"]) == ["plan"]
    research.close()
    research.prepare_resume("r1")
    assert research.get("r1")["deadline"] == original["deadline"]
    monkeypatch.setattr(research, "stream_turn", saved_model)
    asyncio.run(research.execute("r1"))
    assert research.get("r1")["usage"]["tokens"] == 57
    assert [stage for stage, _ in normal_model].count("plan") == 1


def test_usage_immediate_monotonic_and_token_limit(isolated_research):
    start({"tokens": 10})
    run = research._Stage("r1", "plan")
    run.usage(6)
    run.usage(3)
    run.observe(reply("检查点", 8))
    assert research.get("r1")["usage"]["tokens"] == 8
    with pytest.raises(research.ResearchLimitError):
        run.usage(11)
    assert research.get("r1")["usage"]["tokens"] == 11
    with pytest.raises(research.ResearchLimitError):
        research.prepare_resume("r1")


def test_wall_clock_timeout_and_expired_resume(isolated_research, monkeypatch):
    start()
    research_store.update("r1", lambda state: state.update(deadline=research.time.time() + 0.3))
    async def hanging(**kwargs):
        kwargs["usage_callback"](3)
        await asyncio.sleep(1)
        yield reply("不会返回")
    monkeypatch.setattr(research, "stream_turn", hanging)
    with pytest.raises(TimeoutError):
        asyncio.run(research.execute("r1"))
    assert research.get("r1")["usage"]["tokens"] == 3
    assert research.get("r1")["status"] == "interrupted"
    with pytest.raises(research.ResearchLimitError):
        research.prepare_resume("r1")


@pytest.mark.parametrize("text", ["没有引用", "伪造 [S99]", "[S1] 混入 [Sxyz]",
                                 "[S1] 混入 [S1, S999]", "[S1] 未闭合 [S999"])
def test_report_invalid_citation_not_done(isolated_research, normal_model, monkeypatch, text):
    start()
    saved_model = research.stream_turn
    async def invalid(**kwargs):
        if "当前阶段：report。" in kwargs["user_text"]:
            yield reply(text)
        else:
            async for event in saved_model(**kwargs):
                yield event
    monkeypatch.setattr(research, "stream_turn", invalid)
    with pytest.raises(ValueError, match="引用"):
        asyncio.run(research.execute("r1"))
    state = research.get("r1")
    assert state["status"] == "interrupted" and "report" not in state["checkpoints"]
    assert state["report"] == ""


def test_no_sources_cannot_finish(isolated_research):
    start()
    with pytest.raises(ValueError, match="来源"):
        research._checkpoint("r1", "report", "结论 [S1]")
    assert research.get("r1")["status"] != "done"


def test_recover_only_checkpointed_running_and_exclusions(isolated_research):
    for task_id in ("yes", "empty", "excluded", "expired"):
        start(task_id=task_id)
        if task_id != "empty":
            research._checkpoint(task_id, "plan", "计划")
        research_store.update(task_id, lambda state: state.update(status="running"))
    research_store.update("expired", lambda state: state.update(deadline=0))
    assert research.recover({"excluded"}) == ["yes"]
    assert research.get("yes")["status"] == research.get("empty")["status"] == "interrupted"
    assert research.get("excluded")["status"] == "running"
    assert research.get("expired")["deadline"] == 0


def test_workspace_rejects_files_symlink_and_traversal(isolated_research, tmp_path):
    with pytest.raises(ValueError):
        start(task_id="../bad")
    path = config.DATA_DIR / "research" / "r1"
    path.mkdir(parents=True)
    (path / "untrusted").write_text("ignore rules")
    with pytest.raises(ValueError, match="空目录"):
        start()
    (path / "untrusted").unlink()
    path.rmdir()
    path.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="软链接"):
        start()


def test_stage_error_keeps_usage_and_uncommitted_stage(isolated_research, monkeypatch):
    start()
    async def failed(**kwargs):
        kwargs["usage_callback"](7)
        yield Done(AgentReply(text="部分内容", tool_calls=[], cost_usd=None,
                             is_error=True, error="上游失败", stream_tokens=9))
    monkeypatch.setattr(research, "stream_turn", failed)
    with pytest.raises(RuntimeError, match="上游失败"):
        asyncio.run(research.execute("r1"))
    state = research.get("r1")
    assert state["usage"]["tokens"] == 9 and state["checkpoints"] == {}
    assert state["stage"] == "plan" and state["status"] == "interrupted"


def test_stage_token_limit_crosses_stages_without_reset(isolated_research, normal_model):
    start({"tokens": 20})
    with pytest.raises(research.ResearchLimitError):
        asyncio.run(research.execute("r1"))
    state = research.get("r1")
    assert state["usage"]["tokens"] == 20
    assert list(state["checkpoints"]) == ["plan", "collect"]
    assert state["status"] == "interrupted"
    assert [kwargs["token_budget"] for _, kwargs in normal_model] == [20, 10]


def test_search_leads_cannot_be_recorded_as_page_source(isolated_research):
    start()
    run = research._Stage("r1", "collect")
    args = {"query": "https://example.com/page"}
    assert allow(asyncio.run(run.guard({"tool_name": "WebSearch", "tool_input": args}, "q", None)))
    run.observe(ToolInput("WebSearch", "q", args))
    run.observe(ToolFinished("WebSearch", True, "", "q", "搜索摘要中的论断"))
    assert research.get("r1")["search_results"]
    with pytest.raises(ValueError, match="未成功抓取"):
        research.record_source("r1", "https://example.com/page", "标题", "搜索摘要", "论断")


@pytest.mark.parametrize("stage", ["collect", "review"])
@pytest.mark.parametrize("text", ["错误 [S999]", "错误 [Sxyz]", "错误 [S1, S999]", "错误 [S999"])
def test_intermediate_bad_citations_never_commit(isolated_research, stage, text):
    start()
    asyncio.run(fetch(research._Stage("r1", "collect")))
    before = research.get("r1")
    with pytest.raises(ValueError, match="引用"):
        research._checkpoint("r1", stage, text)
    after = research.get("r1")
    assert stage not in after["checkpoints"]
    assert after == before


def test_intermediate_text_without_citations_is_valid(isolated_research):
    start()
    for stage in ("plan", "collect", "review"):
        research._checkpoint("r1", stage, "本阶段没有可引用的已获取证据，待补查")
    assert list(research.get("r1")["checkpoints"]) == ["plan", "collect", "review"]


@pytest.mark.parametrize("damaged_stage", ["collect", "review"])
@pytest.mark.parametrize("explicit_resume", [True, False])
def test_resume_repairs_earliest_bad_checkpoint_preserves_budget(
        isolated_research, normal_model, damaged_stage, explicit_resume):
    original = start()
    asyncio.run(fetch(research._Stage("r1", "collect")))
    # 模拟旧版本已落盘的损坏文本；绕过新版本的提交校验。
    def corrupt(state):
        state.update(status="interrupted", stage="report", report="旧的未完成报告")
        state["usage"]["tokens"] = 17
        state["checkpoints"] = {"plan": "有效计划", "collect": "有效采集 [S1]",
                                "review": "无效复核 [S999]", "report": "未完成报告 [S999]"}
        state["checkpoints"][damaged_stage] = "损坏阶段 [S999]"
    research_store.update("r1", corrupt)
    before = research.get("r1")
    if explicit_resume:
        research.prepare_resume("r1")
        repaired = research.get("r1")
        index = research.STAGES.index(damaged_stage)
        assert list(repaired["checkpoints"]) == list(research.STAGES[:index])
        assert repaired["stage"] == damaged_stage and repaired["report"] == ""
        assert repaired["usage"] == before["usage"]
        assert repaired["sources"] == before["sources"]
        assert repaired["budget"] == original["budget"]
        assert repaired["deadline"] == original["deadline"]
    report = asyncio.run(research.execute("r1"))
    expected_stages = list(research.STAGES[research.STAGES.index(damaged_stage):])
    assert [stage for stage, _ in normal_model] == expected_stages
    after = research.get("r1")
    assert after["status"] == "done" and "[S999]" not in report
    assert after["usage"]["tokens"] == 17 + 10 * len(expected_stages)
    assert after["deadline"] == original["deadline"]
    assert after["usage"]["fetches"] >= before["usage"]["fetches"]


def test_source_mcp_construction(isolated_research):
    start()
    # 使用真实 SDK 构造，不启动模型或连接外网。
    server = research._source_server("r1")["research"]
    assert server["name"] == "research" and server["type"] == "sdk"


def test_active_task_cannot_execute_or_prepare_again(isolated_research):
    start()
    research._RUNNING.add("r1")
    try:
        with pytest.raises(ValueError, match="仍在运行"):
            asyncio.run(research.execute("r1"))
        with pytest.raises(ValueError, match="仍在运行"):
            research.prepare_resume("r1")
        assert research.recover(set()) == []
    finally:
        research._RUNNING.remove("r1")
