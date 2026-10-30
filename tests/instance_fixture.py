"""实例偏好测试数据；不读取开发机的个人配置。"""
import copy

import pytest

from vococo import instance_config


FAKE_INSTANCE = {
    "preferences": {
        "persona": "多层有序、无序或混合列表；子项的列表标记对齐父项正文起点。",
        "memory_sections": ["测试偏好", "测试规则"],
        "memory_rules": "## 记忆归属\n使用 note_memory，目录：{dir}。不要直接 Edit 全局；需要时用 recall_past。",
        "decision_rules": "## 测试决策\n需要用户决定的事项使用 request_decision；当场询问用 ask_user。",
        "people_folders": ["测试笔记"],
    },
    "mcp": {
        "task_keywords": {
            "lemlist": ["lemlist", "campaign"],
            "dataforseo": ["反链", "关键词排名"],
            "analytics-mcp": ["ga4", "网站流量"],
        },
        "groups": {"测试工具组": ["lemlist", "analytics-mcp"]},
    },
}


@pytest.fixture(autouse=True)
def instance_preferences(monkeypatch):
    values = copy.deepcopy(FAKE_INSTANCE)
    monkeypatch.setattr(instance_config, "section", lambda name: copy.deepcopy(values.get(name, {})))
    return values
