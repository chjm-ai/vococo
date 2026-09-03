"""运行数据面板:日志增量 ETL(不重扫已读过的字节)+ 聚合口径。"""
from __future__ import annotations

import json
import time


def _write_log(dirpath, session_id, rows):
    """造一份 Claude SDK 风格的 jsonl(只保留统计要用的字段)。"""
    dirpath.mkdir(parents=True, exist_ok=True)
    f = dirpath / f"{session_id}.jsonl"
    with f.open("a", encoding="utf-8") as fh:
        for model, usage, ts in rows:
            fh.write(json.dumps({
                "type": "assistant", "sessionId": session_id, "timestamp": ts,
                "message": {"model": model, "usage": usage},
            }) + "\n")
    return f


def _usage(inp=100, out=50, cw=0, cr=0):
    return {"input_tokens": inp, "output_tokens": out,
            "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}


def _prepare(isolated, monkeypatch):
    from vococo import config
    from vococo.gateway import stats

    (config.DATA_DIR).mkdir(parents=True, exist_ok=True)
    logs = isolated / "claude" / "projects"
    monkeypatch.setattr(stats, "_LOGS_DIR", logs)
    monkeypatch.setattr(stats, "_stats_db", None)
    return stats, logs


def test_etl_is_incremental(isolated, monkeypatch):
    """第二次扫描只吃新追加的行,不把旧行重复计一遍。"""
    stats, logs = _prepare(isolated, monkeypatch)
    ts = "2026-09-01T10:00:00.000Z"
    path = _write_log(logs / "-Users-me-vococo", "sess-a",
                      [("claude-sonnet-5", _usage(), ts)] * 2)

    stats._run_etl()
    calls = stats._conn().execute("SELECT sum(calls) FROM usage_hourly").fetchone()[0]
    assert calls == 2

    _write_log(path.parent, "sess-a", [("claude-sonnet-5", _usage(), ts)])
    stats._run_etl()
    assert stats._conn().execute("SELECT sum(calls) FROM usage_hourly").fetchone()[0] == 3
    # 没有新内容时再扫一次也不该重复累加
    stats._run_etl()
    assert stats._conn().execute("SELECT sum(calls) FROM usage_hourly").fetchone()[0] == 3


def test_scope_split_and_cost(isolated, monkeypatch):
    """vococo 自己的会话与终端里手跑的分开统计;花费按单价表折算。"""
    stats, logs = _prepare(isolated, monkeypatch)
    ts = "2026-09-01T10:00:00.000Z"
    _write_log(logs / "-Users-me-vococo", "sess-a", [("claude-sonnet-5", _usage(1_000_000, 0), ts)])
    _write_log(logs / "-Users-me-other", "sess-b", [("claude-sonnet-5", _usage(1_000_000, 0), ts)])
    stats._run_etl()

    data = stats.overview("all")
    scopes = {m["scope"]: m for m in data["models"]}
    assert set(scopes) == {"vococo", "other"}
    # sonnet 输入 $3/百万,各 100 万 token → 各 $3；总账的每日花费要合并两个入口。
    assert round(scopes["vococo"]["cost"], 2) == 3.0
    assert round(scopes["other"]["cost"], 2) == 3.0
    assert round(data["daily"]["2026-09-01"]["cost"], 2) == 6.0


def test_price_override(isolated, monkeypatch):
    """data/model_prices.json 能覆盖内置单价,不用改代码。"""
    from vococo import config
    from vococo.gateway import stats as stats_mod

    stats, logs = _prepare(isolated, monkeypatch)
    monkeypatch.setattr(stats_mod, "_PRICES_PATH", config.DATA_DIR / "model_prices.json")
    (config.DATA_DIR / "model_prices.json").write_text(
        json.dumps({"claude-sonnet-5": [30, 0, 0, 0]}), encoding="utf-8")
    _write_log(logs / "-Users-me-vococo", "sess-a",
               [("claude-sonnet-5", _usage(1_000_000, 0), "2026-09-01T10:00:00.000Z")])
    stats._run_etl()
    assert round(stats.overview("all")["models"][0]["cost"], 2) == 30.0


def test_session_rows_join_turns_and_logs(isolated, monkeypatch):
    """会话明细 = state.db 的轮次/工具 + 日志里的花费与缓存,按 sdk 会话号对上。"""
    from vococo.memory import session_store

    stats, logs = _prepare(isolated, monkeypatch)
    turn = session_store.start_turn("web:main", "问一句")
    session_store.finish_turn(
        turn, "答一句",
        events=[{"type": "tool", "name": "Bash", "ok": True},
                {"type": "tool", "name": "Bash", "ok": False}],
    )
    # 工具聚合的水位刻意落后一条(见 _etl_turns),补一轮才能把上面那轮收进来
    session_store.finish_turn(session_store.start_turn("web:main", "再问"), "再答")
    session_store.set_sdk_session_id("web:main", "sess-a")
    _write_log(logs / "-Users-me-vococo", "sess-a",
               [("claude-sonnet-5", _usage(1_000_000, 0, cr=1_000_000),
                 "2026-09-01T10:00:00.000Z")])
    stats._run_etl()
    stats._etl_turns()

    rows = stats.sessions("all")
    row = next(r for r in rows if r["key"] == "web:main")
    assert row["linked"] is True
    assert round(row["cost"], 2) == 3.3        # 输入 $3 + 缓存读 $0.3
    assert row["cache_read"] == 1_000_000      # 命中率取日志,不看老会话缺失的累计字段
    assert row["tools"] == 2 and row["tool_fail"] == 1
    assert row["top_tools"][0] == {"name": "Bash", "calls": 2}


def test_turn_etl_keeps_last_row_for_next_round(isolated, monkeypatch):
    """最后一轮的 events 可能还在写,水位要落后一条,下次再收。"""
    from vococo.memory import session_store

    stats, _ = _prepare(isolated, monkeypatch)
    for i in range(3):
        t = session_store.start_turn("web:main", f"问{i}")
        session_store.finish_turn(t, "答", events=[{"type": "tool", "name": "Read", "ok": True}])

    stats._etl_turns()
    assert stats._conn().execute("SELECT sum(calls) FROM tool_daily").fetchone()[0] == 2

    t = session_store.start_turn("web:main", "再问")
    session_store.finish_turn(t, "答", events=[{"type": "tool", "name": "Read", "ok": True}])
    stats._etl_turns()
    assert stats._conn().execute("SELECT sum(calls) FROM tool_daily").fetchone()[0] == 3


def test_range_filter(isolated, monkeypatch):
    """7 天范围不该把更早的轮次算进来。"""
    from vococo.memory import _db

    stats, _ = _prepare(isolated, monkeypatch)
    conn = _db.conn()
    conn.execute("INSERT INTO turns(session_key, ts, user_text, assistant_text)"
                 " VALUES('web:old', ?, '旧', '旧')", (time.time() - 40 * 86400,))
    conn.execute("INSERT INTO turns(session_key, ts, user_text, assistant_text)"
                 " VALUES('web:new', ?, '新', '新')", (time.time() - 3600,))
    conn.commit()

    assert stats.overview("7d")["overview"]["turns"] == 1
    assert stats.overview("all")["overview"]["turns"] == 2
