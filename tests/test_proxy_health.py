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


def _resp(listed=None, why="", code=0, answered=True) -> dict:
    """一次探测的返回(形状见 core/proxy_health._fetch_sync)。answered=代理应答了。"""
    return {"listed": listed, "why": why, "code": code, "answered": answered}


def _down(why="连不上(URLError)") -> dict:
    return _resp(why=why, answered=False)


def _stub(monkeypatch, fetch, kick=(True, "")):
    """fetch: 每次探测的返回(或可迭代对象);kick: (ok, reason)。返回重启调用记录。"""
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
    calls = _stub(monkeypatch, _resp(listed=env))
    r = _check()
    assert r["ok"] and r["action"] == "" and not calls and r["alert"] is None


def test_missing_then_recovered(env, monkeypatch):
    calls = _stub(monkeypatch, iter([_resp(listed=env[:1]), _resp(listed=env)]))
    monkeypatch.setattr(proxy_health, "_save_state", lambda st: None)  # 不落盘,只看内存结果
    r = _check()
    assert r["action"] == "recovered" and r["ok"] and calls and r["alert"] is None


def test_restart_but_still_missing(env, monkeypatch):
    calls = _stub(monkeypatch, _resp(listed=env[:1]))
    r = _check()
    assert r["action"] == "still-broken" and calls and r["alert"]
    assert _state(env)["strikes"] == 1  # 记一笔:它没救回来


def test_401_does_not_restart(env, monkeypatch):
    calls = _stub(monkeypatch, _resp(why="HTTP 401", code=401))
    r = _check()
    assert r["action"] == "rejected" and not calls
    assert "重启没用" in r["alert"][0]


def test_kickstart_failure_is_not_upstream_evidence(env, monkeypatch):
    calls = _stub(monkeypatch, _resp(listed=env[:1]), kick=(False, "Could not find service"))
    r = _check()
    assert r["action"] == "restart-failed" and calls
    # 重启没跑成:既不记冷却(下次还会试),也不记 strikes(不然 3 次失败的 kickstart 就"放弃"了)
    assert "last_restart" not in _state(env) and "strikes" not in _state(env)


def test_5xx_restarts_but_counts_strikes(env, monkeypatch):
    """500 可能是代理内部抽风,还值得重启一次;但它应答了,所以计数——不能无限踢。"""
    calls = _stub(monkeypatch, _resp(why="HTTP 500", code=500))
    r = _check()
    assert calls and r["action"] == "still-broken"
    assert _state(env)["strikes"] == 1


def test_5xx_gives_up_after_three(env, monkeypatch):
    proxy_health._STATE_PATH.write_text(json.dumps({"strikes": 3, "last_restart": time.time()}))
    calls = _stub(monkeypatch, _resp(why="HTTP 500", code=500))
    r = _check()
    assert not calls and r["action"] == "given-up"


def test_garbage_response_is_not_offline(env, monkeypatch):
    """能应答但格式不认(≠连不上):不该当成掉线永远踢下去。"""
    proxy_health._STATE_PATH.write_text(json.dumps({"strikes": 3, "last_restart": time.time()}))
    calls = _stub(monkeypatch, _resp(why="返回格式不认识"))
    r = _check()
    assert not calls and r["action"] == "given-up"


def test_give_up_after_repeated_failures(env, monkeypatch):
    proxy_health._STATE_PATH.write_text(json.dumps({"strikes": 3, "last_restart": time.time()}))
    calls = _stub(monkeypatch, _resp(listed=env[:1]))
    r = _check()
    assert r["action"] == "given-up" and not calls
    assert "停止自动重启" in r["alert"][0]


def test_offline_always_worth_retrying(env, monkeypatch):
    proxy_health._STATE_PATH.write_text(json.dumps({"strikes": 3, "last_restart": time.time() - 9999}))
    calls = _stub(monkeypatch, _down())
    _check()
    assert calls, "代理整个掉线时不该被'放弃线'拦住"
    assert _state(env)["strikes"] == 0, "见过掉线就说明不是上游下线,旧账该清零"


