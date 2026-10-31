"""本地 GPT 代理体检 + 自愈(服务自己跑的常驻巡检)。

背景(2026-10-16):本地 GPT 代理 cli-proxy-api(127.0.0.1:8317)启动时拉模型清单
失败,退回二进制里内置的旧表(2026-08-03 编译,只到 gpt-5.6),于是面板里点
gpt-6 的档位全部 502「unknown provider for model gpt-6.1-sol」——请求被代理当场
退回,压根没发到上游;要等它 3 小时后的周期刷新自己救回来,期间没人知道。

这件事服务本来就能发现,所以做成服务内的常驻巡检(serve 启动后由 patrol_loop
每 INTERVAL 核一次),不再靠外部定时脚本:该在本地代理上的 GPT 档只要不在代理的
模型清单里、或代理整个不在线,就 launchctl kickstart 把代理重启一次(它
RunAtLoad+KeepAlive,起来会重新拉清单),起来后再核对;救回来了只记日志,
救不回来才报错——报错走 core/alerts.py 的全局出口(系统推送,按小时节流)。

档位名单从 providers 的公开 API 推导(模型面板里 codex 分组、且该档供应商的
base_url 是回环地址),不另抄一份模型名,免得两边不同步。

`vococo doctor` 也调 check(heal=False) 做只查不修的核对。
"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import anyio

from .. import config

INTERVAL = 15 * 60          # 两次巡检的间隔(秒)
FIRST_DELAY = 90            # 启动后先等一等:代理自己也要启动、拉清单
PROBE_TIMEOUT = 10          # HTTP 探测超时(放线程里跑,不卡事件循环)
BOOT_WAIT = 10              # 重启后每轮核对的间隔
BOOT_TRIES = 12             # 最多核对 12 轮(约 120 秒;代理起来约 60 秒后才拉清单,留够余量)
RESTART_COOLDOWN = 30 * 60  # 两次自动重启至少隔 30 分钟
GIVE_UP_AFTER = 3           # 代理在线但连着几次重启都缺档 → 不再自动重启(不像刷新抖动)
STRIKE_RESET = 6 * 3600     # 这么久没再动过手,旧账清零,重新给机会
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# 只接受标签，不接受其他用户域、路径或 launchctl 参数。
_LAUNCHD_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
ALERT_KEY = "gpt-proxy"  # 全局报错出口的节流键(这类故障共用一条,免得换着花样刷屏)
_STATE_PATH = config.DATA_DIR / "proxy_health.json"
_LOG_PATH = config.DATA_DIR / "logs" / "proxy_health.log"
# 回环地址的探测必须绕开 http_proxy:本机 shell 里有 http_proxy=127.0.0.1:7890(VPN),
# 走它的话连"端口没人听"都会被它答成 HTTP 502,判据全乱。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _log(msg: str) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass  # 日志写不进去不该影响判断


def _load_state() -> dict:
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def target() -> tuple[list[str], str, str]:
    """该盯哪些档、代理在哪、用什么 key。

    「该在代理上的档」= 模型面板里 codex 分组、且该档供应商的 base_url 是回环地址
    ——available_models() 已经排除了隐藏的档;再按回环地址过滤一次,是为了别把挂在
    远程供应商上的 mgmt_key(也能凑进 codex 分组)算进来。
    """
    from .. import providers

    rows: list[tuple[str, str, str]] = []  # (档位, 代理地址, key)
    for mid, _label, group in providers.available_models([]):
        if group != "codex":
            continue
        entry = providers.lookup_provider_by_model(mid) or {}
        base = str(entry.get("base_url") or "")
        if (urlsplit(base).hostname or "").lower() not in LOCAL_HOSTS:
            continue
        rows.append((mid, base, str(entry.get("api_key") or "")))
    if not rows:
        return [], "", ""
    # 只盯排在最前的那台本地代理:真要同时有第二台,用这套 base_url/key 也问不到它的
    # 清单,硬凑一起只会把 B 代理的模型算成 A 代理缺档(现阶段只有一台,先按这个来)。
    base_url = rows[0][1]
    mine = [r for r in rows if r[1] == base_url]
    return [m for m, _b, _k in mine], base_url, mine[0][2]


def _fetch_sync(base_url: str, api_key: str) -> dict:
    """同步问代理要模型清单(实跑在线程里)。

    返回 {listed, why, code, answered}:answered = 代理应答了(哪怕答的是 500 或
    一段看不懂的 JSON)——进程活着。这个区分是判断"重启有没有意义"的关键:
    应答了还不行,多半是凭据/地址/上游的问题,踢它没用;连应答都没有,才值得重启。
    """
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/models", headers={"Authorization": "Bearer " + api_key}
    )
    try:
        with _OPENER.open(req, timeout=PROBE_TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        return {"listed": None, "why": f"HTTP {exc.code}", "code": exc.code, "answered": True}
    except (urllib.error.URLError, OSError) as exc:
        return {"listed": None, "why": f"连不上({type(exc).__name__})", "code": 0, "answered": False}
    except http.client.HTTPException as exc:  # 答到一半就断(如 IncompleteRead):它还活着
        return {"listed": None, "why": f"响应被截断({type(exc).__name__})", "code": 0,
                "answered": True}
    # 解析单独一段:JSONDecodeError 也是 ValueError,混进上面那组会被当成"连不上",
    # 于是掉线分支每轮把 strikes 清零 → 对活着的代理无限踢下去。
    try:
        data = json.loads(body)
    except ValueError as exc:
        return {"listed": None, "why": f"返回不是 JSON({type(exc).__name__})", "code": 0,
                "answered": True}
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return {"listed": None, "why": "返回格式不认识", "code": 0, "answered": True}
    listed = [str(m.get("id")) for m in data["data"] if isinstance(m, dict)]
    return {"listed": listed, "why": "", "code": 0, "answered": True}


def _int(value, default: int = 0) -> int:
    """状态文件里的数字(可能被人手改坏 / 被别的版本写成字符串)。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _diagnose() -> dict:
    """探一次,什么都不改。ok=该有的档都在。"""
    want, base_url, api_key = target()
    if not want or not base_url or not api_key:
        return {"configured": False, "ok": True, "want": [], "missing": [], "listed": None,
                "code": 0, "answered": True, "detail": "没配本地 GPT 代理(或档位都被隐藏了)"}
    got = await asyncio.to_thread(_fetch_sync, base_url, api_key)
    listed = got["listed"]
    missing = want if listed is None else [m for m in want if m not in listed]
    return {
        "configured": True, "ok": not missing, "want": want, "missing": missing,
        "listed": listed, "code": got["code"], "answered": got["answered"], "base_url": base_url,
        "detail": (f"代理{'在线' if listed is not None else '没给清单'}:" + (got["why"] or "")).strip(":"),
    }


