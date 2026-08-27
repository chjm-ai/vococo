"""会话持久化 —— SQLite。

- 按 session_key 隔离(CLI 用 "cli",Web 用 "web:<conv_id>")
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
from .audio import (  # noqa: F401 (re-export)
    audio_path,
    purge_session_audio,
    save_turn_audio,
)
from .files import save_turn_files  # noqa: F401 (re-export)
from .images import (  # noqa: F401 (re-export)
    AI_IMAGE_PREFIX,
    append_turn_image,
    image_path,
    purge_session_images,
    save_turn_images,
    thumb_path,
)
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


def _user_image_paths(raw: str | None) -> list[str]:
    """把用户上传图的文件名还原成模型可读取的本机路径。"""
    try:
        names = json.loads(raw) if raw else []
    except (json.JSONDecodeError, ValueError):
        return []
    return [
        str(path) for name in names
        if isinstance(name, str)
        and not name.startswith(AI_IMAGE_PREFIX)
        and (path := image_path(name)) is not None
    ]


def load_recent(session_key: str, limit: int = 40) -> list[Turn]:
    """只取当前会话(水位线之后)的最近 N 轮;跳过进行中(assistant_text='')的 turn。"""
    from ..core.agent import Turn  # 延迟 import:打破模块级循环依赖

    c = _conn()
    wm = _watermark(c, session_key)
    rows = c.execute(
        "SELECT user_text, assistant_text, images FROM turns "
        "WHERE session_key=? AND id>? AND assistant_text!='' ORDER BY id DESC LIMIT ?",
        (session_key, wm, limit),
    ).fetchall()
    return [
        Turn(user=u, assistant=a, image_paths=_user_image_paths(images))
        for u, a, images in reversed(rows)
    ]


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
        "SELECT id, ts, user_text, assistant_text, events, draft_text, images, audios, files FROM turns "
        "WHERE session_key=? AND id>? ORDER BY id DESC LIMIT ?",
        (session_key, wm, limit),
    ).fetchall()
    out: list[dict] = []
    for tid, ts, u, a, ev, draft, imgs, auds, files in reversed(rows):
        try:
            events = json.loads(ev) if ev else []
        except (json.JSONDecodeError, ValueError):
            events = []
        if events and not full_events:
            events = _strip_events(events)
        entry: dict = {"id": tid, "ts": ts, "user": u, "assistant": a, "pending": a == "", "events": events}
        if a == "" and draft:
            entry["draft"] = draft
        # 图片:库里存文件名,给前端换成取图 URL(前端 <img src> 直接用)
        try:
            names = json.loads(imgs) if imgs else []
        except (json.JSONDecodeError, ValueError):
            names = []
        # AI 主动发的图("ai_"前缀)贴回 AI 气泡,用户上传图贴回用户气泡——
        # 混在一条 images 里的话,前端要么两边都不认(见 buildTurnBlock 只读
        # user 气泡的 images),要么误把 AI 发的图也画到用户自己发的那条气泡上。
        user_names = [n for n in names if not n.startswith(AI_IMAGE_PREFIX)]
        ai_names = [n for n in names if n.startswith(AI_IMAGE_PREFIX)]
        if user_names:
            entry["images"] = ["/image?name=" + n for n in user_names]
        if ai_names:
            entry["ai_images"] = ["/image?name=" + n for n in ai_names]
        # 音频:库里存 [{file,filename,text}],给前端换成取音频 URL + 原始文件名 + 转写文字
        try:
            audio_entries = json.loads(auds) if auds else []
        except (json.JSONDecodeError, ValueError):
            audio_entries = []
        if audio_entries:
            entry["audios"] = [
                {
                    "url": "/audio?name=" + e.get("file", ""),
                    "filename": e.get("filename", ""),
                    "text": e.get("text", ""),
                }
                for e in audio_entries
                if e.get("file")
            ]
        try:
            file_entries = json.loads(files) if files else []
        except (json.JSONDecodeError, ValueError):
            file_entries = []
        if file_entries:
            entry["files"] = [
                {
                    "name": str(e.get("name") or e.get("filename") or ""),
                    "media_type": str(e.get("media_type") or ""),
                }
                for e in file_entries
                if isinstance(e, dict) and (e.get("name") or e.get("filename"))
            ]
        out.append(entry)
    return out


def load_document_events(session_key: str, limit: int = 10_000) -> list[dict]:
    """只取含文档创建/编辑工具的事件，避免文档列表扫描整段会话正文与无关工具输出。"""
    c = _conn()
    wm = _watermark(c, session_key)
    tool_markers = ("Write", "Edit", "MultiEdit", "NotebookEdit")
    clauses = " OR ".join("events LIKE ?" for _ in tool_markers)
    rows = c.execute(
        "SELECT ts, events FROM turns "
        f"WHERE session_key=? AND id>? AND events IS NOT NULL AND ({clauses}) "
        "ORDER BY id DESC LIMIT ?",
        (session_key, wm, *(f'%\"name\": \"{name}\"%' for name in tool_markers), limit),
    ).fetchall()
    out: list[dict] = []
    for ts, ev in reversed(rows):
        try:
            events = json.loads(ev)
        except (json.JSONDecodeError, ValueError):
            continue
        out.append({"ts": ts, "events": events})
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
    total_tokens 累加(=当前窗口的消耗)。ctx_tokens=0 代表本轮真实查询失败
    (agent.py 不再用不可靠的累计值兜底),此时保留数据库里的旧值,不拿 0 覆盖。"""
    c = _conn()
    c.execute(
        "INSERT INTO session_meta"
        "(session_key, watermark_id, ctx_tokens, total_tokens, ctx_window, "
        " last_in, last_cache, last_out, model) "
        "VALUES (?,0,?,?,?,?,?,?,?) "
        "ON CONFLICT(session_key) DO UPDATE SET "
        "ctx_tokens=CASE WHEN excluded.ctx_tokens>0 THEN excluded.ctx_tokens "
        "ELSE session_meta.ctx_tokens END, "
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


def finish_turn(
    turn_id: int,
    assistant_text: str,
    events: list | None = None,
    *,
    session_key: str | None = None,
) -> bool:
    """用 AI 回复填完 start_turn 占的坑;events=该轮过程时间线(可选)。

    ``session_key`` 是流式调用方必须传入的归属校验。删除会话后 SQLite 可能复用
    turn id;旧请求不能只凭 id 把回复写进后来新建的会话。保留空值仅兼容历史测试
    和一次性脚本路径，运行中的 Agent 均要传入自己的 session_key。

    顺手把 ts 刷到回复完成的时刻——侧边栏排序按 ts 取最新,这样"AI 回复完"
    和"用户发消息"两个动作都会把会话顶到最前面,而不是停在用户发消息那一刻。
    """
    c = _conn()
    ev_json = None
    if events:
        try:
            ev_json = json.dumps(events, ensure_ascii=False)
        except (TypeError, ValueError):
            ev_json = None  # 时间线序列化失败不影响正文落库
    sql = "UPDATE turns SET assistant_text=?, events=?, draft_text='', ts=? WHERE id=?"
    params: tuple = (assistant_text, ev_json, time.time(), turn_id)
    if session_key is not None:
        sql += " AND session_key=?"
        params += (session_key,)
    cur = c.execute(sql, params)
    c.commit()
    return cur.rowcount == 1


def cancel_turn(turn_id: int, *, session_key: str | None = None) -> bool:
    """AI 回复出错/取消时删掉进行中的 turn,避免孤儿 pending 行残留。"""
    c = _conn()
    sql = "DELETE FROM turns WHERE id=? AND assistant_text=''"
    params: tuple = (turn_id,)
    if session_key is not None:
        sql += " AND session_key=?"
        params += (session_key,)
    cur = c.execute(sql, params)
    c.commit()
    return cur.rowcount == 1


def delete_last_turn(session_key: str, turn_id: int) -> str | None:
    """删掉某会话「最后一轮」,返回其 user_text 供调用方重新入队(= 前端"重新生成")。

    只允许删最新一轮(核对 id 与 DESC LIMIT 1 查到的一致)且该轮已完成
    (assistant_text 非空)——防止前端拿着过期/进行中的 turn_id 误删,打乱
    后续轮次的上下文连续性。不满足条件返回 None,调用方据此拒绝这次请求。

    注:SDK 会话走 resume 续聊,被删掉这轮的问答在模型侧 resume 的 transcript
    里仍"读得到"——重新生成能让用户不再看到旧回复,但不能让模型真正忘记它答过
    什么,这是 resume 模式续聊的固有限制,不在这里解决。
    """
    c = _conn()
    row = c.execute(
        "SELECT id, user_text, assistant_text FROM turns WHERE session_key=? ORDER BY id DESC LIMIT 1",
        (session_key,),
    ).fetchone()
    if not row or row[0] != turn_id or row[2] == "":
        return None
    c.execute("DELETE FROM turns WHERE id=?", (turn_id,))
    c.commit()
    return row[1]


def recover_interrupted_turns() -> list[dict]:
    """将进程重启遗留的进行中回合收尾，避免前端永久显示加载中。

    events 里写中断标记 [{"type":"interrupted"}]（/history 会透传给前端）:
    前端据此识别「这条是被重启打断的回复」，自动/一键继续生成（复用
    /turn/regenerate 把同一句话重发一遍），不用用户手动重打。

    返回受影响的 [{"turn_id", "session_key", "user_text"}, ...]，供重启流程
    对"非发起会话"做自动重发判断（见 gateway/run.py _auto_resend_interrupted）。
    """
    c = _conn()
    rows = c.execute(
        "SELECT id, session_key, user_text FROM turns WHERE assistant_text=''"
    ).fetchall()
    if not rows:
        return []
    message = "⚠️ 服务重启导致本轮回复中断，请重新发送。"
    marker = json.dumps([{"type": "interrupted"}], ensure_ascii=False)
    c.execute(
        "UPDATE turns SET assistant_text=?, events=?, draft_text='', ts=? "
        "WHERE assistant_text=''",
        (message, marker, time.time()),
    )
    c.commit()
    return [
        {"turn_id": row[0], "session_key": row[1], "user_text": row[2]}
        for row in rows
    ]


def flush_draft(turn_id: int, text: str, *, session_key: str | None = None) -> bool:
    """流式进行中:把当前已输出的正文节流写进 draft_text 列,供刷新后兜底。"""
    c = _conn()
    sql = "UPDATE turns SET draft_text=? WHERE id=?"
    params: tuple = (text, turn_id)
    if session_key is not None:
        sql += " AND session_key=?"
        params += (session_key,)
    cur = c.execute(sql, params)
    c.commit()
    return cur.rowcount == 1


def set_external_mcp_names(session_key: str, names: set[str]) -> None:
    """保存用户手动开启的外部 MCP；状态只属于当前会话。"""
    c = _conn()
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id, external_mcp_names) VALUES (?,0,?) "
        "ON CONFLICT(session_key) DO UPDATE SET external_mcp_names=excluded.external_mcp_names",
        (session_key, json.dumps(sorted(names), ensure_ascii=False)),
    )
    c.commit()


