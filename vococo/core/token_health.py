"""订阅令牌探活(服务内常驻巡检)。

为什么要有它:官方模型是唯一踩 CLAUDE_CODE_OAUTH_TOKEN 的路径,而日常对话默认走
第三方端点、后台任务还会自动回落 DeepSeek —— 令牌被吊销后整条链路静默降级,没人
发现。2026-08-16 那次就这么过去了五天(providers.probe_subscription_token 的注释
里记着);当时给 doctor 加了探活,但 doctor 得有人想起来跑。这里把同一件事挂进
服务自己的巡检:每 INTERVAL 探一次,明确被吊销就报一条系统推送,12 小时内不重复。

三态判据沿用 providers.probe_subscription_token(网络不通/限流归 unknown,不算数,
不报)——断网不该变成"你的令牌坏了"。没配令牌(纯第三方供应商)时整件事静默跳过。
"""
from __future__ import annotations

import asyncio

import anyio

from .. import config

INTERVAL = 3 * 3600        # 探测间隔:令牌吊销不差这三小时,省点请求
FIRST_DELAY = 120          # 启动后缓一缓再探(和别的巡检错开)
PROBE_BUDGET = 30          # 单次探活的硬上限(探活自己 timeout=10,这是第二道保险)
ALERT_KEY = "subscription-token"
ALERT_COOLDOWN = 12 * 3600  # 一直没换令牌时,一天最多提醒两次


async def check() -> dict:
    """探一次。applicable=False 表示没配订阅令牌(不适用)。

    alert=(标题, 正文) 表示"这件事该往外报",由调用方走 core/alerts.py 发出去
    (和 core/proxy_health.check 一个形状:本模块不自己发,才好单独测)。
    """
    from .. import providers

    token = (config.OAUTH_TOKEN or "").strip()
    if not token:
        return {"applicable": False, "state": "none", "detail": "没配订阅令牌", "alert": None}
    # 探活自带 timeout=10(只兜 socket);外面再加一道,免得 DNS/连接层卡住不抛异常时
    # 这个 await 永不返回——那巡检就永远停在这一次,连异常分支都不会走。
    with anyio.fail_after(PROBE_BUDGET):
        state, detail = await asyncio.to_thread(providers.probe_subscription_token, token)
    r = {"applicable": True, "state": state, "detail": detail, "alert": None}
    if state == "bad":
        hint = ""
        if not detail.startswith("HTTP 401"):
            # 401 是服务器明说令牌不行(2026-10-16 实测被吊销就是 401);403 还可能是
            # "这个请求不被允许"——出口 IP 被拒/区域封锁也会给 403,动过代理/VPN 就会碰上,
            # 提示一声免得白折腾令牌。
            hint = "(403 也可能是网络出口被拒:刚动过代理/VPN 的话先看这条)"
        r["alert"] = (
            "⚠️ Claude 订阅令牌已失效,官方模型全线不可用",
            f"探活返回:{detail}。{hint}重跑 `claude setup-token` 换新令牌并更新 .env"
            "(见 OPERATIONS.md「订阅令牌」);换好前官方模型会一直不可用,"
            "日常对话/后台任务会走第三方端点兜着。(这条 12 小时内不重复提醒)",
        )
    return r


async def patrol_loop() -> None:
    """常驻:只在明确被拒(bad)时发声;ok/unknown 都安静。"""
    from . import alerts

    await anyio.sleep(FIRST_DELAY)
    while True:
        try:
            r = await check()
            if r["alert"]:
                await alerts.alert(ALERT_KEY, r["alert"][0], r["alert"][1], cooldown=ALERT_COOLDOWN)
        except Exception as exc:  # noqa: BLE001 —— 巡检自己出错不能拖垮这个循环……
            print(f"[令牌探活] 出错:{type(exc).__name__}: {exc}", flush=True)
            try:  # ……也不能静默:它坏了就再没人盯令牌了
                await alerts.alert(
                    "token-patrol-broken", "⚠️ Claude 订阅令牌探活自己出错了",
                    f"{type(exc).__name__}: {exc}。这次没探成,下轮(每 {INTERVAL // 3600} 小时)"
                    "会重试;这条每小时最多报一次——一直报就说明它彻底哑了,那期间令牌失效"
                    "不会有人提醒。",
                )
            except Exception:  # noqa: BLE001
                pass
        await anyio.sleep(INTERVAL)
