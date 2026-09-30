"""Agent —— 「项目」的升级版:一个工作目录 + 自己的人格/目标/笔记 + 挂在它名下的定时任务。

2026-09-29 设计定稿(demo 见 /tmp/agent-demo,讨论记录在 AI_BRAIN/memory/vococo/goals-multibot-research.md):
- Agent 与项目一一对应:每个项目自动就是一个 Agent,id 直接用项目哈希(不变);
  另有一个「通用」Agent(id=general),接住不属于任何项目的会话,它的主会话就是全局主会话。
- 家目录 data/agents/<id>/ 由 vococo 管,不往用户的项目目录里写东西:
    agent.json  名称 / 头像 / 工作目录 / 关联 / 能力 caps(系统要读的结构化设置)
                caps = 默认模型 / 技能白名单 / 常驻 MCP / 禁用工具,在 core/agent.stream_turn 里硬生效
    AGENT.md    人格、技能、目标(纯文本,你写,它每次开工都读)
    NOTES.md    它的笔记
    workspace/  新建 Agent 且没指定目录时的默认工作目录
- 工作目录 = 项目文件夹(会话 key 里的 p<hash>);子会话就是这个项目下原有的会话,不搬数据。
- 主会话固定为 web:p<hash>:main(通用 Agent 为全局主会话)。定时任务带 agent_id,
  跑完后结果除了落任务自己的会话,还会复制一份到所属 Agent 的主会话(record_run),
  并记进 agent_runs 表(右侧面板「动态」读它)。
- 这个 Agent 名下的会话和定时任务,每轮都把 AGENT.md / GOAL.md / PLAN.md / NOTES.md / 关联清单
  作为 system_prompt_extra 带进提示词(prompt_extra_for_session)。

目标闭环(同日追加):目标 ──拆解──> 计划 ──执行──> 采集数据 ──复盘──> 修正 ──写回目标文件
- GOAL.md  目标 / 成功标准 / 不做(你定);「当前进展」「复盘记录」两节由复盘任务写回
- PLAN.md  里程碑 + 任务清单(它拆、它勾)
- runs/    每次定时任务的结果按月追加(record_run),复盘时当数据读
- 目标一写上就自动挂一条每周「目标复盘」定时任务(ensure_goal_review),它按 REVIEW_PROMPT
  读上面三样、写回进展和计划;目标本身只有你同意才改。
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
DOC_NAMES = ("AGENT.md", "GOAL.md", "PLAN.md", "NOTES.md")
PROMPT_MAX_CHARS = 8000  # 注入提示词的上限,超了截断(常驻上下文要小,见调研结论)
# 系统注入轮的标记:前端遇到它渲染成居中一行灰字。与 tools/selfops.SYS_MARKER、前端 SYS_MARK 一致
SYS_MARKER = "⚙️[系统]"

# 像素头像的可选值(前端 agents.js 按这三个键画图,改这里要同步改前端)
AVATAR_SHAPES = ("xiaoyou", "ghost", "blob", "square", "cat", "drop")
AVATAR_COLORS = ("orange", "coral", "lime", "lake", "grape", "pink", "gold", "teal")
AVATAR_EYES = ("dot", "small", "squint")

AGENT_TEMPLATE = "# {name}\n\n## 职责与人格\n\n\n## 技能范围\n\n"
GOAL_TEMPLATE = "# 目标\n\n\n## 成功标准\n\n\n## 不做\n\n\n## 当前进展\n\n\n## 复盘记录\n\n"
PLAN_TEMPLATE = "# 计划\n\n## 里程碑\n\n\n## 任务\n\n"
REVIEW_CRON = "0 9 * * 1"  # 目标复盘默认每周一早 9 点
REVIEW_ROLE = "goal_review"  # 复盘任务在 cron_jobs.json 里的 role 标记,一个 Agent 只挂一条
REVIEW_PROMPT = """【目标复盘】给「{name}」做一次复盘,文件都在 {home}/ 下:
1. 读 GOAL.md、PLAN.md、NOTES.md,以及 runs/ 里最近 7 天的运行记录
2. 对照「成功标准」判断进展,用 runs 里的具体数字说话,没数据就明说缺什么数据
3. 改写 GOAL.md 的「当前进展」一节;在「复盘记录」末尾追加一行:日期 · 进展 · 偏差 · 下步
4. 更新 PLAN.md:勾掉做完的,补上下一步任务
5. 「目标」「成功标准」「不做」三节不许改;觉得该改,在汇报里提建议,等我同意
6. 最近两次复盘都没进展,直说,建议暂停或换方向
最后用 3-5 行汇报:进展、偏差、下步、要我拍板的事。"""
PLAN_PROMPT = """【拆解计划】按 {home}/GOAL.md 的目标和成功标准,拆成 3-5 个里程碑(各带完成标志)和最近一周要做的任务清单(- [ ] 格式),写进 {home}/PLAN.md。需要定时跑的环节,列出来问我要不要建定时任务。最后几行说清拆了什么。"""

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


_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_\-:.*()/ ]{1,80}$")


def _clean_names(items, limit: int = 60) -> list[str]:
    """名字清单(技能 / MCP / 工具名):去空去重、挡掉怪字符,顺序保留。"""
    out: list[str] = []
    if not isinstance(items, (list, tuple)):  # 字符串也可迭代,不挡会被拆成单个字母
        return out
    for x in items:
        s = str(x or "").strip()
        if s and _TOOL_NAME_RE.match(s) and s not in out:
            out.append(s)
    return out[:limit]


def _normalize_caps(caps) -> dict:
    """能力设定(agent.json 的 caps)—— 真正卡住 SDK 的那层,不是写给模型看的文字:
    - model:这个 Agent 名下【新会话】默认用的模型;空=跟随全局。已经锁过模型的老会话不受影响
    - skills:技能白名单;None=跟随全局/coding 设置,[]=一个都不挂(插件自带的照挂)
    - mcp:外部 MCP 名,这个 Agent 的会话每轮都挂(不用等关键词命中)
    - disallowed_tools:硬拦的工具名(如 Bash、mcp__vococo__dispatch_session)"""
    caps = caps if isinstance(caps, dict) else {}
    skills = caps.get("skills")
    return {
        "model": str(caps.get("model") or "").strip()[:80],
        "skills": _clean_names(skills, 200) if isinstance(skills, list) else None,
        "mcp": _clean_names(caps.get("mcp")),
        "disallowed_tools": _clean_names(caps.get("disallowed_tools")),
    }


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
        "caps": _normalize_caps(meta.get("caps")),
        "created_at": meta.get("created_at") or 0,
        "home": str(home(agent_id)),
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
    write_doc(aid, "AGENT.md", AGENT_TEMPLATE.format(name=name))
    projects.upsert_project(workdir)
    return get(aid)  # type: ignore[return-value]


def update(agent_id: str, *, name: str | None = None, avatar: dict | None = None,
           links: list | None = None, caps: dict | None = None) -> dict | None:
    """改名称 / 头像 / 关联 / 能力设定。目录名用 id,不随名称变。"""
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
    if caps is not None:
        meta["caps"] = _normalize_caps(caps)
    _write_meta(agent_id, meta)
    return get(agent_id)


# ── AGENT.md / NOTES.md ─────────────────────────────────────────────────
def read_doc(agent_id: str, name: str) -> str:
    if name not in DOC_NAMES:
        raise ValueError("只能读写 " + " / ".join(DOC_NAMES))
    try:
        return (home(agent_id) / name).read_text(encoding="utf-8")
    except OSError:
        return ""


def write_doc(agent_id: str, name: str, text: str) -> None:
    if name not in DOC_NAMES:
        raise ValueError("只能读写 " + " / ".join(DOC_NAMES))
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
            # agent.json 是系统设置(界面上改),workspace/ 在下面「工作目录」里单独列
            if p.is_file() and not rel.parts[0].startswith(".") and rel.parts[0] != "workspace" \
                    and rel.name not in ("agent.json", "agent.json.tmp"):
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


def has_content(doc: str) -> bool:
    """只有标题和空小节的模板不算内容。"""
    return any(line.strip() and not line.startswith("#") for line in (doc or "").splitlines())


def _without_section(doc: str, title: str) -> str:
    """去掉 markdown 里「## title」那一节(到下一个 ## 为止)。"""
    return re.sub(rf"^## {re.escape(title)}[ \t]*\n.*?(?=^## |\Z)", "", doc or "", flags=re.M | re.S).strip()


