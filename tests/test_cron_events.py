"""事件触发的定时任务(cron/events.py):Webhook 入口、文件夹监听、缓冲合并发车。"""
from __future__ import annotations

import os
import time

import anyio
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from vococo import config
from vococo.cron import events, scheduler


@pytest.fixture
def jobs_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CRON_JOBS_PATH", tmp_path / "cron_jobs.json")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    events._buffer.clear()
    events._last_fired.clear()
    events._scan_busy.clear()
    fired = []
    monkeypatch.setattr(
        scheduler, "_run_job",
        lambda job, push, prompt=None, extra_env=None: fired.append((job["id"], prompt, extra_env)),
    )
    monkeypatch.setattr(events, "_job_busy", lambda jid: False)
    yield fired
    events._buffer.clear()
    events._last_fired.clear()


def _mk(schedule: dict, name: str = "t") -> dict:
    return scheduler.create_job(name=name, prompt="处理事件", schedule=schedule, cwd=None)


async def _noop_push(*a):
    return None


# ── 校验 / 规整 ─────────────────────────────────────────────────────────
def test_validate_and_normalize(tmp_path):
    assert scheduler.validate_schedule({"kind": "webhook"}) is None
    assert "16" in scheduler.validate_schedule({"kind": "webhook", "secret": "short"})
    assert "绝对路径" in scheduler.validate_schedule({"kind": "watch", "path": "rel/dir"})
    assert "不存在" in scheduler.validate_schedule({"kind": "watch", "path": str(tmp_path / "nope")})
    assert scheduler.validate_schedule({"kind": "watch", "path": str(tmp_path)}) is None
    n = events.normalize({"kind": "webhook"})
    assert len(n["secret"]) >= 16
    # 编辑时不传 secret → 沿用旧的
    assert events.normalize({"kind": "webhook"}, n)["secret"] == n["secret"]
    assert events.normalize({"kind": "watch", "path": str(tmp_path)})["glob"] == "*"


def test_create_job_generates_secret_and_tick_skips_event_jobs(jobs_env):
    job = _mk({"kind": "webhook"})
    assert job["schedule"]["secret"]
    scheduler._tick(_noop_push)  # 事件任务不按时间跑,也不该被写 next_run_at
    saved = scheduler.load_jobs()[0]
    assert saved["next_run_at"] is None and jobs_env == []
    assert scheduler.describe_schedule(saved["schedule"]) == "Webhook 触发"


def test_update_keeps_secret(jobs_env):
    job = _mk({"kind": "webhook"})
    upd = scheduler.update_job(job["id"], name="t2", prompt="p", schedule={"kind": "webhook"})
    assert upd["schedule"]["secret"] == job["schedule"]["secret"]


# ── 缓冲与发车 ──────────────────────────────────────────────────────────
def test_flush_merges_and_rate_limits(jobs_env):
    job = _mk({"kind": "webhook"})
    events.submit(job["id"], "第一条")
    events.submit(job["id"], "第二条")
    now = time.time()
    assert events.flush(_noop_push, now=now) == [job["id"]]
    jid, prompt, env = jobs_env[0]
    assert "第一条" in prompt and "第二条" in prompt and "<event_data>" in prompt
    assert "处理事件" in prompt  # 原指令还在
    assert "第二条" in env["VOCOCO_EVENT"]
    # 60 秒内再来 → 先缓冲不发
    events.submit(job["id"], "第三条")
    assert events.flush(_noop_push, now=now + 10) == []
    assert events.flush(_noop_push, now=now + events.MIN_INTERVAL_SEC + 1) == [job["id"]]


def test_flush_waits_while_busy_and_drops_for_disabled(jobs_env, monkeypatch):
    job = _mk({"kind": "webhook"})
    events.submit(job["id"], "x")
    monkeypatch.setattr(events, "_job_busy", lambda jid: True)
    assert events.flush(_noop_push) == []  # 任务在跑:不打断
    assert events._buffer[job["id"]]
    monkeypatch.setattr(events, "_job_busy", lambda jid: False)
    jobs = scheduler.load_jobs()
    jobs[0]["enabled"] = False
    scheduler.save_jobs(jobs)
    assert events.flush(_noop_push) == []
    assert job["id"] not in events._buffer  # 停用 → 积压丢掉


def test_buffer_cap(jobs_env):
    for i in range(events.MAX_BUFFERED + 10):
        events.submit("j", f"e{i}")
    assert len(events._buffer["j"]) == events.MAX_BUFFERED
    assert events._buffer["j"][0]["text"] == "e10"


