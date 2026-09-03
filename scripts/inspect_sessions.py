#!/usr/bin/env python3
"""查会话 —— 排障用的只读命令行工具,直接读 data/state.db,不依赖 vococo 包。

背景:会话数据分散在三处 —— SQLite(turns/session_meta,本工具的数据源)、
git worktree 物理目录(session_meta.worktree_path)、claude CLI 自己的
transcript jsonl(~/.claude/projects/<cwd编码>/<sdk_session_id>.jsonl)。
以前排查靠裸写 SQL/翻目录,这个脚本把常用查询收拢成子命令。

用法:
  uv run python scripts/inspect_sessions.py list
  uv run python scripts/inspect_sessions.py list --platform web --project intertrade
  uv run python scripts/inspect_sessions.py show web:1720000000abc
  uv run python scripts/inspect_sessions.py show web:1720000000abc --all-history --full
  uv run python scripts/inspect_sessions.py search "名古屋 出差"
  uv run python scripts/inspect_sessions.py projects
  uv run python scripts/inspect_sessions.py sdk web:1720000000abc

注意:在某个项目会话的 worktree 里跑本脚本时,会自动识别并改用主仓库下的
data/state.db(worktree 自己那份是全新空库)。自动识别失败或要查别的库,
才需要 --db 显式指定,--db 写在子命令前后都可以。

show/sdk 的 key 支持模糊匹配:精确 key 找不到时会按子串搜索 session_key,
唯一匹配自动采用,多个匹配会列出候选(常见于漏打 web:/tg: 平台前缀)。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_DB = _SCRIPT_DIR.parent / "data" / "state.db"


def _resolve_db_path() -> Path:
    """默认库不存在时,判断是不是在 worktree 里跑,自动定位主仓库真实库。

    worktree 路径形如 <主仓库>/data/worktrees/<hash>/<name>/scripts/...,
    去掉 data/worktrees/<hash>/<name> 这一段就是主仓库根。
    """
    if _DEFAULT_DB.is_file():
        return _DEFAULT_DB
    parts = _SCRIPT_DIR.parts
    if "worktrees" in parts:
        idx = parts.index("worktrees")
        if idx >= 1 and parts[idx - 1] == "data":
            candidate = Path(*parts[: idx - 1]) / "data" / "state.db"
            if candidate.is_file():
                return candidate
    return _DEFAULT_DB


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        sys.exit(
            f"❌ 找不到数据库:{db_path}\n"
            "   自动定位主仓库失败,用 --db 显式指定真实路径,\n"
            "   例如:--db ~/Repos/vococo/data/state.db"
        )
    # 只读 + WAL 兼容:不建表、不迁移,和正在跑的 serve 进程并发读也安全
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _truncate(text: str, limit: int) -> str:
    text = (text or "").replace("\n", " ⏎ ")
    return text if len(text) <= limit else text[:limit] + "…"


def _platform_of(key: str) -> str:
    if key == "main":
        return "main"
    if key.startswith("web:"):
        return "web"
    if key.startswith("tg:"):
        return "tg"
    return "cli"


# === list ===


def cmd_list(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    rows = conn.execute(
        "WITH keys AS ("
        "  SELECT DISTINCT session_key FROM turns"
        "  UNION SELECT session_key FROM session_meta"
        ") "
        "SELECT k.session_key, m.title, m.model, m.worktree_path, m.sdk_session_id, "
        "  COALESCE(m.archived,0) AS archived, "
        "  COUNT(t.id) AS turn_count, MAX(t.ts) AS last_ts "
        "FROM keys k "
        "LEFT JOIN session_meta m ON m.session_key = k.session_key "
        "LEFT JOIN turns t ON t.session_key = k.session_key "
        "GROUP BY k.session_key "
        "ORDER BY COALESCE(last_ts, 0) DESC"
    ).fetchall()

    out = []
    for r in rows:
        if args.platform != "all" and _platform_of(r["session_key"]) != args.platform:
            continue
        if not args.archived and r["archived"]:
            continue
        if args.project:
            hay = (r["session_key"] or "") + " " + (r["worktree_path"] or "")
            if args.project.lower() not in hay.lower():
                continue
        out.append(r)
        if len(out) >= args.limit:
            break

    if not out:
        print("(没有匹配的会话)")
        return

    print(f"{'KEY':<28} {'TITLE':<20} {'轮数':>4} {'最后活跃':<16} {'MODEL':<14} WORKTREE")
    for r in out:
        wt = "有" if r["worktree_path"] else "-"
        arc = " [已归档]" if r["archived"] else ""
        print(
            f"{r['session_key']:<28} {_truncate(r['title'] or '(无标题)', 20):<20} "
            f"{r['turn_count']:>4} {_fmt_ts(r['last_ts']):<16} "
            f"{_truncate(r['model'] or '-', 14):<14} {wt}{arc}"
        )


# === show ===


def _resolve_key(conn: sqlite3.Connection, key: str) -> tuple[str, list[str]]:
    """精确 key 找不到时按子串模糊匹配 session_key。

    返回 (最终使用的 key, 候选列表)。候选列表非空时表示精确匹配失败且
    有 0 个或多个模糊结果,调用方应打印候选并放弃,不要继续查询。
    """
    hit = conn.execute(
        "SELECT 1 FROM session_meta WHERE session_key=? "
        "UNION SELECT 1 FROM turns WHERE session_key=? LIMIT 1",
        (key, key),
    ).fetchone()
    if hit:
        return key, []
    like = f"%{key}%"
    rows = conn.execute(
        "SELECT DISTINCT session_key FROM ("
        "  SELECT session_key FROM session_meta WHERE session_key LIKE ?"
        "  UNION SELECT session_key FROM turns WHERE session_key LIKE ?"
        ") ORDER BY session_key",
        (like, like),
    ).fetchall()
    candidates = [r["session_key"] for r in rows]
    if len(candidates) == 1:
        return candidates[0], []
    return key, candidates


def cmd_show(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    key, candidates = _resolve_key(conn, args.key)
    if candidates:
        print(f"❌ 精确匹配不到 {args.key!r},模糊搜索有 {len(candidates)} 个候选,换完整 key 重试:")
        for c in candidates[:30]:
            print(f"  {c}")
        return
    if key != args.key:
        print(f"(未精确匹配 {args.key!r},自动采用唯一模糊结果: {key})\n")
    args.key = key

    meta = conn.execute(
        "SELECT * FROM session_meta WHERE session_key=?", (args.key,)
    ).fetchone()
    if meta is None:
        print(f"(session_meta 无该会话记录,可能只有裸 turns;仍尝试列出轮次)")
    else:
        print(f"=== {args.key} ===")
        print(f"标题: {meta['title'] or '(无)'}  归档: {bool(meta['archived'])}")
        print(f"模型: {meta['model'] or '-'}  选定模型: {meta['chosen_model'] or '-'}")
        print(
            f"tokens: ctx={meta['ctx_tokens']} total={meta['total_tokens']} "
            f"window={meta['ctx_window']}"
        )
        print(f"worktree: {meta['worktree_path'] or '-'}")
        print(f"sdk_session_id: {meta['sdk_session_id'] or '-'}")
        print()

    watermark = meta["watermark_id"] if meta else 0
    rows = conn.execute(
        "SELECT id, ts, user_text, assistant_text, events FROM turns "
        "WHERE session_key=? ORDER BY id ASC",
        (args.key,),
    ).fetchall()
    if not rows:
        print("(没有任何 turn)")
        return

    if not args.all_history:
        rows = [r for r in rows if r["id"] > watermark]
        hidden = sum(1 for _ in conn.execute(
            "SELECT 1 FROM turns WHERE session_key=? AND id<=?", (args.key, watermark)
        ))
        if hidden:
            print(f"(水位线之前还有 {hidden} 轮历史未显示,--all-history 查看全部)\n")

    rows = rows[-args.n:]
    limit = 10_000 if args.full else 400
    for r in rows:
        tag = "" if r["id"] > watermark else " [历史/不在当前上下文]"
        print(f"--- turn #{r['id']}  {_fmt_ts(r['ts'])}{tag} ---")
        print(f"👤 {_truncate(r['user_text'], limit)}")
        if r["assistant_text"]:
            print(f"🤖 {_truncate(r['assistant_text'], limit)}")
        else:
            print("🤖 (进行中/未完成)")
        if args.events and r["events"]:
            try:
                events = json.loads(r["events"])
            except (json.JSONDecodeError, ValueError):
                events = None
            if events:
                print(f"   events: {json.dumps(events, ensure_ascii=False)[:limit]}")
        print()


# === search ===


def cmd_search(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    terms = [t for t in args.text.split() if t] or [args.text]
    score = " + ".join(
        "(CASE WHEN user_text LIKE ? OR assistant_text LIKE ? THEN 1 ELSE 0 END)"
        for _ in terms
    )
    where = " OR ".join("user_text LIKE ? OR assistant_text LIKE ?" for _ in terms)
    likes = [f"%{t}%" for t in terms]
    params = [p for like in likes for p in (like, like)]
    rows = conn.execute(
        f"SELECT session_key, ts, user_text, assistant_text FROM turns "
        f"WHERE {where} ORDER BY ({score}) DESC, id DESC LIMIT ?",
        (*params, *params, args.limit),
    ).fetchall()
    if not rows:
        print("(无匹配)")
        return
    for r in rows:
        print(f"[{r['session_key']}] {_fmt_ts(r['ts'])}")
        print(f"  👤 {_truncate(r['user_text'], 160)}")
        print(f"  🤖 {_truncate(r['assistant_text'], 160)}")
        print()


# === projects ===


def cmd_projects(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    rows = conn.execute(
        "SELECT hash, path, last_used, hidden FROM projects ORDER BY sort_order ASC, last_used DESC"
    ).fetchall()
    if not rows:
        print("(没有项目)")
        return
    print(f"{'HASH':<12} {'最后使用':<16} {'隐藏':<4} PATH")
    for r in rows:
        print(f"{r['hash']:<12} {_fmt_ts(r['last_used']):<16} {'是' if r['hidden'] else '-':<4} {r['path']}")


# === sdk ===


def _resolve_cwd(conn: sqlite3.Connection, key: str, worktree_path: str | None, db_path: Path) -> tuple[str, str]:
    """按 config.project_cwd_for 同款规则推断该会话实际 cwd:worktree > 项目根 > 进程默认目录。

    返回 (cwd, 来源说明)。
    """
    if worktree_path:
        return worktree_path, "会话独占 worktree"
    # 项目会话 key 形如 web:p<hash>:<conv>,没独立 worktree 时用项目根
    parts = key.split(":")
    if len(parts) >= 3 and parts[0] == "web" and len(parts[1]) > 1 and parts[1][0] == "p":
        row = conn.execute(
            "SELECT path FROM projects WHERE hash=?", (parts[1][1:],)
        ).fetchone()
        if row:
            return row["path"], "项目根(该会话未建独立 worktree)"
    # 非项目会话(main/tg/无项目 web):SDK 用进程启动时的默认 cwd,
    # 即 state.db 所在仓库的根目录(data/ 的上一级)
    return str(db_path.resolve().parent.parent), "进程默认目录(推测,非项目会话)"


def cmd_sdk(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    key, candidates = _resolve_key(conn, args.key)
    if candidates:
        print(f"❌ 精确匹配不到 {args.key!r},模糊搜索有 {len(candidates)} 个候选,换完整 key 重试:")
        for c in candidates[:30]:
            print(f"  {c}")
        return
    if key != args.key:
        print(f"(未精确匹配 {args.key!r},自动采用唯一模糊结果: {key})\n")
    args.key = key

    meta = conn.execute(
        "SELECT worktree_path, sdk_session_id FROM session_meta WHERE session_key=?",
        (args.key,),
    ).fetchone()
    if meta is None or not meta["sdk_session_id"]:
        print("(该会话没有 sdk_session_id —— 要么还没跑过一轮,要么走的是拼历史文本的老路径)")
        return
    sid = meta["sdk_session_id"]
    cwd, source = _resolve_cwd(conn, args.key, meta["worktree_path"], args.db)
    encoded = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    transcript = Path.home() / ".claude" / "projects" / encoded / f"{sid}.jsonl"
    print(f"sdk_session_id: {sid}")
    print(f"推断 cwd: {cwd}  ({source})")
    print(f"transcript 路径: {transcript}")
    if transcript.is_file():
        n = sum(1 for _ in transcript.open())
        print(f"存在,共 {n} 行。可用: tail -f '{transcript}' | jq .")
    else:
        print("文件不存在(路径推断可能有误,建议 ls ~/.claude/projects/ 手动确认目录名)")


def main() -> None:
    # --db 同时挂在主解析器和每个子命令上,这样不管写在子命令前面还是后面都认,
    # 例如 `--db X show key` 和 `show key --db X` 都行。
    # 子命令那份 default 必须用 SUPPRESS,否则 argparse 子解析器初始化默认值时
    # 会用它的 None 覆盖掉主解析器已经拿到的值(实测过的坑,不是理论风险)。
    db_parent_top = argparse.ArgumentParser(add_help=False)
    db_parent_top.add_argument(
        "--db", type=Path, default=None,
        help="state.db 路径(默认自动定位,worktree 里也会找主仓库的库)",
    )
    db_parent_sub = argparse.ArgumentParser(add_help=False)
    db_parent_sub.add_argument("--db", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[db_parent_top],
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="列出会话", parents=[db_parent_sub])
    sp.add_argument("--platform", choices=["all", "web", "tg", "main", "cli"], default="all")
    sp.add_argument("--project", help="按 session_key/worktree_path 子串过滤")
    sp.add_argument("--archived", action="store_true", help="包含已归档会话")
    sp.add_argument("--limit", type=int, default=40)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="查看某会话的完整对话", parents=[db_parent_sub])
    sp.add_argument("key", help="session_key,如 web:xxx / tg:-100xxx / main(支持子串模糊匹配)")
    sp.add_argument("-n", type=int, default=20, help="显示最近 N 轮(默认20)")
    sp.add_argument("--full", action="store_true", help="不截断正文")
    sp.add_argument("--all-history", action="store_true", help="含水位线之前(/new 之前)的历史")
    sp.add_argument("--events", action="store_true", help="附带打印每轮的工具调用时间线")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("search", help="跨会话关键词检索(空格分词,OR 匹配)", parents=[db_parent_sub])
    sp.add_argument("text")
    sp.add_argument("-n", "--limit", type=int, default=20)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("projects", help="列出已注册项目(hash -> 路径)", parents=[db_parent_sub])
    sp.set_defaults(func=cmd_projects)

    sp = sub.add_parser("sdk", help="解析某会话对应的 SDK transcript jsonl 路径", parents=[db_parent_sub])
    sp.add_argument("key", help="session_key(支持子串模糊匹配)")
    sp.set_defaults(func=cmd_sdk)

    args = p.parse_args()
    db_path = args.db if args.db is not None else _resolve_db_path()
    args.db = db_path
    conn = _connect(db_path)
    try:
        args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
