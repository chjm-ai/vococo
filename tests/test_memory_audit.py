"""记忆体检离线验收：真实临时记忆文件，仅替换模型边界。"""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from vococo import config
from vococo.core import agent
from vococo.cron import scheduler
from vococo.memory import audit, session_store


@pytest.fixture
def brain(isolated):
    root = config.AI_BRAIN_DIR
    (root / "memory" / "project").mkdir(parents=True)
    (root / "MEMORY.md").write_text("# 记忆索引\n→ memory/project/INDEX.md\n", encoding="utf-8")
    (root / "memory/project/INDEX.md").write_text("# 项目\n→ decision.md\n", encoding="utf-8")
    (root / "memory/project/decision.md").write_text("# 部署\n当前使用方案甲。\n", encoding="utf-8")
    return root


def test_structure_checks_links_size_duplicates_and_does_not_infer_external_state():
    documents = {"MEMORY.md": "# 目录\n→ memory/a.md\n→ memory/missing.md\n",
                 "memory/a.md": "# 正文\n方案甲\n", "memory/b.md": "# 正文\n方案甲\n",
                 "memory/project/INDEX.md": "# 项目\n[缺失](gone.md)\n" + "长" * 6001,
                 "memory/links.md": "[外部](https://example.com/a.md)\n"}
    issues = audit._structure(documents)
    assert len(issues) == 4
    assert sum(item["kind"] == "重复" for item in issues) == 1
    assert any("memory/missing.md" in item["summary"] for item in issues)
    assert any("memory/project/gone.md" in item["summary"] for item in issues)
    assert any("6000" in item["summary"] for item in issues)


def test_documents_accept_root_symlink_but_reject_internal_escape(brain, tmp_path):
    alias = tmp_path / "shared-brain"
    alias.symlink_to(brain, target_is_directory=True)
    assert "memory/project/decision.md" in audit._documents(alias)[0]
    (brain / "memory/escape.md").symlink_to(tmp_path / "private.md")
    with pytest.raises(ValueError, match="软链"):
        audit._documents(brain)[0]


@pytest.mark.parametrize("case", ["missing", "large", "encoding"])
def test_read_failure_never_becomes_clean_result(brain, monkeypatch, case):
    path = brain / "MEMORY.md"
    if case == "missing":
        path.unlink()
    elif case == "large":
        monkeypatch.setattr(audit, "_MAX_FILE_BYTES", 5)
    else:
        path.write_bytes(b"\xff")
    with pytest.raises((OSError, ValueError, UnicodeError)):
        audit._documents(brain)[0]


def test_chunks_preserve_all_lines_and_rotation_eventually_covers_every_chunk(monkeypatch):
    monkeypatch.setattr(audit, "_CHUNK_CHARS", 12)
    monkeypatch.setattr(audit, "_BODY_CHARS", 20)
    documents = {"a.md": "\n".join(f"行{i}文字" for i in range(10)), "z.md": "最后一行"}
    chunks = audit._chunks(documents)
    reconstructed = [line for chunk in chunks if chunk["path"] == "a.md" for line in chunk["lines"]]
    assert reconstructed == documents["a.md"].splitlines()
    seen, after = set(), []
    for _ in range(len(chunks)):
        selected = audit._batch(chunks, after)
        seen.update((item["path"], item["start"]) for item in selected)
        after = [selected[-1]["path"], selected[-1]["start"]]
    assert len(seen) == len(chunks)
    with pytest.raises(ValueError, match="单行过长"):
        audit._chunks({"huge.md": "x" * 13})


def _issue():
    return {"kind": "过时待核实", "summary": "当前方案缺少适用日期", "suggestion": "原：当前使用方案甲→确认现状后补适用日期。",
            "evidence": [{"path": "memory/project/decision.md", "line": 2, "quote": "当前使用方案甲。"}]}


@pytest.mark.parametrize("bad", ["filename", "line", "quote", "one_conflict", "empty", "shape"])
def test_model_findings_must_match_visible_evidence(brain, bad):
    item = _issue()
    if bad == "filename":
        item["evidence"][0]["path"] = "../outside.md"
    elif bad == "line":
        item["evidence"][0]["line"] = 20
    elif bad == "quote":
        item["evidence"][0]["quote"] = "编造的原文"
    elif bad == "one_conflict":
        item["kind"] = "冲突"
    elif bad == "empty":
        item["evidence"][0]["quote"] = " "
    else:
        item = "不是对象"
    with pytest.raises(ValueError):
        audit._parse(json.dumps({"findings": [item]}, ensure_ascii=False), audit._chunks(audit._documents(brain)[0]))


