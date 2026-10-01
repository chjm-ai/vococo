"""记忆沉淀路由:一条经验该写到哪——项目 Agent 专用,还是跨 Agent 通用。

目录规则(2026-10-01,刻意不做配置项):
- 通用:AI_BRAIN/memory/ 根下的分类文件(lessons.md / tech-decisions.md / preferences.md),登记在全局 MEMORY.md
- Agent 专用:AI_BRAIN/memory/<项目文件夹名>/,索引是该目录的 INDEX.md(只注入这个 Agent 的提示词)。
  vococo、vocotrade 正好对上原有的 memory/vococo/、memory/vocotrade/。没有像样文件夹名的
  (新建 Agent 默认的 workspace/)或撞上保留目录名的,落到 memory/agents/<id>/。

判定标准写在 Agent 提示词里(memory/agents.prompt_extra 的「记忆归属」),这里只管按结果落盘:
- preference(主人的偏好)永远进通用
- lesson / decision:scope=agent 进该 Agent 目录的 lessons.md / decisions.md,scope=global 进全局对应文件
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .. import config

KINDS = ("lesson", "decision", "preference")
_RESERVED_DIRS = {"agents", "archive", "methods", "people", "memory"}
_TOPIC_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
_RESERVED_TOPICS = {"index", "lessons", "decisions"}  # 和目录里的索引 / 追加文件同名会互相覆盖
AUTO_SECTION = "## 自动登记"  # note_memory / save_memory 登记的条目放在 INDEX.md 开头这一节:注入截断也看得见
_AGENT_FILES = {"lesson": ("lessons.md", "踩坑记录"), "decision": ("decisions.md", "决策记录")}


def brain_memory() -> Path:
    return config.AI_BRAIN_DIR / "memory"


def _name_taken_earlier(agent: dict, base: str) -> bool:
    """别的 Agent 的项目文件夹也叫 base,且它建得更早 → 名字归它。只读本地 agent.json,不碰 iCloud。"""
    from . import agents as agents_mod

    root = agents_mod.root_dir()
    if not root.is_dir():
        return False
    mine = (agent.get("created_at") or 0, agent["id"])
    for d in root.iterdir():
        if d.name == agent["id"] or not d.is_dir():
            continue
        meta = agents_mod._read_meta(d.name)
        other = os.path.basename(str(meta.get("workdir") or "").rstrip("/"))
        if other == base and (meta.get("created_at") or 0, d.name) < mine:
            return True
    return False


def agent_memory_dir(agent: dict) -> Path:
    """项目文件夹名 → AI_BRAIN/memory/<名字>/;不合适就 memory/agents/<id>/;
    和更早的 Agent 文件夹同名 → memory/<名字>-<id 前 6 位>/,免得两个 Agent 的记忆串在一起。"""
    base = os.path.basename(str(agent.get("workdir") or "").rstrip("/"))
    if not base or base == "workspace" or base.startswith(".") or base.lower() in _RESERVED_DIRS:
        return brain_memory() / "agents" / agent["id"]
    if _name_taken_earlier(agent, base):
        return brain_memory() / f"{base}-{agent['id'][:6]}"
    return brain_memory() / base


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _register(agent: dict, d: Path, filename: str, desc: str) -> None:
    """在 Agent 目录 INDEX.md 开头的「## 自动登记」一节末尾加一行(已登记过就不重复)。

    放开头不放文件末尾:索引注入提示词有长度上限(超了截尾),而且已有索引(如 vococo)末尾
    是别的小节,追加到最后会被截掉、还会归错节。INDEX.md 不在就先建。"""
    index = d / "INDEX.md"
    if index.exists():
        lines = index.read_text(encoding="utf-8").splitlines()
    else:
        lines = [f"# {agent['name']} 记忆索引", "",
                 f"> 只放「{agent['name']}」专用的记忆(这个 Agent 每轮都会看到本索引);"
                 f"跨 Agent 通用的经验在 {brain_memory()}/ 下的 lessons.md 等分类文件。", ""]
    if any(x.startswith(f"→ {filename} ") for x in lines):
        return
    entry = f"→ {filename} — {' '.join(desc.split())}"
    if AUTO_SECTION in lines:
        # 本节到第一个空行(或下一个小节)为止:插在它最后一条之后
        i = lines.index(AUTO_SECTION) + 1
        while i < len(lines) and lines[i].strip() and not lines[i].startswith("## "):
            i += 1
        lines.insert(i, entry)
    else:
        # 标题和开头那段说明(> 引用)之后,第一个小节/条目之前
        i = 1
        while i < len(lines) and (not lines[i].strip() or lines[i].startswith(">")):
            i += 1
        lines[i:i] = [AUTO_SECTION, entry, ""]
    index.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def save_topic(agent: dict, topic: str, title: str, summary: str, body: str) -> Path:
    """Agent 专用的新主题文件 <dir>/<topic>.md,登记进 INDEX.md。已存在抛 FileExistsError。"""
    if not _TOPIC_RE.match(topic or ""):
        raise ValueError(f"topic「{topic}」非法:只允许字母、数字、下划线、短横线")
    if topic.lower() in _RESERVED_TOPICS:
        raise ValueError(f"topic 不能叫「{topic}」(和目录里的索引 / 追加文件同名),换个名字")
    d = agent_memory_dir(agent)
    path = d / f"{topic}.md"
    if path.exists():
        raise FileExistsError(str(path))
    d.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ncreated: {_today()}\n---\n# {title}\n\n> {summary}\n\n{body.strip()}\n",
                    encoding="utf-8")
    _register(agent, d, path.name, summary)
    return path


def _cell(text: str) -> str:
    """塞进 markdown 表格的一格:换行压成空格,竖线换成全角,不然会把表格撑坏。"""
    return " ".join(str(text).split()).replace("|", "｜")


def _insert_table_row(path: Path, row: str) -> None:
    """把一行插到文件里第一张 markdown 表格的末尾;没有表格就追加到文件末尾。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    start = next((i for i, x in enumerate(lines) if x.lstrip().startswith("|")), None)
    if start is None:
        lines.append(row)
    else:
        end = start
        while end + 1 < len(lines) and lines[end + 1].lstrip().startswith("|"):
            end += 1
        lines.insert(end + 1, row)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_agent_note(agent: dict, kind: str, text: str, label: str) -> Path:
    d = agent_memory_dir(agent)
    filename, title = _AGENT_FILES[kind]
    path = d / filename
    d.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# {agent['name']} · {title}\n\n", encoding="utf-8")
        _register(agent, d, filename, f"{title}(按日期追加)")
    head = f"**{_cell(label)}**:" if label else ""
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"- {_today()} {head}{text.strip()}\n")
    return path