# ── 目标闭环 ────────────────────────────────────────────────────────────
def review_job(agent_id: str) -> dict | None:
    from ..cron import scheduler

    return next((j for j in scheduler.load_jobs()
                 if j.get("agent_id") == agent_id and j.get("role") == REVIEW_ROLE), None)


def ensure_goal_review(agent_id: str) -> dict | None:
    """目标有内容、还没挂复盘任务 → 挂一条每周复盘(只建一次;之后改时间/停用都在「定时」里改)。"""
    from ..cron import scheduler

    a = get(agent_id)
    if a is None or not has_content(read_doc(agent_id, "GOAL.md")):
        return None
    job = review_job(agent_id)
    if job:
        return job
    job = scheduler.create_job(
        name="目标复盘", prompt=REVIEW_PROMPT.format(name=a["name"], home=a["home"]),
        schedule={"kind": "cron", "expr": REVIEW_CRON}, cwd=a["workdir"], agent_id=agent_id,
    )
    jobs = scheduler.load_jobs()
    for j in jobs:
        if j["id"] == job["id"]:
            j["role"] = REVIEW_ROLE
    scheduler.save_jobs(jobs)
    return job


def _first_line(doc: str, section: str | None = None) -> str:
    """取正文第一行(可限定某个 ## 小节),给欢迎屏当一句话简介。"""
    if section:
        m = re.search(rf"^## {re.escape(section)}[ \t]*\n(.*?)(?=^## |\Z)", doc or "", re.M | re.S)
        doc = m.group(1) if m else ""
    for line in (doc or "").splitlines():
        s = line.strip().lstrip("-*> ").strip()
        if s and not line.startswith("#"):
            return s[:120]
    return ""


