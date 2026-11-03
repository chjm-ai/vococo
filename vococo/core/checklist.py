"""SDK 内置清单的只读投影：成功回执才提交，不参与后台任务调度。"""
from __future__ import annotations

import re
from dataclasses import dataclass

CHECKLIST_TOOLS = frozenset({"TodoWrite", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskDelete"})
STATUSES = frozenset({"pending", "in_progress", "completed"})
_CREATE = re.compile(r"^Task #(\d+) created successfully:")
_GET = re.compile(r"^Task #(\d+): (.*?)\nStatus: (pending|in_progress|completed)(?:\n|$)")
_LIST = re.compile(r"^#(\d+) \[(pending|in_progress|completed)\] (.+)$")


@dataclass
class ChecklistUpdated:
    items: list[dict]
    session_id: str


def normalize_items(items: list) -> list[dict]:
    if not isinstance(items, list):
        return []
    valid = []
    for item in items:
        if not isinstance(item, dict) or item.get("status") not in STATUSES:
            continue
        subject = item.get("subject") or item.get("content")
        if isinstance(subject, str) and subject.strip():
            valid.append({"id": str(item.get("id", len(valid) + 1)), "subject": subject,
                          "status": item["status"]})
    return valid


class ChecklistTracker:
    def __init__(self, items: list | None = None):
        self.items = {item["id"]: item for item in normalize_items(items or [])}
        self.pending: dict[str, tuple[str, dict]] = {}

    def remember(self, tool_id: str, name: str, data: dict, parent_id=None) -> None:
        if not parent_id and name in CHECKLIST_TOOLS:
            self.pending[tool_id] = (name, data)

    def finish(self, tool_id: str, content: str, is_error: bool) -> list[dict] | None:
        call = self.pending.pop(tool_id, None)
        if call is None or is_error:
            return None
        name, data = call
        changed = self._apply(name, data, content.strip())
        return [dict(item) for item in self.items.values()] if changed else None

    def _apply(self, name: str, data: dict, response: str) -> bool:
        if name == "TodoWrite":
            todos = data.get("todos")
            if not isinstance(todos, list) or len(normalize_items(todos)) != len(todos):
                return False
            self.items = {item["id"]: item for item in normalize_items(todos)}
            return True
        if name == "TaskCreate":
            match = _CREATE.match(response)
            if not match or not isinstance(data.get("subject"), str):
                return False
            self.items[match[1]] = {"id": match[1], "subject": data["subject"], "status": "pending"}
            return True
        if name in {"TaskUpdate", "TaskDelete"}:
            return self._update(data, response)
        return self._read(name, response)

    def _update(self, data: dict, response: str) -> bool:
        task_id = str(data.get("taskId", ""))
        if task_id not in self.items:
            return False  # 未知任务不靠编号猜标题；TaskList/TaskGet 可重新校准。
        status = data.get("status")
        if status == "deleted" and response == f"Updated task #{task_id} deleted":
            del self.items[task_id]
            return True
        if not response.startswith(f"Updated task #{task_id} "):
            return False
        if status is not None and status not in STATUSES:
            return False
        item = self.items[task_id]
        if status is not None:
            item["status"] = status
        if isinstance(data.get("subject"), str) and data["subject"].strip():
            item["subject"] = data["subject"]
        return True

    def _read(self, name: str, response: str) -> bool:
        if name == "TaskGet":
            match = _GET.match(response)
            if not match:
                return False
            self.items[match[1]] = {"id": match[1], "subject": match[2], "status": match[3]}
            return True
        if name != "TaskList":
            return False
        if response == "No tasks found":
            self.items = {}
            return True
        matches = [_LIST.fullmatch(line) for line in response.splitlines()]
        if not matches or not all(matches):
            return False  # 协议漂移不能被当成空清单或部分成功。
        self.items = {m[1]: {"id": m[1], "subject": m[3], "status": m[2]} for m in matches}
        return True