async def _kickstart() -> tuple[bool, str]:
    """重启代理。返回(有没有执行成功, 失败原因)。"""
    from ..instance_config import text

    try:
        label = text("local_services", "proxy_launchd_label")
        if not _LAUNCHD_LABEL.fullmatch(label):
            return False, "未配置有效的 local_services.proxy_launchd_label，未执行重启"
        service = f"gui/{os.getuid()}/{label}"
        proc = await asyncio.create_subprocess_exec(
            "launchctl", "kickstart", "-k", service,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (OSError, ValueError, asyncio.TimeoutError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return False, out.decode("utf-8", "replace").strip()[:200]
    return True, ""


async def check(*, heal: bool = True) -> dict:
    """体检一次。heal=True 时缺档/掉线就地重启代理并复核(patrol 用);
    heal=False 只查不修(doctor 用)。

    返回值里 alert=(标题, 正文) 表示"这件事该往外报",由调用方走 core/alerts.py
    发出去——本模块不自己发,doctor 这种只查询的场景才不会顺带打扰人。
    """
    now = time.time()
    state = _load_state()
    r = await _diagnose()
    r["action"], r["alert"] = "", None
    if not r["configured"] or r["ok"]:
        if state.get("strikes"):  # 好了,把缺档旧账清掉
            state["strikes"] = 0
            _save_state(state)
        return r
    if not heal:
        return r

    answered, want, listed, missing = r["answered"], r["want"], r["listed"], r["missing"]
    if answered and r["code"] and r["code"] < 500:
        # 代理在、只是 4xx 拒了清单请求:重启救不了——它能应答说明进程活着,不认的是
        # 凭据或地址;而且白踢一次会掐断正好在飞的 GPT 请求。
        r["action"] = "rejected"
        r["alert"] = (
            "⚠️ GPT 代理拒绝了模型清单请求(重启没用)",
            f"请求 {r['base_url']}/v1/models 返回 {r['detail']}。"
            "多半是设置页里这个 GPT 供应商的 API key / 地址不对了(或代理那边换了凭据),"
            "改完自愈,不用重启代理。",
        )
        return r

    # 只有"代理应答了"才累计放弃计数:应答了还不行(缺档/5xx/格式不认)踢几次就算了,
    # 免得每 15 分钟空转;整个连不上时永远值得救,而且顺手把旧账清零——都掉过一次了,
    # 说明不是"上游把档下线"。
    strikes = _int(state.get("strikes")) if answered else 0
    last_restart = float(_int(state.get("last_restart")))
    if answered and now - last_restart > STRIKE_RESET:
        strikes = 0  # 隔了这么久,旧账清零
    if answered and strikes >= GIVE_UP_AFTER:
        r["action"] = "given-up"
        r["alert"] = (
            "⚠️ GPT 代理不对(缺档 / 响应异常),已停止自动重启",
            f"连着 {strikes} 次重启都没恢复,不像刷新抖动,更像上游真把这些档下线了"
            f"或代理本身出了问题。缺的档:{'、'.join(missing) or '(全都没拿到)'};{r['detail']}。"
            "看 代理日志 里的 model refresh;确属档位下线就去设置页摘掉"
            "(摘完自愈,这条就不再报)。",
        )
        return r
    if now - last_restart < RESTART_COOLDOWN:
        r["action"] = "cooling"  # 刚重启过,给代理时间,这一轮不打扰人
        return r

    ok, reason = await _kickstart()
    if not ok:
        # 重启根本没跑成(服务没加载/权限错):这不算"上游下线"的证据,既不记冷却也不
        # 记 strikes(否则 3 次失败的 kickstart 能把体检推成"不再自动重启")。
        _log(f"清单缺档({'、'.join(missing)}),但重启没执行成功:{reason}")
        r["action"] = "restart-failed"
        r["alert"] = (
            "⚠️ GPT 代理有问题,而且自动重启没能执行",
            f"缺的档:{'、'.join(missing) or '(全都没拿到)'};{r['detail']}。重启失败原因:{reason}。"
            "检查实例配置中的代理服务标签及对应 launchd 服务,急着用就先手动起代理。",
        )
        return r

    state["last_restart"] = now
    state["strikes"] = strikes + 1 if answered else 0
    _save_state(state)
    _log(f"清单缺档({'、'.join(missing)}),已重启代理,等它重新拉清单")

    r["action"] = "restarted"
    for _ in range(BOOT_TRIES):
        await anyio.sleep(BOOT_WAIT)
        again = await _diagnose()
        if again["ok"]:
            state["strikes"] = 0
            _save_state(state)
            _log("重启后已恢复")
            return {**again, "action": "recovered", "alert": None}
        r = again
    r["action"] = "still-broken"
    r["alert"] = (
        "⚠️ GPT 代理缺档,重启后仍不行",
        f"缺的档:{'、'.join(r['missing'])};{r['detail']}。重启执行成功但起来后清单还是不对。"
        "可能:①拉清单的网络还没通(它只在启动时拉一次,失败就退回内置旧表,3 小时后再试)"
        " ②上游真把这档下线了。细节看 代理日志 的 model refresh。",
    )
    return r


async def patrol_loop() -> None:
    """常驻巡检:坏了就救,救不回来走全局报错出口。正常时全程静默(只写日志)。"""
    from . import alerts

    await anyio.sleep(FIRST_DELAY)
    while True:
        try:
            r = await check(heal=True)
            if r.get("alert"):
                await alerts.alert(ALERT_KEY, r["alert"][0], r["alert"][1])
        except Exception as exc:  # noqa: BLE001 —— 巡检自己出错不能拖垮这个循环……
            print(f"[代理体检] 出错:{type(exc).__name__}: {exc}", flush=True)
            try:  # ……但也不能静默:它坏了就再没人盯代理了(按小时节流)
                await alerts.alert(
                    "proxy-patrol-broken", "⚠️ GPT 代理巡检自己出错了",
                    f"{type(exc).__name__}: {exc}。巡检每 15 分钟一跑,这条每小时最多报一次;"
                    "期间 GPT 档位如果出问题(502)不会再自动重启,要手动看。",
                )
            except Exception:  # noqa: BLE001
                pass
        await anyio.sleep(INTERVAL)