def get_external_mcp_names(session_key: str) -> set[str]:
    row = _conn().execute(
        "SELECT external_mcp_names FROM session_meta WHERE session_key=?", (session_key,)
    ).fetchone()
    try:
        names = json.loads(row[0]) if row and row[0] else []
    except (TypeError, json.JSONDecodeError):
        names = []
    return {name for name in names if isinstance(name, str)}


def set_auto_external_mcp_names(session_key: str, names: set[str]) -> None:
    """记录自动命中的 MCP，供短时「继续」请求复用，不作为全局开关。"""
    c = _conn()
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id, auto_external_mcp_names, auto_external_mcp_at) "
        "VALUES (?,0,?,?) ON CONFLICT(session_key) DO UPDATE SET "
        "auto_external_mcp_names=excluded.auto_external_mcp_names, "
        "auto_external_mcp_at=excluded.auto_external_mcp_at",
        (session_key, json.dumps(sorted(names), ensure_ascii=False), time.time()),
    )
    c.commit()


def get_recent_auto_external_mcp_names(session_key: str, max_age: float = 1800) -> set[str]:
    row = _conn().execute(
        "SELECT auto_external_mcp_names, auto_external_mcp_at FROM session_meta WHERE session_key=?",
        (session_key,),
    ).fetchone()
    if not row or not row[0] or not row[1] or time.time() - float(row[1]) > max_age:
        return set()
    try:
        names = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        names = []
    return {name for name in names if isinstance(name, str)}


