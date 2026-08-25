#!/usr/bin/env python3
"""从真机日志还原「通话期间屏幕常亮锁到底有没有全程持有」。

背景:2026-08-25 定案的开车场景——主人开车时长通话、从不手动锁屏,断线全是
"聊太久,手机到了自动熄屏时间自己熄"。要证明修复有没有用,只能看真机日志里
常亮锁的持有区间,源码断言测试证明不了任何真机行为。

用法:
    uv run python scripts/inspect_wakelock.py                     # 默认读 data/logs/vococo.out.log
    uv run python scripts/inspect_wakelock.py --log 路径 --tail 2000
    uv run python scripts/inspect_wakelock.py --since 11:00 --until 12:00

判读:
    掉锁空窗(lost→regained)是唯一要盯的数字。iOS 自动锁屏最短 30 秒,所以
    任何一段 >30s 的空窗都足以让屏幕自己熄掉、通话被打断。
    修复目标:通话期间不出现 >30s 的空窗;wakelock.held 心跳全程 held=true。
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DBG = re.compile(r"\[voice/dbg\]\s*(\{.*\})\s*$")
LOCK_LOST_GRACE_S = 30.0  # iOS 最短自动锁屏 30s:空窗超过它就可能真的熄屏


@dataclass
class Event:
    t: float          # 当天秒数(已做跨零点展开)
    raw: str          # 原始 HH:MM:SS.mmm
    tag: str
    state: str
    x: object = None


def parse_clock(s: str) -> float:
    h, m, rest = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def load(path: Path, tail: int | None) -> list[Event]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if tail:
        lines = lines[-tail:]
    out: list[Event] = []
    day = 0.0
    prev = -1.0
    for line in lines:
        m = DBG.search(line)
        if not m:
            continue
        try:
            d = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        raw = d.get("t") or ""
        if not raw:
            continue
        try:
            t = parse_clock(raw)
        except ValueError:
            continue
        if prev >= 0 and t < prev - 3600:  # 跨零点
            day += 86400
        prev = t
        out.append(Event(t + day, raw, d.get("tag", ""), d.get("state", ""), d.get("x")))
    return out


@dataclass
class Gap:
    start: Event
    end: Event | None = None

    @property
    def seconds(self) -> float:
        return (self.end.t - self.start.t) if self.end else float("inf")


@dataclass
class Report:
    acquired: int = 0
    released: int = 0
    fails: int = 0
    watchdog: int = 0
    deferred: int = 0
    heartbeats_held: int = 0
    heartbeats_lost: int = 0
    gaps: list[Gap] = field(default_factory=list)
    hold_durations: list[float] = field(default_factory=list)


def analyse(events: list[Event]) -> Report:
    r = Report()
    open_gap: Gap | None = None

    def lose(ev: Event) -> None:
        nonlocal open_gap
        if open_gap is None:
            open_gap = Gap(start=ev)

    def regain(ev: Event) -> None:
        nonlocal open_gap
        if open_gap is not None:
            open_gap.end = ev
            r.gaps.append(open_gap)
            open_gap = None

    for ev in events:
        tag = ev.tag
        if tag == "wakelock.acquired":
            r.acquired += 1
            regain(ev)
        elif tag == "wakelock.released":
            r.released += 1
            if isinstance(ev.x, dict) and isinstance(ev.x.get("heldMs"), (int, float)):
                if ev.x["heldMs"] >= 0:
                    r.hold_durations.append(ev.x["heldMs"] / 1000)
            lose(ev)
        elif tag == "wakelock.fail":
            r.fails += 1
            lose(ev)
        elif tag == "wakelock.watchdog":
            r.watchdog += 1
            lose(ev)
        elif tag == "wakelock.deferred":
            r.deferred += 1
        elif tag == "wakelock.held":
            held = bool(ev.x.get("held")) if isinstance(ev.x, dict) else False
            if held:
                r.heartbeats_held += 1
                regain(ev)
            else:
                r.heartbeats_lost += 1
                lose(ev)
        elif tag in ("button-mode.return", "os-suspend.enter", "bg.enter"):
            # 通话结束/进后台:锁本来就该没有,未闭合的空窗不算数
            open_gap = None
    if open_gap is not None:
        r.gaps.append(open_gap)
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="data/logs/vococo.out.log")
    ap.add_argument("--tail", type=int, default=None, help="只看最后 N 行")
    ap.add_argument("--since", default=None, help="起始时刻 HH:MM")
    ap.add_argument("--until", default=None, help="结束时刻 HH:MM")
    args = ap.parse_args()

    path = Path(args.log)
    if not path.exists():
        print(f"日志不存在:{path}")
        return 1

    events = load(path, args.tail)
    if args.since or args.until:
        lo = parse_clock(args.since + ":00") if args.since else 0.0
        hi = parse_clock(args.until + ":00") if args.until else 1e18
        events = [e for e in events if lo <= (e.t % 86400) <= hi]

    lock_events = [e for e in events if e.tag.startswith("wakelock.")]
    if not lock_events:
        print("这段日志里没有任何 wakelock.* 事件。")
        print("→ 要么这段时间没通话,要么跑的还是旧版前端(新版每 30s 有 wakelock.held 心跳)。")
        return 0

    r = analyse(events)
    print("═══ 常亮锁真机报告 ═══")
    print(f"日志:{path}  事件数:{len(events)}(其中 wakelock.* {len(lock_events)} 条)")
    print(f"acquired={r.acquired}  released={r.released}  fail={r.fails}  "
          f"watchdog补救={r.watchdog}  不可见挂起={r.deferred}")
    print(f"心跳:held={r.heartbeats_held}  lost={r.heartbeats_lost}", end="")
    if r.heartbeats_held == 0 and r.heartbeats_lost == 0:
        print("   ← 没有心跳 = 跑的是旧版前端,本次验证无效")
    else:
        print()
    if r.hold_durations:
        longest = max(r.hold_durations)
        print(f"单次持有时长:最长 {longest:.0f}s,共 {len(r.hold_durations)} 段有记录")

    bad = [g for g in r.gaps if g.seconds > LOCK_LOST_GRACE_S]
    print(f"\n掉锁空窗:{len(r.gaps)} 段,其中超过 {LOCK_LOST_GRACE_S:.0f}s 的 {len(bad)} 段")
    for g in sorted(r.gaps, key=lambda g: -g.seconds)[:10]:
        dur = "未闭合(直到日志结束)" if g.end is None else f"{g.seconds:.1f}s"
        mark = "⚠️" if g.seconds > LOCK_LOST_GRACE_S else "  "
        print(f"  {mark} {g.start.raw} [{g.start.tag}] → "
              f"{g.end.raw if g.end else '—'}  空窗 {dur}")

    print("\n判定:", end=" ")
    if r.heartbeats_held == 0 and r.heartbeats_lost == 0:
        print("无效——日志里没有心跳,说明手机上跑的还是旧版前端,先强刷页面再测。")
    elif bad:
        print(f"❌ 没防住。有 {len(bad)} 段空窗超过 {LOCK_LOST_GRACE_S:.0f}s,"
              "屏幕在这些窗口里可以自己熄掉。")
    elif r.heartbeats_lost:
        print("⚠️ 基本防住,但心跳抓到过掉锁瞬间(已被补回,空窗都在 30s 内)。")
    else:
        print("✅ 全程持有,没有掉锁空窗。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
