"""Agent —— 「项目」的升级版:一个工作目录 + 自己的人格/目标/笔记 + 挂在它名下的定时任务。

2026-09-29 设计定稿(demo 见 /tmp/agent-demo,讨论记录在 AI_BRAIN/memory/vococo/goals-multibot-research.md):
- Agent 与项目一一对应:每个项目自动就是一个 Agent,id 直接用项目哈希(不变);
  另有一个「通用」Agent(id=general),接住不属于任何项目的会话,它的主会话就是全局主会话。
- 家目录 data/agents/<id>/ 由 vococo 管,不往用户的项目目录里写东西:
    agent.json  名称 / 头像 / 工作目录 / 关联(系统要读的结构化设置)
    AGENT.md    人格、技能、目标(纯文本,你写,它每次开工都读)
    NOTES.md    它的笔记
    workspace/  新建 Agent 且没指定目录时的默认工作目录
- 工作目录 = 项目文件夹(会话 key 里的 p<hash>);子会话就是这个项目下原有的会话,不搬数据。
- 主会话固定为 web:p<hash>:main(通用 Agent 为全局主会话)。定时任务带 agent_id,
  跑完后结果除了落任务自己的会话,还会复制一份到所属 Agent 的主会话(record_run),
  并记进 agent_runs 表(右侧面板「动态」读它)。
- 这个 Agent 名下的会话和定时任务,每轮都把 AGENT.md / NOTES.md / 关联清单作为
  system_prompt_extra 带进提示词(prompt_extra_for_session)。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Callable

from .. import config
from . import _db, projects

GENERAL_ID = "general"
DOC_NAMES = ("AGENT.md", "NOTES.md")
PROMPT_MAX_CHARS = 8000  # 注入提示词的上限,超了截断(常驻上下文要小,见调研结论)
# 系统注入轮的标记:前端遇到它渲染成居中一行灰字。与 tools/selfops.SYS_MARKER、前端 SYS_MARK 一致
SYS_MARKER = "⚙️[系统]"

# 像素头像的可选值(前端 agents.js 按这三个键画图,改这里要同步改前端)
AVATAR_SHAPES = ("xiaoyou", "ghost", "blob", "square", "cat", "drop")
AVATAR_COLORS = ("orange", "coral", "lime", "lake", "grape", "pink", "gold", "teal")
AVATAR_EYES = ("dot", "small", "squint")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs(
  id INTEGER PRIMARY KEY,
  agent_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  job_name TEXT NOT NULL DEFAULT '',
  ts REAL NOT NULL,
  status TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  turn_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agent_runs ON agent_runs(agent_id, ts);
"""
_ready_for = None  # 已建表的那条连接(测试会换库重连,换了就重新建表)
_listener: Callable[[str], None] | None = None


def _conn():
    global _ready_for
    c = _db.conn()
    if c is not _ready_for:
        c.executescript(_SCHEMA)
        _ready_for = c
    return c


def set_listener(fn: Callable[[str], None] | None) -> None:
    """定时结果写进主会话后回调(参数是主会话的 conv),Web 端据此刷新。"""
    global _listener
    _listener = fn


# ── 家目录与 agent.json ──────────────────────────────────────────────────
def root_dir() -> Path:
    return config.DATA_DIR / "agents"


_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")


def valid_id(agent_id: str) -> bool:
    """id 会拼进文件路径,只认 general 和十六进制串(项目哈希 / uuid 片段),挡住 ../ 之类。"""
    return agent_id == GENERAL_ID or bool(_ID_RE.match(agent_id or ""))


def home(agent_id: str) -> Path:
    if not valid_id(agent_id):
        raise ValueError(f"非法 Agent id:{agent_id!r}")
    return root_dir() / agent_id