def test_strikes_decay_after_six_hours(env, monkeypatch):
    proxy_health._STATE_PATH.write_text(json.dumps({"strikes": 3, "last_restart": time.time() - 7 * 3600}))
    calls = _stub(monkeypatch, _resp(listed=env[:1]))
    _check()
    assert calls, "隔了 6 小时应重新给机会"


def test_cooldown_stays_quiet(env, monkeypatch):
    proxy_health._STATE_PATH.write_text(json.dumps({"strikes": 1, "last_restart": time.time()}))
    calls = _stub(monkeypatch, _resp(listed=env[:1]))
    r = _check()
    assert r["action"] == "cooling" and not calls and r["alert"] is None


def test_not_configured_is_silent(env, monkeypatch):
    monkeypatch.setattr(proxy_health, "target", lambda: ([], "", ""))
    _stub(monkeypatch, _down("不该被调用"))
    r = _check()
    assert r["configured"] is False and r["ok"] is True and r["alert"] is None


def test_doctor_mode_never_restarts(env, monkeypatch):
    calls = _stub(monkeypatch, _resp(listed=env[:1]))
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


def _serve_once(body: bytes, code: int = 200):
    """起一个只应答一次的本机 HTTP 服务,用来直接测 _fetch_sync 的真实解析路径。"""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # 别往测试输出里喷访问日志
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_200_with_non_json_body_counts_as_answered():
    """200 但不是合法 JSON(截断/网关塞了 HTML):代理活着,不许当成掉线无限踢。"""
    srv, base = _serve_once(b"<html>not json</html>")
    try:
        got = proxy_health._fetch_sync(base, "sk-x")
    finally:
        srv.server_close()
    assert got["answered"] is True and got["listed"] is None and "JSON" in got["why"]


def test_truncated_response_counts_as_answered():
    """响应被截断(声明 100 字节只发 10 字节就断):代理活着,别当成掉线无限踢。"""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        conn.recv(65536)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: 100\r\n\r\n{\"data\":"
        )
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    got = proxy_health._fetch_sync(f"http://127.0.0.1:{port}", "sk-x")
    assert got["answered"] is True and got["listed"] is None


def test_http_error_counts_as_answered():
    srv, base = _serve_once(b"boom", code=500)
    try:
        got = proxy_health._fetch_sync(base, "sk-x")
    finally:
        srv.server_close()
    assert got["answered"] is True and got["code"] == 500 and got["listed"] is None


def test_refused_connection_counts_as_offline():
    """端口没人听 = 真的连不上:这个才该永远值得重启。"""
    got = proxy_health._fetch_sync("http://127.0.0.1:1", "sk-x")
    assert got["answered"] is False and got["code"] == 0


def test_hang_alert_never_blocks_suicide(monkeypatch):
    """假死看门狗自杀前发通知,只等有限几秒:推送链路卡住也必须照常重启。"""
    from vococo.gateway import watchdog as wd

    monkeypatch.setattr(wd, "_ALERT_WAIT", 0.2)
    monkeypatch.setattr(alerts, "alert_blocking", lambda *a, **kw: time.sleep(30))
    t0 = time.time()
    wd._alert_hang(999)
    assert time.time() - t0 < 3


def test_alert_blocking_does_not_hang_callers(sent, monkeypatch):
    from vococo.gateway.adapters import web_push

    async def hang(**kw):
        await anyio.sleep(30)

    monkeypatch.setattr(web_push.PUSH, "notify", hang)
    t0 = time.time()
    assert alerts.alert_blocking("k4", "标题", "正文", timeout=1) is False
    assert time.time() - t0 < 5  # 报错链路不许拖住调用方(如假死看门狗的自杀线程)
