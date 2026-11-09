"""已移除研究功能的历史报告与进度读取，不包含创建或执行能力。"""
from __future__ import annotations

from . import research_store as store


def get(task_id: str) -> dict | None:
    return store.get(task_id)



def progress(task_id: str, task: dict | None = None) -> dict | None:
    """只公开已提交产出与操作元数据，绝不携带模型流或网页正文。"""
    state = get(task_id)
    if state is None:
        return None
    task = task or {}
    snapshot = {key: state.get(key) for key in (
        "id", "status", "stage", "created_at", "updated_at", "deadline", "usage", "budget")}
    snapshot.update(
        title=task.get("title", state.get("title", "研究")),
        task_status=task.get("status", {"ready": "queued", "interrupted": "failed"}.get(
            state["status"], state["status"])),
        updated_at=max(state["updated_at"], task.get("updated_at", 0)),
        limited_stages=list(state.get("limited_stages", [])),
        checkpoints={key: value for key, value in state.get("checkpoints", {}).items()
                     if key in {"plan", "collect", "review"}},
        sources=[_source_progress(source) for source in state.get("sources", [])],
        activity=[{key: value for key, value in item.items() if key in {
            "id", "tool", "stage", "query", "url", "status", "started_at", "finished_at", "summary"}}
            for item in state.get("activity", [])],
    )
    return snapshot



def _source_progress(source: dict) -> dict:
    return {"id": source["id"], "title": source.get("title", ""), "url": source.get("url", ""),
            "fetched_at": source.get("fetched_at") or max(
                (item.get("fetched_at", 0) for item in source.get("extracts", [])), default=0),
            "claims_count": len(source.get("claims", []))}
