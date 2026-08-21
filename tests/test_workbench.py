"""工作台(GTD 待办看板):种子导入 / 项目 CRUD / 任务 CRUD / 图片落盘。"""
from __future__ import annotations

import base64


def test_first_access_seeds_demo_data(isolated):
    from vococo.memory import workbench

    projects = workbench.list_projects()
    assert {p["name"] for p in projects} == {"AI 咨询", "VocoTrade", "面料外贸", "离职过渡"}
    tasks = workbench.list_tasks()
    assert len(tasks) == 12


def test_create_project_not_hardcoded(isolated):
    """核心诉求:项目不写死,能随时新建并立刻在列表里看到。"""
    from vococo.memory import workbench

    before = {p["id"] for p in workbench.list_projects()}
    project = workbench.create_project("新项目")
    after = workbench.list_projects()
    assert project["id"] not in before
    assert any(p["id"] == project["id"] and p["name"] == "新项目" for p in after)


def test_rename_and_archive_project(isolated):
    from vococo.memory import workbench

    project = workbench.create_project("临时项目")
    renamed = workbench.rename_project(project["id"], "改名后")
    assert renamed["name"] == "改名后"

    workbench.archive_project(project["id"])
    assert project["id"] not in {p["id"] for p in workbench.list_projects()}


def test_reorder_projects(isolated):
    from vococo.memory import workbench

    ids = [p["id"] for p in workbench.list_projects()]
    new_order = list(reversed(ids))
    workbench.reorder_projects(new_order)
    assert [p["id"] for p in workbench.list_projects()] == new_order


def test_create_update_delete_task(isolated):
    from vococo.memory import workbench

    project = workbench.create_project("任务测试项目")
    task = workbench.create_task(project["id"], "写个任务", date="2026-08-21", month="2026-08", week="2026-08-17")
    assert task["status"] == "todo"
    assert task["project"] == project["id"]
    assert task["date"] == "2026-08-21"
    assert task["week"] == "2026-08-17"

    child = workbench.create_task(
        project["id"], "子任务", parent_id=task["id"], month="2026-08", week="2026-08-17"
    )
    assert child["parentId"] == task["id"]
    assert child["date"] is None

    unscheduled = workbench.update_task(child["id"], date=None, month=None, week=None)
    assert unscheduled["date"] is None
    assert unscheduled["month"] is None
    assert unscheduled["week"] is None

    updated = workbench.update_task(task["id"], status="done", title="改了标题")
    assert updated["status"] == "done"
    assert updated["title"] == "改了标题"

    # delete_task 是软删除(移入回收站):get_task 仍能查到、但从 list_tasks 里消失。
    assert workbench.delete_task(task["id"]) is True
    assert workbench.get_task(task["id"])["deletedAt"] is not None
    assert task["id"] not in {t["id"] for t in workbench.list_tasks()}
    assert workbench.delete_task(task["id"]) is False  # 已经在回收站,再删返回 False


def test_trash_restore_and_purge(isolated):
    from vococo.memory import workbench

    project = workbench.create_project("回收站测试项目")
    task = workbench.create_task(project["id"], "会被删的任务")
    workbench.delete_task(task["id"])

    trashed = workbench.list_deleted_tasks()
    assert [t["id"] for t in trashed] == [task["id"]]

    restored = workbench.restore_task(task["id"])
    assert restored["deletedAt"] is None
    assert task["id"] in {t["id"] for t in workbench.list_tasks()}
    assert workbench.list_deleted_tasks() == []
    assert workbench.restore_task(task["id"]) is None  # 不在回收站里,恢复返回 None

    assert workbench.purge_task(task["id"]) is False  # 还没删,彻底删除拒绝
    workbench.delete_task(task["id"])
    assert workbench.purge_task(task["id"]) is True
    assert workbench.get_task(task["id"]) is None  # 彻底删除后真的没了
    assert workbench.purge_task(task["id"]) is False  # 已经不存在,再删返回 False


def test_completed_at_tracks_status_toggle(isolated):
    """已完成视图按「完成当天」分组，靠的就是这个字段——status 切 done/切回都要跟上。"""
    from vococo.memory import workbench

    project = workbench.create_project("完成时间测试项目")
    task = workbench.create_task(project["id"], "待完成任务")
    assert task["completedAt"] is None

    done = workbench.update_task(task["id"], status="done")
    assert done["completedAt"] is not None

    reopened = workbench.update_task(task["id"], status="todo")
    assert reopened["completedAt"] is None

    created_done = workbench.create_task(project["id"], "创建即完成", status="done")
    assert created_done["completedAt"] is not None


def test_empty_trash(isolated, monkeypatch):
    from vococo import config
    from vococo.memory import workbench

    monkeypatch.setattr(config, "IMAGES_DIR", isolated / "data" / "images")

    project = workbench.create_project("清空回收站测试项目")
    kept = workbench.create_task(project["id"], "不会被删的任务")
    trashed_a = workbench.create_task(project["id"], "会被清空的任务A")
    trashed_b = workbench.create_task(project["id"], "会被清空的任务B")
    data = base64.b64encode(b"fake-png-bytes").decode()
    image_name = workbench.add_task_image(trashed_a["id"], data, "image/png")

    workbench.delete_task(trashed_a["id"])
    workbench.delete_task(trashed_b["id"])

    assert workbench.empty_trash() == 2
    assert workbench.list_deleted_tasks() == []
    assert workbench.get_task(trashed_a["id"]) is None
    assert workbench.get_task(trashed_b["id"]) is None
    assert not (config.IMAGES_DIR / image_name).is_file()  # 图片跟着彻底清理
    assert workbench.get_task(kept["id"]) is not None  # 没删的任务不受影响
    assert workbench.empty_trash() == 0  # 已经空了，再清一次是 no-op


def test_update_task_rejects_invalid_status(isolated):
    from vococo.memory import workbench

    project = workbench.create_project("状态测试项目")
    task = workbench.create_task(project["id"], "任务")
    result = workbench.update_task(task["id"], status="not-a-status")
    assert result["status"] == "todo"  # 非法枚举被忽略,原值不变


def test_task_image_roundtrip(isolated, monkeypatch):
    from vococo import config
    from vococo.memory import workbench

    monkeypatch.setattr(config, "IMAGES_DIR", isolated / "data" / "images")

    project = workbench.create_project("图片测试项目")
    task = workbench.create_task(project["id"], "带图任务")
    data = base64.b64encode(b"fake-png-bytes").decode()

    name = workbench.add_task_image(task["id"], data, "image/png")
    assert name is not None
    assert (config.IMAGES_DIR / name).is_file()
    assert workbench.get_task(task["id"])["images"] == [name]

    assert workbench.remove_task_image(task["id"], name) is True
    assert not (config.IMAGES_DIR / name).is_file()
    assert workbench.get_task(task["id"])["images"] == []


def test_soft_delete_keeps_images_purge_cleans_up(isolated, monkeypatch):
    """软删除只是移入回收站,图片要留着方便恢复;真正清理要等 purge_task。"""
    from vococo import config
    from vococo.memory import workbench

    monkeypatch.setattr(config, "IMAGES_DIR", isolated / "data" / "images")

    project = workbench.create_project("清理测试项目")
    task = workbench.create_task(project["id"], "带图任务")
    data = base64.b64encode(b"fake-png-bytes").decode()
    name = workbench.add_task_image(task["id"], data, "image/png")

    workbench.delete_task(task["id"])
    assert (config.IMAGES_DIR / name).is_file()  # 软删除不动图片

    workbench.purge_task(task["id"])
    assert not (config.IMAGES_DIR / name).is_file()  # 彻底删除才清理