def brief(agent_id: str) -> dict:
    """欢迎屏用的一句话:职责(AGENT.md「职责与人格」首行)、目标(GOAL.md 首行)、有没有计划。"""
    return {
        "summary": _first_line(read_doc(agent_id, "AGENT.md"), "职责与人格"),
        "goal": _first_line(read_doc(agent_id, "GOAL.md")),
        "has_plan": has_content(read_doc(agent_id, "PLAN.md")),
    }


def plan_prompt(agent: dict) -> str:
    return PLAN_PROMPT.format(home=agent["home"])


def _log_run(agent_id: str, name: str, status: str, text: str, ts: float) -> None:
    """运行结果按月追加进 runs/YYYY-MM.md,复盘任务读它当数据。"""
    d = home(agent_id) / "runs"
    d.mkdir(parents=True, exist_ok=True)
    t = time.localtime(ts)
    with open(d / time.strftime("%Y-%m.md", t), "a", encoding="utf-8") as f:
        f.write(f"\n## {time.strftime('%m-%d %H:%M', t)} · {name} · {status}\n\n{text.strip()[:4000]}\n")


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
    aid = agent["id"]
    doc = read_doc(aid, "AGENT.md").strip()
    doc = doc if has_content(doc) else ""
    # 复盘记录是历史,不常驻提示词(要看自己去读文件)
    goal = _without_section(read_doc(aid, "GOAL.md"), "复盘记录")
    goal = goal if has_content(goal) else ""
    plan = read_doc(aid, "PLAN.md").strip()
    plan = plan if goal and has_content(plan) else ""
    notes = read_doc(aid, "NOTES.md").strip()
    links = agent.get("links") or []
    if not (doc or goal or notes or links):
        return ""
    parts = [f"# 当前 Agent:{agent['name']}\n以下是这个 Agent 的设定,本会话里按它办事。"
             f"它的文件在 {agent['home']}/(GOAL.md 目标、PLAN.md 计划、NOTES.md 笔记、runs/ 运行记录)。"]
    if doc:
        parts.append(doc)
    if goal:
        parts.append("## 目标(GOAL.md)\n" + goal.removeprefix("# 目标").strip())
        parts.append("## 计划(PLAN.md)\n" + (plan.removeprefix("# 计划").strip() if plan else "还没拆。"))
        parts.append("## 目标纪律\n- 做事前对照目标和「不做」;明显和目标无关的事,先提醒我再做\n"
                     "- 做完计划里的任务,顺手在 PLAN.md 里勾掉\n"
                     "- 「目标」「成功标准」「不做」只有我同意才能改")
    if notes:
        parts.append("## 笔记(NOTES.md)\n" + notes)
    if links:
        parts.append("## 关联目录/文件(需要时再打开)\n" + "\n".join(
            f"- {x['path']}" + (f" · {x['note']}" if x["note"] else "") + (" · 可写" if x["writable"] else " · 只读")
            for x in links))
    text = "\n\n".join(parts)
    return text[:PROMPT_MAX_CHARS]


def caps_for_session(session_key: str | None) -> dict:
    """会话所属 Agent 的能力设定;不属于任何 Agent / 读失败 → 全空(行为同以前)。"""
    empty = _normalize_caps(None)
    if not session_key:
        return empty
    try:
        agent = agent_for_session(session_key)
    except Exception as exc:  # noqa: BLE001 —— 读不到设定不影响正常对话
        print(f"[agents] 读取 Agent 能力设定失败:{exc}", flush=True)
        return empty
    return agent["caps"] if agent else empty


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
    now = time.time()
    try:
        _log_run(agent_id, name, status, text, now)
    except OSError as exc:
        print(f"[agents] 写运行记录失败:{exc}", flush=True)
    c = _conn()
    cur = c.execute(
        "INSERT INTO turns(session_key, ts, user_text, assistant_text) VALUES (?,?,?,?)",
        (key, now, f"{SYS_MARKER} ⏰ {name}", text),
    )
    turn_id = cur.lastrowid
    c.execute(
        "INSERT INTO agent_runs(agent_id, job_id, job_name, ts, status, text, turn_id) VALUES (?,?,?,?,?,?,?)",
        (agent_id, job.get("id") or "", name, now, status, text[:4000], turn_id),
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