# ── 文件夹监听 ──────────────────────────────────────────────────────────
def test_diff_scan_baseline_stable_and_changes():
    now = 1000.0
    changed, st = events.diff_scan(None, {"a": 900.0}, now)
    assert changed == [] and st == {"a": 900.0}  # 首次只记基线
    changed, st2 = events.diff_scan(st, {"a": 900.0, "b": 998.0}, now)
    assert changed == [] and "b" not in st2  # b 刚写入 2 秒,算还在写
    changed, st3 = events.diff_scan(st2, {"a": 950.0, "b": 998.0}, now + 10)
    assert changed == ["a", "b"] and st3 == {"a": 950.0, "b": 998.0}


def test_scan_watches_end_to_end(jobs_env, tmp_path):
    d = tmp_path / "rec"
    d.mkdir()
    (d / "old.m4a").write_text("x")
    old = time.time() - 60
    os.utime(d / "old.m4a", (old, old))
    job = _mk({"kind": "watch", "path": str(d), "glob": "*.m4a"})

    anyio.run(events.scan_watches)  # 基线
    assert job["id"] not in events._buffer
    (d / "new.m4a").write_text("y")
    (d / "note.txt").write_text("z")  # 不匹配 glob
    for f in ("new.m4a", "note.txt"):
        os.utime(d / f, (old, old))
    anyio.run(events.scan_watches)
    ev = events._buffer[job["id"]][0]
    assert ev["files"] == [str(d / "new.m4a")]
    events.flush(_noop_push)
    assert str(d / "new.m4a") in jobs_env[0][2]["VOCOCO_EVENT_FILES"]


# ── Webhook 入口 ────────────────────────────────────────────────────────
@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def hook_app(jobs_env, monkeypatch):
    from vococo.gateway.adapters.web import WebAdapter

    monkeypatch.setattr(config, "WEB_AUTH_TOKEN", "secret-login")
    adapter = WebAdapter()
    app = web.Application()
    app.add_routes([web.post("/hook/{job_id}", adapter._handle_hook)])
    return app


@pytest.mark.anyio
async def test_webhook_auth_and_payload(hook_app):
    job = _mk({"kind": "webhook"})
    key = job["schedule"]["secret"]
    async with TestClient(TestServer(hook_app)) as client:
        # 不需要 Web 登录口令,但要任务自己的 key
        assert (await client.post(f"/hook/{job['id']}", data="hi")).status == 403
        assert (await client.post(f"/hook/{job['id']}?key=wrong", data="hi")).status == 403
        assert (await client.post("/hook/nope?key=" + key, data="hi")).status == 404
        r = await client.post(f"/hook/{job['id']}?key={key}", json={"place": "home"})
        assert r.status == 202
        r2 = await client.post(
            f"/hook/{job['id']}", data="via header", headers={"X-Vococo-Key": key}
        )
        assert r2.status == 202
        big = "x" * (events.WEBHOOK_MAX_BYTES + 1)
        assert (await client.post(f"/hook/{job['id']}?key={key}", data=big)).status == 413
    texts = [e["text"] for e in events._buffer[job["id"]]]
    assert '"place": "home"' in texts[0] and "via header" in texts[1]


@pytest.mark.anyio
async def test_webhook_rejects_disabled_and_non_webhook(hook_app, tmp_path):
    cron_job = _mk({"kind": "cron", "expr": "0 8 * * *"})
    hook = _mk({"kind": "webhook"})
    jobs = scheduler.load_jobs()
    for j in jobs:
        if j["id"] == hook["id"]:
            j["enabled"] = False
    scheduler.save_jobs(jobs)
    async with TestClient(TestServer(hook_app)) as client:
        assert (await client.post(f"/hook/{cron_job['id']}?key=whatever", data="x")).status == 404
        assert (await client.post(
            f"/hook/{hook['id']}?key={hook['schedule']['secret']}", data="x")).status == 404


def test_rotate_webhook_secret(jobs_env):
    job = _mk({"kind": "webhook"})
    old = job["schedule"]["secret"]
    new_job = scheduler.rotate_webhook_secret(job["id"])
    assert new_job["schedule"]["secret"] != old
    assert scheduler.load_jobs()[0]["schedule"]["secret"] == new_job["schedule"]["secret"]
    cron = _mk({"kind": "cron", "expr": "0 8 * * *"}, name="c")
    assert scheduler.rotate_webhook_secret(cron["id"]) is None
    assert scheduler.rotate_webhook_secret("nope") is None
