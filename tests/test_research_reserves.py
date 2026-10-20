"""阶段预算回归：搜集必须为复核和成稿留额度，不通过提高总预算掩盖失败。"""
import asyncio
import json
import time

import pytest

from vococo.core import research
from test_research import allow, fetch, isolated_research, reply


def test_collect_stops_network_before_spending_report_reserve(isolated_research):
    research.initialize("r1", "复杂研究", "fixed-model", {"tokens": 65000})
    run = research._Stage("r1", "collect")
    research.store.update("r1", lambda state: state["usage"].update(tokens=34000))
    result = asyncio.run(run.guard({"tool_name": "WebSearch", "tool_input": {
        "query": "继续补查"}}, "s1", None))
    assert not allow(result)
    assert "成稿" in result["hookSpecificOutput"]["permissionDecisionReason"]
    assert research.get("r1")["usage"]["searches"] == 0


def test_review_stops_network_before_report_reserve(isolated_research):
    research.initialize("r1", "复杂研究", "fixed-model", {"tokens": 65000})
    research.store.update("r1", lambda state: state["usage"].update(tokens=50000))
    result = asyncio.run(research._Stage("r1", "review").guard({
        "tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}}, "f1", None))
    assert not allow(result)
    assert research.get("r1")["usage"]["fetches"] == 0


def test_time_reserve_stops_network_but_allows_source_registration(isolated_research):
    research.initialize("r1", "复杂研究", "fixed-model")
    research.store.update("r1", lambda state: state.update(deadline=time.time() + 30))
    run = research._Stage("r1", "collect")
    denied = asyncio.run(run.guard({"tool_name": "WebSearch", "tool_input": {}}, "s1", None))
    allowed = asyncio.run(run.guard({"tool_name": "mcp__research__record_source",
                                   "tool_input": {}}, "r1", None))
    assert not allow(denied) and allow(allowed)


def test_budget_constrained_research_reaches_report_without_reset(isolated_research, monkeypatch):
    research.initialize("r1", "复杂研究", "fixed-model", {"tokens": 65000})
    async def model(**kwargs):
        stage = kwargs["user_text"].split("。", 1)[0].split("：")[1]
        if stage == "collect":
            run = research._Stage("r1", stage)
            await fetch(run)
            # 不合作的模型在下一次工具调用前直接继续生成，不能依赖它服从收束提示。
            kwargs["usage_callback"](35000)
            raise AssertionError("执行器必须在阶段额度耗尽时主动结束模型流")
        amount = {"plan": 4000, "review": 8000, "report": 8000}[stage]
        yield reply("证据有限，未覆盖全部要求 [S1]" if stage == "report" else "已收束，待核清单", amount)
    monkeypatch.setattr(research, "stream_turn", model)
    report = asyncio.run(research.execute("r1"))
    state = research.get("r1")
    assert state["status"] == "done" and state["usage"]["tokens"] == 55000
    assert "未完成" in state["checkpoints"]["collect"]
    assert "有限报告" in report
    assert state["budget"]["tokens"] == 65000
    assert "未覆盖全部要求" in report


def test_convergence_without_sources_cannot_fabricate_success(isolated_research, monkeypatch):
    research.initialize("r1", "复杂研究", "fixed-model", {"tokens": 100})
    async def model(**kwargs):
        stage = kwargs["user_text"].split("。", 1)[0].split("：")[1]
        if stage == "collect":
            kwargs["usage_callback"](50)
        yield reply("计划", 1)
    monkeypatch.setattr(research, "stream_turn", model)
    with pytest.raises(research.ResearchLimitError, match="尚无成功获取的来源"):
        asyncio.run(research.execute("r1"))
    assert research.get("r1")["report"] == ""
    assert research.get("r1")["status"] == "interrupted"


@pytest.mark.parametrize("failure", ["is_error", "budget_exceeded"])
def test_failed_done_at_reserve_is_not_success(isolated_research, monkeypatch, failure):
    research.initialize("r1", "测试", "fixed-model", {"tokens": 100})
    async def model(**kwargs):
        stage = kwargs["user_text"].split("。", 1)[0].split("：")[1]
        if stage == "collect":
            research._save_page("r1", "https://example.com/", "已保存证据", "f1")
        event = reply("有限结论 [S1]", 50 if stage == "collect" else 1)
        if stage == "collect":
            setattr(event.reply, failure, True)
            event.reply.error = "HTTP 500" if failure == "is_error" else "阶段超限"
        yield event
    monkeypatch.setattr(research, "stream_turn", model)
    with pytest.raises(RuntimeError, match="HTTP 500|阶段超限"):
        asyncio.run(research.execute("r1"))
    state = research.get("r1")
    assert state["status"] == "interrupted" and state["usage"]["tokens"] == 51
    assert "collect" not in state["checkpoints"]
    assert "collect" not in state.get("limited_stages", []) and state["report"] == ""


def test_absolute_budget_still_wins_over_soft_convergence(isolated_research):
    research.initialize("r1", "复杂研究", "fixed-model", {"tokens": 100})
    with pytest.raises(research.ResearchLimitError, match="token 预算"):
        research._Stage("r1", "collect").usage(101)
    assert research.get("r1")["usage"]["tokens"] == 101
    assert research.get("r1")["budget"]["tokens"] == 100


def test_resume_does_not_refresh_network_allowance(isolated_research):
    state = research.initialize("r1", "复杂研究", "fixed-model", {"tokens": 100})
    research.store.update("r1", lambda item: item["usage"].update(tokens=60))
    research.prepare_resume("r1")
    restored = research.get("r1")
    assert restored["deadline"] == state["deadline"]
    assert restored["usage"]["tokens"] == 60
    with pytest.raises(research.ResearchLimitError, match="成稿"):
        research._check_network_reserve(restored, "collect")


def test_stage_prompt_has_remaining_and_stop_targets(isolated_research):
    state = research.initialize("r1", "复杂研究", "fixed-model", {"tokens": 65000})
    text = research._prompt(state, "collect")
    payload = json.loads(text[text.index("{"):])
    assert payload["stage_limits"]["stop_network_at_tokens"] < 65000
    assert payload["stage_limits"]["reserved_tokens"] > 0