def clear(session_key: str) -> None:
    c = _conn()
    purge_session_images(c, session_key)  # 先清图片文件,再删轮次
    purge_session_audio(c, session_key)  # 音频同理
    c.execute("DELETE FROM turns WHERE session_key=?", (session_key,))
    c.execute(
        "UPDATE session_meta SET ctx_tokens=0, total_tokens=0, "
        "last_in=0, last_cache=0, last_out=0, sdk_session_id=NULL, "
        "external_mcp_names=NULL, auto_external_mcp_names=NULL, auto_external_mcp_at=NULL "
        "WHERE session_key=?",
        (session_key,),
    )
    c.commit()


# === Web 会话管理(侧边栏)===
# Web 端自带多会话:每个对话一个 session_key(如 web:1720000000abc)。
# 下面这几个 helper 让前端能列出/命名/删除会话,TG/CLI 不受影响。


def duplicate_session(src_key: str, dst_key: str, title: str) -> None:
    """复制会话:全部轮次(含 images/audios/files/events)搬到新 key,标题用新标题。

    ts 统一刷新为当前时间 —— 侧边栏按 MAX(t.ts) 倒序,新副本要顶到列表最前
    (保持原 ts 会沉到原会话旁边,用户复制完找不到)。
    图片/音频本体已落盘在共享目录,turns 里只存文件名列表,无需复制文件。
    不搬 watermark/token 计量(新会话从零开始,上下文窗口独立)、不搬置顶/
    归档/项目绑定 —— 副本是干净的新会话,只继承对话内容。
    """
    c = _conn()
    c.execute(
        "INSERT INTO turns(session_key, ts, user_text, assistant_text, "
        "draft_text, events, images, audios, files) "
        "SELECT ?, ?, user_text, assistant_text, draft_text, events, images, audios, files "
        "FROM turns WHERE session_key=?",
        (dst_key, time.time(), src_key),
    )
    c.execute(
        "INSERT INTO session_meta(session_key, watermark_id, title) VALUES (?,0,?)",
        (dst_key, title),
    )
    c.commit()


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


