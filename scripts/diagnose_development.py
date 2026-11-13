#!/usr/bin/env python3
"""机器及开发工作树只读诊断；正常静默，异常可登记铃铛供人工确认。"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time


def command(*args: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    result = subprocess.run(args, env=environment, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ValueError("诊断命令失败")
    return result.stdout.strip()


def readonly_database(path: Path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("数据库不存在或路径异常")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    connection.execute("PRAGMA query_only=ON")
    return closing(connection)


def machine_issues(root: Path) -> list[str]:
    issues = []
    disk = shutil.disk_usage(root)
    free_gb = disk.free / 1073741824
    if free_gb < 10 or disk.free / disk.total < 0.05:
        issues.append(f"磁盘空间不足：剩余 {free_gb:.1f} GiB（{disk.free / disk.total:.1%}）")
    load = os.getloadavg()[2]
    cores = os.cpu_count() or 1
    if load > cores * 1.5:
        issues.append(f"15 分钟负载偏高：{load:.1f}，逻辑核心 {cores}")
    if sys.platform == "darwin":
        pressure = int(command("/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"))
        if pressure not in (1,):
            issues.append(f"系统内存压力告警：级别 {pressure}")
    return issues


def heartbeat_issues(data: Path, now: float) -> list[str]:
    heartbeat = data / "heartbeat"
    if heartbeat.is_symlink() or not heartbeat.is_file():
        return ["运行心跳缺失或路径异常，请核对是否正在运行服务"]
    age = now - heartbeat.stat().st_mtime
    if age > 900 or age < -60:
        return [f"运行心跳异常：距当前 {age / 60:.0f} 分钟"]
    return []


def task_issues(data: Path, now: float) -> list[str]:
    path = data / "voice" / "voice.db"
    if not path.exists():
        return []
    with readonly_database(path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('running','queued') AND updated_at<?",
            (now - 86400,),
        ).fetchone()[0]
    return [f"有 {count} 个运行/排队任务超过 24 小时未更新，需核对，勿自动取消"] if count else []


def worktree_listing(root: Path) -> list[dict]:
    text = command("git", "--no-replace-objects", "-C", str(root), "worktree", "list", "--porcelain", "-z")
    rows = []
    for block in text.split("\0\0"):
        fields = [field for field in block.split("\0") if field]
        if fields:
            rows.append(dict(field.split(" ", 1) if " " in field else (field, "") for field in fields))
    return rows


def _worktree_inventory(roots: list[Path], bindings: set[str], data: Path) -> tuple[int, int, int]:
    total, orphaned, broken = set(), set(), set()
    for root in roots:
        if not (root / ".git").exists():
            continue
        for row in worktree_listing(root)[1:]:
            path = Path(row["worktree"])
            if not (path.is_relative_to(root / "data" / "worktrees")
                    or path.is_relative_to(data / "worktrees") or str(path) in bindings):
                continue
            total.add(str(path))
            if str(path) not in bindings:
                orphaned.add(str(path))
            if "prunable" in row or not path.is_dir() or path.is_symlink():
                broken.add(str(path))
    broken.update(path for path in bindings if not Path(path).is_dir())
    return len(total), len(orphaned), len(broken)


def worktree_issues(root: Path, data: Path, max_worktrees: int) -> list[str]:
    with readonly_database(data / "state.db") as connection:
        roots = {root, *(Path(row[0]).expanduser().resolve()
                         for row in connection.execute("SELECT path FROM projects"))}
        bindings = {row[0] for row in connection.execute(
            "SELECT worktree_path FROM session_meta WHERE worktree_path IS NOT NULL AND worktree_path!=''")}
    total, orphaned, broken = _worktree_inventory(sorted(roots), bindings, data)
    issues = []
    if total >= max_worktrees:
        issues.append(f"开发工作树数量偏多：{total} 个（阈值 {max_worktrees}）")
    if orphaned:
        issues.append(f"发现 {orphaned} 个无会话绑定的受管工作树；仅列清单，不自动删除")
    if broken:
        issues.append(f"发现 {broken} 个失效/异常工作树绑定或 Git 登记")
    return issues


def cleanup_issues(data: Path, now: float) -> list[str]:
    path = data / "repository-cleanup.json"
    if not path.exists():
        return ["尚无每日仓库巡检回执，请核对服务版本和巡检是否启用"]
    if path.is_symlink():
        raise ValueError("巡检回执路径异常")
    report = json.loads(path.read_text())
    if now - float(report["at"]) > 2 * 86400:
        return ["每日仓库巡检回执已超过 48 小时未更新"]
    rows = report["results"]
    failed = sum("失败" in row["reason"] for row in rows)
    legacy = [row for row in rows if row["reason"].startswith("旧存量仅列清单")]
    issues = [f"仓库巡检有 {failed} 项检查失败，成果已保留"] if failed else []
    if legacy:
        # 不输出本地路径、文件名或任意回执正文；散列只用于对照本机清单，不携带凭据。
        identifiers = [hashlib.sha256(row["path"].encode()).hexdigest()[:10] for row in legacy]
        issues.append(f"待人工确认的旧开发工作树 {len(legacy)} 个；清单 ID：" + "、".join(identifiers[:20]))
    return issues


def storage_issues(data: Path, now: float) -> list[str]:
    path = data / "storage-maintenance.json"
    if not path.exists():
        return ["尚无每日存储养护回执，请核对服务版本和巡检是否启用"]
    if path.is_symlink():
        raise ValueError("存储回执路径异常")
    report = json.loads(path.read_text())
    age = now - float(report["at"])
    if not 0 <= age <= 2 * 86400:
        return ["每日存储养护回执已过期或时间异常"]
    issues = []
    for label in ("附件", "恢复备份", "数据库", "日志", "提示词缓存", "视频预览", "工作树"):
        size = report["sizes"].get(label)
        if size is None:
            continue
        if size >= report["limits"][label]:
            issues.append(f"{label}容量偏高：{size / 1048576:.0f} MiB；请审核保留策略，不自动删除成果")
        samples = [row for row in report.get("samples", [])
                   if 20 * 3600 <= now - row["at"] <= 8 * 86400 and label in row["sizes"]]
        if samples:
            previous = min(samples, key=lambda row: abs(now - row["at"] - 7 * 86400))
            growth = size - previous["sizes"][label]
            if growth >= 256 * 1048576 and growth >= previous["sizes"][label] * 0.2:
                issues.append(f"{label}增长偏快：近 {(now - previous['at']) / 86400:.1f} 天新增 {growth / 1048576:.0f} MiB；需人工审核")
    if report.get("errors"):
        issues.append(f"存储养护有 {len(report['errors'])} 项检查或回收失败；未处理内容已保留")
    return issues


def diagnose(root: Path, data: Path, *, max_worktrees: int = 30) -> list[str]:
    now = time.time()
    probes = {"机器状态": lambda: machine_issues(root),
              "运行心跳": lambda: heartbeat_issues(data, now),
              "任务状态": lambda: task_issues(data, now),
              "工作树": lambda: worktree_issues(root, data, max_worktrees),
              "回收巡检": lambda: cleanup_issues(data, now),
              "存储养护": lambda: storage_issues(data, now)}
    issues = []
    for label, probe in probes.items():
        try:
            issues.extend(probe())
        except Exception as exc:
            # 命令异常可能带配置/凭据，不输出 stderr 或异常原文。
            issues.append(f"{label}检查失败：{type(exc).__name__}；未执行任何修复或删除")
    return issues


def render(issues: list[str]) -> str:
    if not issues:
        return "##CRON_SIGNAL:0##"
    return "\n".join(["运行机器 / 开发工作树诊断异常", *(f"- {issue}" for issue in issues),
                      "请人工确认上述异常；启用通知时已登记到铃铛。未经确认，不执行修复、删除或重启。",
                      "##CRON_SIGNAL:1##"])


def notify_decisions(issues: list[str], agent_id: str, root: Path) -> None:
    # 可选通知仅登记拍板，不执行修复；默认诊断保持只读。
    sys.path.insert(0, str(root))
    from vococo.memory import agents, notices

    owner = agents.get(agent_id)
    if not owner:
        raise ValueError("诊断归属 Agent 不存在")
    for issue in issues:
        category = re.sub(r"\d+(?:\.\d+)?", "N", issue.split("：", 1)[0])
        notices.add(session_key=agents.main_session_key(owner), kind="decision", status="expired",
                    reason=f"机器诊断：{category}", detail=category,
                    prompt=f"每日诊断发现：{issue}\n尚未判断根因，未修改、删除或重启。建议先只读排查，再给出处理方案。",
                    options=["安排只读排查", "给出处理方案", "暂时观察"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--max-worktrees", type=int, default=30)
    parser.add_argument("--notify-agent", help="异常登记到指定 Agent 的铃铛；省略时只读")
    args = parser.parse_args()
    if args.max_worktrees < 1:
        parser.error("工作树阈值必须大于零")
    root = args.root.expanduser().resolve()
    data = (args.data_dir or root / "data").expanduser().resolve()
    issues = diagnose(root, data, max_worktrees=args.max_worktrees)
    if issues and args.notify_agent:
        try:
            notify_decisions(issues, args.notify_agent, root)
        except Exception as exc:
            issues.append(f"诊断通知登记失败：{type(exc).__name__}；请人工查看诊断结果")
    print(render(issues))


if __name__ == "__main__":
    main()
