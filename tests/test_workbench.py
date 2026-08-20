"""工作台(GTD 待办看板):种子导入 / 项目 CRUD / 任务 CRUD / 图片落盘。"""
from __future__ import annotations

import base64


def test_first_access_seeds_demo_data(isolated):
    from vococo.memory import workbench

    projects = workbench.list_projects()
    assert {p["name"] for p in projects} == {"AI 咨询", "VocoTrade", "面料外贸", "离职过渡"}
    tasks = workbench.list_tasks()
    assert len(tasks) == 12
    assert workbench.list_sources()


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

    updated = workbench.update_task(task["id"], status="done", title="改了标题")
    assert updated["status"] == "done"
    assert updated["title"] == "改了标题"

    assert workbench.delete_task(task["id"]) is True
    assert workbench.get_task(task["id"]) is None
    assert workbench.delete_task(task["id"]) is False  # 已删除,再删返回 False


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


def test_delete_task_cleans_up_images(isolated, monkeypatch):
    from vococo import config
    from vococo.memory import workbench

    monkeypatch.setattr(config, "IMAGES_DIR", isolated / "data" / "images")

    project = workbench.create_project("清理测试项目")
    task = workbench.create_task(project["id"], "带图任务")
    data = base64.b64encode(b"fake-png-bytes").decode()
    name = workbench.add_task_image(task["id"], data, "image/png")

    workbench.delete_task(task["id"])
    assert not (config.IMAGES_DIR / name).is_file()