def list_sessions(prefix: str, *, archived: bool | None = None) -> list[dict]:
    """列出 session_key 以 prefix 开头的会话摘要,最近活跃的排前面。

    返回 [{key, title, turns, last_ts}, ...]。turns 只算当前上下文窗口
    (水位线之后),= 该会话现在带着多少轮记忆。

    会话键来源取「有 turn」∪「有标题」:新建会话在 AI 回复完之前只写了
    session_meta 标题、还没有 turn 落库,若只按 turns 表列会导致它刚发完
    第一条就从侧边栏消失(要等回复完才出现)。带标题即视为一个会话。

    archived=None(默认)不过滤,兼容旧调用;传 True/False 时在 SQL 层
    (HAVING,因为 archived 是聚合后的字段)只取归档/未归档会话——过滤越境
    (跨境隧道带宽有限)传输,不要整包拉回来再在调用方/前端筛。
    """
    c = _conn()
    having = ""
    params: list = [prefix + "%", prefix + "%"]
    if archived is not None:
        having = " HAVING COALESCE(MAX(m.archived),0) = ?"
        params.append(1 if archived else 0)
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
        "  COALESCE(MAX(m.last_error),0), MAX(m.title) "
        "FROM keys k "
        "LEFT JOIN session_meta m ON k.session_key = m.session_key "
        "LEFT JOIN turns t ON t.session_key = k.session_key "
        "  AND t.id > COALESCE(m.watermark_id, 0) "
        "GROUP BY k.session_key" + having + " ORDER BY 3 DESC",
        params,
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        key, turns, last_ts = row[0], row[1], row[2]
        item = {"key": key, "title": row[15] or "新对话", "turns": turns, "last_ts": last_ts}
        item.update(_usage_fields(row[3:11]))
        item["archived"] = bool(row[11])
        item["pending_review"] = bool(row[12])
        item["pinned"] = bool(row[13])
        item["last_error"] = bool(row[14])
        out.append(item)
    return out


def find_session(session_key: str) -> dict | None:
    """按完整 key 精确确认一个侧边栏会话，只取续接/状态查询所需字段。

    不能拿 list_sessions() 后再遍历：后者会为同一前缀下的每个会话聚合轮次、排序并
    组装 token 摘要。这里以 session_meta 的主键和 turns 的联合索引做两次精确探测，
    保持「有轮次或有标题才算会话」与 list_sessions() 相同的可见性语义。
    """
    row = _conn().execute(
        "SELECT EXISTS(SELECT 1 FROM turns WHERE session_key=?), "
        "m.title, COALESCE(m.last_error, 0) "
        "FROM (SELECT ? AS session_key) AS k "
        "LEFT JOIN session_meta AS m ON m.session_key=k.session_key",
        (session_key, session_key),
    ).fetchone()
    if not row or (not row[0] and not row[1]):
        return None
    return {
        "key": session_key,
        "title": row[1] or "新对话",
        "last_error": bool(row[2]),
    }


def _usage_fields(vals) -> dict:
    """把 (ctx, total, window, last_in, last_cache, last_out, model, chosen_model) 组装成统一字段。"""
    ctx, total, window, l_in, l_cache, l_out, model, chosen = vals
    # 早期 GPT-5.6 会话按官方 API 的 1.05M 写入，但本机 Codex 实际目录是
    # 272k × 95%=258,400。摘要是 Web 进度条唯一数据源，在此归一可立即修正旧
    # 会话显示，不必等下一轮成功请求覆盖数据库残值。
    effective_model = chosen or model or ""
    if effective_model.lower().startswith("gpt-5.6"):
        from ..core.agent import context_window

        window = context_window(effective_model)
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
    voice_list_sessions(origin="web") 靠它筛出"卡住等续聊"的网页会话,不用去猜最后一条
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
    purge_session_audio(c, session_key)  # 音频同理
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
