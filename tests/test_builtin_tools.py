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


def test_gen_image_rejects_deprecated_model(isolated):
    from vococo.tools import builtin

    out = _text(_run(builtin.generate_image.handler({"prompt": "猫", "model": "gpt-image-1.5"})))
    assert "当前仅支持 gpt-image-2" in out


def test_gen_image_no_provider(isolated, monkeypatch):
    from vococo.tools import builtin

    monkeypatch.setattr("vococo.providers.sidecar_env", lambda name: None)
    out = _text(_run(builtin.generate_image.handler({"prompt": "猫"})))
    assert "未配置 codex-gpt" in out


# === generate_image 的参考图读取 ===
_PNG_STUB = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_ref_images_reads_real_png(tmp_path):
    from vococo.tools import builtin

    f = tmp_path / "a.png"
    f.write_bytes(_PNG_STUB)
    refs, err = builtin._load_reference_images([str(f)])
    assert err == ""
    assert refs[0][0] == "image/png" and refs[0][1] == "a.png"


def test_ref_images_rejects_non_image_by_content(tmp_path):
    """后缀是 .png 但内容是文本 —— 按内容判断必须拦下,防任意文件外传。"""
    from vococo.tools import builtin

    f = tmp_path / "fake.png"
    f.write_text("PRIVATE KEY", encoding="utf-8")
    refs, err = builtin._load_reference_images([str(f)])
    assert refs == [] and "不是图片文件" in err


def test_ref_images_rejects_missing_path(tmp_path):
    from vococo.tools import builtin

    refs, err = builtin._load_reference_images([str(tmp_path / "nope.png")])
    assert refs == [] and "不存在" in err


def test_ref_images_caps_count(tmp_path):
    from vococo.tools import builtin

    paths = []
    for i in range(builtin._MAX_REF_IMAGES + 1):
        f = tmp_path / f"{i}.png"
        f.write_bytes(_PNG_STUB)
        paths.append(str(f))
    refs, err = builtin._load_reference_images(paths)
    assert refs == [] and "最多" in err


def test_ref_images_rejects_oversize_before_reading(tmp_path, monkeypatch):
    """先看大小再读文件:超限的文件不能先进内存。"""
    from vococo.tools import builtin

    monkeypatch.setattr(builtin, "_MAX_REF_BYTES", 8)
    f = tmp_path / "big.png"
    f.write_bytes(_PNG_STUB)
    refs, err = builtin._load_reference_images([str(f)])
    assert refs == [] and "过大" in err


def test_ref_images_rejects_non_list():
    from vococo.tools import builtin

    refs, err = builtin._load_reference_images("a.png")
    assert refs == [] and "数组" in err


def _patch_session(monkeypatch, calls: list):
    """换掉 aiohttp.ClientSession:记录请求参数,回一张假图。"""
    import base64

    import aiohttp

    class FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def json(self):
            return {"data": [{"b64_json": base64.b64encode(_PNG_STUB).decode()}]}

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return FakeResp()

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)


def _fake_provider(monkeypatch):
    monkeypatch.setattr(
        "vococo.providers.sidecar_env",
        lambda name: (
            "gpt-6.1-sol",
            {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "ANTHROPIC_API_KEY": "k"},
        ),
    )


def test_gen_image_with_refs_goes_to_edits(tmp_path, monkeypatch, isolated):
    """带参考图必须走 /v1/images/edits 的 multipart,字段名固定 image[]。"""
    from vococo import config
    from vococo.tools import builtin

    ref = tmp_path / "box.png"
    ref.write_bytes(_PNG_STUB)
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "images")
    _fake_provider(monkeypatch)
    calls: list = []
    _patch_session(monkeypatch, calls)

    out = _text(_run(builtin.generate_image.handler(
        {"prompt": "摆三个盒子", "reference_images": [str(ref)]}
    )))

    url, kwargs = calls[0]
    assert url.endswith("/v1/images/edits")
    # aiohttp 3.14 的 _fields 元素是 (<MultiDict('name': ...)>, headers, value),
    # 老版本第一位直接是字符串 —— 两种都认,免得升个依赖测试就红
    def field_name(field) -> str:
        head = field[0]
        return head["name"] if hasattr(head, "get") else head

    names = [field_name(f) for f in kwargs["data"]._fields]
    assert "image[]" in names, "字段名只能是 image[](OpenAI 多图约定)"
    assert {"model", "prompt", "size", "n"} <= set(names)
    assert "已生成图片" in out


def test_gen_image_without_refs_keeps_generations(tmp_path, monkeypatch, isolated):
    """不带参考图的老路径不能改坏。"""
    from vococo import config
    from vococo.tools import builtin

    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "images")
    _fake_provider(monkeypatch)
    calls: list = []
    _patch_session(monkeypatch, calls)

    _run(builtin.generate_image.handler({"prompt": "猫"}))

    url, kwargs = calls[0]
    assert url.endswith("/v1/images/generations")
    assert kwargs["json"]["model"] == "gpt-image-2"


def test_send_image_rejects_same_turn_auto_generated_image(tmp_path, monkeypatch):
    """generate_image 已自动展示的文件，send_image 必须拒绝重复发送。"""
    from vococo.gateway import clarify
    from vococo.gateway.adapters import web as web_mod
    from vococo.tools import builtin

    class FakeWebAdapter:
        async def send_image(self, *args):
            raise AssertionError("不应重复发送")

    monkeypatch.setattr(web_mod, "WebAdapter", FakeWebAdapter)
    image_path = tmp_path / "generated.png"
    token = clarify.set_current("web:test", FakeWebAdapter(), "test")
    try:
        clarify.current().auto_sent_image_paths.add(str(image_path.resolve()))
        out = _text(_run(builtin.send_image.handler({"path": str(image_path)})))
    finally:
        clarify.reset_current(token)

    assert "已由 generate_image 自动发送" in out


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


