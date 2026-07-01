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
import uuid
from typing import Awaitable, Callable

import anyio
from croniter import croniter

from .. import config
from ..core.agent import run_turn
from ..memory import session_store

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


def create_job(
    *, name: str, prompt: str, schedule: dict, target: dict | None = None,
    model: str | None = None,
) -> dict:
    """新建一个 cron 任务并落盘,返回该任务。接受建议(accept_suggestion)时由此创建。"""
    jobs = load_jobs()
    job = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "prompt": prompt,
        "schedule": schedule,
        "target": target,
        "model": model,
        "enabled": True,
        "next_run_at": None,
        "last_run_at": None,
        "last_status": None,
    }
    jobs.append(job)
    save_jobs(jobs)
    return job


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


# === 记忆晋升:定时反思,把会话沉淀成 AI_BRAIN 长期记忆 ===
_REFLECT_PROMPT = (
    "下面是我们最近的对话。请回顾,做两件事:\n"
    "1)挑出【值得长期记住】的东西(踩过的坑+根因+修复、技术/方案决策、我明确表达的"
    "偏好、某工具/服务器/项目的关键路径与配置),用 save_memory 沉淀成新主题记忆;"
    "若属于已有分类(lessons/preferences/tech-decisions 等)就用文件工具按原格式追加。\n"
    "2)如果发现我【反复问/反复做】的事适合排成定时任务(如每天简报、定期检查),"
    "用 suggest_automation 提一条【建议】(不会自动开跑,等我一键接受)。\n"
    "没有值得记/值得提的就什么都别做,绝不为凑数硬写。最后一句话告诉我你记了/提了什么(或没有)。\n\n"
    "[最近对话]\n{convo}"
)


async def _reflect(push: PushFn) -> None:
    """回顾统一会话,让 agent 自主用 save_memory 沉淀长期记忆。"""
    history = session_store.load_recent(config.SESSION_KEY, limit=80)
    if not history:
        print("[反思] 当前会话无历史,跳过。")
        return
    convo = "\n".join(
        f"我:{t.user}" + (f"\n你:{t.assistant}" if t.assistant else "")
        for t in history
    )
    reply = await run_turn([], _REFLECT_PROMPT.format(convo=convo))
    summary = (reply.text or "").strip() if reply else ""
    target = config.REFLECT_TARGET
    if summary and ":" in target:
        platform, _, chat_id = target.partition(":")
        await push(platform.strip(), chat_id.strip(), f"🧠 记忆复盘\n\n{summary}")
    else:
        print(f"[反思] {summary or '(无输出)'}")


def _schedule_after(expr: str, after: float) -> float:
    return croniter(expr, datetime.datetime.fromtimestamp(after)).get_next(float)


async def run_scheduler(push: PushFn) -> None:
    """常驻调度循环:心跳 + 到期任务 +(可选)定时反思。启动时播种起步建议目录。"""
    try:
        from . import suggestion_catalog
        n = suggestion_catalog.seed()
        if n:
            print(f"💡 已登记 {n} 条起步自动化建议(用 /建议 查看接受)")
    except Exception as e:  # 播种失败不拖垮调度
        print(f"[建议] 播种起步目录出错: {e}")
    extra = f" · 反思 {config.REFLECT_CRON}" if config.REFLECT_ENABLED else ""
    print(f"⏱  调度器启动(每 {config.SCHEDULER_TICK_SEC}s 一跳{extra})")
    reflect_next: float | None = None
    while True:
        try:
            _write_heartbeat()
            await _tick(push)
            if config.REFLECT_ENABLED:
                now = time.time()
                if reflect_next is None:
                    reflect_next = _schedule_after(config.REFLECT_CRON, now)
                elif now >= reflect_next:
                    reflect_next = _schedule_after(config.REFLECT_CRON, now)
                    try:
                        await _reflect(push)
                    except Exception as e:  # 反思失败不拖垮调度
                        print(f"[反思] 出错: {e}")
        except Exception as e:
            print(f"[调度器] tick 出错: {e}")
        await anyio.sleep(config.SCHEDULER_TICK_SEC)