def _default_avatar(agent_id: str) -> dict:
    n = sum(ord(c) for c in agent_id)
    return {
        "shape": AVATAR_SHAPES[n % len(AVATAR_SHAPES)],
        "color": AVATAR_COLORS[(n // len(AVATAR_SHAPES)) % len(AVATAR_COLORS)],
        "eyes": "dot",
    }


def _read_meta(agent_id: str) -> dict:
    try:
        return json.loads((home(agent_id) / "agent.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_meta(agent_id: str, meta: dict) -> None:
    d = home(agent_id)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "agent.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, d / "agent.json")


def _normalize_avatar(av: dict | None, agent_id: str) -> dict:
    base = _default_avatar(agent_id)
    av = av or {}
    return {
        "shape": av.get("shape") if av.get("shape") in AVATAR_SHAPES else base["shape"],
        "color": av.get("color") if av.get("color") in AVATAR_COLORS else base["color"],
        "eyes": av.get("eyes") if av.get("eyes") in AVATAR_EYES else base["eyes"],
    }


def _normalize_links(links) -> list[dict]:
    out = []
    for x in links or []:
        if not isinstance(x, dict) or not str(x.get("path") or "").strip():
            continue
        out.append({
            "path": projects.normalize_project_path(str(x["path"]).strip()),
            "note": str(x.get("note") or "").strip()[:80],
            "writable": bool(x.get("writable")),
        })
    return out


def _build(agent_id: str, meta: dict, workdir: str | None) -> dict:
    """把 agent.json + 项目信息拼成对外的 Agent 字典。"""
    if agent_id == GENERAL_ID:
        name = meta.get("name") or "通用"
        phash, main = None, "main"
    else:
        phash = projects.project_hash(workdir) if workdir else None
        name = meta.get("name") or (os.path.basename(workdir or "") or agent_id)
        main = f"p{phash}:main" if phash else "main"
    return {
        "id": agent_id,
        "name": name,
        "avatar": _normalize_avatar(meta.get("avatar"), agent_id),
        "workdir": workdir,
        "project_hash": phash,
        "main_conv": main,
        "links": _normalize_links(meta.get("links")),
        "created_at": meta.get("created_at") or 0,
    }


def list_agents() -> list[dict]:
    """通用 Agent + 每个可见项目各一个(按项目排序)。

    项目还没有 agent.json 的,这里顺手建一个(id = 项目哈希),以后改名/换头像都落在它上面。
    新建 Agent 时 id 不是项目哈希(见 create),所以另按工作目录反查一遍。"""
    out = [_build(GENERAL_ID, _read_meta(GENERAL_ID), None)]
    by_hash = _agents_by_project_hash()
    for p in projects.list_projects():
        aid = by_hash.get(p["hash"])
        if aid is None:
            aid = p["hash"]
            meta = {"workdir": p["path"], "created_at": time.time()}
            _write_meta(aid, meta)
            by_hash[p["hash"]] = aid
        out.append(_build(aid, _read_meta(aid), p["path"]))
    return out


def _agents_by_project_hash() -> dict[str, str]:
    """扫家目录:项目哈希 → Agent id。"""
    out: dict[str, str] = {}
    r = root_dir()
    if not r.is_dir():
        return out
    for d in r.iterdir():
        if not d.is_dir() or d.name == GENERAL_ID or not valid_id(d.name):
            continue
        wd = _read_meta(d.name).get("workdir")
        if wd:
            out.setdefault(projects.project_hash(wd), d.name)
    return out


def _materialize(phash: str) -> dict:
    """项目还没有 agent.json → 以项目哈希为 id 落一份(和 list_agents 里的自动补建同一规则)。"""
    meta = _read_meta(phash)
    if not meta.get("workdir"):
        path = projects.path_for_hash(phash)
        if not path:
            return {}
        meta = {"workdir": path, "created_at": time.time()}
        _write_meta(phash, meta)
    return meta


def get(agent_id: str) -> dict | None:
    if not valid_id(agent_id):
        return None
    if agent_id == GENERAL_ID:
        return _build(GENERAL_ID, _read_meta(GENERAL_ID), None)
    meta = _read_meta(agent_id) or _materialize(agent_id)
    if not meta.get("workdir"):
        return None
    return _build(agent_id, meta, meta["workdir"])


def by_project_hash(phash: str) -> dict | None:
    aid = _agents_by_project_hash().get(phash)
    return get(aid) if aid else get(phash)


def create(name: str, path: str | None = None) -> dict:
    """新建 Agent。path 为空 → 在家目录下建 workspace/ 当工作目录;否则绑定指定目录。

    绑定的目录已经是某个 Agent 的工作目录时,不重复建,直接返回那个 Agent(改个名)。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("名字不能为空")
    if path:
        workdir = projects.normalize_project_path(path)
        if not os.path.isdir(workdir):
            raise ValueError(f"目录不存在:{workdir}")
        existing = _agents_by_project_hash().get(projects.project_hash(workdir))
        if existing:
            update(existing, name=name)
            projects.upsert_project(workdir)
            return get(existing)  # type: ignore[return-value]
        aid = uuid.uuid4().hex[:10]
    else:
        aid = uuid.uuid4().hex[:10]
        workdir = str(home(aid) / "workspace")
        Path(workdir).mkdir(parents=True, exist_ok=True)
        workdir = projects.normalize_project_path(workdir)
    _write_meta(aid, {"name": name, "workdir": workdir, "created_at": time.time(),
                      "avatar": _default_avatar(aid)})
    write_doc(aid, "AGENT.md", f"# {name}\n\n## 职责与人格\n\n\n## 目标\n\n\n## 技能范围\n\n")
    projects.upsert_project(workdir)
    return get(aid)  # type: ignore[return-value]


def update(agent_id: str, *, name: str | None = None, avatar: dict | None = None,
           links: list | None = None) -> dict | None:
    """改名称 / 头像 / 关联。目录名用 id,不随名称变。"""
    if agent_id != GENERAL_ID and get(agent_id) is None:
        return None
    meta = _read_meta(agent_id)
    if name is not None:
        name = name.strip()[:40]
        if not name:
            raise ValueError("名字不能为空")
        meta["name"] = name
        # AGENT.md 的一级标题跟着改,免得文件里和界面上不一致
        doc = read_doc(agent_id, "AGENT.md")
        if doc.startswith("# "):
            write_doc(agent_id, "AGENT.md", f"# {name}" + doc[doc.find("\n"):] if "\n" in doc else f"# {name}\n")
    if avatar is not None:
        meta["avatar"] = _normalize_avatar(avatar, agent_id)
    if links is not None:
        meta["links"] = _normalize_links(links)
    _write_meta(agent_id, meta)
    return get(agent_id)


# ── AGENT.md / NOTES.md ─────────────────────────────────────────────────
def read_doc(agent_id: str, name: str) -> str:
    if name not in DOC_NAMES:
        raise ValueError("只能读写 AGENT.md / NOTES.md")
    try:
        return (home(agent_id) / name).read_text(encoding="utf-8")
    except OSError:
        return ""


def write_doc(agent_id: str, name: str, text: str) -> None:
    if name not in DOC_NAMES:
        raise ValueError("只能读写 AGENT.md / NOTES.md")
    d = home(agent_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def list_files(agent_id: str, limit: int = 200) -> dict:
    """「文件」面板:家目录(递归)+ 工作目录第一层 + 关联清单。"""
    a = get(agent_id)
    if a is None:
        return {}
    h = home(agent_id)
    own = []
    if h.is_dir():
        for p in sorted(h.rglob("*")):
            rel = p.relative_to(h)
            if p.is_file() and not rel.parts[0].startswith(".") and rel.name != "agent.json.tmp":
                own.append(str(rel))
            if len(own) >= limit:
                break
    top = []
    wd = a["workdir"]
    if wd and os.path.isdir(wd):
        try:
            for e in sorted(os.scandir(wd), key=lambda e: (not e.is_dir(), e.name.lower())):
                if not e.name.startswith("."):
                    top.append({"name": e.name, "dir": e.is_dir()})
                if len(top) >= limit:
                    break
        except OSError:
            pass
    return {"home": str(h), "files": own, "workdir": wd, "workdir_top": top, "links": a["links"]}


# ── 会话 ↔ Agent ───────────────────────────────────────────────────────
def main_session_key(agent: dict) -> str:
    return config.resolve_session_key("web", agent["main_conv"])


def agent_for_session(session_key: str) -> dict | None:
    """会话属于哪个 Agent:项目会话按项目哈希;定时任务会话按 job 的 agent_id;其余 None。

    通用 Agent 不自动套到普通会话上——它的 AGENT.md 默认是空的,真写了内容再按需接。"""
    phash = config.project_hash_from_key(session_key)
    if phash:
        return by_project_hash(phash)
    if session_key.startswith("task:"):
        from ..cron import scheduler

        job = next((j for j in scheduler.load_jobs() if j.get("id") == session_key[5:]), None)
        if job and job.get("agent_id"):
            return get(job["agent_id"])
    if session_key == config.SESSION_KEY:
        return get(GENERAL_ID)
    return None


def prompt_extra(agent: dict | None) -> str:
    """AGENT.md + NOTES.md + 关联清单 → 追加进 system prompt 的文本;都没内容时返回空串
    (不改动提示词,已有会话的缓存不受影响)。"""
    if not agent:
        return ""
    doc = read_doc(agent["id"], "AGENT.md").strip()
    # 只有标题和空小节的模板不算内容
    if not any(line.strip() and not line.startswith("#") for line in doc.splitlines()):
        doc = ""
    notes = read_doc(agent["id"], "NOTES.md").strip()
    links = agent.get("links") or []
    if not (doc or notes or links):
        return ""
    parts = [f"# 当前 Agent:{agent['name']}\n以下是这个 Agent 的设定,本会话里按它办事。"]
    if doc:
        parts.append(doc)
    if notes:
        parts.append("## 笔记(NOTES.md)\n" + notes)
    if links:
        parts.append("## 关联目录/文件(需要时再打开)\n" + "\n".join(
            f"- {x['path']}" + (f" · {x['note']}" if x["note"] else "") + (" · 可写" if x["writable"] else " · 只读")
            for x in links))
    text = "\n\n".join(parts)
    return text[:PROMPT_MAX_CHARS]


def prompt_extra_for_session(session_key: str) -> str:
    try:
        return prompt_extra(agent_for_session(session_key))
    except Exception as exc:  # noqa: BLE001 —— 读不到设定不影响正常对话
        print(f"[agents] 读取 Agent 设定失败:{exc}", flush=True)
        return ""


# ── 定时任务结果 → 主会话 + 动态 ─────────────────────────────────────────
def record_run(agent_id: str, job: dict, status: str, text: str) -> int | None:
    """把一次定时任务的结果写进该 Agent 的主会话(居中一行「⏰ 任务名」+ 结果),并记进动态。"""
    from . import session_store

    agent = get(agent_id)
    if agent is None:
        return None
    key = main_session_key(agent)
    name = job.get("name") or "定时任务"
    c = _conn()
    cur = c.execute(
        "INSERT INTO turns(session_key, ts, user_text, assistant_text) VALUES (?,?,?,?)",
        (key, time.time(), f"{SYS_MARKER} ⏰ {name}", text),
    )
    turn_id = cur.lastrowid
    c.execute(
        "INSERT INTO agent_runs(agent_id, job_id, job_name, ts, status, text, turn_id) VALUES (?,?,?,?,?,?,?)",
        (agent_id, job.get("id") or "", name, time.time(), status, text[:4000], turn_id),
    )
    c.commit()
    session_store.set_pending_review(key, True)
    if _listener is not None:
        try:
            _listener(agent["main_conv"])
        except Exception as exc:  # noqa: BLE001
            print(f"[agents] 刷新通知失败:{exc}", flush=True)
    return turn_id


def recent_runs(agent_id: str, limit: int = 50, job_id: str | None = None) -> list[dict]:
    sql = "SELECT job_id, job_name, ts, status, text, turn_id FROM agent_runs WHERE agent_id=?"
    args: list = [agent_id]
    if job_id:
        sql += " AND job_id=?"
        args.append(job_id)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    return [
        {"job_id": r[0], "job_name": r[1], "ts": r[2], "status": r[3], "text": r[4], "turn_id": r[5]}
        for r in _conn().execute(sql, args).fetchall()
    ]
