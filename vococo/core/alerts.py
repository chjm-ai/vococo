"""全局报错出口 —— 服务自己发现的故障,统一从这里往外报。

起因(2026-10-16):本地 GPT 代理启动时拉模型清单失败,退回内置旧表,面板里
gpt-6 档位全部 502,一直没人知道,直到自己撞上去(见 core/proxy_health.py)。
这类「服务本来就能发现」的问题,以前只能靠各自 print 进日志,人在手机/外面
完全收不到。现在统一走这里:一条系统推送(dist 到所有订阅设备)+ 一行日志。

约定:
- 按 key 节流:同一个 key 在 cooldown 内只报一次。反复坏的故障只提醒一次,
  不刷屏;换一个 key 才算另一件事。
- 推送没配 / 没有设备订阅时,日志照样留痕,不会凭空消失。
- 调用方给稳定 key(如 "gpt-proxy"),标题说人话、正文写清现象 + 处理办法。
- 事件循环已经卡死、或不在异步上下文里的调用方(如假死看门狗线程)用
  alert_blocking —— 它在自己新起的循环里发,报错链路绝不能拖住调用方。
"""
from __future__ import annotations

import asyncio
import json
import time

from .. import config

_STATE_PATH = config.DATA_DIR / "alerts_state.json"
_LOG_PATH = config.DATA_DIR / "logs" / "alerts.log"
DEFAULT_COOLDOWN = 3600  # 同一个 key 默认 1 小时内只报一次


def _load() -> dict:
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save(state: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _log(title: str, body: str) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {title} | {body}\n")
    except OSError:
        pass


async def alert(
    key: str, title: str, body: str, *, cooldown: float = DEFAULT_COOLDOWN, url: str = ""
) -> bool:
    """报一条错。返回 True = 这次确实报了(不代表设备一定收到),False = 被节流跳过。"""
    state = _load()
    now = time.time()
    if cooldown > 0 and now - float(state.get(key) or 0) < cooldown:
        return False
    # 先落盘再发:推送网关抽风时也算报过了,避免下一轮又重试刷一遍。
    state[key] = now
    _save(state)
    _log(title, body)
    print(f"[告警] {title} —— {body}"[:300], flush=True)
    try:
        from ..gateway.adapters.web_push import PUSH  # 懒加载:core 不在导入期依赖网关

        got = await PUSH.notify(
            title=title, body=body, conv="main", kind="error",
            tag=f"alert-{key}", url=url or "/",
        )
    except Exception as exc:  # noqa: BLE001 —— 推送失败不影响调用方,日志已留痕
        print(f"[告警] 系统推送发送失败:{exc}", flush=True)
        return True
    if not got:
        print("[告警] 没有设备收到(推送没配或没有订阅),只写了日志", flush=True)
    return True


def alert_blocking(
    key: str, title: str, body: str, *, cooldown: float = DEFAULT_COOLDOWN, timeout: float = 15
) -> bool:
    """给不在异步上下文、或事件循环已卡死的调用方(如 gateway/watchdog 的自杀线程):
    新起一个事件循环把消息发出去,超时就放弃——绝不因为报错本身卡住调用方。"""
    try:
        return asyncio.run(asyncio.wait_for(alert(key, title, body, cooldown=cooldown), timeout))
    except Exception as exc:  # noqa: BLE001 —— 超时/推送失败都不拦调用方(状态和日志已落下)
        print(f"[告警] 同步发送失败:{type(exc).__name__} {exc}".rstrip(), flush=True)
        return False
