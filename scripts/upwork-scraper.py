#!/usr/bin/env python3
"""
Upwork Job Scraper — 通过 CDP 连接已登录的 Chrome 抓取 Upwork 岗位,只报「新增」。

数据来源:
  - find-work 的 Best matches / Most recent 两个 feed(按个人资料推荐,实测不被 Cloudflare 拦)
  - 关键词搜索 /nx/search/jobs(--search 才抓;该页常年被 Cloudflare 拦,需先在
    Chrome 窗口里手动过一次验证)

页面状态分四种:OK / BLOCKED(Cloudflare)/ LOGGED_OUT / PARSE_FAIL。
全部来源都不 OK 时不写岗位文件,免得假 0 条污染「新增」对比基线。

用法:
  python3 scripts/upwork-scraper.py              # 两个 feed(定时任务用这个)
  python3 scripts/upwork-scraper.py --search     # 再加关键词搜索
  python3 scripts/upwork-scraper.py --quick      # 兼容旧参数,等同默认
"""

import json, time, sys, os, re, random, subprocess, urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────
CDP_PORT = 9228
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE = Path.home() / ".chrome-profiles/upwork"
# 不放 iCloud 目录(iCloud 托管目录读写可能卡住);data/ 已被 .gitignore 忽略
OUTPUT_DIR = Path.home() / "Repos/vococo/data/upwork"
SEEN_FILE = OUTPUT_DIR / "seen_ids.json"
SEEN_KEEP_DAYS = 30

FEEDS = [
    ("best-matches", "https://www.upwork.com/nx/find-work/best-matches"),
    ("most-recent", "https://www.upwork.com/nx/find-work/most-recent"),
]

SEARCH_QUERIES = [
    "AI agent automation",
    "Claude Code",
    "n8n AI workflow automation",
]

# 和 Wesley 方向相关的词,命中越多星越多(只是粗筛,细判交给总结 AI)
MATCH_KEYWORDS = [
    "claude", "agent", "automation", "automate", "n8n", "make.com", "zapier",
    "workflow", "lead gen", "lead generation", "prospecting", "outreach",
    "scrap", "crawl", "mcp", "rag", "chatbot", "llm", "openai", "gpt",
]

# ── Chrome / CDP ─────────────────────────────────────────

# 本机地址不能走系统代理(之前的 502 Bad Gateway 就是代理回的)
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_ws_url(port):
    """获取 CDP WebSocket URL,连不上返回 None"""
    try:
        resp = _NO_PROXY.open(f"http://127.0.0.1:{port}/json/version", timeout=3)
        return json.loads(resp.read()).get("webSocketDebuggerUrl")
    except Exception:
        return None