@pytest.mark.anyio
async def test_review_only_uses_memory_keeps_sources_unchanged_and_deduplicates(brain, monkeypatch):
    before = {str(p): p.read_bytes() for p in brain.rglob("*.md")}
    monkeypatch.setattr(session_store, "load_recent", Mock(side_effect=AssertionError("禁止读取聊天")))

    async def analyze(chunks):
        assert any(chunk["path"] == "MEMORY.md" for chunk in chunks)
        assert all(chunk["path"].endswith(".md") for chunk in chunks)
        return json.dumps({"findings": [_issue()]}, ensure_ascii=False)

    monkeypatch.setattr(audit, "_analyze", analyze)
    first = await audit.review()
    assert "待确认 1 项" in first.text
    assert "未修改任何记忆" in first.text
    assert first.state["covered"] == first.state["segments"]
    audit.save_result(first)
    second = await audit.review()
    assert second.text == ""
    assert second.state["findings"]
    assert {str(p): p.read_bytes() for p in brain.rglob("*.md")} == before
    assert json.loads((config.DATA_DIR / "memory-audit.json").read_text())["version"] == 1


@pytest.mark.anyio
async def test_analyze_isolates_history_settings_and_denies_every_tool(brain, monkeypatch):
    calls = []

    async def fake_stream(history, prompt, **kwargs):
        calls.append(kwargs)
        assert history == []
        assert "session_key" not in kwargs and "resume" not in kwargs
        assert kwargs["disallowed_tools"] == ["WebSearch", "WebFetch"]
        assert kwargs["max_turns"] == 1
        assert "不回顾聊天" in kwargs["system_prompt_extra"]
        for name in ["Bash", "Read", "Write", "WebFetch", "mcp__vococo__recall_past"]:
            decision = await kwargs["tool_guard"]({"tool_name": name}, None, None)
            assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
        yield agent.Done(agent.AgentReply(text='{"findings": []}', tool_calls=[], cost_usd=None, is_error=False))

    monkeypatch.setattr(agent, "stream_turn", fake_stream)
    assert await audit._analyze(audit._chunks(audit._documents(brain)[0])) == '{"findings": []}'
    assert len(calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [TimeoutError(), ValueError("模型没有有效原文依据")])
async def test_failed_audit_notifies_without_advancing_or_reading_history(brain, monkeypatch, failure):
    monkeypatch.setattr(config, "REFLECT_TARGET", "web:main")
    monkeypatch.setattr(session_store, "load_recent", Mock(side_effect=AssertionError("禁止读取聊天")))
    notices = []

    async def analyze(_chunks):
        raise failure

    async def push(platform, target, text):
        notices.append(text)

    monkeypatch.setattr(audit, "_analyze", analyze)
    await scheduler._reflect(push)
    assert len(notices) == 1 and "未完成" in notices[0]
    assert not (config.DATA_DIR / "memory-audit.json").exists()


@pytest.mark.anyio
async def test_clean_audit_is_silent_but_records_coverage(brain, monkeypatch):
    monkeypatch.setattr(config, "REFLECT_TARGET", "web:main")

    async def analyze(_chunks):
        return '{"findings": []}'

    async def push(*_args):
        raise AssertionError("无问题不应打扰")

    monkeypatch.setattr(audit, "_analyze", analyze)
    await scheduler._reflect(push)
    assert json.loads((config.DATA_DIR / "memory-audit.json").read_text())["covered"] == 3


def test_invalid_state_fails_explicitly(brain):
    config.DATA_DIR.mkdir()
    (config.DATA_DIR / "memory-audit.json").write_text('{"version": 1, "after": "bad"}')
    with pytest.raises(ValueError, match="游标"):
        audit._load_state()


def test_reading_redacts_known_secret_shapes(brain):
    secret = "sk-ant-api03-" + "a" * 60
    (brain / "memory/project/decision.md").write_text(secret)
    assert secret not in audit._documents(brain)[0]["memory/project/decision.md"]


