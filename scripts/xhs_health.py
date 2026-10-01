#!/usr/bin/env python3
"""小红书运营 · 链路健康检查(脚本定时任务,零 token,挂在「小红书运营」Agent 名下)。

检查三段:爬虫(launchd「每日8点30分-小红书爬虫」,不在 vococo 里)→ 分析(vococo 任务,监听爬虫报告目录触发)
→ 队列发布(vococo 脚本任务 b98cda06)。全部正常输出 ##CRON_SIGNAL:0##(不打扰);有问题列出原因和处理建议。

2026-10-01 建:当天发现爬虫 09-30(断网)、10-01(9333 端口 Chrome 页面卡死)连续两天失败,没人知道。
"""
import datetime
import json
import pathlib

HOME = pathlib.Path.home()
CRAWL = HOME / "Repos/linxai-xhs-research"
CRON_JOBS = HOME / "Repos/vococo/data/cron_jobs.json"  # 读主仓的(worktree 里跑也看线上任务状态)
PUBLISH_JOB = "b98cda06"
now = datetime.datetime.now()
today = now.date().isoformat()

# 失败日志里的关键字 → 人话原因 + 怎么处理
HINTS = [
    ("check-login", "9333 端口的 Chrome 页面卡死或小红书登录过期", "关掉 9333 端口那个 Chrome 让预检重开;还不行就扫码重登"),
    ("exit IP", "8:30 时网络不通(电脑睡眠或断网)", "确认电脑没睡眠、网络正常,可手动重跑 scripts/daily.sh"),
    ("安全中心", "触发了小红书风控", "人工打开浏览器过验证,当天别重跑"),
    ("captcha", "触发了验证码", "人工打开浏览器过验证,当天别重跑"),
]


def crawler() -> tuple[list[str], list[str], datetime.datetime | None]:
    log = CRAWL / "logs/daily.log"
    if not log.exists():
        return [], ["爬虫日志不存在:" + str(log)], None
    lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()[-600:]
    todays = [x for x in lines if x.startswith(f"[{today}")]
    done = next((x for x in todays if "=== daily.sh done ===" in x), None)
    if done:
        ts = datetime.datetime.fromisoformat(done[1:20])
        return [f"爬虫 {ts:%H:%M} 跑完"], [], ts
    if now.hour < 10:  # 8:30 开跑,正常 15 分钟内完成;10 点前不算失败
        return ["爬虫还在跑或未到检查时间"], [], None
    if not todays:
        return [], ["爬虫今天没开跑(电脑睡眠或 launchd 没触发);可手动跑 ~/Repos/linxai-xhs-research/scripts/daily.sh"], None
    text = "\n".join(todays)
    for key, why, fix in HINTS:
        if key in text:
            return [], [f"爬虫失败:{why}。处理:{fix}"], None
    fail = next((x.split("] ", 1)[-1] for x in todays if "FAIL" in x), "跑了但没写完成标记")
    return [], [f"爬虫失败:{fail}(细节看 logs/daily.log)"], None


def analysis(crawl_done: datetime.datetime | None) -> tuple[list[str], list[str]]:
    if crawl_done is None:
        return [], []
    if (CRAWL / f"data/daily_analysis/{today}/analysis.completed").exists():
        return ["分析已出"], []
    if (now - crawl_done).total_seconds() < 45 * 60:  # 爬完 45 分钟内算正常排队
        return ["分析排队中"], []
    return [], ["爬虫跑完超过 45 分钟,分析还没出:去 vococo 看「每日小红书爬虫数据分析」任务会话"]


def publish() -> tuple[list[str], list[str]]:
    try:
        job = next(j for j in json.loads(CRON_JOBS.read_text(encoding="utf-8")) if j.get("id") == PUBLISH_JOB)
    except (OSError, ValueError, StopIteration):
        return [], [f"读不到发布任务 {PUBLISH_JOB}({CRON_JOBS})"]
    if not job.get("enabled"):
        return [], ["发布任务被停用了"]
    last = job.get("last_run_at") or 0
    recent = (now.timestamp() - last) < 26 * 3600
    if recent and job.get("last_status") == "error":
        return [], ["队列发布上一轮失败:先看远程机 ~/Library/Logs/linxai-xhs-publish.log(失败项不会隔天自动补发)"]
    return (["发布正常"] if recent else ["发布今天没轮到"]), []


def main() -> None:
    ok, bad, done_at = crawler()
    for part in (analysis(done_at), publish()):
        ok += part[0]
        bad += part[1]
    if bad:
        print("⚠️ 小红书链路异常")
        for b in bad:
            print("- " + b)
        if ok:
            print("正常的:" + "、".join(ok))
        print("##CRON_SIGNAL:1##")
    else:
        print("✅ 小红书链路正常:" + "、".join(ok))
        print("##CRON_SIGNAL:0##")


if __name__ == "__main__":
    main()
