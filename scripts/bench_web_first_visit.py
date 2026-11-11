"""本地真实浏览器首访对照与按需入口验收,不连接生产、不调用模型。

用法:python scripts/bench_web_first_visit.py --baseline 5f3accd --runs 3 --out /tmp/web-bench.json
依赖当前 Python 环境的 aiohttp、playwright 和已安装的 Chromium。
同一临时数据库/同一服务交换基线与改后静态资源;显式指定基线避免提交后误比同版。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", "test-token")

from playwright.async_api import async_playwright

PROFILES = {"4g": (1600, 120), "slow": (400, 300)}
MOBILE = dict(
    viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, device_scale_factor=3,
    user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
)


async def context(browser, base, profile=None, cold=True, sw=False):
    ctx = await browser.new_context(**MOBILE, service_workers="allow" if sw else "block")
    await ctx.add_init_script("localStorage.setItem('vococo_token','bench-test-token')")
    page = await ctx.new_page()
    errors, dialogs = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), asyncio.create_task(dialog.accept())))
    cdp = await ctx.new_cdp_session(page)
    await cdp.send("Network.enable")
    await cdp.send("Network.setCacheDisabled", {"cacheDisabled": cold})
    await cdp.send("Network.setBypassServiceWorker", {"bypass": not sw})
    await cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
    if profile:
        kbps, latency = PROFILES[profile]
        await cdp.send("Network.emulateNetworkConditions", dict(
            offline=False, latency=latency, downloadThroughput=kbps*1000/8, uploadThroughput=kbps*1000/8))
    return ctx, page, errors, dialogs


async def visit(page, base):
    # 在导航前安装观测器,不把 Python 等待/轮询延迟算进页面进入时间。
    await page.add_init_script("""document.addEventListener('DOMContentLoaded',()=>{
      const timer=setInterval(()=>{
        if(typeof S==='undefined') return;
        const ready=S.surface==='call' && !document.getElementById('callView').hidden;
        const navigation=performance.getEntriesByType('navigation')[0];
        if(!ready && performance.now()-navigation.domContentLoadedEventStart<10000) return;
        clearInterval(timer);
        const resources=performance.getEntriesByType('resource');
        window.__bench={ms:ready ? performance.now() : null,
          failure:ready ? null : '脚本全部就绪后等10秒仍未进入默认界面', surface:S.surface, service:S.serviceState,
          shellMs:navigation.domContentLoadedEventStart,
          bytes:resources.reduce((s,r)=>s+r.encodedBodySize,0)+navigation.encodedBodySize,
          networkBytes:resources.reduce((s,r)=>s+r.transferSize,0)+navigation.transferSize,
          scripts:resources.filter(r=>/\\.js(\\?|$)/.test(r.name)).length,
          conversations:resources.filter(r=>new URL(r.name).pathname==='/conversations').map(r=>r.duration)};
      },10);
    },{once:true});""")
    await page.goto(base, wait_until="commit")
    await page.wait_for_function("()=>window.__bench", timeout=60000)
    return await page.evaluate("window.__bench")


async def measure(browser, base, runs, out):
    rows = []
    # 每对相邻样本只切换静态版本,避免先全跑旧版、后全跑新版时主机负载漂移。
    for profile in PROFILES:
        for cache in ("cold", "warm"):
            for run in range(runs):
                for label in ("before", "after"):
                    set_static(label)
                    ctx, page, errors, dialogs = await context(browser, base, profile, cold=cache=="cold")
                    try:
                        if cache == "warm":
                            await visit(page, base)
                        result = await visit(page, base)
                        result.update(label=label, profile=profile, cache=cache, run=run+1, errors=list(errors))
                    except Exception as error:
                        result = dict(label=label, profile=profile, cache=cache, run=run+1,
                                      failure=str(error), errors=list(errors))
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                    rows.append(result)
                    out.write_text(json.dumps({"measurements":rows}, ensure_ascii=False, indent=2))
                    await ctx.close()
    return rows


async def click(page, name, repeat=1):
    if name == "settings":
        await page.locator("#settingsBtn").evaluate("(e,n)=>{for(let i=0;i<n;i++) e.click();}", repeat)
    else:
        label = {"workbench": "工作台", "library": "作品库"}[name]
        row = page.locator("#convTopRows .conv").filter(has_text=label)
        await row.evaluate("(e,n)=>{for(let i=0;i<n;i++) e.click();}", repeat)


async def opened(page, name):
    selector = {"settings": "setModal", "workbench": "workbenchView", "library": "libraryView"}[name]
    await page.wait_for_function("id=>!!document.getElementById(id) && !document.getElementById(id).hidden", arg=selector)
    if name == "workbench":
        await page.wait_for_function("()=>typeof WB_DATA!=='undefined' && !!document.querySelector('#workbenchView .wb-toolbar')")


async def data_ready(page, name):
    if name == "settings":
        await page.wait_for_function("()=>SET.data!==null")
        # 默认统计页会另行扫描终端历史;不把原有统计慢请求算成模块下载失败。
        await page.locator('#setTabs [data-tab="appearance"]').click(force=True)
        assert await page.locator("#setPane .tsopt").count() == 3
    elif name == "library":
        await page.wait_for_function("()=>!LIB.loading && document.getElementById('libraryMessage').textContent==='暂无匹配作品。'")


async def smoke(browser, base):
    results = []
    for profile in PROFILES:
        ctx, page, errors, dialogs = await context(browser, base, profile)
        await visit(page, base)
        requested = []
        page.on("request", lambda request: requested.append(request.url))
        for name in ("settings", "library", "workbench"):
            start = await page.evaluate("performance.now()")
            await click(page, name, 6)
            await opened(page, name)
            elapsed = await page.evaluate("performance.now()") - start
            await data_ready(page, name)
            data_elapsed = await page.evaluate("performance.now()") - start
            requests = [url for url in requested if f"/{name}.js?" in url]
            assert len(requests) == 1, (name, requests)
            download = await page.evaluate("url=>performance.getEntriesByName(url).at(-1).duration", requests[0])
            results.append(dict(scenario="first-open-repeat", profile=profile, module=name,
                                ms=elapsed, dataReadyMs=data_elapsed, downloadMs=download, requests=requests))
            if name == "settings":
                await page.locator("#setClose").click(force=True)
        assert not errors and not dialogs, (errors, dialogs)
        await ctx.close()
    # 每个模块下载失败只报一次,再次点击重试成功。
    for name in ("settings", "library", "workbench"):
        ctx, page, errors, dialogs = await context(browser, base)
        await visit(page, base)
        async def fail(route):
            await asyncio.sleep(.2)
            await route.abort()
        await page.route(f"**/{name}.js*", fail)
        async with page.expect_event("dialog"):
            await click(page, name, 6)
        assert len(dialogs) == 1, dialogs
        await page.unroute(f"**/{name}.js*", fail)
        await click(page, name)
        await opened(page, name)
        assert not errors, errors
        results.append(dict(scenario="failure-retry", module=name, passed=True))
        await ctx.close()
    # 独立窗口及刷新。
    ctx, page, errors, dialogs = await context(browser, base)
    await page.goto(base + "workbench/window")
    await opened(page, "workbench")
    await page.reload()
    await opened(page, "workbench")
    assert await page.evaluate("document.body.classList.contains('wb-standalone')")
    assert not errors and not dialogs, (errors, dialogs)
    results.append(dict(scenario="standalone-refresh", passed=True))
    await ctx.close()
    results.extend(await initialization_failure(browser, base))
    results.extend(await extra_smoke(browser, base))
    results.extend(await cache_smoke(browser, base))
    for result in results:
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return results


async def initialization_failure(browser, base):
    ctx, page, errors, dialogs = await context(browser, base)
    await visit(page, base)
    requests = []
    async def broken(route):
        requests.append(route.request.url)
        await route.fulfill(content_type="text/javascript", body="window.__initCount=(window.__initCount||0)+1;throw new Error('expected-init-failure');")
    await page.route("**/settings.js*", broken)
    async with page.expect_event("dialog"):
        await click(page, "settings", 6)
    async with page.expect_event("dialog"):
        await click(page, "settings")
    assert len(requests) == 1 and len(dialogs) == 2, (requests, dialogs)
    assert all("请刷新" in message for message in dialogs)
    assert await page.evaluate("window.__initCount") == 1
    assert len(errors) == 1 and "expected-init-failure" in errors[0], errors
    await ctx.close()
    return [dict(scenario="partial-init-error-visible-no-reexecution", passed=True)]


async def extra_smoke(browser, base):
    ctx, page, errors, dialogs = await context(browser, base)
    await visit(page, base)
    assert await page.evaluate("()=>!performance.getEntriesByType('resource').some(r=>/\\/(settings|workbench|library)\\.js/.test(r.name))")
    await page.evaluate("()=>openConv('main')")
    await page.wait_for_function("()=>S.commands.length>0")
    await page.locator("#ta").fill("/")
    await page.wait_for_function("()=>!document.getElementById('cmdPop').hidden && !!document.querySelector('#cmdPop .ci')")
    # 没打开设置也应能打开会话菜单和滚动历史;不执行任何删除操作。
    await page.evaluate("()=>{closeCmdMenu();openConvMenu(document.getElementById('settingsBtn'),'main');}")
    assert await page.locator(".convmenu .cmitem").count() >= 3
    await page.evaluate("()=>closeConvMenu()")
    async def delayed(route):
        await asyncio.sleep(.5)
        await route.continue_()
    await page.route("**/workbench.js*", delayed)
    await click(page, "workbench")
    await page.evaluate("()=>openCallView()")
    await page.wait_for_function("()=>typeof WB_DATA!=='undefined'")
    assert await page.evaluate("S.surface==='call'")
    assert not errors and not dialogs, (errors, dialogs)
    await ctx.close()
    return [dict(scenario="no-eager-modules-slash-conv-menu-navigation-cancel", passed=True)]


async def cached_modules(page):
    await page.wait_for_function("""async ()=>{
      const cache=await caches.open('vococo-shell-v10');
      const keys=await cache.keys();
      return ['settings','workbench','library','lazy'].every(name=>keys.some(k=>new URL(k.url).pathname==='/'+name+'.js'));
    }""")


async def cache_smoke(browser, base):
    # 真正先访问旧版并安装旧 SW,再让同一服务提供新版;不伪造缓存内容。
    set_static("before")
    ctx, page, errors, dialogs = await context(browser, base, cold=False, sw=True)
    await visit(page, base)
    await page.evaluate("()=>navigator.serviceWorker.ready.then(()=>true)")
    await page.wait_for_function("()=>!!navigator.serviceWorker.controller")
    old = await page.locator('script[src*="settings.js"]').get_attribute("src")
    set_static("after")
    await visit(page, base)
    await page.wait_for_function("()=>typeof vococoLazy!=='undefined'")
    new = await page.evaluate("JSON.parse(document.getElementById('lazyAssets').textContent).settings")
    assert old != new and '?v=' in new, (old, new)
    # 未曾下载的新模块离线打不开,应明确报错;联网后点击能重试。
    await ctx.set_offline(True)
    async with page.expect_event("dialog"):
        await click(page, "settings", 6)
    assert len(dialogs) == 1, dialogs
    await ctx.set_offline(False)
    for name in ("settings", "library", "workbench"):
        await click(page, name)
        await opened(page, name)
        if name == "settings":
            await page.locator("#setClose").click(force=True)
    await cached_modules(page)
    await ctx.set_offline(True)
    await page.reload(wait_until="load")
    for name in ("settings", "library", "workbench"):
        await click(page, name)
        await opened(page, name)
        if name == "settings":
            await page.locator("#setClose").click(force=True)
    # 只验证外壳/模块可加载;动态数据仍必须联网,设置/作品库保留原有错误态。
    assert len(dialogs) == 1 and not errors, (dialogs, errors)
    await ctx.set_offline(False)
    await visit(page, base)
    await click(page, "settings")
    await opened(page, "settings")
    assert not errors, errors
    await ctx.close()
    return [dict(scenario="old-sw-upgrade-offline-miss-retry-cached-modules-refresh", passed=True)]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--smoke-only", action="store_true")
    args = parser.parse_args()
    from vococo import config
    from vococo.core import task_runner
    from vococo.memory import _db, session_store
    from vococo.gateway.adapters import web as web_mod
    with tempfile.TemporaryDirectory(prefix="vococo-first-visit-") as temp:
        temp = Path(temp)
        original_data = config.DATA_DIR
        for name, value in list(vars(config).items()):
            if isinstance(value, Path) and value.is_relative_to(original_data):
                setattr(config, name, temp / "data" / value.relative_to(original_data))
        config.AI_BRAIN_DIR = temp / "brain"
        config.AGENT_WORKSPACES_DIR = temp / "workspaces"
        config.WEB_HOST, config.WEB_PORT = "127.0.0.1", 0
        config.WEB_AUTH_TOKEN, config.VOICE_ENABLED = "bench-test-token", False
        async def no_recovery():
            pass
        task_runner.heal_after_restart = no_recovery
        session_store.backfill_thumbs = lambda: None
        archive = temp / "baseline.tar"
        with archive.open("wb") as output:
            subprocess.run(["git", "archive", args.baseline, "vococo/gateway/adapters/web_static"], stdout=output, check=True)
        before = temp / "before"
        with tarfile.open(archive) as source:
            source.extractall(before, filter="data")
        static = web_mod._STATIC
        global set_static
        def set_static(label):
            web_mod._STATIC = before / "vococo/gateway/adapters/web_static" if label=="before" else static
        adapter = web_mod.WebAdapter()
        await adapter._start_server()
        base = f"http://127.0.0.1:{adapter._runner.addresses[0][1]}/"
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                rows = [] if args.smoke_only else await measure(browser, base, args.runs, args.out)
                set_static("after")
                checks = await smoke(browser, base)
                await browser.close()
            report = dict(baseline=args.baseline, measurements=rows, checks=checks)
            args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            for label in ("before", "after"):
                for profile in PROFILES:
                    for cache in ("cold", "warm"):
                        group = [r for r in rows if r["label"]==label and r["profile"]==profile and r["cache"]==cache and r.get("ms") is not None]
                        if group:
                            print(label, profile, cache, {key: statistics.median(r[key] for r in group) for key in ("ms", "bytes", "scripts")}, flush=True)
        finally:
            web_mod._STATIC = static
            await adapter._runner.cleanup()
            _db.reset()


if __name__ == "__main__":
    asyncio.run(main())