def test_rotation_does_not_lose_prefix_sibling_files(monkeypatch):
    monkeypatch.setattr(audit, "_BODY_CHARS", 12)
    chunks = [{"path": "memory/a/x.md", "start": n, "lines": ["123456"]} for n in range(1, 5)]
    chunks += [{"path": "memory/a.md", "start": 1, "lines": ["尾部"]}]
    after, visited = [], set()
    for _ in range(12):
        selected = audit._batch(chunks, after)
        visited.update((item["path"], item["start"]) for item in selected)
        after = [selected[-1]["path"], selected[-1]["start"]]
    assert visited == {(item["path"], item["start"]) for item in chunks}


@pytest.mark.anyio
async def test_overflow_findings_stay_pending_until_actually_shown(brain, monkeypatch):
    (brain / "MEMORY.md").write_text("# 索引\n" + "\n".join(f"→ memory/absent{i}.md" for i in range(8)))
    runs = []

    async def analyze(_chunks):
        runs.append(1)
        return json.dumps({"findings": [_issue()] if len(runs) == 1 else []}, ensure_ascii=False)

    monkeypatch.setattr(audit, "_analyze", analyze)
    first = await audit.review()
    assert "另 1 项" in first.text
    assert len(first.state["reported"]) == 8
    assert len(first.state["pending"]) == 1
    audit.save_result(first)
    second = await audit.review()
    assert "当前方案缺少适用日期" in second.text
    assert second.state["pending"] == {}
    assert len(second.state["reported"]) == 9


def test_link_fingerprints_include_target_and_normalize_evidence_order():
    index = "[A](memory/a.md) [B](memory/b.md)"
    first = audit._structure({"MEMORY.md": index, "memory/b.md": "B"})[0]
    second = audit._structure({"MEMORY.md": index, "memory/a.md": "A"})[0]
    assert audit._issue_key(first) != audit._issue_key(second)
    item = _issue()
    item["evidence"].append({"path": "memory/other.md", "line": 1, "quote": "另一原文"})
    original = audit._issue_key(item)
    item["evidence"].reverse()
    assert audit._issue_key(item) == original


def test_redaction_preserves_original_line_numbers_and_raw_duplicate_check(brain):
    first = "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n结论在原文第四行。\n"
    second = first.replace("AAAA", "BBBB")
    (brain / "memory/project/decision.md").write_text(first)
    (brain / "memory/project/other.md").write_text(second)
    documents, fingerprints = audit._documents(brain)
    assert documents["memory/project/decision.md"].splitlines()[3] == "结论在原文第四行。"
    assert "AAAA" not in documents["memory/project/decision.md"]
    assert documents["memory/project/decision.md"] == documents["memory/project/other.md"]
    assert not any(item["kind"] == "重复" for item in audit._structure(documents, fingerprints))


def test_encoded_markdown_links_are_valid():
    documents = {"MEMORY.md": "[空格](memory/my%20note.md)\n[中文](memory/%E4%B8%AD%E6%96%87.md)",
                 "memory/my note.md": "正文甲", "memory/中文.md": "正文乙"}
    assert audit._structure(documents) == []


@pytest.mark.anyio
async def test_state_save_failure_is_reported_even_when_review_is_clean(brain, monkeypatch):
    monkeypatch.setattr(config, "REFLECT_TARGET", "web:main")
    notices = []

    async def analyze(_chunks):
        return '{"findings": []}'

    async def push(_platform, _target, text):
        notices.append(text)

    monkeypatch.setattr(audit, "_analyze", analyze)
    monkeypatch.setattr(audit, "save_result", Mock(side_effect=OSError("磁盘已满")))
    await scheduler._reflect(push)
    assert len(notices) == 1 and "状态保存失败" in notices[0]
    assert not (config.DATA_DIR / "memory-audit.json").exists()


def test_resolved_link_does_not_survive_in_pending_queue():
    issue = audit._structure({"MEMORY.md": "[A](memory/a.md)"})[0]
    key = audit._issue_key(issue)
    state = {"pending": {key: {"issue": issue, "sources": {"MEMORY.md": "same"}}}}
    assert audit._pending_issues([], state, {"MEMORY.md": "same", "memory/a.md": "new"}) == {}
