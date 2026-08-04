"""会话内容检索:子串 LIKE 匹配(中文友好;FTS5 默认分词器切不开中文,个人规模够用)。

2026-07-23 从 session_store.py 拆出(见 images.py 顶部说明)。
"""
from __future__ import annotations

from .. import config
from . import _db

# 侧边栏全局搜索(⌘F)覆盖的会话前缀:普通 Web 会话 + 统一后台任务(语音/cron/chat
# 三种触发方共用的 task: 前缀,2026-07-29 起 voice-task:/cron-task: 合并于此)。
_SEARCH_PREFIXES = ("web:", "task:")


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
    return _db.conn().execute(
        f"SELECT session_key, user_text, assistant_text FROM turns "
        f"WHERE {where} ORDER BY ({score}) DESC, id DESC LIMIT ?",
        (*where_params, *score_params, limit),
    ).fetchall()


def _like_escape(text: str) -> str:
    """转义 LIKE 通配符,让用户输入里的 % _ 按字面匹配(配合 ESCAPE '\\')。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snippet(text: str, q: str, radius: int = 36) -> str:
    """截取命中关键词前后的一小段当摘要;压平空白成单行。"""
    idx = text.lower().find(q.lower())
    if idx < 0:
        return ""
    start = max(0, idx - radius)
    frag = text[start : idx + len(q) + radius * 2]
    frag = " ".join(frag.split())
    return ("…" if start > 0 else "") + frag


def search_sessions(q: str, limit: int = 50) -> list[dict]:
    """侧边栏全局搜索:标题命中排前,其次正文命中;归档会话照常返回
    (archived 字段带回给前端标注),被删除的会话已物理删除,天然搜不到。

    返回 [{key, title, archived, last_ts, match, snippet}];match 为
    "title"/"content"。个人规模(几千轮/约 1MB 文本)LIKE 全表扫描毫秒级,
    与 search() 同理不上 FTS(FTS5 默认分词器切不开中文)。
    """
    c = _db.conn()
    like = f"%{_like_escape(q)}%"
    # 主会话(SESSION_KEY,与 TG/CLI 共享)也参与搜索,按完整 key 精确匹配
    key_where = "(" + " OR ".join(
        ["session_key LIKE ?"] * len(_SEARCH_PREFIXES) + ["session_key = ?"]
    ) + ")"
    key_params = [p + "%" for p in _SEARCH_PREFIXES] + [config.SESSION_KEY]
    out: list[dict] = []
    seen: set[str] = set()
    # 1) 标题命中(last_ts 用该会话最近一轮时间,供前端排序展示)
    rows = c.execute(
        "SELECT session_key, title, COALESCE(archived,0), "
        "  (SELECT MAX(ts) FROM turns t WHERE t.session_key=session_meta.session_key) "
        f"FROM session_meta WHERE title LIKE ? ESCAPE '\\' AND {key_where} "
        "ORDER BY 4 DESC LIMIT ?",
        (like, *key_params, limit),
    ).fetchall()
    for key, title, arch, ts in rows:
        seen.add(key)
        out.append({
            "key": key, "title": title, "archived": bool(arch),
            "last_ts": ts, "match": "title", "snippet": "",
        })
    # 2) 正文命中:每个会话只取最近一条命中轮,截关键词上下文当摘要
    rows = c.execute(
        "SELECT t.session_key, t.user_text, t.assistant_text, t.ts FROM turns t "
        "JOIN (SELECT session_key, MAX(id) AS mid FROM turns "
        "      WHERE (user_text LIKE ? ESCAPE '\\' OR assistant_text LIKE ? ESCAPE '\\') "
        f"        AND {key_where} GROUP BY session_key) m ON t.id=m.mid "
        "ORDER BY t.ts DESC LIMIT ?",
        (like, like, *key_params, limit),
    ).fetchall()
    for key, u, a, ts in rows:
        if key in seen or len(out) >= limit:
            continue
        meta = c.execute(
            "SELECT title, COALESCE(archived,0) FROM session_meta WHERE session_key=?",
            (key,),
        ).fetchone()
        out.append({
            "key": key,
            "title": (meta[0] if meta else None) or "新对话",
            "archived": bool(meta[1]) if meta else False,
            "last_ts": ts,
            "match": "content",
            "snippet": _snippet(u, q) or _snippet(a, q),
        })
    return out[:limit]
