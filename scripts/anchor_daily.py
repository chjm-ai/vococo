#!/usr/bin/env python3
"""只读取最近三线巡检原始回执，不执行扫描或网络操作。"""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from anchor_checks import RAW_DIR
from anchor_report import report

JOBS_PATH = Path("/Users/wesley/Repos/vococo/data/cron_jobs.json")


def latest_report(directory, now, jobs_path=JOBS_PATH):
    paths = sorted(directory.glob("*.json"), reverse=True)
    if not paths:
        raise ValueError("没有巡检回执")
    path = paths[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    checked_at = payload["checked_at"]
    checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    age = now - checked
    if age < timedelta(0) or age > timedelta(hours=1):
        raise ValueError("最近巡检回执超过一小时或时间异常")
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    job = next((item for item in jobs if item.get("id") == "13f146ce"), None)
    if not job or job.get("last_status") != "success":
        raise ValueError("最近一次巡检未成功或调度记录缺失")
    if "running_run_id" in job:
        raise ValueError("巡检运行中，回执尚未确认")
    run_id = job.get("last_run_id")
    if not isinstance(run_id, str) or not run_id.strip() or payload.get("run_id") != run_id:
        raise ValueError("回执与最近一次巡检运行 ID 缺失或不匹配")
    finished = datetime.fromtimestamp(job["last_run_at"], timezone.utc)
    if not -timedelta(seconds=1) < finished - checked <= timedelta(minutes=40):
        raise ValueError("回执与最近一次巡检不匹配")
    text, failed = report(payload["results"], checked_at, path)
    return "以下为最近一次巡检的实际回执，调度成功本身不代表覆盖完整。\n" + text, failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    args = parser.parse_args()
    try:
        text, failed = latest_report(args.raw_dir, datetime.now(timezone.utc))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(f"日报无法确认近期三箱覆盖：{exc}；请查看巡检异常，不得报告零询盘。")
        return 1
    print(text)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