def ensure_chrome(port):
    """CDP 连不上就用专用 profile 拉起 Chrome,最多等 20 秒"""
    ws = get_ws_url(port)
    if ws:
        return ws
    print(f">>> CDP {port} 未运行,自动启动 Chrome")
    subprocess.Popen(
        [CHROME_BIN, f"--remote-debugging-port={port}",
         f"--user-data-dir={CHROME_PROFILE}", "--no-first-run"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(20):
        time.sleep(1)
        ws = get_ws_url(port)
        if ws:
            return ws
    return None


# ── 页面抓取 ──────────────────────────────────────────────

def page_state(page):
    """判断当前页是否被拦/掉登录"""
    url = page.url
    title = page.title()
    head = page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 300)")
    if ("__cf_chl" in url or title in ("请稍候…", "Just a moment...")
            or "请验证您是真人" in head or "Verify you are human" in head):
        return "BLOCKED"
    if "/ab/account-security/login" in url or "/login" in url:
        return "LOGGED_OUT"
    return "OK"


EXTRACT_JS = r"""() => {
  const tiles = [...document.querySelectorAll(
    '[data-test="job-tile-list"] > section, article[data-test="JobTile"], article[data-ev-job-uid]')];
  const txt = (el, sel) => { const e = el.querySelector(sel); return e ? e.innerText.trim().replace(/\s+/g, ' ') : ''; };
  return tiles.map(t => {
    const a = t.querySelector('a[href*="/jobs/"][href*="_~"]');
    const href = a ? a.getAttribute('href') : '';
    let id = t.getAttribute('data-ev-opening_uid') || t.getAttribute('data-ev-job-uid') || '';
    const m = href.match(/_~0?(\d+)/);
    if (!id && m) id = m[1];
    const title = txt(t, '.job-tile-title') || txt(t, 'h2') || txt(t, 'h3') || (a ? a.innerText.trim() : '');
    const budget = [txt(t, '[data-test="job-type"]'), txt(t, '[data-test="budget"]'),
                    txt(t, '[data-test="is-fixed-price"]')].filter(Boolean).join(' ');
    return {
      id, title,
      url: href ? 'https://www.upwork.com' + href.split('?')[0] : '',
      posted: txt(t, '[data-test="posted-on"]'),
      proposals: txt(t, '[data-test="proposals-tier"]'),
      budget,
      tier: txt(t, '[data-test="contractor-tier"]'),
      duration: txt(t, '[data-test="duration"]'),
      skills: [...t.querySelectorAll('[data-test="attr-item"]')].map(e => e.innerText.trim()),
      client_spent: txt(t, '[data-test="client-spendings"]'),
      client_country: txt(t, '[data-test="client-country"]'),
      payment_verified: /verified/i.test(txt(t, '[data-test="payment-verification-status"]')) &&
                        !/unverified/i.test(txt(t, '[data-test="payment-verification-status"]')),
      description: txt(t, '[data-test="job-description-text"]').slice(0, 500),
    };
  }).filter(j => j.id && j.title);
}"""


def render_all_tiles(page):
    """feed 是懒渲染(没滚到的卡片是 placeholder),逐个滚进视野让它们渲染出来。
    滚动发生在页面内部容器里,mouse.wheel 无效,只能 scrollIntoView。"""
    for _ in range(60):
        left = page.evaluate("""() => {
            const ph = document.querySelector('[data-test="job-tile-placeholder"]');
            if (ph) ph.scrollIntoView({block: 'center'});
            return !!ph;
        }""")
        if not left:
            break
        time.sleep(0.6)


def scrape_url(page, url):
    """打开一个列表页,返回 (状态, 岗位列表)"""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
    except Exception:
        pass
    try:
        page.wait_for_selector('[data-test="job-tile-list"], article[data-test="JobTile"]',
                               timeout=15000)
    except Exception:
        pass
    state = page_state(page)
    if state != "OK":
        return state, []
    render_all_tiles(page)
    jobs = page.evaluate(EXTRACT_JS)
    return ("OK" if jobs else "PARSE_FAIL"), jobs


# ── 新增对比 ──────────────────────────────────────────────

def load_seen():
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_seen(seen):
    cutoff = (datetime.now() - timedelta(days=SEEN_KEEP_DAYS)).strftime("%Y-%m-%d")
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")


def match_stars(job):
    text = " ".join([job["title"], job["description"], " ".join(job["skills"])]).lower()
    hits = sum(1 for k in MATCH_KEYWORDS if k in text)
    return min(hits, 3)


def job_line(job):
    stars = "★" * job["stars"] or "·"
    parts = [f"{stars} {job['title']}"]
    meta = [x for x in [job["budget"], job["tier"], f"投标 {job['proposals']}" if job["proposals"] else "",
                        job["client_spent"], job["client_country"],
                        "" if job["payment_verified"] else "未验证付款"] if x]
    parts.append("   " + " | ".join(meta))
    parts.append(f"   {job['url']}")
    return "\n".join(parts)


def write_outputs(new_jobs, all_jobs, status):
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d_%H%M")
    json_path = OUTPUT_DIR / f"jobs_{ts}.json"
    json_path.write_text(json.dumps({
        "scraped_at": now.isoformat(),
        "status": status,
        "total_unique": len(all_jobs),
        "new_count": len(new_jobs),
        "jobs": all_jobs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 同一天的 md 按场次追加,只记新增
    md_path = OUTPUT_DIR / f"jobs_{now.strftime('%Y-%m-%d')}.md"
    first = not md_path.exists()
    with open(md_path, "a", encoding="utf-8") as f:
        if first:
            f.write(f"# Upwork AI 岗位扫描 — {now.strftime('%Y-%m-%d')}\n\n")
        f.write(f"## {now.strftime('%H:%M')} 场 — 抓到 {len(all_jobs)} 条,新增 {len(new_jobs)} 条\n\n")
        for job in new_jobs:
            f.write(f"### {'★' * job['stars']} [{job['title']}]({job['url']})\n\n")
            f.write(f"- **发布**: {job['posted']} · 投标: {job['proposals']}\n")
            f.write(f"- **预算**: {job['budget']} · {job['tier']} · {job['duration']}\n")
            f.write(f"- **客户**: {job['client_spent']} · {job['client_country']}"
                    f"{'' if job['payment_verified'] else ' · 未验证付款'}\n")
            if job["skills"]:
                f.write(f"- **技能**: {', '.join(job['skills'])}\n")
            f.write(f"- **描述**: {job['description'][:300]}\n")
            f.write(f"- **来源**: {job['source']}\n\n")
    return json_path, md_path


HINTS = {
    "BLOCKED": "被 Cloudflare 拦截 → 在 9228 那个 Chrome 窗口里手动打开该页过一次验证",
    "LOGGED_OUT": "Upwork 登录已失效 → 在 9228 那个 Chrome 窗口里重新登录",
    "PARSE_FAIL": "页面正常但一张岗位卡片都没解析到 → Upwork 可能改版,需要改 EXTRACT_JS",
}


def main():
    with_search = "--search" in sys.argv

    ws_url = ensure_chrome(CDP_PORT)
    if not ws_url:
        print("##CRON_SIGNAL:1##")
        print("Chrome CDP 自动启动失败。手动启动:")
        print(f'  "{CHROME_BIN}" --remote-debugging-port={CDP_PORT} '
              f'--user-data-dir="{CHROME_PROFILE}" --no-first-run')
        sys.exit(1)

    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    try:
        browser = p.chromium.connect_over_cdp(ws_url)
    except Exception as e:
        print("##CRON_SIGNAL:1##")
        print(f"CDP 连接失败: {e}")
        p.stop()
        sys.exit(1)

    # 开新标签干活,不碰用户正在看的页面
    page = browser.contexts[0].new_page()

    sources = [(name, url) for name, url in FEEDS]
    if with_search:
        sources += [(f"search:{q}",
                     f"https://www.upwork.com/nx/search/jobs/?q={q.replace(' ', '%20')}&sort=recency")
                    for q in SEARCH_QUERIES]

    states, all_jobs, seen_ids = {}, [], set()
    search_blocked = False
    for name, url in sources:
        if search_blocked and name.startswith("search:"):
            states[name] = "BLOCKED"   # 搜索页被拦一次就别再撞了
            continue
        state, jobs = scrape_url(page, url)
        states[name] = state
        if state == "BLOCKED" and name.startswith("search:"):
            search_blocked = True
        fresh = 0
        for j in jobs:
            if j["id"] not in seen_ids:
                seen_ids.add(j["id"])
                j["source"] = name
                j["stars"] = match_stars(j)
                all_jobs.append(j)
                fresh += 1
        print(f"  [{name}] {state} · 卡片 {len(jobs)} · 去重后 +{fresh}")
        time.sleep(random.uniform(3, 8))

    page.close()
    p.stop()

    problems = {n: s for n, s in states.items() if s != "OK"}
    ok_any = len(problems) < len(states)

    if not ok_any:
        print("##CRON_SIGNAL:1##")
        print("全部来源都没抓到数据,本次不写文件(避免污染新增对比):")
        for n, s in problems.items():
            print(f"  - {n}: {s} — {HINTS[s]}")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    seen = load_seen()
    today = datetime.now().strftime("%Y-%m-%d")
    new_jobs = [j for j in all_jobs if j["id"] not in seen]
    for j in all_jobs:
        seen[j["id"]] = today   # 记「最后一次见到」,还挂在 feed 上的就不会被清掉
    save_seen(seen)
    new_jobs.sort(key=lambda j: -j["stars"])

    status = "OK" if not problems else "PARTIAL"
    json_path, md_path = write_outputs(new_jobs, all_jobs, status)

    print(f"\n抓到 {len(all_jobs)} 条,新增 {len(new_jobs)} 条(★=方向关键词命中数)")
    if problems:
        print("部分来源异常:")
        for n, s in problems.items():
            print(f"  - {n}: {s} — {HINTS[s]}")
    print(f"已保存: {md_path.name} / {json_path.name}")

    # 没新增也没异常 → 零 LLM 调用
    if not new_jobs and not problems:
        print("##CRON_SIGNAL:0##")
        return
    print("##CRON_SIGNAL:1##")
    for j in new_jobs:
        print(job_line(j))


if __name__ == "__main__":
    main()
