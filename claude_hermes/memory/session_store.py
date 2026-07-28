"""会话持久化 —— SQLite。

- 按 session_key 隔离(CLI 用 "cli",Telegram 用 "tg:<chat_id>")
- load_recent 取最近 N 轮喂上下文 → 重启不失忆
- search 用 LIKE 子串匹配(中文友好;FTS5 默认分词器切不开中文,个人规模 LIKE 足够)

本文件只管「会话的轮次(turns)+ session_meta」这一个关注点。图片 blob(images.py)、
项目路径哈希(projects.py)、worktree 绑定(worktrees.py)、检索(search.py)、用户偏好
(prefs.py)已拆成同包兄弟模块各自维护——2026-07-23 前这些都挤在本文件里,45 个函数
只共享一份连接单例,现按关注点收口成各自的深模块(连接单例本身也收口进 _db.py)。
下面仍原样 re-export 它们的公开函数,是为了不动全仓库几十处 `session_store.xxx(...)`
调用点(此次拆分只重排内部结构,不改对外接口)。
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from . import _db
from .images import image_path, purge_session_images, save_turn_images  # noqa: F401 (re-export)
from .prefs import get_prefs, set_prefs  # noqa: F401 (re-export)
from .projects import (  # noqa: F401 (re-export)
    hide_project,
    list_projects,
    normalize_project_path,
    path_for_hash,
    project_hash,
    reorder_projects,
    touch_project,
    upsert_project,
)
from .search import search, search_sessions  # noqa: F401 (re-export)
from .worktrees import (  # noqa: F401 (re-export)
    all_worktree_paths,
    clear_worktree,
    get_worktree,
    set_worktree,
)

if TYPE_CHECKING:  # 仅类型注解;运行时延迟 import,避免 agent↔store 循环导入
    from ..core.agent import Turn


def _conn():
    return _db.conn()


def _watermark(c, session_key: str) -> int:
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


# 与前端 tool-card.js 的 renderToolCard() 决策保持一致:这几个工具名不看 input 内容,
# 一定会渲出卡片;其余工具只有 input 里带 file_path/path/pattern/query/url 这类"提示字段"
# 才会渲卡。瘦身时算出 hasCard(该不该占位)+ hint(提示字段本身,通常很短),
# 这样空壳阶段就能还原出「这一条要不要显示卡片」,不必等点开才知道。
_ALWAYS_CARD_TOOLS = {"Task", "Agent", "ExitPlanMode", "Write", "Edit", "MultiEdit", "Bash"}
_HINT_KEYS = ("file_path", "path", "pattern", "query", "url")


def _strip_tool_block(blk: dict) -> dict:
    """工具块瘦身成"空壳":砍掉 input/preview/detail(单个工具调用输出可能几KB到几十KB),
    只留渲染占位卡够用的字段。前端点开卡片时靠 load_turn_events 单独按需补全。"""
    name = blk.get("name")
    inp = blk.get("input") or {}
    failed = blk.get("ok", True) is False
    detail = (blk.get("detail") or "").strip()
    hint = ""
    if name == "TodoWrite":
        produced = bool(inp.get("todos"))
    elif name in _ALWAYS_CARD_TOOLS:
        produced = True
    else:
        hint = next((inp.get(k) for k in _HINT_KEYS if inp.get(k)), "")
        produced = bool(hint)
    light: dict = {
        "type": "tool", "name": name, "id": blk.get("id"),
        "ok": blk.get("ok", True), "done": "preview" in blk,
        "hasCard": produced or failed or bool(detail),
    }
    if hint:
        light["hint"] = hint
    if blk.get("subs"):
        light["subs"] = blk["subs"]  # 子代理步骤本就轻(仅 name/ok),原样带上
    return light


def _strip_events(events: list) -> list:
    return [_strip_tool_block(b) if b.get("type") == "tool" else b for b in events]


def load_history(session_key: str, limit: int = 40, *, full_events: bool = False) -> list[dict]:
    """历史展示用:含进行中 turn,pending=True 标注;events 是该轮过程时间线。

    pending 轮有 draft_text 时一并返回 —— 前端刷新后可用部分内容起底重建气泡,
    避免「刷新时进行中的回复一片空白」;SSE 恢复后同 seg 全文覆盖无缝续上。

    full_events=False(默认)时,tool 块的 input/preview/detail 会被砍掉只留空壳
    ——这几个字段是单轮体积的大头(工具调用多的一轮能到几十KB),前端只在用户
    点开某个工具卡片时才用 load_turn_events(turn_id) 单独按需取那一轮的完整版。
    """
    c = _conn()
    wm = _watermark(c, session_key)
    rows = c.execute(
        "SELECT id, user_text, assistant_text, events, draft_text, images FROM turns "
        "WHERE session_key=? AND id>? ORDER BY id DESC LIMIT ?",
        (session_key, wm, limit),
    ).fetchall()
    out: list[dict] = []
    for tid, u, a, ev, draft, imgs in reversed(rows):
        try:
            events = json.loads(ev) if ev else []
        except (json.JSONDecodeError, ValueError):
            events = []
        if events and not full_events:
            events = _strip_events(events)
        entry: dict = {"id": tid, "user": u, "assistant": a, "pending": a == "", "events": events}
        if a == "" and draft:
            entry["draft"] = draft
        # 图片:库里存文件名,给前端换成取图 URL(前端 <img src> 直接用)
        try:
            names = json.loads(imgs) if imgs else []
        except (json.JSONDecodeError, ValueError):
            names = []
        if names:
            entry["images"] = ["/image?name=" + n for n in names]
        out.append(entry)
    return out


def load_turn_events(session_key: str, turn_id: int) -> list | None:
    """按 id 精确取某一轮的完整过程时间线(懒加载工具卡详情用)。

    带 session_key 一起查,防止越权拿到别的会话的轮次;查不到/对不上返回 None。
    """
    row = _conn().execute(
        "SELECT events FROM turns WHERE id=? AND session_key=?", (turn_id, session_key)
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, ValueError):
        return None


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
    purge_session_images(c, session_key)  # 先清图片文件,再删轮次
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


def ensure_title(session_key: str, from_text: str, limit: int = 40) -> str | None:
    """会话还没名字时,拿首句用户输入截断当【兜底】标题。

    limit 与 core/title.MAX_LEN 保持一致。返回这次新设的兜底标题;已有标题
    返回 None——调用方(web 适配器)据此决定要不要异步起模型总结覆盖它。
    """
    if get_title(session_key):
        return None
    title = " ".join(from_text.split())[:limit].strip() or "新对话"
    set_title(session_key, title)
    return title


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
        "  MAX(m.chosen_model), COALESCE(MAX(m.archived),0), "
        "  COALESCE(MAX(m.pending_review),0), COALESCE(MAX(m.pinned),0), "
        "  COALESCE(MAX(m.last_error),0) "
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
        item["pending_review"] = bool(row[12])
        item["pinned"] = bool(row[13])
        item["last_error"] = bool(row[14])
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


def set_pending_review(session_key: str, pending: bool) -> None:
    """标记会话是否有新完成内容待用户查看;打开会话后清零。"""
    c = _conn()
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id, pending_review) VALUES (?,0,?) "
        "ON CONFLICT(session_key) DO UPDATE SET pending_review=excluded.pending_review",
        (session_key, 1 if pending else 0),
    )
    c.commit()


def set_conv_pinned(session_key: str, pinned: bool) -> None:
    """置顶/取消置顶:侧边栏顶部\"置顶\"分组的开关。"""
    c = _conn()
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id, pinned) VALUES (?,0,?) "
        "ON CONFLICT(session_key) DO UPDATE SET pinned=excluded.pinned",
        (session_key, 1 if pinned else 0),
    )
    c.commit()


