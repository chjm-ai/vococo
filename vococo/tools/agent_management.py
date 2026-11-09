"""对话中的持久 Agent 管理：只允许配置已存在的能力。"""
from __future__ import annotations

import os

from claude_agent_sdk import tool

from ..gateway import settings_store
from ..memory import agents, projects
from . import danger


def _result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _capabilities(args: dict) -> dict:
    result = {}
    for key in ("skills", "mcp"):
        if key not in args:
            continue
        value = args[key]
        if value is None:
            result[key] = None
            continue
        if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
            raise ValueError(f"{key} 必须是非空名称的列表或 null")
        names = list(dict.fromkeys(x.strip() for x in value))
        if key == "skills":
            available = {x["name"] for x in settings_store.list_skills()}
        else:
            available = {x["name"] for x in settings_store.list_external() if x.get("enabled", True)}
        missing = sorted(set(names) - available)
        if missing:
            scope = "设置页全局技能中未找到" if key == "skills" else "未找到或未全局启用"
            raise ValueError(f"{key} {scope}：{', '.join(missing)}")
        result[key] = names
    return result


def _check_new(name: str, path: str | None) -> None:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
        raise ValueError("名称须为 1–40 个字符")
    by_hash = agents._agents_by_project_hash()
    for project in projects.list_projects():
        agent_id = by_hash.get(project["hash"], project["hash"])
        meta = agents._read_meta(agent_id)
        if not meta.get("name") and not (agents.home(agent_id) / "AGENT.md").is_file():
            continue
        existing = agents._build(agent_id, meta, project["path"])
        if existing["name"].casefold() == name.strip().casefold():
            raise ValueError("同名 Agent 已存在，请勿重复创建")
    if path is not None:
        if not isinstance(path, str) or not os.path.isabs(path) or not os.path.isdir(path):
            raise ValueError("工作目录须为已存在的绝对路径")
        normalized = projects.normalize_project_path(path)
        phash = projects.project_hash(normalized)
        if by_hash.get(phash) or (agents.home(phash) / "AGENT.md").is_file() or agents._read_meta(phash).get("name"):
            raise ValueError("工作目录已绑定 Agent，不能通过创建操作重命名")


def _describe_caps(changes: dict) -> str:
    return "；".join(f"{k}: {'跟随全局' if v is None else ', '.join(v) or '无'}" for k, v in changes.items()) or "能力跟随全局"


def _cosmetic_settings(args: dict) -> dict:
    changes = {}
    if "name" in args:
        name = args["name"]
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
            raise ValueError("名称须为 1–40 个字符")
        changes["name"] = name.strip()
    if "avatar" in args:
        avatar = args["avatar"]
        allowed = {"shape": agents.AVATAR_SHAPES, "color": agents.AVATAR_COLORS, "eyes": agents.AVATAR_EYES}
        if not isinstance(avatar, dict) or not avatar:
            raise ValueError("头像须为包含 shape/color/eyes 的对象")
        if any(key not in allowed or not isinstance(value, str) or value not in allowed[key]
               for key, value in avatar.items()):
            raise ValueError("头像字段或取值无效")
        changes["avatar"] = avatar
    return changes


def _read_agent(agent_id: str) -> dict | None:
    meta = agents._read_meta(agent_id)
    if meta.get("workdir"):
        return agents._build(agent_id, meta, meta["workdir"])
    for project in projects.list_projects():
        if project["hash"] != agent_id or agents._agents_by_project_hash().get(agent_id):
            continue
        if meta.get("name") or (agents.home(agent_id) / "AGENT.md").is_file():
            return agents._build(agent_id, meta, project["path"])
    return None