# === 工作台任务查询 / 新建 / 更新 ===
def test_list_workbench_tasks_filters(isolated):
    import datetime

    from vococo.memory import workbench
    from vococo.tools import builtin

    today = datetime.date.today().isoformat()
    workbench.create_task("consulting", "今天的活", date=today)
    workbench.create_task("consulting", "很久以前", date="2020-01-01", status="done")

    by_status = _text(_run(builtin.list_workbench_tasks.handler({"status": "focus"})))
    assert "约见第一梯队" in by_status and "今天的活" not in by_status

    by_project = _text(_run(builtin.list_workbench_tasks.handler({"project": "面料外贸"})))
    assert "DIFS 展会预热" in by_project and "今天的活" not in by_project

    by_today = _text(_run(builtin.list_workbench_tasks.handler({"date": "today"})))
    assert "今天的活" in by_today and "很久以前" not in by_today

    overdue = _text(_run(builtin.list_workbench_tasks.handler({"date": "overdue"})))
    assert "很久以前" not in overdue

    bad = _text(_run(builtin.list_workbench_tasks.handler({"status": "nope"})))
    assert "未知状态" in bad


def test_create_and_update_workbench_task_roundtrip(isolated):
    from vococo.memory import workbench
    from vococo.tools import builtin

    out = _text(_run(builtin.create_workbench_task.handler({
        "project": "AI 咨询", "title": "写周报", "detail": "提纲", "date": "2026-11-05",
        "assignee": "ai",
    })))
    assert "新建任务" in out and "写周报" in out
    task = next(t for t in workbench.list_tasks() if t["title"] == "写周报")
    assert task["week"] == "2026-11-02" and task["month"] == "2026-11" and task["assignee"] == "ai"

    upd = _text(_run(builtin.update_workbench_task.handler({
        "task": "写周报", "status": "done", "project": "VocoTrade", "title": "写双周报",
    })))
    assert "已更新" in upd
    task = workbench.get_task(task["id"])
    assert task["status"] == "done" and task["completedAt"] is not None
    assert task["project"] == "vocotrade" and task["title"] == "写双周报"

    miss = _text(_run(builtin.update_workbench_task.handler({"task": "写双周报", "project": "不存在"})))
    assert "没找到项目" in miss


# === switch_model(自然语言切模型) ===
def _point_settings(tmp_path, monkeypatch):
    """把设置页存储指向临时文件,避免读到真实 data/web_settings.json 的供应商。"""
    from vococo.gateway import settings_store

    monkeypatch.setattr(settings_store, "_PATH", tmp_path / "web_settings.json")


def _run_switch_model(model: str):
    from vococo.tools import builtin

    return _text(_run(builtin.switch_model.handler({"model": model})))


def test_switch_model_no_ctx_refused(isolated, monkeypatch):
    """没有会话上下文(后台任务等非对话轮次)时拒绝,不乱切。"""
    from vococo.tools import builtin

    _point_settings(isolated, monkeypatch)
    assert "无法切换" in _run_switch_model("opus 4.6")
    # 空参也要有明确报错
    assert "需要 model" in _run_switch_model("  ")


def test_switch_model_hit_writes_chosen_model(isolated, monkeypatch):
    """口语命中唯一模型 → 写入 chosen_model,下一轮生效。"""
    from vococo.gateway import clarify
    from vococo.memory import session_store

    _point_settings(isolated, monkeypatch)
    token = clarify.set_current("cli-switch-1", object(), "chat")
    try:
        out = _run_switch_model("opus 4.6")
    finally:
        clarify.reset_current(token)
    assert "claude-opus-4-6" in out
    assert "下一轮" in out
    assert session_store.get_chosen_model("cli-switch-1") == "claude-opus-4-6"


def test_switch_model_ambiguous_asks_back(isolated, monkeypatch):
    """「opus」命中多档 → 不擅自挑,列候选让用户澄清,库不动。"""
    from vococo.gateway import clarify
    from vococo.memory import session_store

    _point_settings(isolated, monkeypatch)
    token = clarify.set_current("cli-switch-2", object(), "chat")
    try:
        out = _run_switch_model("opus")
    finally:
        clarify.reset_current(token)
    assert "多个模型" in out and "claude-opus-5-5" in out
    assert session_store.get_chosen_model("cli-switch-2") == ""


def test_switch_model_miss_lists_available(isolated, monkeypatch):
    from vococo.gateway import clarify

    _point_settings(isolated, monkeypatch)
    token = clarify.set_current("cli-switch-3", object(), "chat")
    try:
        out = _run_switch_model("gpt-9")
    finally:
        clarify.reset_current(token)
    assert "没找到" in out and "可用的有" in out


def test_switch_model_restore_default_clears(isolated, monkeypatch):
    """「恢复默认」清掉本会话的模型覆盖,回到回落链。"""
    from vococo.gateway import clarify
    from vococo.memory import session_store

    _point_settings(isolated, monkeypatch)
    session_store.set_chosen_model("cli-switch-4", "claude-opus-4-6")
    token = clarify.set_current("cli-switch-4", object(), "chat")
    try:
        out = _run_switch_model("恢复默认")
    finally:
        clarify.reset_current(token)
    assert "取消" in out
    assert session_store.get_chosen_model("cli-switch-4") == ""
