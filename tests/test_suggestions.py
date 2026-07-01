"""建议(suggestion)系统:登记/去重/接受/忽略、目录播种、suggest_automation 工具。"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def sugg_env(isolated, monkeypatch):
    # isolated 把 DATA_DIR 指到 tmp;但 SUGGESTIONS_PATH/CRON_JOBS_PATH 是导入期常量,单独 patch
    from claude_hermes import config

    data = isolated / "data"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "SUGGESTIONS_PATH", data / "suggestions.json")
    monkeypatch.setattr(config, "CRON_JOBS_PATH", data / "cron_jobs.json")
    return data


def _spec(name="晨报", cron="0 8 * * *"):
    return {"name": name, "prompt": "do it", "schedule": {"kind": "cron", "expr": cron}}


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def test_add_and_list_pending(sugg_env):
    from claude_hermes.cron import suggestions

    rec = suggestions.add_suggestion(
        title="晨报", description="每天8点", source="catalog",
        job_spec=_spec(), dedup_key="catalog:晨报",
    )
    assert rec is not None
    pending = suggestions.list_pending()
    assert len(pending) == 1 and pending[0]["title"] == "晨报"


def test_dedup_pending_and_dismissed(sugg_env):
    from claude_hermes.cron import suggestions

    assert suggestions.add_suggestion(
        title="A", description="", source="usage", job_spec=_spec(), dedup_key="k1"
    )
    # 相同 dedup_key 仍待定 → 不重复
    assert suggestions.add_suggestion(
        title="A", description="", source="usage", job_spec=_spec(), dedup_key="k1"
    ) is None
    # 忽略后其 dedup_key 永不再提
    sid = suggestions.list_pending()[0]["id"]
    assert suggestions.dismiss_suggestion(sid)
    assert suggestions.add_suggestion(
        title="A", description="", source="usage", job_spec=_spec(), dedup_key="k1"
    ) is None
    assert suggestions.list_pending() == []


def test_accept_creates_job(sugg_env):
    from claude_hermes.cron import scheduler, suggestions

    suggestions.add_suggestion(
        title="晨报", description="", source="catalog",
        job_spec=_spec(), dedup_key="catalog:晨报",
    )
    sid = suggestions.list_pending()[0]["id"]
    origin = {"platform": "telegram", "chat_id": 123}
    job = suggestions.accept_suggestion(sid, origin=origin)
    assert job is not None
    assert job["name"] == "晨报"
    assert job["target"] == origin
    assert job["enabled"] is True
    # 真落进 cron_jobs.json
    assert any(j["name"] == "晨报" for j in scheduler.load_jobs())
    # 状态变 accepted,不再 pending
    assert suggestions.list_pending() == []


def test_accept_nonpending_returns_none(sugg_env):
    from claude_hermes.cron import suggestions

    assert suggestions.accept_suggestion("nope") is None


def test_max_pending(sugg_env):
    from claude_hermes.cron import suggestions

    for i in range(suggestions.MAX_PENDING):
        assert suggestions.add_suggestion(
            title=f"T{i}", description="", source="usage",
            job_spec=_spec(), dedup_key=f"k{i}",
        )
    # 满了 → 丢弃
    assert suggestions.add_suggestion(
        title="over", description="", source="usage", job_spec=_spec(), dedup_key="kover"
    ) is None


def test_get_by_index_and_title(sugg_env):
    from claude_hermes.cron import suggestions

    suggestions.add_suggestion(
        title="晨报", description="", source="catalog", job_spec=_spec(), dedup_key="d1"
    )
    assert suggestions.get_suggestion("1")["title"] == "晨报"       # 1-based 序号
    assert suggestions.get_suggestion("晨报")["dedup_key"] == "d1"  # 按标题


def test_catalog_seed_idempotent(sugg_env):
    from claude_hermes.cron import suggestion_catalog, suggestions

    n1 = suggestion_catalog.seed()
    assert n1 >= 1
    assert suggestion_catalog.seed() == 0  # 第二次全被 dedup 跳过
    assert len(suggestions.list_pending()) == n1


def test_suggest_automation_tool_ok(sugg_env):
    from claude_hermes.cron import suggestions
    from claude_hermes.tools import builtin

    out = _text(asyncio.run(builtin.suggest_automation.handler(
        {"title": "每日站会", "description": "每天9点", "cron": "0 9 * * *", "prompt": "站会"}
    )))
    assert "已提建议" in out
    assert any(s["title"] == "每日站会" for s in suggestions.list_pending())


def test_suggest_automation_bad_cron(sugg_env):
    from claude_hermes.tools import builtin

    out = _text(asyncio.run(builtin.suggest_automation.handler(
        {"title": "x", "description": "y", "cron": "乱写", "prompt": "z"}
    )))
    assert "不合法" in out


def test_suggest_command_lists_and_accepts(sugg_env):
    from claude_hermes.cron import suggestion_catalog
    from claude_hermes.gateway import core

    suggestion_catalog.seed()
    # 无参 → 出 Choice(带接受/忽略按钮)
    out = core.handle_command("/suggest", "tg:-100", "m")
    assert out.choice is not None
    assert any("接受" in label for _, label in out.choice.options)
    # 接受第 1 条(用 1-based 序号)
    out2 = core.handle_command("/suggest accept 1", "tg:-100", "m")
    assert "已接受" in out2.reply


# === cron 管理工具 ===
def test_cron_admin_list_toggle_delete(sugg_env):
    from claude_hermes.cron import scheduler, suggestions
    from claude_hermes.tools import builtin

    # 先接受一条建议造出一个任务
    suggestions.add_suggestion(
        title="晨报", description="", source="catalog", job_spec=_spec(), dedup_key="c"
    )
    sid = suggestions.list_pending()[0]["id"]
    suggestions.accept_suggestion(sid, origin={"platform": "telegram", "chat_id": 1})

    assert "晨报" in _text(asyncio.run(builtin.list_cron_jobs.handler({})))

    out = _text(asyncio.run(builtin.set_cron_job_enabled.handler({"ref": "晨报", "enabled": False})))
    assert "已停用" in out
    assert scheduler.load_jobs()[0]["enabled"] is False

    out = _text(asyncio.run(builtin.delete_cron_job.handler({"ref": "1"})))  # 按序号
    assert "已删除" in out
    assert scheduler.load_jobs() == []


def test_cron_admin_not_found(sugg_env):
    from claude_hermes.tools import builtin

    out = _text(asyncio.run(builtin.delete_cron_job.handler({"ref": "查无此任务"})))
    assert "没找到" in out