def set_last_error(session_key: str, is_error: bool) -> None:
    """记下该会话最近一轮是否以报错收尾(限流/超时/模型层错误等)——语音端
    voice_list_web_sessions 靠它筛出"卡住等续聊"的网页会话,不用去猜最后一条
    回复文本是不是错误提示。"""
    c = _conn()
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id, last_error) VALUES (?,0,?) "
        "ON CONFLICT(session_key) DO UPDATE SET last_error=excluded.last_error",
        (session_key, 1 if is_error else 0),
    )
    c.commit()


def set_chosen_model(session_key: str, model: str) -> None:
    """记住某会话用户选定的模型(下一轮要用哪个);/model 切换时写入,刷新/重启都不丢。

    如果模型确实发生变化,同时清掉 sdk_session_id —— 不同模型/供应商的 CLI
    transcript 不兼容,resume 旧 session 会延续旧模型的调用身份,导致"切换了
    模型仍然报旧模型的限额/429"。清掉后下一轮用历史 blob 起新会话,真正切到
    新模型。
    """
    c = _conn()
    row = c.execute(
        "SELECT chosen_model FROM session_meta WHERE session_key=? AND chosen_model IS NOT NULL",
        (session_key,),
    ).fetchone()
    if row and row[0] and row[0] != model:
        # 模型切换:旧 SDK 会话不能再 resume
        c.execute(
            "UPDATE session_meta SET sdk_session_id=NULL WHERE session_key=?",
            (session_key,),
        )
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


def backfill_chosen_models() -> None:
    """把"已经聊过、但从没显式 /model 选过"的老会话,按它们最后一轮实际用的模型
    (model 列)冻结成 chosen_model —— 在全局默认模型即将改变的那一刻调用,防止这些
    老会话在下一轮读到新默认、被悄悄带跑(缓存/上下文错位)。"""
    c = _conn()
    c.execute(
        "UPDATE session_meta SET chosen_model = model "
        "WHERE (chosen_model IS NULL OR chosen_model = '') "
        "AND model IS NOT NULL AND model != ''"
    )
    c.commit()


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


def delete_session(session_key: str) -> None:
    """彻底删掉一个会话(所有轮次 + 元数据 + 图片文件)。"""
    c = _conn()
    purge_session_images(c, session_key)  # 先清图片文件,再删轮次
    c.execute("DELETE FROM turns WHERE session_key=?", (session_key,))
    c.execute("DELETE FROM session_meta WHERE session_key=?", (session_key,))
    c.commit()


def set_conv_archived(session_key: str, archived: bool) -> None:
    """设置会话归档状态;若 session_meta 行不存在则先插入。"""
    c = _conn()
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id, archived) VALUES(?,0,?) "
        "ON CONFLICT(session_key) DO UPDATE SET archived=excluded.archived",
        (session_key, 1 if archived else 0),
    )
    c.commit()
