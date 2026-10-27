#!/usr/bin/env python3
"""Mac 本地性能采样与日报：仅用标准库，不联网、不调用模型。"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys

ROOT = Path.home() / "Library/Logs/vococo-mbp-health"
APP = Path.home() / "Library/Application Support/vococo-mbp-health"
LABEL = "com.vococo.mbp-health"


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=15)


def parse_top(text):
    matches = re.findall(r"CPU usage:.*?([\d.]+)% idle", text)
    if not matches:
        raise ValueError("无法解析 CPU 空闲率")
    return round(100 - float(matches[-1]), 2)


def parse_vm(text):
    size = re.search(r"page size of (\d+) bytes", text)
    if not size:
        raise ValueError("无法解析内存页大小")
    values = dict(re.findall(r"^([^:\n]+):\s+(\d+)\.", text, re.M))
    return {key: int(values[name]) * int(size[1]) / 1048576
            for key, name in (("swapin_mb", "Swapins"), ("swapout_mb", "Swapouts"))}


def parse_swap(text):
    match = re.search(r"used =\s*([\d.]+)([KMG])", text)
    if not match:
        raise ValueError("无法解析交换内存")
    return float(match[1]) * {"K": 1 / 1024, "M": 1, "G": 1024}[match[2]]


def parse_thermal(text):
    values = re.findall(r"CPU_(?:Speed|Scheduler)_Limit\s*=\s*(\d+)", text)
    available = re.search(r"CPU_Available_CPUs\s*=\s*(\d+)", text)
    if values:
        fewer_cpus = bool(available and int(available[1]) < (os.cpu_count() or 1))
        return {"limited": any(int(v) < 100 for v in values) or fewer_cpus,
                "limits": values, "available_cpus": int(available[1]) if available else None}
    if "No performance warning" in text and "No thermal warning" in text:
        return {"limited": False, "limits": []}
    raise ValueError("无法识别温控状态")


def processes():
    rows = []
    for line in run("/bin/ps", "-axo", "pcpu=,rss=,comm=").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3:
            rows.append({"name": Path(parts[2]).name, "cpu": float(parts[0]),
                         "rss_mb": round(int(parts[1]) / 1024, 1)})
    return {"cpu": sorted(rows, key=lambda r: r["cpu"], reverse=True)[:5],
            "memory": sorted(rows, key=lambda r: r["rss_mb"], reverse=True)[:5]}


def collect():
    row = {"timestamp": datetime.now().astimezone().isoformat(), "errors": {}}
    probes = {
        "cpu_pct": lambda: parse_top(run("/usr/bin/top", "-l", "2", "-s", "1", "-n", "0")),
        "load": lambda: list(os.getloadavg()),
        "pressure": lambda: int(run("/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level")),
        "swap_mb": lambda: parse_swap(run("/usr/sbin/sysctl", "vm.swapusage")),
        "vm": lambda: parse_vm(run("/usr/bin/vm_stat")),
        "boot": lambda: run("/usr/sbin/sysctl", "-n", "kern.boottime").strip(),
        "thermal": lambda: parse_thermal(run("/usr/bin/pmset", "-g", "therm")),
        "disk_free_gb": lambda: round(shutil.disk_usage(Path.home()).free / 1073741824, 1),
        "processes": processes,
    }
    for name, probe in probes.items():
        try:
            row[name] = probe()
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            row["errors"][name] = str(exc)[:300]
    return row


def prune(root, now):
    for folder, days in (("samples", 30), ("reports", 180)):
        for path in (root / folder).glob("*"):
            try:
                day = datetime.strptime(path.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < (now - timedelta(days=days)).date():
                path.unlink()


def sample(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "sample.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        row = collect()
        folder = root / "samples"
        folder.mkdir(exist_ok=True)
        day = datetime.fromisoformat(row["timestamp"]).date()
        with (folder / f"{day}.jsonl").open("a") as output:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
        prune(root, datetime.now().astimezone())


def read_window(root, now):
    start, rows, damaged = now - timedelta(hours=24), [], 0
    for day in {start.date(), now.date()}:
        path = root / "samples" / f"{day}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(errors="replace").splitlines():
            try:
                if "�" in line:
                    raise ValueError("日志含截断的 UTF-8 字符")
                row = json.loads(line)
                stamp = datetime.fromisoformat(row["timestamp"])
                if start < stamp <= now:
                    rows.append(row)
            except (ValueError, KeyError, TypeError):
                damaged += 1
    return sorted(rows, key=lambda r: r["timestamp"]), damaged


def swap_delta(rows):
    total, pairs = 0.0, 0
    for before, after in zip(rows, rows[1:]):
        gap = (datetime.fromisoformat(after["timestamp"]) -
               datetime.fromisoformat(before["timestamp"])).total_seconds()
        if not (0 < gap <= 450 and before.get("boot") and before.get("boot") == after.get("boot")):
            continue
        if "vm" not in before or "vm" not in after:
            continue
        delta = after["vm"]["swapout_mb"] - before["vm"]["swapout_mb"]
        if delta >= 0:
            total += delta
            pairs += 1
    return total, pairs


def metric_line(rows, key, label, unit):
    values = sorted(r[key] for r in rows if key in r)
    if not values:
        return f"- {label}：未采集成功"
    p95 = values[max(0, (95 * len(values) + 99) // 100 - 1)]
    return (f"- {label}：平均 {sum(values) / len(values):.1f}{unit} / "
            f"P95 {p95:.1f}{unit} / 峰值 {max(values):.1f}{unit}（{len(values)} 次）")


def render_report(rows, damaged, now):
    lines = [f"MBP 性能日报｜{now:%Y-%m-%d %H:%M}（过去24小时）",
             f"- 采样 {len(rows)}/288 次；缺失含休眠、关机或采样故障，不计为低负载"]
    if not rows:
        return "\n".join(lines + ["- 暂无数据，无法判断性能或升级必要性。"])
    lines += [metric_line(rows, "cpu_pct", "CPU", "%"),
              metric_line(rows, "swap_mb", "交换内存", "MB")]
    busy = sum(r.get("cpu_pct", 0) >= 80 for r in rows)
    pressure = [r["pressure"] for r in rows if "pressure" in r]
    thermal = [r["thermal"] for r in rows if "thermal" in r]
    disks = [r["disk_free_gb"] for r in rows if "disk_free_gb" in r]
    delta, pairs = swap_delta(rows)
    lines += [f"- CPU≥80%：{busy} 个采样点；内存压力告警：{sum(v != 1 for v in pressure)}/{len(pressure)} 次",
              f"- 系统限速：{sum(v['limited'] for v in thermal)}/{len(thermal)} 次；磁盘最低剩余：{min(disks) if disks else '未知'}GB",
              f"- 相邻有效区间换出：{delta:.1f}MB（{pairs} 段；排除重启、长缺口，不等于全天总量）"]
    names = Counter(r["processes"]["cpu"][0]["name"] for r in rows
                    if r.get("processes", {}).get("cpu"))
    lines += ["- 进程累计平均CPU榜首：" + ("、".join(f"{n} {c}次" for n, c in names.most_common(3)) or "未知"),
              f"- 采样有错误：{sum(bool(r.get('errors')) for r in rows)} 次；损坏日志：{damaged} 行",
              "- 以上是瞬时采样，不能换算为持续高负载时长；先累计两周，再结合卡顿体验评估升级。"]
    return "\n".join(lines)


def report(root):
    now = datetime.now().astimezone()
    rows, damaged = read_window(root, now)
    text = render_report(rows, damaged, now)
    folder = root / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{now:%Y-%m-%d}.txt").write_text(text + "\n")
    prune(root, now)
    print(text)
    print("##CRON_SIGNAL:0##")


def install():
    APP.mkdir(parents=True, exist_ok=True)
    ROOT.mkdir(parents=True, exist_ok=True)
    target = APP / "mbp_health.py"
    plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if target.exists() or plist.exists():
        raise RuntimeError("安装目标已存在；请先检查，避免覆盖已有配置")
    shutil.copyfile(Path(__file__), target)
    plist.parent.mkdir(parents=True, exist_ok=True)
    config = {"Label": LABEL, "ProgramArguments": ["/usr/bin/python3", str(target), "sample"],
              "StartInterval": 300, "RunAtLoad": True, "ProcessType": "Background",
              "LowPriorityIO": True, "Nice": 10,
              "StandardOutPath": str(ROOT / "launchd.stdout.log"),
              "StandardErrorPath": str(ROOT / "launchd.stderr.log")}
    plist.write_bytes(plistlib.dumps(config))
    run("/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist))
    print(f"已安装：{target}\n采样日志：{ROOT}\nLaunchAgent：{plist}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["sample", "report", "install"])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "install":
        install()
    elif args.action == "sample":
        sample(args.root)
    else:
        report(args.root)


if __name__ == "__main__":
    main()
