"""本地 GPT 代理体检:该重启的重启、不该重启的不许踢、报错不刷屏。

起因(2026-10-16):代理启动时拉模型清单失败 → 退回二进制内置旧表 → 面板里 gpt-6
档位全部 502,要等它 3 小时后的周期刷新才自愈,期间没人知道。服务内常驻巡检盯这件事
(core/proxy_health.py),报错走 core/alerts.py。

这里钉住的是判断本身(每一条都是真实会踩的坑):
- 缺档/掉线 → 重启一次;起来了但清单还不齐 → 报错;
- 401/404/500 → 不重启(进程活着,它不认的是凭据/地址,白踢还会掐断在飞的请求);
- kickstart 没跑成 ≠ "上游把档下线了":不记冷却、不记 strikes;
- 连着几次救不回来 → 停止自动重启,别再每 15 分钟空转;
- 掉线永远值得救,而且旧账要清零;隔 6 小时也重新给机会;
- 同一个 key 一小时内只报一次(全局报错出口的节流)。
"""
from __future__ import annotations

import json
import time

import anyio
import pytest

from vococo.core import alerts, proxy_health


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离状态/日志文件,并给一份"该有的档"名单。"""
    monkeypatch.setattr(proxy_health, "_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(proxy_health, "_LOG_PATH", tmp_path / "ph.log")
    monkeypatch.setattr(proxy_health, "BOOT_WAIT", 0)
    monkeypatch.setattr(proxy_health, "BOOT_TRIES", 2)
    want = ["gpt-6.1-sol", "gpt-6-astra"]
    monkeypatch.setattr(proxy_health, "target", lambda: (want, "http://127.0.0.1:8317", "sk-x"))
    return want


def _stub(monkeypatch, fetch, kick=(True, "")):
    """fetch: 每次探测的返回值(或可迭代对象);kick: (ok, reason)。返回重启调用记录。"""
    calls: list[int] = []
    if hasattr(fetch, "__next__"):
        monkeypatch.setattr(proxy_health, "_fetch_sync", lambda b, k: next(fetch))
    else:
        monkeypatch.setattr(proxy_health, "_fetch_sync", lambda b, k: fetch)

    async def _kick():
        calls.append(1)
        return kick

    monkeypatch.setattr(proxy_health, "_kickstart", _kick)
    return calls


def _state(env) -> dict:
    path = proxy_health._STATE_PATH
    return json.loads(path.read_text()) if path.exists() else {}


def _check(heal=True) -> dict:
    return anyio.run(lambda: proxy_health.check(heal=heal))


def test_healthy_no_restart(env, monkeypatch):
    calls = _stub(monkeypatch, (env, "", 0))
    r = _check()
    assert r["ok"] and r["action"] == "" and not calls and r["alert"] is None


def test_missing_then_recovered(env, monkeypatch):
    calls = _stub(monkeypatch, iter([(env[:1], "", 0), (env, "", 0)]))
    monkeypatch.setattr(proxy_health, "_save_state", lambda st: None)  # 不落盘,只看内存结果
    r = _check()
    assert r["action"] == "recovered" and r["ok"] and calls and r["alert"] is None


def test_restart_but_still_missing(env, monkeypatch):
    calls = _stub(monkeypatch, (env[:1], "", 0))
    r = _check()
    assert r["action"] == "still-broken" and calls and r["alert"]
    assert _state(env)["strikes"] == 1  # 记一笔:它没救回来


def test_401_does_not_restart(env, monkeypatch):
    calls = _stub(monkeypatch, (None, "HTTP 401", 401))
    r = _check()
    assert r["action"] == "rejected" and not calls
    assert "重启没用" in r["alert"][0]


def test_kickstart_failure_is_not_upstream_evidence(env, monkeypatch):
    calls = _stub(monkeypatch, (env[:1], "", 0), kick=(False, "Could not find service"))
    r = _check()
    assert r["action"] == "restart-failed" and calls
    # 重启没跑成:既不记冷却(下次还会试),也不记 strikes(不然 3 次失败的 kickstart 就"放弃"了)
    assert "last_restart" not in _state(env) and "strikes" not in _state(env)


def test_give_up_after_repeated_failures(env, monkeypatch):
    proxy_health._STATE_PATH.write_text(json.dumps({"strikes": 3, "last_restart": time.time()}))
    calls = _stub(monkeypatch, (env[:1], "", 0))
    r = _check()
    assert r["action"] == "given-up" and not calls
    assert "停止自动重启" in r["alert"][0]


def test_offline_always_worth_retrying(env, monkeypatch):
    proxy_health._STATE_PATH.write_text(json.dumps({"strikes": 3, "last_restart": time.time() - 9999}))
    calls = _stub(monkeypatch, (None, "连不上(URLError)", 0))
    _check()
    assert calls, "代理整个掉线时不该被'放弃线'拦住"
    assert _state(env)["strikes"] == 0, "见过掉线就说明不是上游下线,旧账该清零"


def test_strikes_decay_after_six_hours(env, monkeypatch):
    proxy_health._STATE_PATH.write_text(json.dumps({"strikes": 3, "last_restart": time.time() - 7 * 3600}))
    calls = _stub(monkeypatch, (env[:1], "", 0))
    _check()
    assert calls, "隔了 6 小时应重新给机会"


def test_cooldown_stays_quiet(env, monkeypatch):
    proxy_health._STATE_PATH.write_text(json.dumps({"strikes": 1, "last_restart": time.time()}))
    calls = _stub(monkeypatch, (env[:1], "", 0))
    r = _check()
    assert r["action"] == "cooling" and not calls and r["alert"] is None


def test_not_configured_is_silent(env, monkeypatch):
    monkeypatch.setattr(proxy_health, "target", lambda: ([], "", ""))
    _stub(monkeypatch, (None, "不该被调用", 0))
    r = _check()
    assert r["configured"] is False and r["ok"] is True and r["alert"] is None


def test_doctor_mode_never_restarts(env, monkeypatch):
    calls = _stub(monkeypatch, (env[:1], "", 0))
    r = _check(heal=False)
    assert not r["ok"] and not calls and r["alert"] is None


# ── 全局报错出口 ─────────────────────────────────────────────────────────────


@pytest.fixture
def sent(tmp_path, monkeypatch):
    from vococo.gateway.adapters import web_push

    monkeypatch.setattr(alerts, "_STATE_PATH", tmp_path / "alerts.json")
    monkeypatch.setattr(alerts, "_LOG_PATH", tmp_path / "alerts.log")
    box: list[dict] = []

    async def fake_notify(**kw):
        box.append(kw)
        return 1

    monkeypatch.setattr(web_push.PUSH, "notify", fake_notify)
    return box


def test_alert_throttled_by_key(sent):
    assert anyio.run(alerts.alert, "k1", "标题", "正文") is True
    assert anyio.run(alerts.alert, "k1", "标题", "正文") is False  # 冷却期内不重复
    assert anyio.run(alerts.alert, "k2", "标题", "正文") is True   # 换 key 照发
    assert len(sent) == 2 and sent[0]["kind"] == "error"


def test_alert_survives_push_failure(sent, monkeypatch):
    from vococo.gateway.adapters import web_push

    async def boom(**kw):
        raise RuntimeError("推送网关 500")

    monkeypatch.setattr(web_push.PUSH, "notify", boom)
    assert anyio.run(alerts.alert, "k3", "标题", "正文") is True  # 推送失败不能抛给调用方
    assert "标题" in alerts._LOG_PATH.read_text()  # 日志必须留痕


def test_alert_blocking_does_not_hang_callers(sent, monkeypatch):
    from vococo.gateway.adapters import web_push

    async def hang(**kw):
        await anyio.sleep(30)

    monkeypatch.setattr(web_push.PUSH, "notify", hang)
    t0 = time.time()
    assert alerts.alert_blocking("k4", "标题", "正文", timeout=1) is False
    assert time.time() - t0 < 5  # 报错链路不许拖住调用方(如假死看门狗的自杀线程)