def _append_global(kind: str, text: str, label: str) -> Path:
    mem = brain_memory()
    mem.mkdir(parents=True, exist_ok=True)
    if kind == "lesson":  # lessons.md 第一张表是「| 场景 | 可复用规则 |」
        path = mem / "lessons.md"
        _insert_table_row(path, f"| {_cell(label) or '通用'} | {_cell(text)} |")
    elif kind == "decision":  # tech-decisions.md:「| 日期 | 决策 | 原因 | 项目 |」
        path = mem / "tech-decisions.md"
        _insert_table_row(path, f"| {_today()} | {_cell(text)} | {_cell(label)} | 通用 |")
    else:  # preferences.md:每条一个小节
        path = mem / "preferences.md"
        old = path.read_text(encoding="utf-8").rstrip() if path.exists() else "# 偏好"
        path.write_text(f"{old}\n\n## {label or '偏好'}({_today()} 定)\n\n{text.strip()}\n", encoding="utf-8")
    return path


def note(agent: dict | None, kind: str, text: str, label: str = "") -> Path:
    """追加一条踩坑 / 决策 / 偏好,返回写到的文件。agent=None 即通用;preference 一律通用。"""
    if kind not in KINDS:
        raise ValueError(f"kind 只能是 {' / '.join(KINDS)}")
    if not (text or "").strip():
        raise ValueError("text 不能为空")
    if agent is None or kind == "preference":
        return _append_global(kind, text, label)
    return _append_agent_note(agent, kind, text, label)
