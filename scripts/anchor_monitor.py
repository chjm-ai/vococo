#!/usr/bin/env python3
"""隔热三线只读统计；复用面料 API 连接，不执行其 daily/scan 或写面料台账。"""
from __future__ import annotations

import argparse
import importlib.util
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

CN = timezone(timedelta(hours=8))
BUSINESS = Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Wesley notes/2.重点项目"
SOURCE = BUSINESS / "面料外贸/scripts/yxx_auto.py"
OUTPUT = BUSINESS / "隔热材料外贸/邮件获客/数据/自动化"
CAMPAIGNS = {
    "陶纤": "cam_nqmKP7cQcuBgWG9Pc",
    "过滤管": "cam_4Ztaw7Wr4DimXG44u",
    "微孔板": "cam_6okvMMYg8HhjwRWGk",
}


def load_source(path=SOURCE):
    spec = importlib.util.spec_from_file_location("anchor_existing_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fetch_pages(api, path, size):
    rows, offset, previous = [], 0, None
    while True:
        sep = "&" if "?" in path else "?"
        batch = api.lem("GET", f"{path}{sep}limit={size}&offset={offset}")
        if isinstance(batch, dict):
            batch = batch.get("data")
        if not isinstance(batch, list):
            raise ValueError("分页读取失败或响应格式错误")
        if batch and batch == previous:
            raise ValueError("分页返回重复页面，覆盖不完整")
        previous = batch
        rows.extend(batch)
        if len(batch) < size:
            return rows
        offset += size


def unique_leads(rows):
    if any(not row.get("leadId") for row in rows):
        raise ValueError("活动缺 leadId，不能准确计算去重人数")
    return {row["leadId"] for row in rows}


def daily_count(rows, start, end):
    count = 0
    for row in rows:
        stamp = datetime.fromisoformat(row["createdAt"].replace("Z", "+00:00"))
        if start <= stamp < end:
            count += 1
    return count


def rolling_bounce(acts, end, cfg):
    window_start = end - timedelta(days=cfg["bounce_window_days"])
    first_sent = {}
    for row in acts["emailsSent"]:
        stamp = datetime.fromisoformat(row["createdAt"].replace("Z", "+00:00"))
        lid = row["leadId"]
        first_sent[lid] = min(stamp, first_sent.get(lid, stamp))
    cohort = {lid for lid, stamp in first_sent.items() if window_start <= stamp < end}
    bounced = len(cohort & unique_leads(acts["emailsBounced"]))
    result = {"days": cfg["bounce_window_days"], "new_leads": len(cohort),
              "bounced_leads": bounced, "threshold": cfg["anchor_alert_threshold"],
              "min_sample": cfg["bounce_min_sample"]}
    alert = len(cohort) >= cfg["bounce_min_sample"] and bounced > len(cohort) * cfg["anchor_alert_threshold"]
    return result, alert


def campaign_snapshot(api, cid, day):
    info = api.lem("GET", f"/campaigns/{cid}")
    if not isinstance(info, dict) or info.get("_id") != cid:
        raise ValueError("campaign 读取失败或 ID 不匹配")
    leads = fetch_pages(api, f"/campaigns/{cid}/leads", 500)
    acts = {kind: fetch_pages(api, f"/activities?campaignId={cid}&type={kind}", 100)
            for kind in ("emailsSent", "emailsBounced", "emailsReplied")}
    sendable = api.export_leads(cid, "reviewed")
    start = datetime.combine(day, datetime.min.time(), CN)
    result = {"id": cid, "name": info.get("name"), "status": info.get("status", info.get("state")),
              "lead_states": dict(Counter(row.get("state") for row in leads)), "leads": len(leads),
              "reviewed_state": sum(row.get("state") == "reviewed" for row in leads),
              "sendable_export": len(sendable) if sendable is not None else None,
              "warnings": []}
    for kind, rows in acts.items():
        result[kind] = {"events_all": len(rows), "leads_all": len(unique_leads(rows)),
                        "events_day": daily_count(rows, start, start + timedelta(days=1))}
    sent = result["emailsSent"]["leads_all"]
    result["bounce_rate_all"] = result["emailsBounced"]["leads_all"] / sent if sent else None
    result["rolling"], alert = rolling_bounce(acts, start + timedelta(days=1), api.load_cfg())
    if alert:
        result["warnings"].append("滚动退信超现有隔热告警阈值（仅记录，不暂停）")
    if sendable is None:
        result["warnings"].append("reviewed 导出失败；state 数量不是可发送池")
    if result["status"] != "running":
        result["warnings"].append("campaign 非 running（仍纳入统计）")
    return result


def collect(api, day):
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "day_cn": day.isoformat(),
              "scope": "anchor_only", "campaigns": {}}
    for name, cid in CAMPAIGNS.items():
        try:
            result["campaigns"][name] = campaign_snapshot(api, cid, day)
        except Exception as exc:
            # 不记录可能含凭据的请求异常原文；错误不能伪装成零发送。
            result["campaigns"][name] = {"id": cid, "error": type(exc).__name__, "coverage": "failed"}
    return result


def record(result, output):
    output.mkdir(parents=True, exist_ok=True)
    with (output / "隔热指标.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", type=lambda value: datetime.strptime(value, "%Y-%m-%d").date())
    parser.add_argument("--read-only", action="store_true", help="不落盘，只读核查")
    args = parser.parse_args()
    day = args.day or datetime.now(CN).date() - timedelta(days=1)
    try:
        result = collect(load_source(), day)
        if not args.read_only:
            record(result, OUTPUT)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        failed = any(row.get("error") or row.get("warnings") for row in result["campaigns"].values())
    except Exception as exc:
        print(f"隔热统计未覆盖：{type(exc).__name__}")
        failed = True
    print(f"##CRON_SIGNAL:{int(failed)}##")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
