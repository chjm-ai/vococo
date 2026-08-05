"""memory 包内部共享的 SQLite 连接 + schema。

不对外(包外)暴露 —— 只给 session_store.py / images.py / projects.py / worktrees.py /
prefs.py / search.py 这几个同包兄弟模块用,它们共享同一个 state.db 文件/同一条连接
(而不是各开各的连接)。2026-07-23 从 session_store.py 拆出:原先 45 个函数、6 个不
相关关注点全挤在一个文件里,只共享这一份 _conn()/_DB——现在关注点各自成文件,
这份连接单例本身也单独成一个 seam,兄弟模块经 conn() 取用,不用互相挖对方的私有属性。

2026-08 租户化:单例升级为按租户连接池(_DBS,键=tenant_id)。personal 模式
tenancy.context.current() 恒为 "local",全局仍只有一条连接、同一个 state.db,
行为与改造前完全一致;server 模式每个租户一个独立 state.db(物理隔离,
见 docs/design/server-edition-tech-plan.md §3)。
"""
from __future__ import annotations

import sqlite3

from ..tenancy import context as tenant_context
from ..tenancy import paths as tenant_paths

_DBS: dict[str, sqlite3.Connection] = {}

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
  hidden INTEGER NOT NULL DEFAULT 0,
  sort_order REAL,
  pinned INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_prefs(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def _connect(db_path) -> sqlite3.Connection:
    """建一条连接并应用 schema + 幂等迁移(每租户首次连接时各跑一遍)。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    # 迁移:session_meta 增列(老库平滑升级)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(session_meta)")}
    if "title" not in cols:  # Web 会话列表用
        conn.execute("ALTER TABLE session_meta ADD COLUMN title TEXT")
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
            conn.execute(
                f"ALTER TABLE session_meta ADD COLUMN {col} INTEGER DEFAULT 0"
            )
    if "model" not in cols:
        conn.execute("ALTER TABLE session_meta ADD COLUMN model TEXT")
    # chosen_model=用户为该会话选定、下一轮要用的模型(与 model「实际跑过的带日期 id」分开)
    if "chosen_model" not in cols:
        conn.execute("ALTER TABLE session_meta ADD COLUMN chosen_model TEXT")
    # sdk_session_id=SDK(claude CLI)自己维护的会话 id。存下它,下一轮用 resume=<id>
    # 让 SDK 从它自己的 transcript 重放【真·多轮历史】,而不是我们把历史拼成一坨文本喂进去
    # (后者会让模型误判自己"记不住/被压缩")。NULL=还没有,起新会话。
    if "sdk_session_id" not in cols:
        conn.execute("ALTER TABLE session_meta ADD COLUMN sdk_session_id TEXT")
    if "archived" not in cols:
        conn.execute("ALTER TABLE session_meta ADD COLUMN archived INTEGER DEFAULT 0")
    # worktree_path=该会话独占的 git worktree 目录(每会话一分支的物理隔离);
    # NULL=没有独立 worktree,回退项目根/进程默认目录。
    if "worktree_path" not in cols:
        conn.execute("ALTER TABLE session_meta ADD COLUMN worktree_path TEXT")
    # pending_review=1 表示该会话已有新完成内容但用户还没打开看过;打开会话后清零。
    if "pending_review" not in cols:
        conn.execute("ALTER TABLE session_meta ADD COLUMN pending_review INTEGER DEFAULT 0")
    # pinned: 侧边栏「置顶」分组标记(0=不置顶,1=置顶);老库默认 0。
    if "pinned" not in cols:
        conn.execute("ALTER TABLE session_meta ADD COLUMN pinned INTEGER DEFAULT 0")
    # last_error=1 表示该会话最近一轮以报错收尾(限流/超时/模型层错误等)。
    # 语音端 voice_list_sessions(origin=web) 靠它筛出"卡住等续聊"的网页会话,不用去猜
    # 最后一条回复文本是不是错误提示。
    if "last_error" not in cols:
        conn.execute("ALTER TABLE session_meta ADD COLUMN last_error INTEGER DEFAULT 0")
    # agent_id:server 模式多 agent 产品(agents_manifest.yaml)里该会话绑定的数字员工;
    # NULL=未绑定(personal 模式恒如此,见 docs/design/server-edition-tech-plan.md §7)。
    if "agent_id" not in cols:
        conn.execute("ALTER TABLE session_meta ADD COLUMN agent_id TEXT")
    # 迁移:turns 增 events 列 —— 该轮的过程时间线(文字段+工具调用)JSON,
    # 供前端刷新后完整重建"工具卡与文字交错"的画面;老行为 NULL(只有纯文本)。
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
    if "events" not in tcols:
        conn.execute("ALTER TABLE turns ADD COLUMN events TEXT")
    # draft_text: 流式进行中把"当前已输出的正文"节流写进该列;
    # 前端刷新后 /history 能拿到部分内容兜底,避免"刷新时进行中的回复一片空白"。
    # 轮末 finish_turn 把 assistant_text 写全并清空 draft(最终版都在 assistant_text)。
    if "draft_text" not in tcols:
        conn.execute("ALTER TABLE turns ADD COLUMN draft_text TEXT NOT NULL DEFAULT ''")
    # images: 该轮用户发的图片文件名 JSON 列表(图片本体落盘在 config.IMAGES_DIR,
    # 这里只存文件名)。刷新后 /history 带出文件名 → 前端用 /image?name= 取回显示。
    if "images" not in tcols:
        conn.execute("ALTER TABLE turns ADD COLUMN images TEXT")
    # audios: 该轮用户发的音频,JSON 列表 [{"file":文件名,"text":转写文字}]。
    # 音频本体落盘在 config.AUDIO_DIR;转写文字随行一起存,不用每次刷新重新转写。
    if "audios" not in tcols:
        conn.execute("ALTER TABLE turns ADD COLUMN audios TEXT")
    # sort_order: 侧边栏项目分组的手动拖拽顺序(升序);老库里全是 NULL,
    # 用 -last_used 补一次初值,让升级后的默认顺序等价于原来的"最近使用在前"。
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
    if "sort_order" not in pcols:
        conn.execute("ALTER TABLE projects ADD COLUMN sort_order REAL")
        conn.execute("UPDATE projects SET sort_order = -last_used WHERE sort_order IS NULL")
    # pinned: 侧边栏「置顶」分组标记,老库默认 0(不置顶)。
    if "pinned" not in pcols:
        conn.execute("ALTER TABLE projects ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
    # 2026-07-29 统一后台任务引擎:voice-task:/cron-task: 两个各自为政的前缀合并
    # 成中性的 task:(见 core/tasks.py 模块头说明)。老库里这两个前缀的历史会话
    # 原地改名,不然改名上线那一刻这些会话就从所有查询里"消失"(代码只认新前缀)
    # ——语音任务/定时任务的完整历史、worktree 绑定(都在 session_meta 这两张表里)
    # 因此保留,用户看不出任何差异。用 REPLACE 把前缀替换成 task:,turns 和
    # session_meta 两张表都要改(query 每次重连都会跑,但改完之后 WHERE 命中 0 行,
    # 代价可忽略,不必再加一个「是否已迁移过」的标记列)。
    for table in ("turns", "session_meta"):
        for old_prefix in ("voice-task:", "cron-task:"):
            conn.execute(
                f"UPDATE {table} SET session_key = 'task:' || substr(session_key, ?) "
                f"WHERE session_key LIKE ?",
                (len(old_prefix) + 1, old_prefix + "%"),
            )
    conn.commit()
    return conn


def conn() -> sqlite3.Connection:
    """当前租户的会话库连接(personal 恒为全局唯一一条;server 按租户各一条)。"""
    tid = tenant_context.current()  # server 模式未注入租户上下文会在这里抛错(fail-closed)
    c = _DBS.get(tid)
    if c is None:
        c = _connect(tenant_paths.data_dir() / "state.db")
        _DBS[tid] = c
    return c


def reset() -> None:
    """测试专用:关闭并清空全部连接(下次 conn() 重新建连,配合 tmp_path 隔离用例)。"""
    for c in _DBS.values():
        c.close()
    _DBS.clear()
