"""原生工具:recall_past 召回 / save_memory 写回(各分支 + 防护)。

工具 handler 是协程,用 asyncio.run 同步调用,免装 pytest-asyncio。
"""
from __future__ import annotations

import asyncio


def _run(coro):
    return asyncio.run(coro)


def _text(result: dict) -> str:
    return result["content"][0]["text"]


# === recall_past ===
def test_recall_hit(isolated):
    from vococo.memory import session_store
    from vococo.tools import builtin

    session_store.append("cli", "我要去名古屋出差", "记下了")
    out = _text(_run(builtin.recall_past.handler({"query": "名古屋"})))
    assert "名古屋出差" in out
    assert "来源会话 cli" in out


def test_recall_miss(isolated):
    from vococo.tools import builtin

    out = _text(_run(builtin.recall_past.handler({"query": "查无此词zzz"})))
    assert "没有找到" in out


def test_recall_empty_query(isolated):
    from vococo.tools import builtin

    out = _text(_run(builtin.recall_past.handler({"query": "  "})))
    assert "非空" in out


# === save_memory ===
def test_save_new_writes_file_and_index(isolated):
    from vococo import config
    from vococo.tools import builtin

    out = _text(_run(builtin.save_memory.handler(
        {"topic": "demo-srv", "title": "演示服务器", "summary": "一句话摘要", "body": "## 访问\n- ssh ..."}
    )))
    assert "✅" in out
    f = config.AI_BRAIN_DIR / "memory" / "demo-srv.md"
    content = f.read_text(encoding="utf-8")
    assert "# 演示服务器" in content
    assert "> 一句话摘要" in content
    assert "created:" in content
    index = (config.AI_BRAIN_DIR / "MEMORY.md").read_text(encoding="utf-8")
    assert "## 其他主题" in index
    assert "→ memory/demo-srv.md — 一句话摘要" in index


def test_save_with_category_goes_to_section(isolated):
    from vococo import config
    from vococo.tools import builtin

    # 预置一个带分节的索引
    (config.AI_BRAIN_DIR).mkdir(parents=True, exist_ok=True)
    (config.AI_BRAIN_DIR / "MEMORY.md").write_text(
        "# 记忆索引\n\n## 服务器 / 基础设施\n→ memory/old.md — 旧条目\n\n## 用户偏好\n→ memory/p.md — 偏好\n",
        encoding="utf-8",
    )
    _run(builtin.save_memory.handler(
        {"topic": "new-vps", "title": "新机", "summary": "新机摘要",
         "body": "x", "category": "服务器 / 基础设施"}
    ))
    index = (config.AI_BRAIN_DIR / "MEMORY.md").read_text(encoding="utf-8")
    # 新行应登记在「服务器 / 基础设施」分节内,而非「用户偏好」之后
    srv_pos = index.index("## 服务器 / 基础设施")
    pref_pos = index.index("## 用户偏好")
    new_pos = index.index("→ memory/new-vps.md — 新机摘要")
    assert srv_pos < new_pos < pref_pos


def test_save_existing_rejected(isolated):
    from vococo import config
    from vococo.tools import builtin

    mem = config.AI_BRAIN_DIR / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "dup.md").write_text("已有内容", encoding="utf-8")
    out = _text(_run(builtin.save_memory.handler(
        {"topic": "dup", "title": "x", "summary": "y", "body": "z"}
    )))
    assert "已存在" in out
    assert (mem / "dup.md").read_text(encoding="utf-8") == "已有内容"  # 没被覆盖


def test_save_illegal_topic_blocks_traversal(isolated):
    from vococo.tools import builtin

    out = _text(_run(builtin.save_memory.handler(
        {"topic": "../evil", "title": "x", "summary": "y", "body": "z"}
    )))
    assert "非法" in out


def test_save_missing_fields(isolated):
    from vococo.tools import builtin

    out = _text(_run(builtin.save_memory.handler(
        {"topic": "a", "title": "", "summary": "y", "body": "z"}
    )))
    assert "四项都非空" in out


def test_save_summary_too_long(isolated):
    from vococo.tools import builtin

    out = _text(_run(builtin.save_memory.handler(
        {"topic": "long", "title": "x", "summary": "字" * 200, "body": "z"}
    )))
    assert "太长" in out


# === generate_image ===
def test_gen_image_empty_prompt(isolated):
    from vococo.tools import builtin

    out = _text(_run(builtin.generate_image.handler({"prompt": "  "})))
    assert "非空 prompt" in out


def test_gen_image_bad_size(isolated):
    from vococo.tools import builtin

    out = _text(_run(builtin.generate_image.handler({"prompt": "猫", "size": "999x999"})))
    assert "仅支持 1024x1024" in out


def test_gen_image_no_provider(isolated, monkeypatch):
    from vococo.tools import builtin

    monkeypatch.setattr("vococo.providers.sidecar_env", lambda name: None)
    out = _text(_run(builtin.generate_image.handler({"prompt": "猫"})))
    assert "未配置 codex-gpt" in out


# === 工作台项目操作 ===
def test_create_rename_and_archive_workbench_project(isolated, monkeypatch):
    from vococo.memory import workbench
    from vococo.tools import builtin

    async def approve(*args):
        return True

    created = _text(_run(builtin.create_workbench_project.handler({"name": "新项目"})))
    assert "已创建" in created
    renamed = _text(_run(builtin.rename_workbench_project.handler({
        "project": "新项目", "name": "已改名项目",
    })))
    assert "重命名" in renamed
    monkeypatch.setattr("vococo.tools.danger.require_approval", approve)
    archived = _text(_run(builtin.archive_workbench_project.handler({"project": "已改名项目"})))
    assert "已归档" in archived
    assert "已改名项目" not in {project["name"] for project in workbench.list_projects()}


def test_move_workbench_task_moves_task_tree_to_project(isolated):
    from vococo.memory import workbench
    from vococo.tools import builtin

    projects = {project["name"]: project["id"] for project in workbench.list_projects()}
    out = _text(_run(builtin.move_workbench_task.handler({
        "task": "crawler-plan", "project": "VocoTrade",
    })))

    assert "已将任务" in out
    assert workbench.get_task("crawler-plan")["project"] == projects["VocoTrade"]