@tool(
    "create_agent",
    "创建持久 Agent，需经过审批。名称或工作目录已被占用时拒绝，不会重命名已有 Agent。"
    "skills/mcp 可选，仅接受设置页列出的全局技能和已启用的外部 MCP；省略或 null 跟随全局，"
    "空列表清空专属名单，但插件技能或会话临时 MCP 仍可能加载。不会安装技能、改变权限或创建定时任务。",
    {"type": "object", "properties": {
        "name": {"type": "string"}, "path": {"type": "string"},
        "skills": {"type": ["array", "null"], "items": {"type": "string"}},
        "mcp": {"type": ["array", "null"], "items": {"type": "string"}},
    }, "required": ["name"]},
)
async def create_agent(args: dict) -> dict:
    name, path = args.get("name"), args.get("path")
    try:
        _check_new(name, path)
        changes = _capabilities(args)
    except ValueError as exc:
        return _result(f"未创建：{exc}")
    resolved_path = projects.normalize_project_path(path) if path else None
    detail = f"创建「{name.strip()}」；工作目录：{resolved_path or '自动新建'}；{_describe_caps(changes)}"
    if not await danger.require_approval("创建或配置 Agent", detail):
        return _result("未批准，未创建 Agent。")
    with agents._UPDATE_LOCK:
        try:
            if path and projects.normalize_project_path(path) != resolved_path:
                raise ValueError("工作目录在审批期间发生变化，请重新确认")
            _check_new(name, resolved_path)
            changes = _capabilities(args)
        except ValueError as exc:
            return _result(f"未创建：{exc}")
        try:
            agent = agents.create(name.strip(), resolved_path)
        except Exception as exc:
            return _result(f"创建失败：{exc}；可能留下部分文件，请先检查 Agent 列表和工作目录，勿直接重试。")
        try:
            if changes:
                agent = agents.update(agent["id"], **changes)
        except Exception as exc:
            return _result(f"Agent 已创建（ID：{agent['id']}），但配置能力失败：{exc}；请检查后重试配置。")
    return _result(f"已创建 Agent「{agent['name']}」，ID：{agent['id']}，工作目录：{agent['workdir']}；{_describe_caps(changes)}。")


@tool(
    "update_agent",
    "配置已有 Agent。仅修改 name(名称)或 avatar(头像)免审批；技能和外部 MCP 仍需审批。"
    "按 ID 指定；能力名单省略不修改，null 跟随全局，空列表清空专属名单。"
    "仅接受设置页已有技能和启用的 MCP，不更改权限。头像字段 shape/color/eyes 使用已有合法值。",
    {"type": "object", "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "avatar": {"type": "object", "properties": {
            "shape": {"type": "string", "enum": sorted(agents.AVATAR_SHAPES)},
            "color": {"type": "string", "enum": sorted(agents.AVATAR_COLORS)},
            "eyes": {"type": "string", "enum": sorted(agents.AVATAR_EYES)},
        }, "additionalProperties": False},
        "skills": {"type": ["array", "null"], "items": {"type": "string"}},
        "mcp": {"type": ["array", "null"], "items": {"type": "string"}},
    }, "required": ["id"]},
)
async def update_agent(args: dict) -> dict:
    agent_id = args.get("id")
    if not isinstance(agent_id, str) or not agents.valid_id(agent_id) or agent_id == agents.GENERAL_ID:
        return _result("请提供有效的项目 Agent ID；总助理使用全局设置。")
    before = _read_agent(agent_id)
    if not before:
        return _result("Agent 不存在。")
    try:
        changes = _capabilities(args)
        cosmetic = _cosmetic_settings(args)
        if not changes and not cosmetic:
            raise ValueError("请指定 name、avatar、skills 或 mcp")
    except ValueError as exc:
        return _result(f"未修改：{exc}")
    detail = f"配置「{before['name']}」（{agent_id}）；原配置：{_describe_caps({k: before.get(k) for k in changes})}；新配置：{_describe_caps(changes)}"
    if changes and not await danger.require_approval("创建或配置 Agent", detail):
        return _result("未批准，未修改 Agent。")
    with agents._UPDATE_LOCK:
        current = _read_agent(agent_id)
        if not current or any(current.get(k) != before.get(k) for k in ("name", "workdir", *changes)):
            return _result("Agent 或相关配置在审批期间发生变化，请重新确认。")
        try:
            changes = _capabilities(args)
            # agents.update 在同一把锁内重读元数据，只提交指定字段，不回写权限快照。
            updated = agents.update(agent_id, **changes, **cosmetic)
        except ValueError as exc:
            return _result(f"未修改：{exc}")
    summary = _describe_caps(changes) if changes else "已更新名称/头像，未更改能力或权限"
    return _result(f"已配置 Agent「{updated['name']}」（{agent_id}）：{summary}。")
