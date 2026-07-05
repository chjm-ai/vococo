"""会话持久化 —— SQLite。

- 按 session_key 隔离(CLI 用 "cli",Telegram 用 "tg:<chat_id>")
- load_recent 取最近 N 轮喂上下文 → 重启不失忆
- search 用 LIKE 子串匹配(中文友好;FTS5 默认分词器切不开中文,个人规模 LIKE 足够)
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .. import config

if TYPE_CHECKING:  # 仅类型注解;运行时延迟 import,避免 agent↔store 循环导入
    from ..core.agent import Turn

_DB: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns(
  id INTEGER PRIMARY KEY,
  session_key TEXT NOT NULL,
  ts REAL NOT NULL,
  user_text TEXT NOT NULL,
  assistant_text TEXT NOT NULL,
  draft_text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_key, id);
CREATE TABLE IF NOT EXISTS session_meta(
  session_key TEXT PRIMARY KEY,
  watermark_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS projects(
  hash TEXT PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  last_used REAL NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_prefs(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    global _DB
    if _DB is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _DB = sqlite3.connect(
            str(config.DATA_DIR / "state.db"), check_same_thread=False
        )
        _DB.execute("PRAGMA journal_mode=WAL")
        _DB.executescript(_SCHEMA)
        # 迁移:session_meta 增列(老库平滑升级)
        cols = {r[1] for r in _DB.execute("PRAGMA table_info(session_meta)")}
        if "title" not in cols:  # Web 会话列表用
            _DB.execute("ALTER TABLE session_meta ADD COLUMN title TEXT")
        # token 计量:ctx=当前上下文占用,total=累计消耗,window=模型窗口,
        # last_*=最近一轮明细(展开面板用),model=实际模型
        for col in (
            "ctx_tokens",
            "total_tokens",
            "ctx_window",
            "last_in",
            "last_cache",
            "last_out",
        ):
            if col not in cols:
                _DB.execute(
                    f"ALTER TABLE session_meta ADD COLUMN {col} INTEGER DEFAULT 0"
                )
        if "model" not in cols:
            _DB.execute("ALTER TABLE session_meta ADD COLUMN model TEXT")
        # chosen_model=用户为该会话选定、下一轮要用的模型(与 model「实际跑过的带日期 id」分开)
        if "chosen_model" not in cols:
            _DB.execute("ALTER TABLE session_meta ADD COLUMN chosen_model TEXT")
        # sdk_session_id=SDK(claude CLI)自己维护的会话 id。存下它,下一轮用 resume=<id>
        # 让 SDK 从它自己的 transcript 重放【真·多轮历史】,而不是我们把历史拼成一坨文本喂进去
        # (后者会让模型误判自己"记不住/被压缩")。NULL=还没有,起新会话。
        if "sdk_session_id" not in cols:
            _DB.execute("ALTER TABLE session_meta ADD COLUMN sdk_session_id TEXT")
        if "archived" not in cols:
            _DB.execute("ALTER TABLE session_meta ADD COLUMN archived INTEGER DEFAULT 0")
        # worktree_path=该会话独占的 git worktree 目录(每会话一分支的物理隔离);
        # NULL=没有独立 worktree,回退项目根/进程默认目录。
        if "worktree_path" not in cols:
            _DB.execute("ALTER TABLE session_meta ADD COLUMN worktree_path TEXT")
        # 迁移:turns 增 events 列 —— 该轮的过程时间线(文字段+工具调用)JSON,
        # 供前端刷新后完整重建"工具卡与文字交错"的画面;老行为 NULL(只有纯文本)。
        tcols = {r[1] for r in _DB.execute("PRAGMA table_info(turns)")}
        if "events" not in tcols:
            _DB.execute("ALTER TABLE turns ADD COLUMN events TEXT")
        # draft_text: 流式进行中把"当前已输出的正文"节流写进该列;
        # 前端刷新后 /history 能拿到部分内容兜底,避免"刷新时进行中的回复一片空白"。
        # 轮末 finish_turn 把 assistant_text 写全并清空 draft(最终版都在 assistant_text)。
        if "draft_text" not in tcols:
            _DB.execute("ALTER TABLE turns ADD COLUMN draft_text TEXT NOT NULL DEFAULT ''")
        _DB.commit()
    return _DB


def _watermark(c: sqlite3.Connection, session_key: str) -> int:
    row = c.execute(
        "SELECT watermark_id FROM session_meta WHERE session_key=?", (session_key,)
    ).fetchone()
    return row[0] if row else 0


def load_recent(session_key: str, limit: int = 40) -> list[Turn]:
    """只取当前会话(水位线之后)的最近 N 轮;跳过进行中(assistant_text='')的 turn。"""
    from ..core.agent import Turn  # 延迟 import:打破模块级循环依赖

    c = _conn()
    wm = _watermark(c, session_key)
    rows = c.execute(
        "SELECT user_text, assistant_text FROM turns "
        "WHERE session_key=? AND id>? AND assistant_text!='' ORDER BY id DESC LIMIT ?",
        (session_key, wm, limit),
    ).fetchall()
    return [Turn(user=u, assistant=a) for u, a in reversed(rows)]


def load_history(session_key: str, limit: int = 40) -> list[dict]:
    """历史展示用:含进行中 turn,pending=True 标注;events 是该轮过程时间线。

    pending 轮有 draft_text 时一并返回 —— 前端刷新后可用部分内容起底重建气泡,
    避免「刷新时进行中的回复一片空白」;SSE 恢复后同 seg 全文覆盖无缝续上。
    """
    c = _conn()
    wm = _watermark(c, session_key)
    rows = c.execute(
        "SELECT user_text, assistant_text, events, draft_text FROM turns "
        "WHERE session_key=? AND id>? ORDER BY id DESC LIMIT ?",
        (session_key, wm, limit),
    ).fetchall()
    out: list[dict] = []
    for u, a, ev, draft in reversed(rows):
        try:
            events = json.loads(ev) if ev else []
        except (json.JSONDecodeError, ValueError):
            events = []
        entry: dict = {"user": u, "assistant": a, "pending": a == "", "events": events}
        if a == "" and draft:
            entry["draft"] = draft
        out.append(entry)
    return out


def new_session(session_key: str) -> None:
    """开新会话:把水位线推到当前最大 id —— 旧轮次保留在库(可 search),但不再载入上下文。"""
    c = _conn()
    row = c.execute(
        "SELECT COALESCE(MAX(id),0) FROM turns WHERE session_key=?", (session_key,)
    ).fetchone()
    # 上下文清零 → token 计量也归零(进度/消耗都描述"当前窗口")
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id, ctx_tokens, total_tokens) "
        "VALUES (?,?,0,0) "
        "ON CONFLICT(session_key) DO UPDATE SET "
        "watermark_id=excluded.watermark_id, ctx_tokens=0, total_tokens=0, "
        "last_in=0, last_cache=0, last_out=0, sdk_session_id=NULL",
        (session_key, row[0]),
    )
    c.commit()


def record_usage(
    session_key: str,
    ctx_tokens: int,
    add_tokens: int,
    *,
    window: int = 0,
    last_in: int = 0,
    last_cache: int = 0,
    last_out: int = 0,
    model: str = "",
) -> None:
    """一轮结束后更新 token 计量:ctx_tokens/明细取最新(=当前上下文占用),
    total_tokens 累加(=当前窗口的消耗)。"""
    c = _conn()
    c.execute(
        "INSERT INTO session_meta"
        "(session_key, watermark_id, ctx_tokens, total_tokens, ctx_window, "
        " last_in, last_cache, last_out, model) "
        "VALUES (?,0,?,?,?,?,?,?,?) "
        "ON CONFLICT(session_key) DO UPDATE SET "
        "ctx_tokens=excluded.ctx_tokens, "
        "total_tokens=COALESCE(session_meta.total_tokens,0)+excluded.total_tokens, "
        "ctx_window=excluded.ctx_window, last_in=excluded.last_in, "
        "last_cache=excluded.last_cache, last_out=excluded.last_out, "
        "model=excluded.model",
        (session_key, ctx_tokens, add_tokens, window, last_in, last_cache, last_out, model),
    )
    c.commit()


def append(session_key: str, user_text: str, assistant_text: str) -> None:
    c = _conn()
    c.execute(
        "INSERT INTO turns(session_key, ts, user_text, assistant_text) "
        "VALUES (?,?,?,?)",
        (session_key, time.time(), user_text, assistant_text),
    )
    c.commit()


def start_turn(session_key: str, user_text: str) -> int:
    """入库进行中 turn(assistant_text 留空);返回 row id 供 finish_turn/cancel_turn 配对。"""
    c = _conn()
    cur = c.execute(
        "INSERT INTO turns(session_key, ts, user_text, assistant_text) VALUES (?,?,?,'')",
        (session_key, time.time(), user_text),
    )
    c.commit()
    return cur.lastrowid  # type: ignore[return-value]


def finish_turn(turn_id: int, assistant_text: str, events: list | None = None) -> None:
    """用 AI 回复填完 start_turn 占的坑;events=该轮过程时间线(可选)。"""
    c = _conn()
    ev_json = None
    if events:
        try:
            ev_json = json.dumps(events, ensure_ascii=False)
        except (TypeError, ValueError):
            ev_json = None  # 时间线序列化失败不影响正文落库
    c.execute(
        "UPDATE turns SET assistant_text=?, events=?, draft_text='' WHERE id=?",
        (assistant_text, ev_json, turn_id),
    )
    c.commit()


def cancel_turn(turn_id: int) -> None:
    """AI 回复出错/取消时删掉进行中的 turn,避免孤儿 pending 行残留。"""
    c = _conn()
    c.execute("DELETE FROM turns WHERE id=? AND assistant_text=''", (turn_id,))
    c.commit()


def flush_draft(turn_id: int, text: str) -> None:
    """流式进行中:把当前已输出的正文节流写进 draft_text 列,供刷新后兜底。"""
    c = _conn()
    c.execute("UPDATE turns SET draft_text=? WHERE id=?", (text, turn_id))
    c.commit()


def clear(session_key: str) -> None:
    c = _conn()
    c.execute("DELETE FROM turns WHERE session_key=?", (session_key,))
    c.execute(
        "UPDATE session_meta SET ctx_tokens=0, total_tokens=0, "
        "last_in=0, last_cache=0, last_out=0, sdk_session_id=NULL WHERE session_key=?",
        (session_key,),
    )
    c.commit()


# === Web 会话管理(侧边栏)===
# Web 端自带多会话:每个对话一个 session_key(如 web:1720000000abc)。
# 下面这几个 helper 让前端能列出/命名/删除会话,TG/CLI 不受影响。


def set_title(session_key: str, title: str) -> None:
    """给会话起个显示名(upsert,不动 watermark)。"""
    c = _conn()
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id, title) VALUES (?,0,?) "
        "ON CONFLICT(session_key) DO UPDATE SET title=excluded.title",
        (session_key, title),
    )
    c.commit()


def get_title(session_key: str) -> str | None:
    row = _conn().execute(
        "SELECT title FROM session_meta WHERE session_key=?", (session_key,)
    ).fetchone()
    return row[0] if row and row[0] else None


def ensure_title(session_key: str, from_text: str, limit: int = 24) -> None:
    """会话还没名字时,拿首句用户输入截断当标题。"""
    if get_title(session_key):
        return
    title = " ".join(from_text.split())[:limit].strip() or "新对话"
    set_title(session_key, title)


def list_sessions(prefix: str) -> list[dict]:
    """列出 session_key 以 prefix 开头的会话摘要,最近活跃的排前面。

    返回 [{key, title, turns, last_ts}, ...]。turns 只算当前上下文窗口
    (水位线之后),= 该会话现在带着多少轮记忆。

    会话键来源取「有 turn」∪「有标题」:新建会话在 AI 回复完之前只写了
    session_meta 标题、还没有 turn 落库,若只按 turns 表列会导致它刚发完
    第一条就从侧边栏消失(要等回复完才出现)。带标题即视为一个会话。
    """
    c = _conn()
    rows = c.execute(
        "WITH keys AS ("
        "  SELECT session_key FROM turns WHERE session_key LIKE ?"
        "  UNION"
        "  SELECT session_key FROM session_meta "
        "  WHERE session_key LIKE ? AND title IS NOT NULL AND title != ''"
        ") "
        # 还没 turn 的新会话用「当前时间」兜底,好让它在等首条回复期间稳定
        # 排在列表最前(前端按 last_ts 倒序);回复落库后自然换成真实 turn 时间。
        "SELECT k.session_key, COUNT(t.id), "
        "  COALESCE(MAX(t.ts), strftime('%s','now')), "
        "  COALESCE(MAX(m.ctx_tokens),0), COALESCE(MAX(m.total_tokens),0), "
        "  COALESCE(MAX(m.ctx_window),0), COALESCE(MAX(m.last_in),0), "
        "  COALESCE(MAX(m.last_cache),0), COALESCE(MAX(m.last_out),0), MAX(m.model), "
        "  MAX(m.chosen_model), COALESCE(MAX(m.archived),0) "
        "FROM keys k "
        "LEFT JOIN session_meta m ON k.session_key = m.session_key "
        "LEFT JOIN turns t ON t.session_key = k.session_key "
        "  AND t.id > COALESCE(m.watermark_id, 0) "
        "GROUP BY k.session_key ORDER BY 3 DESC",
        (prefix + "%", prefix + "%"),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        key, turns, last_ts = row[0], row[1], row[2]
        item = {"key": key, "title": get_title(key) or "新对话", "turns": turns, "last_ts": last_ts}
        item.update(_usage_fields(row[3:11]))
        item["archived"] = bool(row[11])
        out.append(item)
    return out


def _usage_fields(vals) -> dict:
    """把 (ctx, total, window, last_in, last_cache, last_out, model, chosen_model) 组装成统一字段。"""
    ctx, total, window, l_in, l_cache, l_out, model, chosen = vals
    return {
        "ctx_tokens": ctx or 0,
        "total_tokens": total or 0,
        "ctx_window": window or 0,
        "last_in": l_in or 0,
        "last_cache": l_cache or 0,
        "last_out": l_out or 0,
        "model": model or "",         # 实际跑过的模型(带日期,给 token 面板)
        "chosen_model": chosen or "",  # 会话选定的模型(给输入框胶囊,空=用默认)
    }


def session_summary(session_key: str) -> dict:
    """单个会话摘要(给固定入口如统一主会话用)。"""
    c = _conn()
    wm = _watermark(c, session_key)
    row = c.execute(
        "SELECT COUNT(*), MAX(ts) FROM turns WHERE session_key=? AND id>?",
        (session_key, wm),
    ).fetchone()
    turns = row[0] if row else 0
    last_ts = row[1] if row and row[1] else None
    trow = c.execute(
        "SELECT COALESCE(ctx_tokens,0), COALESCE(total_tokens,0), "
        "COALESCE(ctx_window,0), COALESCE(last_in,0), COALESCE(last_cache,0), "
        "COALESCE(last_out,0), model, chosen_model FROM session_meta WHERE session_key=?",
        (session_key,),
    ).fetchone()
    out = {
        "key": session_key,
        "title": get_title(session_key),
        "turns": turns,
        "last_ts": last_ts,
    }
    out.update(_usage_fields(trow if trow else (0, 0, 0, 0, 0, 0, "", "")))
    return out


def set_chosen_model(session_key: str, model: str) -> None:
    """记住某会话用户选定的模型(下一轮要用哪个);/model 切换时写入,刷新/重启都不丢。"""
    c = _conn()
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id, chosen_model) VALUES (?,0,?) "
        "ON CONFLICT(session_key) DO UPDATE SET chosen_model=excluded.chosen_model",
        (session_key, model),
    )
    c.commit()


def get_chosen_model(session_key: str) -> str:
    """会话选定的模型;没设过返回空串(调用方回落到 config.MODEL 默认)。"""
    row = _conn().execute(
        "SELECT chosen_model FROM session_meta WHERE session_key=?", (session_key,)
    ).fetchone()
    return (row[0] if row else "") or ""


def get_sdk_session_id(session_key: str) -> str | None:
    """该会话上一轮拿到的 SDK 会话 id;下一轮用它 resume 真·多轮历史。无则 None(起新会话)。"""
    row = _conn().execute(
        "SELECT sdk_session_id FROM session_meta WHERE session_key=?", (session_key,)
    ).fetchone()
    return (row[0] if row else None) or None


def set_sdk_session_id(session_key: str, sid: str) -> None:
    """记住本轮 SDK 会话 id(每轮结束用最新值覆盖,链就不会断);upsert,不动其余字段。"""
    c = _conn()
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id, sdk_session_id) VALUES (?,0,?) "
        "ON CONFLICT(session_key) DO UPDATE SET sdk_session_id=excluded.sdk_session_id",
        (session_key, sid),
    )
    c.commit()


# === 每会话独立 worktree(借鉴 Claude Code)===
# 会话↔工作目录的绑定落库,而非实时靠「当前分支」猜 —— 这样每个会话读自己的
# worktree、各在各的分支,物理隔离,不再互相抢分支。


def get_worktree(session_key: str) -> str | None:
    """该会话独占的 worktree 目录;没有则 None(回退项目根)。"""
    row = _conn().execute(
        "SELECT worktree_path FROM session_meta WHERE session_key=?", (session_key,)
    ).fetchone()
    return (row[0] if row else None) or None


def all_worktree_paths() -> list[str]:
    """DB 里当前所有会话绑定的 worktree 目录 —— 启动清孤儿时的「活会话」白名单。"""
    rows = _conn().execute(
        "SELECT worktree_path FROM session_meta "
        "WHERE worktree_path IS NOT NULL AND worktree_path != ''"
    ).fetchall()
    return [r[0] for r in rows]


def set_worktree(session_key: str, path: str) -> None:
    """记住某会话的 worktree 目录(upsert,不动其余字段)。"""
    c = _conn()
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id, worktree_path) VALUES (?,0,?) "
        "ON CONFLICT(session_key) DO UPDATE SET worktree_path=excluded.worktree_path",
        (session_key, path),
    )
    c.commit()


def clear_worktree(session_key: str) -> None:
    """解除会话与 worktree 的绑定(目录被清理后调用)。"""
    c = _conn()
    c.execute(
        "UPDATE session_meta SET worktree_path=NULL WHERE session_key=?", (session_key,)
    )
    c.commit()


def delete_session(session_key: str) -> None:
    """彻底删掉一个会话(所有轮次 + 元数据)。"""
    c = _conn()
    c.execute("DELETE FROM turns WHERE session_key=?", (session_key,))
    c.execute("DELETE FROM session_meta WHERE session_key=?", (session_key,))
    c.commit()


# === 项目(Web 端)===
# 项目 = 用户选的一个文件夹当 agent 的 cwd,身份即其规范化绝对路径。
# 会话 key 里以路径短哈希编码(web:p<hash>:<conv>);这张表存 哈希→路径 供 UI 显示。
# 「移除项目」是软移除(hidden=1):记录与其会话历史都留库,再加回同一文件夹即复活。


def project_hash(path: str) -> str:
    """规范化绝对路径 → 定长短哈希(同文件夹恒得同哈希,天然去重)。"""
    norm = normalize_project_path(path)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def normalize_project_path(path: str) -> str:
    """展开 ~、转绝对路径并消解 .. ——「路径即身份」的规范化基准。"""
    return str(Path(os.path.expanduser(path)).resolve())


def upsert_project(path: str) -> dict:
    """按文件夹路径建/复活项目;返回 {hash, path, name, last_used}。

    同一文件夹已存在则复活(hidden=0)并刷新 last_used;不存在则新建。
    """
    norm = normalize_project_path(path)
    h = project_hash(norm)
    now = time.time()
    c = _conn()
    c.execute(
        "INSERT INTO projects(hash, path, last_used, hidden) VALUES (?,?,?,0) "
        "ON CONFLICT(hash) DO UPDATE SET last_used=excluded.last_used, hidden=0",
        (h, norm, now),
    )
    c.commit()
    return {"hash": h, "path": norm, "name": os.path.basename(norm) or norm, "last_used": now}


def list_projects() -> list[dict]:
    """未隐藏的项目,最近使用的排前面。名 = 文件夹名(basename)。"""
    rows = _conn().execute(
        "SELECT hash, path, last_used FROM projects WHERE hidden=0 ORDER BY last_used DESC"
    ).fetchall()
    return [
        {"hash": h, "path": p, "name": os.path.basename(p) or p, "last_used": ts}
        for h, p, ts in rows
    ]


def path_for_hash(h: str) -> str | None:
    """按哈希反查文件夹路径(隐藏的也返回,好让在跑的会话仍有 cwd);找不到返回 None。"""
    row = _conn().execute(
        "SELECT path FROM projects WHERE hash=?", (h,)
    ).fetchone()
    return row[0] if row else None


def hide_project(h: str) -> None:
    """软移除:仅从列表隐藏,项目记录与其会话历史都保留(可复活)。"""
    c = _conn()
    c.execute("UPDATE projects SET hidden=1 WHERE hash=?", (h,))
    c.commit()


def touch_project(h: str) -> None:
    """标记项目最近被使用(刷新侧边栏排序)。"""
    c = _conn()
    c.execute("UPDATE projects SET last_used=? WHERE hash=?", (time.time(), h))
    c.commit()


def search(text: str, limit: int = 10) -> list[tuple[str, str, str]]:
    """子串检索,返回 (session_key, user_text, assistant_text)。

    按空白把查询拆成多个关键词:任一命中即返回(OR),命中关键词多的排前面,
    其次按时间倒序。中文长句直接整串 LIKE 很难命中,拆词后召回率高得多
    (agent 传「出差 名古屋」比传整句更容易召回到历史片段)。
    """
    terms = [t for t in text.split() if t] or [text]
    # 每个关键词:命中 user 或 assistant 记 1 分,SUM 为该轮总命中分
    score = " + ".join(
        "(CASE WHEN user_text LIKE ? OR assistant_text LIKE ? THEN 1 ELSE 0 END)"
        for _ in terms
    )
    where = " OR ".join("user_text LIKE ? OR assistant_text LIKE ?" for _ in terms)
    likes = [f"%{t}%" for t in terms]
    score_params = [p for like in likes for p in (like, like)]
    where_params = list(score_params)
    return _conn().execute(
        f"SELECT session_key, user_text, assistant_text FROM turns "
        f"WHERE {where} ORDER BY ({score}) DESC, id DESC LIMIT ?",
        (*where_params, *score_params, limit),
    ).fetchall()


def set_conv_archived(session_key: str, archived: bool) -> None:
    """设置会话归档状态;若 session_meta 行不存在则先插入。"""
    c = _conn()
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id, archived) VALUES(?,0,?) "
        "ON CONFLICT(session_key) DO UPDATE SET archived=excluded.archived",
        (session_key, 1 if archived else 0),
    )
    c.commit()


def get_prefs() -> dict:
    """返回全部用户偏好(key→value 字符串字典)。"""
    rows = _conn().execute("SELECT key, value FROM user_prefs").fetchall()
    return {k: v for k, v in rows}


def set_prefs(updates: dict) -> None:
    """批量写入用户偏好;未传的 key 保持不变。"""
    c = _conn()
    for k, v in updates.items():
        c.execute(
            "INSERT INTO user_prefs(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(k), str(v)),
        )
    c.commit()
