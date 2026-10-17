"""聊天中的研究入口；执行期间不挂载这些工具，避免任务套娃。"""
from __future__ import annotations

import json

from claude_agent_sdk import tool

from ..core import research, task_runner, tasks
from ..gateway import clarify


def request_options(arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("请求必须是对象")
    result = {}
    for key, maximum in (("title", 80), ("prompt", 30000)):
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"{key} 必须非空，且不超过 {maximum} 字符")
        result[key] = value.strip()
    model = arguments.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("model 必须是非空字符串")
    result.update(model=model, budget=arguments.get("budget"))
    return result


def _result(data: dict, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
            "isError": error}


@tool(
    "start_research",
    "用户要求深度研究、deep research、多来源行业/竞品/技术调研时使用；简单事实查询不要用。"
    "创建独立研究任务，固定模型、有限预算、保存证据和阶段，完成后可在侧边栏查看报告。"
    "prompt 必须自包含，仅研究公共资料，不包含凭据或未授权外发的私有信息。"
    "预算可设 seconds/searches/fetches/tokens/max_turns；token 是超限打断，不是精确费用封顶。",
    {"type": "object", "properties": {
        "title": {"type": "string"}, "prompt": {"type": "string"},
        "model": {"type": "string"}, "budget": {"type": "object"}},
     "required": ["title", "prompt"]},
)
async def start_research(arguments: dict) -> dict:
    try:
        options = request_options(arguments)
        context = clarify.get_context()
        if context and tasks.task_id_from_session_key(context.session_key):
            raise ValueError("后台任务不可再派生研究，请在主聊天中发起")
        row = task_runner.dispatch_research(
            **options, context_session_key=context.session_key if context else None,
            dispatch_platform=getattr(context.adapter, "platform", None) if context else None,
            dispatch_chat_id=str(context.chat_id) if context else None,
        )
        return _result({"id": row["id"], "status": row["status"],
                        "conversation": tasks.session_key(row["id"]),
                        "message": "独立研究已排队；最终报告在任务会话中展示，请勿宣称已完成",
                        "report_url": f"/tasks/{row['id']}/report"})
    except (ValueError, RuntimeError) as error:
        return _result({"error": str(error)}, error=True)


@tool("get_research", "查询研究阶段、来源证据、预算用量与报告；id 是 start_research 返回的任务 id。",
      {"id": str})
async def get_research(arguments: dict) -> dict:
    task_id = arguments.get("id")
    if not isinstance(task_id, str):
        return _result({"error": "id 必须是字符串"}, error=True)
    state = research.get(task_id)
    if state is None:
        return _result({"error": "研究不存在"}, error=True)
    # 工具上下文不装入全部网页提取内容；证据详情由受认证 HTTP 接口提供。
    result = {key: value for key, value in state.items() if key not in {"sources", "search_results"}}
    result["sources"] = [{key: value for key, value in source.items() if key != "extracts"}
                         for source in state["sources"]]
    result["task_status"] = (tasks.get(task_id) or {}).get("status")
    return _result(result)


@tool("resume_research", "用户明确要求恢复失败或取消的研究时使用；不重置已用额度和原截止时间。已完成研究不能恢复。",
      {"id": str})
async def resume_research(arguments: dict) -> dict:
    task_id = arguments.get("id")
    if not isinstance(task_id, str):
        return _result({"error": "id 必须是字符串"}, error=True)
    context = clarify.get_context()
    if context and tasks.task_id_from_session_key(context.session_key):
        return _result({"error": "请在主聊天中明确恢复研究"}, error=True)
    result = await task_runner.resume_research(task_id)
    return _result(result, error=not result["ok"])


TOOLS = [start_research, get_research, resume_research]
