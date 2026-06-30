"""Hermes 原生工具 —— 接通"记忆"这条灵魂线。

两个工具,挂成一个 SDK MCP server(名字 hermes),全入口共用:

- recall_past:检索跨会话的历史对话(agent 自己读不到 SQLite,必须给工具)。
- save_memory:把一条值得长期记的【新主题】记忆写入 ~/AI_BRAIN/memory/<topic>.md,
  并在 MEMORY.md 末尾登记索引。只负责"新建独立主题文件"这种安全情形;
  要往已有分类文件(lessons/preferences/tech-decisions)追加,交给 agent 用文件
  编辑工具按其现有格式做 —— 避免硬编码格式覆坏既有 Obsidian 记忆。

工具暴露名为 mcp__hermes__recall_past / mcp__hermes__save_memory。
"""
from __future__ import annotations

import datetime
import re

from claude_agent_sdk import create_sdk_mcp_server, tool

from .. import config
from ..memory import session_store

_TOPIC_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_OTHER_SECTION = "## 其他主题"


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "recall_past",
    "检索跨会话的历史对话记录(当前会话之外、可能已被 /new 归档的旧对话)。"
    "当用户提到「上次/之前我们聊过/我记得说过」之类、而当前上下文里找不到时,用它召回。"
    "传入关键词(中文友好,子串匹配)。",
    {"query": str},
)
async def recall_past(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return _ok("recall_past 需要一个非空的检索关键词。")
    rows = session_store.search(query, limit=8)
    if not rows:
        return _ok(f"没有找到与「{query}」相关的历史对话。")
    parts = []
    for session_key, user_text, assistant_text in rows:
        block = f"[来源会话 {session_key}]\n我:{user_text}"
        if assistant_text:
            block += f"\n你:{assistant_text}"
        parts.append(block)
    return _ok(f"与「{query}」相关的历史片段(最多 8 条):\n\n" + "\n\n---\n\n".join(parts))


@tool(
    "save_memory",
    "把一条值得【长期记住】的新主题记忆写进 Wesley 的 AI_BRAIN(~/AI_BRAIN/memory/<topic>.md),"
    "并自动登记到 MEMORY.md 索引。仅用于【新建独立主题文件】(如某服务器/工具的关键路径与踩坑)。"
    "若该 topic 文件已存在,本工具会拒绝(不覆盖);要往已有记忆或 lessons/preferences/"
    "tech-decisions 等分类文件追加,请改用 Read+Edit 文件工具按其现有格式追加。"
    "topic:英文短横线 slug(如 vpn-speeedai-server);title:中文标题;"
    "summary:一句话摘要(也会写进索引);body:markdown 正文。",
    {"topic": str, "title": str, "summary": str, "body": str},
)
async def save_memory(args: dict) -> dict:
    topic = (args.get("topic") or "").strip()
    title = (args.get("title") or "").strip()
    summary = (args.get("summary") or "").strip()
    body = (args.get("body") or "").strip()

    if not (topic and title and summary and body):
        return _ok("save_memory 需要 topic / title / summary / body 四项都非空。")
    if not _TOPIC_RE.match(topic):
        return _ok(f"topic「{topic}」非法:只允许字母、数字、下划线、短横线(防路径穿越)。")

    mem_dir = config.AI_BRAIN_DIR / "memory"
    path = mem_dir / f"{topic}.md"
    if path.exists():
        return _ok(
            f"⚠️ memory/{topic}.md 已存在,未改动。要追加内容请用 Read+Edit 打开它,"
            "按其现有格式追加,避免覆盖。"
        )

    today = datetime.date.today().isoformat()
    content = f"---\ncreated: {today}\n---\n# {title}\n\n> {summary}\n\n{body}\n"
    mem_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    _append_index(topic, summary)
    return _ok(f"✅ 已写入 memory/{topic}.md 并登记索引。")


def _append_index(topic: str, summary: str) -> None:
    """在 MEMORY.md 的「## 其他主题」分节末尾登记一行;没有索引文件则新建。"""
    index = config.AI_BRAIN_DIR / "MEMORY.md"
    line = f"→ memory/{topic}.md — {summary}"
    try:
        text = index.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        index.write_text(f"# 记忆索引\n\n{_OTHER_SECTION}\n{line}\n", encoding="utf-8")
        return
    text = text.rstrip("\n")
    if _OTHER_SECTION in text:
        text = f"{text}\n{line}\n"
    else:
        text = f"{text}\n\n{_OTHER_SECTION}\n{line}\n"
    index.write_text(text, encoding="utf-8")


def build_mcp_servers() -> dict:
    """返回挂给 ClaudeAgentOptions.mcp_servers 的 server 表。"""
    return {
        "hermes": create_sdk_mcp_server(
            "hermes", tools=[recall_past, save_memory]
        )
    }
