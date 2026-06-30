"""定时调度 + 心跳(参考 Hermes 的 cron ticker)。

- 每 SCHEDULER_TICK_SEC 秒一跳:写心跳时间戳 + 跑到期任务 → 主动推送到平台
- 任务存 data/cron_jobs.json,支持 cron / interval / once 三种调度

job 结构:
{
  "id": "morning", "name": "晨间简报", "prompt": "...",
  "schedule": {"kind": "cron", "expr": "0 8 * * *"}        # 或 {"kind":"interval","minutes":60} / {"kind":"once","run_at": <epoch>}
  "target": {"platform": "telegram", "chat_id": 123},
  "model": null, "enabled": true,
  "next_run_at": null, "last_run_at": null, "last_status": null
}
"""
from __future__ import annotations

import datetime
import json
import time
from typing import Awaitable, Callable

import anyio
from croniter import croniter

from .. import config
from ..core.agent import run_turn

PushFn = Callable[[str, object, str], Awaitable[None]]  # (platform, chat_id, text)


def load_jobs() -> list[dict]:
    try:
        return json.loads(config.CRON_JOBS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_jobs(jobs: list[dict]) -> None:
    config.CRON_JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.CRON_JOBS_PATH.write_text(
        json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _next_run(schedule: dict, after: float) -> float | None:
    kind = schedule.get("kind")
    if kind == "cron":
        base = datetime.datetime.fromtimestamp(after)
        return croniter(schedule["expr"], base).get_next(float)
    if kind == "interval":
        return after + float(schedule.get("minutes", 60)) * 60
    if kind == "once":
        ra = float(schedule.get("run_at", 0))
        return ra if ra > after else None
    return None


def _write_heartbeat() -> None:
    config.HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.HEARTBEAT_PATH.write_text(str(int(time.time())), encoding="utf-8")


async def _run_job(job: dict, push: PushFn) -> None:
    reply = await run_turn([], job["prompt"], model=job.get("model"))
    job["last_run_at"] = int(time.time())
    job["last_status"] = "error" if reply.is_error else "success"
    tgt = job.get("target") or {}
    if reply.text and tgt.get("platform") and tgt.get("chat_id") is not None:
        await push(tgt["platform"], tgt["chat_id"], f"⏰ {job.get('name','任务')}\n\n{reply.text}")


async def _tick(push: PushFn) -> None:
    jobs = load_jobs()
    now = time.time()
    changed = False
    for job in jobs:
        if not job.get("enabled"):
            continue
        if job.get("next_run_at") is None:
            job["next_run_at"] = _next_run(job["schedule"], now)
            changed = True
            continue
        if job["next_run_at"] <= now:
            # 先推进(at-most-once),再跑
            if job["schedule"].get("kind") == "once":
                job["enabled"] = False
                job["next_run_at"] = None
            else:
                job["next_run_at"] = _next_run(job["schedule"], now)
            changed = True
            try:
                await _run_job(job, push)
            except Exception as e:  # 单个任务失败不拖垮调度
                job["last_status"] = f"error: {e}"
    if changed:
        save_jobs(jobs)


async def run_scheduler(push: PushFn) -> None:
    """常驻调度循环:心跳 + 到期任务。"""
    print(f"⏱  调度器启动(每 {config.SCHEDULER_TICK_SEC}s 一跳)")
    while True:
        try:
            _write_heartbeat()
            await _tick(push)
        except Exception as e:
            print(f"[调度器] tick 出错: {e}")
        await anyio.sleep(config.SCHEDULER_TICK_SEC)
