"""运行数据统计:把散落的三处原始数据聚成设置页「数据」面板要的几张表。

数据来源(都是既有的,不新增埋点):
  1. state.db turns        —— 每轮对话的时间戳/工具事件 → 工作强度日历、工具排行
  2. state.db session_meta —— 会话级 token/缓存/模型/异常 → 会话明细
  3. ~/.claude/projects/*/*.jsonl —— SDK 逐条写的 usage 明细 → 按模型的用量与花费

第 3 项是唯一的重活:1700+ 文件 1GB,全量扫一遍约 20 秒,不能每次开面板都扫。
所以这里维护一个 data/stats.db 做增量 ETL:按 (路径, mtime, size) 判断文件有没有
新增内容,只从上次读到的字节偏移往后解析,聚合结果落表。首轮建库 ~20s,之后每次
不到 1 秒。jsonl 是只追加写的,所以"记住偏移继续读"是安全的;万一文件被截断
(size < 偏移),就整份重扫那一个文件。

花费口径:jsonl 里没有金额字段,按内置单价表自己算,得到的是「按官方价折算的等值
成本」。Claude 官方走订阅、实际不额外扣钱,这个数用来体现干了多少活;DeepSeek/
Kimi/中转这些按量计费的才是真实支出。单价会变,可以用 data/model_prices.json 覆盖。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from .. import config

# ── 单价表($/百万 token:输入 / 输出 / 缓存写 / 缓存读)────────────────────
# 官方价随时会变,中转模型(K2.7 Code / gpt-5.6-* 等)更是按同档估的,只求量级对。
# 要改单价不用动代码:在 data/model_prices.json 写 {"模型名":[in,out,cache_w,cache_r]}。
_DEFAULT_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5": (15, 75, 18.75, 1.5),
    "claude-opus-4-8": (15, 75, 18.75, 1.5),
    "claude-opus-4-6": (15, 75, 18.75, 1.5),
    "claude-sonnet-5": (3, 15, 3.75, 0.3),
    "claude-fable-5": (3, 15, 3.75, 0.3),
    "claude-haiku-4-5-20251001": (0.8, 4, 1, 0.08),
    "deepseek-v4-flash": (0.28, 0.42, 0.28, 0.028),
    "deepseek-v4-pro": (0.55, 2.19, 0.55, 0.055),
    "gpt-5.6-terra": (1.25, 10, 1.25, 0.125),
    "gpt-5.6-luna": (1.25, 10, 1.25, 0.125),
    "gpt-5.6-sol": (1.25, 10, 1.25, 0.125),
    "gpt-5.5": (1.25, 10, 1.25, 0.125),
    "kimi-k3": (0.6, 2.5, 0.6, 0.06),
    "K2.7 Code": (0.6, 2.5, 0.6, 0.06),
}
_FALLBACK_PRICE = (3.0, 15.0, 3.75, 0.3)  # 认不出的模型按 sonnet 档估,别当准数
_PRICES_PATH = config.DATA_DIR / "model_prices.json"

_LOGS_DIR = Path(os.path.expanduser("~/.claude/projects"))
_ETL_MIN_INTERVAL = 300.0  # 两次增量扫描的最小间隔(秒);面板刷新再频繁也不会反复扫盘

_STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_files(
  path TEXT PRIMARY KEY,
  mtime REAL NOT NULL,
  size INTEGER NOT NULL,
  offset INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_hourly(
  day TEXT NOT NULL, hour INTEGER NOT NULL, model TEXT NOT NULL, scope TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0, in_tok INTEGER NOT NULL DEFAULT 0,
  out_tok INTEGER NOT NULL DEFAULT 0, cache_w INTEGER NOT NULL DEFAULT 0,
  cache_r INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, hour, model, scope)
);
CREATE INDEX IF NOT EXISTS idx_usage_hourly_day ON usage_hourly(day);
CREATE TABLE IF NOT EXISTS usage_session(
  sdk_id TEXT NOT NULL, model TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0, in_tok INTEGER NOT NULL DEFAULT 0,
  out_tok INTEGER NOT NULL DEFAULT 0, cache_w INTEGER NOT NULL DEFAULT 0,
  cache_r INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(sdk_id, model)
);
CREATE TABLE IF NOT EXISTS tool_daily(
  day TEXT NOT NULL, name TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, name)
);
CREATE TABLE IF NOT EXISTS tool_session(
  session_key TEXT NOT NULL, name TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(session_key, name)
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_stats_db: sqlite3.Connection | None = None
_etl_lock = asyncio.Lock()


def _conn() -> sqlite3.Connection:
    global _stats_db
    if _stats_db is None:
        _stats_db = sqlite3.connect(
            str(config.DATA_DIR / "stats.db"), check_same_thread=False
        )
        _stats_db.executescript(_STATS_SCHEMA)
        _stats_db.commit()
    return _stats_db


def _prices() -> dict[str, tuple[float, float, float, float]]:
    """内置单价表 + data/model_prices.json 覆盖(文件坏了就当没有,不影响面板)。"""
    out = dict(_DEFAULT_PRICES)
    try:
        raw = json.loads(_PRICES_PATH.read_text("utf-8"))
        for name, vals in raw.items():
            if isinstance(vals, list) and len(vals) == 4:
                out[str(name)] = tuple(float(v) for v in vals)  # type: ignore[assignment]
    except Exception:
        pass
    return out


def cost_of(model: str, in_tok: int, out_tok: int, cache_w: int, cache_r: int,
            prices: dict | None = None) -> float:
    p = (prices or _prices()).get(model, _FALLBACK_PRICE)
    return (in_tok * p[0] + out_tok * p[1] + cache_w * p[2] + cache_r * p[3]) / 1e6


# ── 增量 ETL ────────────────────────────────────────────────────────────────
def _scan_file(path: Path, offset: int) -> tuple[dict, dict, int]:
    """从 offset 起解析一份 jsonl,返回 (按小时聚合, 按会话聚合, 新的字节偏移)。

    scope 用日志所在目录名区分:vococo 自己的会话 vs 终端里手跑的 Claude Code
    ——目录名是 cwd 路径转写来的,vococo 的会话 cwd 总在项目/worktree 下。
    """
    scope = "vococo" if "vococo" in path.parent.name else "other"
    hourly: dict[tuple, list[int]] = {}
    per_sess: dict[tuple, list[int]] = {}
    with path.open("rb") as fh:
        fh.seek(offset)
        raw = fh.read()
        new_offset = fh.tell()
    # 最后一行可能正被写到一半,留到下次;以最后一个换行为准
    cut = raw.rfind(b"\n")
    if cut < 0:
        return hourly, per_sess, offset
    new_offset = offset + cut + 1
    for line in raw[: cut + 1].split(b"\n"):
        if b'"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        msg = d.get("message") or {}
        u = msg.get("usage")
        if not isinstance(u, dict):
            continue
        model = msg.get("model") or "?"
        if model == "<synthetic>":  # SDK 自己造的占位消息,不是真实调用
            continue
        vals = (
            int(u.get("input_tokens") or 0),
            int(u.get("output_tokens") or 0),
            int(u.get("cache_creation_input_tokens") or 0),
            int(u.get("cache_read_input_tokens") or 0),
        )
        ts = str(d.get("timestamp") or "")
        if len(ts) >= 13:  # UTC ISO,转本地时区后再切日期/小时,否则热力图会整体偏
            try:
                import datetime as _dt

                lt = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                day, hour = lt.strftime("%Y-%m-%d"), lt.hour
            except Exception:
                day, hour = ts[:10], 0
        else:
            day, hour = "1970-01-01", 0
        for bucket, key in ((hourly, (day, hour, model, scope)),
                            (per_sess, (str(d.get("sessionId") or ""), model))):
            cur = bucket.setdefault(key, [0, 0, 0, 0, 0])
            cur[0] += 1
            for i, v in enumerate(vals):
                cur[i + 1] += v
    return hourly, per_sess, new_offset


def _run_etl() -> dict[str, int]:
    """扫一遍日志目录,只解析有新增的部分。阻塞式,调用方放线程里跑。"""
    db = _conn()
    known = {p: (m, s, o) for p, m, s, o in db.execute(
        "SELECT path, mtime, size, offset FROM usage_files")}
    files = sorted(_LOGS_DIR.glob("*/*.jsonl")) if _LOGS_DIR.is_dir() else []
    touched = 0
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        prev = known.get(str(f))
        offset = 0
        if prev:
            p_mtime, p_size, p_offset = prev
            if st.st_mtime <= p_mtime and st.st_size == p_size:
                continue                      # 没动过
            offset = p_offset if st.st_size >= p_offset else 0  # 被截断就整份重扫
        try:
            hourly, per_sess, new_offset = _scan_file(f, offset)
        except OSError:
            continue
        db.executemany(
            "INSERT INTO usage_hourly(day,hour,model,scope,calls,in_tok,out_tok,cache_w,cache_r)"
            " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(day,hour,model,scope) DO UPDATE SET"
            " calls=calls+excluded.calls, in_tok=in_tok+excluded.in_tok,"
            " out_tok=out_tok+excluded.out_tok, cache_w=cache_w+excluded.cache_w,"
            " cache_r=cache_r+excluded.cache_r",
            [(*k, *v) for k, v in hourly.items()],
        )
        db.executemany(
            "INSERT INTO usage_session(sdk_id,model,calls,in_tok,out_tok,cache_w,cache_r)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(sdk_id,model) DO UPDATE SET"
            " calls=calls+excluded.calls, in_tok=in_tok+excluded.in_tok,"
            " out_tok=out_tok+excluded.out_tok, cache_w=cache_w+excluded.cache_w,"
            " cache_r=cache_r+excluded.cache_r",
            [(*k, *v) for k, v in per_sess.items()],
        )
        db.execute(
            "INSERT INTO usage_files(path,mtime,size,offset) VALUES(?,?,?,?)"
            " ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size,"
            " offset=excluded.offset",
            (str(f), st.st_mtime, st.st_size, new_offset),
        )
        touched += 1
    db.execute("INSERT INTO meta(key,value) VALUES('etl_at',?)"
               " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(time.time()),))
    db.commit()
    return {"files": len(files), "updated": touched}


def _etl_turns() -> int:
    """把 turns.events 里的工具调用也增量聚合掉。

    events 是一大坨 JSON,全表重解析要 7~8 秒——面板每次打开都等这个不现实。
    turns 表只追加,所以按 id 水位往后处理即可;水位刻意落后一条,因为最后一轮的
    events 可能还在写(工具结果是回合结束才补齐的),早收进来会少算。
    """
    db, st = _conn(), _state_conn()
    row = db.execute("SELECT value FROM meta WHERE key='turn_id'").fetchone()
    try:
        watermark = int(row[0]) if row else 0
    except (TypeError, ValueError):
        watermark = 0
    max_id = (st.execute("SELECT COALESCE(max(id),0) FROM turns").fetchone() or (0,))[0]
    upto = max_id - 1                       # 最后一条留到下次
    if upto <= watermark:
        return 0
    daily: dict[tuple[str, str], list[int]] = {}
    per_sess: dict[tuple[str, str], list[int]] = {}
    n = 0
    for key, ts, ev in st.execute(
        "SELECT session_key, ts, events FROM turns WHERE id>? AND id<=? AND events IS NOT NULL",
        (watermark, upto),
    ):
        try:
            arr = json.loads(ev)
        except Exception:
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        for e in arr if isinstance(arr, list) else []:
            if not (isinstance(e, dict) and e.get("type") == "tool"):
                continue
            name = str(e.get("name") or "?")
            ok = 1 if e.get("ok") else 0
            for bucket, k in ((daily, (day, name)), (per_sess, (key, name))):
                cur = bucket.setdefault(k, [0, 0])
                cur[0] += 1
                cur[1] += ok
            n += 1
    db.executemany(
        "INSERT INTO tool_daily(day,name,calls,ok) VALUES(?,?,?,?)"
        " ON CONFLICT(day,name) DO UPDATE SET calls=calls+excluded.calls, ok=ok+excluded.ok",
        [(*k, *v) for k, v in daily.items()],
    )
    db.executemany(
        "INSERT INTO tool_session(session_key,name,calls,ok) VALUES(?,?,?,?)"
        " ON CONFLICT(session_key,name) DO UPDATE SET calls=calls+excluded.calls, ok=ok+excluded.ok",
        [(*k, *v) for k, v in per_sess.items()],
    )
    db.execute("INSERT INTO meta(key,value) VALUES('turn_id',?)"
               " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(upto),))
    db.commit()
    return n


def _last_etl_at() -> float:
    row = _conn().execute("SELECT value FROM meta WHERE key='etl_at'").fetchone()
    try:
        return float(row[0]) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


async def ensure_fresh(force: bool = False) -> None:
    """需要时跑一次增量 ETL。首轮(空库)会等它跑完,之后按最小间隔节流。"""
    if not force and time.time() - _last_etl_at() < _ETL_MIN_INTERVAL:
        return
    async with _etl_lock:
        if not force and time.time() - _last_etl_at() < _ETL_MIN_INTERVAL:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _run_etl)
        await loop.run_in_executor(None, _etl_turns)


# ── 聚合查询 ────────────────────────────────────────────────────────────────
def _state_conn() -> sqlite3.Connection:
    from ..memory import _db as memory_db

    return memory_db.conn()


_RANGES = {"7d": 7, "30d": 30, "all": 0}


def _since(range_key: str) -> float:
    days = _RANGES.get(range_key, 30)
    return 0.0 if days == 0 else time.time() - days * 86400


def overview(range_key: str = "30d") -> dict[str, Any]:
    """总览:概览数字 + 每日强度 + 模型花费 + 每日花费 + 工具排行。"""
    st, db = _state_conn(), _conn()
    since = _since(range_key)
    prices = _prices()
    tw = "WHERE ts>=?" if since else ""
    args = (since,) if since else ()

    daily = {d: {"turns": n} for d, n in st.execute(
        f"SELECT date(ts,'unixepoch','localtime') d, count(*) FROM turns {tw} GROUP BY d", args)}
    turns, days_active = (st.execute(
        f"SELECT count(*), count(DISTINCT date(ts,'unixepoch','localtime')) FROM turns {tw}",
        args).fetchone() or (0, 0))
    first_day = st.execute("SELECT date(min(ts),'unixepoch','localtime') FROM turns").fetchone()
    sessions, archived, errors, tot_tok, cache_r, fresh_in = st.execute(
        "SELECT count(*), COALESCE(sum(archived),0), COALESCE(sum(last_error),0),"
        " COALESCE(sum(total_tokens),0), COALESCE(sum(cache_read_total),0),"
        " COALESCE(sum(input_fresh_total),0) FROM session_meta").fetchone()

    # 用量:范围内按 (模型, 归属) 聚合;顺带把每天花费摊进 daily
    dw = "WHERE day>=?" if since else ""
    dargs = (time.strftime("%Y-%m-%d", time.localtime(since)),) if since else ()
    models: dict[tuple[str, str], dict] = {}
    hit_read = hit_fresh = 0   # 缓存命中按日志算(session_meta 的累计字段老会话缺失)
    for day, model, scope, calls, i, o, cw, cr in db.execute(
        f"SELECT day, model, scope, sum(calls), sum(in_tok), sum(out_tok), sum(cache_w),"
        f" sum(cache_r) FROM usage_hourly {dw} GROUP BY day, model, scope", dargs
    ):
        c = cost_of(model, i, o, cw, cr, prices)
        m = models.setdefault((model, scope), dict(
            model=model, scope=scope, calls=0, in_tok=0, out_tok=0, cache_w=0, cache_r=0, cost=0.0))
        m["calls"] += calls; m["in_tok"] += i; m["out_tok"] += o
        m["cache_w"] += cw; m["cache_r"] += cr; m["cost"] += c
        hit_read += cr
        hit_fresh += i + cw
        d = daily.setdefault(day, {"turns": 0})
        d["cost"] = round(d.get("cost", 0.0) + c, 4)

    tools = {name: [calls, ok] for name, calls, ok in db.execute(
        f"SELECT name, sum(calls), sum(ok) FROM tool_daily {dw} GROUP BY name", dargs)}
    top_tools = sorted(
        ({"name": k, "calls": v[0], "ok": v[1]} for k, v in tools.items()),
        key=lambda x: -x["calls"])[:10]

    return {
        "range": range_key,
        "overview": {
            "days": days_active, "first_day": first_day[0] if first_day else None,
            "sessions": sessions, "archived": archived, "turns": turns, "errors": errors,
            "total_tokens": tot_tok,
            "cache_read": hit_read or cache_r, "input_fresh": hit_fresh or fresh_in,
            "tool_calls": sum(v[0] for v in tools.values()),
            "tool_ok": sum(v[1] for v in tools.values()),
        },
        "daily": {k: v for k, v in sorted(daily.items())},
        "models": sorted(models.values(), key=lambda m: -m["cost"]),
        "tools": top_tools,
        "etl_at": _last_etl_at(),
    }


def sessions(range_key: str = "30d", limit: int = 200) -> list[dict]:
    """会话列表:每个会话一行,带 token / 花费 / 缓存命中 / 工具失败数。"""
    st, db = _state_conn(), _conn()
    since = _since(range_key)
    prices = _prices()
    per_sdk: dict[str, dict] = {}
    for sdk, model, calls, i, o, cw, cr in db.execute(
        "SELECT sdk_id, model, calls, in_tok, out_tok, cache_w, cache_r FROM usage_session"
    ):
        e = per_sdk.setdefault(sdk, {"calls": 0, "cost": 0.0, "models": [],
                                     "cache_read": 0, "input_fresh": 0})
        e["calls"] += calls
        e["cost"] += cost_of(model, i, o, cw, cr, prices)
        # 缓存命中优先用日志算:session_meta 的 cache_read_total 是 2026-08 才加的字段,
        # 之前的会话一律是 0,直接用会把老会话全画成"命中 0%"。
        e["cache_read"] += cr
        e["input_fresh"] += i + cw
        e["models"].append({"model": model, "calls": calls})

    tstat = {k: (n, a, b) for k, n, a, b in st.execute(
        "SELECT session_key, count(*), min(ts), max(ts) FROM turns GROUP BY session_key")}
    tools: dict[str, list] = {}
    for k, name, calls, ok in db.execute(
        "SELECT session_key, name, calls, ok FROM tool_session"
    ):
        cur = tools.setdefault(k, [0, 0, {}])
        cur[0] += calls
        cur[1] += calls - ok
        cur[2][name] = cur[2].get(name, 0) + calls

    out: list[dict] = []
    for (key, title, model, chosen, tot, cr, fresh, arch, err, sdk, wt, ctx, win) in st.execute(
        "SELECT session_key, title, model, chosen_model, total_tokens, cache_read_total,"
        " input_fresh_total, archived, last_error, sdk_session_id, worktree_path,"
        " ctx_tokens, ctx_window FROM session_meta"
    ):
        t = tstat.get(key)
        if not t or (since and t[2] < since):
            continue
        u = per_sdk.get(sdk or "", {})
        tl = tools.get(key, [0, 0, {}])
        top = sorted(tl[2].items(), key=lambda x: -x[1])[:5]
        out.append({
            "key": key, "title": title or "(未命名)", "model": chosen or model or "-",
            "turns": t[0], "start": t[1], "end": t[2],
            "tokens": tot or 0,
            "cache_read": u.get("cache_read", cr or 0),
            "input_fresh": u.get("input_fresh", fresh or 0),
            "archived": bool(arch), "error": bool(err), "worktree": bool(wt),
            "ctx": ctx or 0, "ctx_window": win or 0,
            "cost": round(u.get("cost", 0.0), 4), "calls": u.get("calls", 0),
            "linked": bool(u),  # 老会话没记 sdk_session_id,关联不上日志 → 花费显示"—"
            "models": sorted(u.get("models", []), key=lambda m: -m["calls"])[:4],
            "tools": tl[0], "tool_fail": tl[1],
            "top_tools": [{"name": n, "calls": c} for n, c in top],
        })
    out.sort(key=lambda s: -s["end"])
    return out[:limit]


async def payload(range_key: str = "30d") -> dict[str, Any]:
    """面板一次要的全部数据。ETL 与两段聚合都在线程里跑,别卡住事件循环。"""
    await ensure_fresh()
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, overview, range_key)
    data["sessions"] = await loop.run_in_executor(None, sessions, range_key)
    return data
