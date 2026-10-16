#!/usr/bin/env python3
"""GPT 代理 · 模型清单看门狗(脚本定时任务,零 token,每 15 分钟一跑)。

背景(2026-10-16):本地 GPT 代理 cli-proxy-api(127.0.0.1:8317)启动时去拉新模型
清单失败,退回二进制里内置的旧表(2026-08-03 编译,只到 gpt-5.6),于是 vococo 里
点 gpt-6 的档位全部 502「unknown provider for model gpt-6.1-sol」——请求被代理当场
退回,压根没发到上游;一直要等它 3 小时后的周期刷新自己救回来,而这事当时是人肉
撞上去才发现的。

这个脚本盯同一件事:代理不在线、或者 vococo 摆出来的 GPT 档没在代理清单里
→ 重启代理(launchctl kickstart,它 RunAtLoad+KeepAlive,起来会重新拉清单),
起来后再核对;救回来了只记日志不打扰,救不回来才报错,并且有节流,不刷屏。

榜单来自 vococo 自己的路由结果(providers.available_models + 供应商 base_url 是
回环地址),不另抄一份模型名,免得两边不同步。

输出约定见 cron/scheduler.py:只打 ##CRON_SIGNAL:0## = 完全静默,不推送;注意
「什么都不输出」不算静默,反而会被当成有事推一条"(无输出)",所以每条静默路径
都必须显式打这个标记。
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

# 盯的是线上那套配置,所以固定读主仓(worktree 里的副本读完也是看主仓,同 scripts/xhs_health.py)。
REPO = pathlib.Path.home() / "Repos/vococo"
sys.path.insert(0, str(REPO))

DATA = REPO / "data"
STATE = DATA / "gpt_proxy_watchdog.json"
LOG = DATA / "logs" / "gpt_proxy_watchdog.log"
# launchd 标签写死:launchd 里没有"按二进制反查服务"的正路,改名就跟着改这一行。
SERVICE = f"gui/{os.getuid()}/com.wesley.cli-proxy-api"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

HTTP_TIMEOUT = 10
BOOT_WAIT = 15     # 重启后每次核对的间隔(秒)
BOOT_TRIES = 8     # 最多核对 8 次(约 2 分钟;代理起来后约 60 秒才开始拉清单)
RESTART_COOLDOWN = 30 * 60  # 两次自动重启至少隔 30 分钟
ALERT_COOLDOWN = 60 * 60    # 两次报错至少隔 1 小时
GIVE_UP_AFTER = 3           # 代理在线但连着几次重启都缺档 → 认定不是刷新抖动,停止自动重启
STRIKE_RESET = 6 * 3600     # 这么久没再动过手,旧账清零,重新给机会
SILENT = "##CRON_SIGNAL:0##"


def log(line: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass  # 日志写不进去不该影响判断


def load_state() -> dict:
    try:
        raw = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def proxy_target() -> tuple[list[str], str, str]:
    """要看哪些档、代理在哪。

    「该在代理上的档」= 模型面板里 codex 分组、且该档供应商的 base_url 是回环地址
    ——available_models() 已经排除了被隐藏的档;再按回环地址过滤一次,是为了别把
    挂在远程供应商上的 mgmt_key(也能凑进 codex 分组)算进来。
    """
    from vococo import providers

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
    # 只盯排在最前的那台本地代理:真要同时有第二台,用这套 base_url/key 也问不到它的清单,
    # 硬凑一起只会把 B 代理的模型算成 A 代理缺档(现阶段只有一台,先按这个来)。
    base_url = rows[0][1]
    mine = [r for r in rows if r[1] == base_url]
    return [m for m, _b, _k in mine], base_url, mine[0][2]


def listed_models(base_url: str, api_key: str) -> tuple[list[str] | None, str, int]:
    """问代理要模型清单。返回(清单 或 None, 失败原因, HTTP 状态码或 0)
    ——原因要原样写进告警,别让人猜;状态码用来区分"代理不在"和"代理不认这个 key"。"""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/models", headers={"Authorization": "Bearer " + api_key}
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}", exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, f"连不上({type(exc).__name__})", 0
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None, "返回格式不认识", 0
    return [str(m.get("id")) for m in data["data"] if isinstance(m, dict)], "", 0


def restart_proxy() -> tuple[bool, str]:
    """重启代理。返回(有没有执行成功, 失败原因)。"""
    try:
        proc = subprocess.run(
            ["launchctl", "kickstart", "-k", SERVICE],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:200]
    return True, ""


def alert(state: dict, now: float, title: str, lines: list[str]) -> None:
    """报错(按小时节流);被节流时照样要打静默标记,否则空输出会被当成有事推出去。"""
    if now - state.get("last_alert", 0) < ALERT_COOLDOWN:
        print(SILENT)
        return
    state["last_alert"] = now
    save_state(state)
    print(title)
    for line in lines:
        print("- " + line)
    print("##CRON_SIGNAL:1##")


def run() -> None:
    state = load_state()
    now = time.time()
    want, base_url, api_key = proxy_target()
    if not want or not base_url or not api_key:
        print(SILENT)  # 没配本地 GPT 代理(或档位全隐藏了),没什么可盯的
        return

    listed, why, code = listed_models(base_url, api_key)
    online = listed is not None
    missing = want if not online else [m for m in want if m not in listed]
    if not missing:
        if state.get("strikes"):
            state["strikes"] = 0
            save_state(state)
        print(SILENT)
        return

    # 代理在、但不给清单(401/404/500):重启救不了(它能应答说明进程活着,不认的
    # 是凭据或地址),别白踢——踢的时候正好有 GPT 请求在飞会被掐断。
    if not online and 400 <= code < 500:
        alert(state, now, "⚠️ GPT 代理拒绝了模型清单请求(重启没用)", [
            f"请求 {base_url}/v1/models 返回 {why}。",
            "多半是设置页里这个 GPT 供应商的 API key / 地址不对了(或代理那边换了凭据)。"
            "改完自愈,不用重启代理。",
        ])
        return

    # 有病,准备重启。代理整个掉线永远值得救;「在线但缺档」连着几次救不回来
    # 就当上游真把档下线了,别再踢,免得每 15 分钟空转。
    strikes = int(state.get("strikes") or 0) if online else 0
    if online and now - state.get("last_restart", 0) > STRIKE_RESET:
        strikes = 0
    if online and strikes >= GIVE_UP_AFTER:
        alert(state, now, "⚠️ GPT 代理缺档,已停止自动重启", [
            f"连着 {strikes} 次重启都没恢复,不像刷新抖动,更像上游真把这些档下线了。",
            f"缺的档:{'、'.join(missing)}",
            f"代理当前清单:{'、'.join(listed)}",
            "处理:看 /tmp/cli-proxy-api.log 里的 model refresh;确属下线就去 vococo 设置页摘掉对应档位。",
            "（若是设置页改完就自愈,这条下次就不再报。）",
        ])
        return
    if now - state.get("last_restart", 0) < RESTART_COOLDOWN:
        print(SILENT)  # 刚重启过,给它时间,别连着踢
        return

    ok, reason = restart_proxy()
    if not ok:
        # 重启根本没跑成(服务没加载/权限错):这不能算"上游下线"的证据,既不记冷却
        # 也不记 strikes(否则 3 次失败的 kickstart 就能把看门狗推成"不再自动重启")。
        log(f"清单缺档({'、'.join(missing)}),但重启没执行成功:{reason}")
        alert(state, now, "⚠️ GPT 代理有问题,而且自动重启没能执行", [
            f"缺的档:{'、'.join(missing)}",
            f"代理当前状态:{'、'.join(listed) if online else (why or '不在线')}",
            f"重启失败原因:{reason}",
            f"处理:终端跑 launchctl print {SERVICE} 看服务在不在;急着用就先手动起代理。",
        ])
        return

    # 重启执行成功:掉线时顺手把缺档的旧账清零——都掉过一次了,说明不是"上游把档下线"。
    state["last_restart"] = now
    state["strikes"] = strikes + 1 if online else 0
    save_state(state)
    log(f"清单缺档({'、'.join(missing)}),已重启代理,等它重新拉清单")

    for _ in range(BOOT_TRIES):
        time.sleep(BOOT_WAIT)
        listed, why, _code = listed_models(base_url, api_key)
        online = listed is not None
        if not (want if not online else [m for m in want if m not in listed]):
            state["strikes"] = 0
            save_state(state)
            log("重启后已恢复")
            print(SILENT)
            return

    alert(state, now, "⚠️ GPT 代理缺档,重启后仍不行", [
        f"缺的档:{'、'.join(missing)}",
        f"代理当前状态:{'、'.join(listed) if online else (why or '不在线')}",
        f"重启:已执行(kickstart 成功),起来后清单还是不对",
        "可能:①拉清单的网络还没通(它只在启动时拉一次,失败就退回内置旧表,3 小时后再试)"
        " ②上游真把这档下线了。细节看 /tmp/cli-proxy-api.log 的 model refresh。",
    ])


def main() -> None:
    try:
        run()
    except Exception as exc:  # 看门狗自己坏了:报一行干净的说明(不打栈,栈会被 cron 原样推给他)
        state = load_state()
        now = time.time()
        if now - state.get("last_alert", 0) >= ALERT_COOLDOWN:
            state["last_alert"] = now
            save_state(state)
            print(f"⚠️ GPT 代理看门狗自己出错了(每小时最多报一次):{type(exc).__name__}: {exc}"[:300])
            print("##CRON_SIGNAL:1##")
        else:
            print(SILENT)


if __name__ == "__main__":
    main()
