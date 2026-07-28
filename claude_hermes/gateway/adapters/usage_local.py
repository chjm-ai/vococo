"""本地 Claude 用量估算:封装 claude-monitor CLI,解析 ~/.claude/projects/*.jsonl。

这是官方 RateLimitEvent 的兜底:官方订阅经常拿不到具体利用率(utilization=null),
但本地日志始终能算出一个估算值。返回统一结构供 /api/usage 合并展示。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from typing import Any

# 缓存 60s,避免每次打开模型面板都重新扫一遍日志(实测扫描约 8s)。
_CACHE_TTL = 60.0
_cache: dict[str, Any] | None = None
_cache_at = 0.0
_lock = asyncio.Lock()


def _parse_ts(value: Any) -> int | None:
    """把 ISO 字符串或秒级时间戳统一转成秒级 int,失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            from datetime import datetime, timezone

            # 兼容带时区 ISO,如 2026-07-28T05:00:00+00:00
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            try:
                return int(value)
            except Exception:
                return None
    return None


def _run_monitor_sync() -> dict[str, Any]:
    """同步调用 claude-monitor --once --output json,返回原始 JSON。"""
    exe = shutil.which("claude-monitor")
    if not exe:
        raise RuntimeError("未安装 claude-monitor(请 uv sync 或 pipx install claude-monitor)")

    proc = subprocess.run(
        [exe, "--once", "--output", "json"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"claude-monitor 失败: {err[:300]}")

    data = json.loads(proc.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("claude-monitor 返回非对象 JSON")
    return data


def _make_limit(raw: dict[str, Any] | None, fallback_reset: int | None = None) -> dict[str, Any]:
    """把 claude-monitor 的 limits.five_hour / seven_day 转成前端统一格式。"""
    if not raw:
        return {}
    used_pct = raw.get("used_percentage")
    tokens_used = raw.get("tokens_used")
    token_limit = raw.get("token_limit")
    resets_at = _parse_ts(raw.get("resets_at_epoch") or raw.get("resets_at")) or fallback_reset

    limit: dict[str, Any] = {"resets_at": resets_at}
    if isinstance(token_limit, (int, float)) and token_limit > 0:
        limit["limit"] = int(token_limit)
        if isinstance(tokens_used, (int, float)):
            limit["remaining"] = max(0, int(token_limit) - int(tokens_used))
        if isinstance(used_pct, (int, float)):
            limit["utilization"] = min(1.0, max(0.0, used_pct / 100.0))
    elif isinstance(used_pct, (int, float)):
        # 没有 token_limit 但至少知道百分比
        limit["utilization"] = min(1.0, max(0.0, used_pct / 100.0))
    return limit


def _extract_details(raw: dict[str, Any]) -> dict[str, Any]:
    """从 claude-monitor JSON 提取前端 hover 卡片需要的扩展字段。"""
    local = raw.get("local") or {}
    forecast = raw.get("forecast") or {}
    pace = raw.get("pace") or {}
    history = raw.get("local_history") or {}

    return {
        "confidence": raw.get("confidence", "local_estimate"),
        "local": {
            "tokens": local.get("tokens") or {},
            "cost_usd": local.get("cost_usd"),
            "sent_messages": local.get("sent_messages"),
            "burn_rate_tokens_per_minute": local.get("burn_rate_tokens_per_minute"),
            "burn_rate_cost_per_hour": local.get("burn_rate_cost_per_hour"),
            "model_distribution": local.get("model_distribution") or [],
        },
        "forecast": {
            "predicted_tokens_exhausted_at": forecast.get("predicted_tokens_exhausted_at"),
            "predicted_tokens_exhausted_epoch": forecast.get("predicted_tokens_exhausted_epoch"),
            "tokens_remaining": forecast.get("tokens_remaining"),
            "minutes_remaining": forecast.get("minutes_remaining"),
            "display": forecast.get("display"),
        },
        "pace": {
            "used_percentage": pace.get("used_percentage"),
            "elapsed_percentage": pace.get("elapsed_percentage"),
        },
        "local_history": {
            "total_tokens": history.get("total_tokens"),
            "total_cost_usd": history.get("total_cost_usd"),
        },
    }


async def get_local_claude_usage() -> dict[str, Any] | None:
    """获取本地估算的 Claude 用量,带 60s 缓存;失败返回 None,不抛异常。"""
    global _cache, _cache_at
    now = time.monotonic()
    async with _lock:
        if _cache is not None and (now - _cache_at) < _CACHE_TTL:
            return dict(_cache)

    try:
        raw = await asyncio.get_event_loop().run_in_executor(None, _run_monitor_sync)
    except Exception as ex:
        # 日志里留痕,但接口层面降级:返回 None,让调用方决定是否显示错误
        print(f"[usage_local] 本地用量估算失败: {ex}", flush=True)
        return None

    limits_raw = raw.get("limits") or {}
    five = _make_limit(limits_raw.get("five_hour"))
    seven = _make_limit(limits_raw.get("seven_day"), fallback_reset=five.get("resets_at"))

    result = {
        "provider": "claude",
        "source": "local_estimate",
        "limits": {"five_hour": five, "seven_day": seven},
        **_extract_details(raw),
    }

    async with _lock:
        _cache = result
        _cache_at = time.monotonic()
    return dict(result)


def clear_cache() -> None:
    """调试用:清空本地用量缓存。"""
    global _cache, _cache_at
    _cache = None
    _cache_at = 0.0
