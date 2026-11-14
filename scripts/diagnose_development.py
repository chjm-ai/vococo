#!/usr/bin/env python3
"""机器及开发工作树只读诊断；正常静默，确有风险时才汇总成一条铃铛等人工决定。

去噪与销账约定（2026-11-14）：
- 已确认保留、且不再变化的存量记入 data/development-diagnostics.json，不再每天要求拍板；
  失效绑定和旧存量清单只报数量变化，不逐个罗列。
- 容量只在“增长速度偏快”或“磁盘剩余会因此吃紧”时报警，不因超过固定提醒线就报。
- 问题消失、存量确认保留后，自动关闭对应旧提醒；未经确认不删除目录、分支或附件原件。
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time

_MIB = 1048576
_GIB = 1073741824
# 容量组拆开算：业务副本是否正常占用不该让工作树单独背锅。
_CAPACITY_GROUPS = {"附件": ("附件",), "工作树": ("工作树", "业务副本")}
_REASON_PREFIX = "机器诊断："
_FACT = re.compile(r"：(\D*?)\s*(\d+)")


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


class Issues(list):
    """异常清单；group 记来源分组，用于生成稳定的提醒分类，不参与文字输出。"""

    def __init__(self, items=()):
        super().__init__(items)
        self.groups: dict[str, str] = {}

    def add(self, text: str, group: str) -> None:
        self.append(text)
        self.groups[text] = group


def machine_issues(root: Path) -> list[str]:
    issues = []
    disk = shutil.disk_usage(root)
    free_gb = disk.free / _GIB
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


def _worktree_inventory(roots: list[Path], bindings: set[str], data: Path) -> dict:
    total, orphaned, broken = set(), set(), set()
    unreadable = 0
    for root in roots:
        if not (root / ".git").exists():
            continue
        try:
            rows = worktree_listing(root)[1:]
        except (OSError, ValueError, subprocess.SubprocessError):
            # 读不了就少统计了这个仓库，必须报出来，不能让存量“看起来减少了”。
            unreadable += 1
            continue
        for row in rows:
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
    return {"total": len(total), "orphaned": len(orphaned), "broken": len(broken), "unreadable": unreadable}


def worktree_issues(root: Path, data: Path, max_worktrees: int) -> tuple[list[str], list[str]]:
    """返回 (每次都要人看的异常, 只在变化时报告一次的存量)。"""
    with readonly_database(data / "state.db") as connection:
        roots = {root, *(Path(row[0]).expanduser().resolve()
                         for row in connection.execute("SELECT path FROM projects"))}
        bindings = {row[0] for row in connection.execute(
            "SELECT worktree_path FROM session_meta WHERE worktree_path IS NOT NULL AND worktree_path!=''")}
    inventory = _worktree_inventory(sorted(roots), bindings, data)
    issues = []
    if inventory["total"] >= max_worktrees:
        issues.append(f"开发工作树数量偏多：{inventory['total']} 个（阈值 {max_worktrees}）")
    if inventory["orphaned"]:
        issues.append(f"发现 {inventory['orphaned']} 个无会话绑定的受管工作树；仅列清单，不自动删除")
    if inventory["unreadable"]:
        issues.append(f"工作树统计可能不全：有 {inventory['unreadable']} 个项目仓库读取工作树登记失败，请只读排查")
    facts = [f"治理存量记录：失效/异常工作树绑定或 Git 登记 {inventory['broken']} 个（不改动）",
             f"治理存量记录：无会话绑定的受管工作树 {inventory['orphaned']} 个（不改动）",
             f"治理存量记录：开发工作树 {inventory['total']} 个"]
    return issues, facts


def cleanup_issues(data: Path, now: float) -> tuple[list[str], list[str]]:
    path = data / "repository-cleanup.json"
    if not path.exists():
        return ["尚无每日仓库巡检回执，请核对服务版本和巡检是否启用"], []
    if path.is_symlink():
        raise ValueError("巡检回执路径异常")
    report = json.loads(path.read_text())
    if now - float(report["at"]) > 2 * 86400:
        return ["每日仓库巡检回执已超过 48 小时未更新"], []
    rows = report["results"]
    failed = sum("失败" in row["reason"] for row in rows)
    legacy = [row for row in rows if row["reason"].startswith("旧存量仅列清单")]
    issues = [f"仓库巡检有 {failed} 项检查失败，成果已保留"] if failed else []
    facts = []
    if legacy:
        # 只报数量变化，不逐个列 ID；确认保留后不再每天要求拍板。
        digest = hashlib.sha256("\0".join(sorted(row["path"] for row in legacy)).encode()).hexdigest()[:10]
        facts.append(f"治理存量记录：待人工确认的旧开发工作树 {len(legacy)} 个（组 {digest}）")
    return issues, facts


def _capacity_issues(report: dict, now: float, root: Path) -> list[str]:
    """容量看增长速度和磁盘剩余：超过固定提醒线本身不算异常。"""
    issues = []
    sizes, limits = report["sizes"], report["limits"]
    samples = report.get("samples", [])
    disk = shutil.disk_usage(root)
    free, total = disk.free, disk.total
    for label, members in _CAPACITY_GROUPS.items():
        # 上游把业务副本并进了“工作树”，没有单独的“业务副本”键；有就加，没有就只算工作树。
        if any(sizes.get(name) is None for name in members if name != "业务副本"):
            continue
        members = [name for name in members if sizes.get(name) is not None]
        size = sum(sizes[name] for name in members)
        limit = max(sum(limits.get(name, 0) for name in members) or 2048 * _MIB, 256 * _MIB)
        daily = 0
        # 以回执本身的采样为准：拿最早的一份（最多 30 天前）对比，慢增长才看得出趋势。
        previous = [row for row in samples
                    if 20 * 3600 <= now - row["at"] <= 31 * 86400
                    and all(name in row["sizes"] for name in members)]
        if previous:
            old = previous[0]
            old_size = sum(old["sizes"][name] for name in members)
            growth = size - old_size
            days = (now - old["at"]) / 86400
            if growth >= 256 * _MIB and growth >= old_size * 0.2:
                issues.append(f"{label}增长偏快：近 {days:.1f} 天新增 {growth / _MIB:.0f} MiB；需人工审核")
            daily = growth / days
        if size < max(3 * limit, 10 * _GIB):
            continue
        # 按日均增速外推，看会不会在半年内把磁盘余量吃光（扣除 5% 系统缓冲）。
        days_left = (free - 0.05 * total) / daily if daily > 0 else float("inf")
        if size >= 0.15 * total or days_left < 180:
            issues.append(f"{label}占用较大：{size / _GIB:.1f} GiB，磁盘仅剩 {free / _GIB:.1f} GiB；"
                          "按当前增速会挤占运行空间，需审核保留策略，不自动删除成果")
    return issues


def storage_issues(data: Path, now: float, root: Path | None = None) -> list[str]:
    path = data / "storage-maintenance.json"
    if not path.exists():
        return ["尚无每日存储养护回执，请核对服务版本和巡检是否启用"]
    if path.is_symlink():
        raise ValueError("存储回执路径异常")
    report = json.loads(path.read_text())
    age = now - float(report["at"])
    if not 0 <= age <= 2 * 86400:
        return ["每日存储养护回执已过期或时间异常"]
    issues = _capacity_issues(report, now, root or data)
    for label in ("恢复备份", "数据库", "日志", "提示词缓存", "视频预览"):
        size = report["sizes"].get(label)
        if size is None:
            continue
        if size >= report["limits"][label]:
            issues.append(f"{label}容量偏高：{size / _MIB:.0f} MiB；请审核保留策略，不自动删除成果")
        samples = [row for row in report.get("samples", [])
                   if 20 * 3600 <= now - row["at"] <= 8 * 86400 and label in row["sizes"]]
        if samples:
            previous = min(samples, key=lambda row: abs(now - row["at"] - 7 * 86400))
            growth = size - previous["sizes"][label]
            if growth >= 256 * _MIB and growth >= previous["sizes"][label] * 0.2:
                issues.append(f"{label}增长偏快：近 {(now - previous['at']) / 86400:.1f} 天新增 "
                              f"{growth / _MIB:.0f} MiB；需人工审核")
    if report.get("errors"):
        issues.append(f"存储养护有 {len(report['errors'])} 项检查或回收失败；未处理内容已保留")
    return issues


# 每条异常归到固定分类，作为铃铛去重键；未列出的按前缀截取。
_CLASSIFY = (
    ("心跳", ("运行心跳",)),
    ("任务滞留", ("运行/排队任务",)),
    ("工作树检查失败", ("工作树检查失败",)),
    ("工作树", ("工作树", "无会话绑定")),
    ("磁盘", ("磁盘空间",)),
    ("负载", ("负载偏高",)),
    ("内存", ("内存压力",)),
    ("巡检失败", ("检查失败", "验证未通过", "尚未初始化", "回收失败", "超过 48 小时未更新", "尚无每日")),
    ("容量", ("容量", "增长偏快", "占用较大")),
)


def classify(issue: str) -> str:
    for label, prefixes in _CLASSIFY:
        if issue.startswith(prefixes):
            return label
    return issue.split("：", 1)[0][:12]


def _state_path(data) -> Path:
    return Path(data) / "development-diagnostics.json"


def _load_state(data) -> dict:
    path = _state_path(data)
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_state(data, state: dict, now: float) -> None:
    # 账本写不进去不影响诊断本身，只是明天还会再报一次；路径异常同样不溯源。
    path = _state_path(data)
    try:
        path.write_text(json.dumps({**state, "at": now}, ensure_ascii=False, indent=2))
    except OSError:
        pass


def _compare(facts: list[str], previous: dict) -> list[str]:
    """存量只在数量变化时报告一次；没变化的存量不进异常清单。状态里按标签记数字。"""
    issues = []
    current = {}
    for fact in facts:
        match = _FACT.search(fact)
        if not match:
            continue
        label, count = match.group(1).strip(), int(match.group(2))
        # 按标签对上一条旧记录：数量变了文字也变，不能拿整句当键。
        previous = previous if isinstance(previous, dict) else {}
        old = previous.get(label, previous.get(fact))
        current[label] = count
        if old is None:
            if previous:
                # 首次出现的新存量清单：只计数量变化，不整体报一遍。
                issues.append(f"{label}存量变化：0 → {count}")
        elif old != count:
            direction = "增加" if count > old else "减少"
            issues.append(f"{label}存量{direction}：{old} → {count}")
    if isinstance(previous, dict):
        # 原地更新成最新数字，下次直接按标签比对；旧版本整句键在下一次比对后自然淘汰。
        previous.clear()
        previous.update(current)
    return issues


def diagnose(root: Path, data: Path, *, max_worktrees: int = 30,
             state: dict | None = None) -> list[str]:
    now = time.time()
    if state is None:
        state = _load_state(data)
    issues = Issues()

    def add_all(values, group: str) -> None:
        for value in values:
            issues.add(value, group)

    probes = {"机器状态": lambda: machine_issues(root),
              "运行心跳": lambda: heartbeat_issues(data, now),
              "任务状态": lambda: task_issues(data, now)}
    for label, probe in probes.items():
        try:
            add_all(probe(), "异常")
        except Exception as exc:
            # 命令异常可能带配置/凭据，不输出 stderr 或异常原文。
            issues.add(f"{label}检查失败：{type(exc).__name__}；未执行任何修复或删除", "异常")
    try:
        found, facts = worktree_issues(root, data, max_worktrees)
        add_all(found, "异常")
        add_all(_compare(facts, state.setdefault("worktrees", {})), "异常")
    except Exception as exc:
        issues.add(f"工作树检查失败：{type(exc).__name__}；未执行任何修复或删除", "异常")
    try:
        found, facts = cleanup_issues(data, now)
        add_all(found, "异常")
        add_all(_compare(facts, state.setdefault("cleanup", {})), "异常")
    except Exception as exc:
        issues.add(f"回收巡检检查失败：{type(exc).__name__}；未执行任何修复或删除", "异常")
    try:
        add_all(storage_issues(data, now, root), "容量")
    except Exception as exc:
        issues.add(f"存储养护检查失败：{type(exc).__name__}；未执行任何修复或删除", "异常")
    return issues


def render(issues: list[str]) -> str:
    if not issues:
        return "##CRON_SIGNAL:0##"
    return "\n".join(["运行机器 / 开发工作树诊断异常", *(f"- {issue}" for issue in issues),
                      "同次巡检已汇总为一条通知；确需决定的已登记到铃铛，请人工确认。未经确认，不执行修复、删除或重启。",
                      "##CRON_SIGNAL:1##"])


def _keep_state(state: dict) -> dict:
    return {key: value for key, value in state.items() if key != "at"}


def reconcile(issues: list[str], agent_id: str, root: Path, data, now: float | None = None) -> list[str]:
    """同次巡检只留一条提醒、只登记需要拍板的；问题消失后自动关闭旧提醒。"""
    now = time.time() if now is None else now
    state = _load_state(data)
    sys.path.insert(0, str(root))
    from vococo.memory import agents, notices

    owner = agents.get(agent_id)
    if not owner:
        raise ValueError("诊断归属 Agent 不存在")
    session_key = agents.main_session_key(owner)
    open_rows = [row for row in notices.list_open(1000)
                 if row["kind"] == "decision" and row["reason"].startswith(_REASON_PREFIX)
                 and row["session_key"] == session_key]
    if issues:
        reason = f"{_REASON_PREFIX}{_notice_detail(issues)}"
        notices.add(session_key=session_key, kind="decision", status="expired", reason=reason,
                    detail=reason,
                    prompt=("每日诊断汇总发现：" + "\n".join(f"- {issue}" for issue in issues)
                            + "\n尚未判断根因，未修改、删除或重启。建议先只读排查，再给出处理方案。"),
                    options=["安排只读排查", "给出处理方案", "暂时观察"])
        # 换了分类才新开提醒；旧分类同时关掉，铃铛始终只挂一条。
        pending = [row for row in open_rows if row["session_key"] == session_key
                   and row["reason"] == reason and row["status"] == "expired"]
    else:
        pending = []
        # 没异常但存量有变化：结果写进状态文件，不进铃铛（不需要拍板）。
        for row in open_rows:
            detail = row["prompt"].split("\n", 1)[1].split("\n尚未判断根因")[0] if "\n" in row["prompt"] else ""
            if "存量" not in detail:
                continue
            state.setdefault("changes", []).append({"at": now, "detail": detail, "text": "已复查，无需拍板"})
        state["changes"] = state.get("changes", [])[-30:]
        _save_state(data, _keep_state(state), now)
    # 本次仍存在的提醒保留；其余（含同会话旧分类、已解决分类）一律销账。
    keep = {row["id"] for row in pending}
    closed = []
    for row in open_rows:
        if row["id"] in keep or row["status"] == "pending":
            continue  # 正在等的提问不替主人关
        notices.set_status(row["id"], "dismissed",
                           "每日复查确认已无该问题，自动关闭（" + time.strftime("%Y-%m-%d %H:%M", time.localtime(now)) + "）")
        closed.append(row["reason"][len(_REASON_PREFIX):] or row["reason"])
    return closed


def _notice_detail(issues: list[str]) -> str:
    labels = []
    for issue in issues:
        label = classify(issue)
        if label not in labels:
            labels.append(label)
    return "、".join(labels)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--max-worktrees", type=int, default=30)
    parser.add_argument("--notify-agent", help="确需拍板的异常登记到指定 Agent 的铃铛；省略时只读")
    args = parser.parse_args()
    if args.max_worktrees < 1:
        parser.error("工作树阈值必须大于零")
    root = args.root.expanduser().resolve()
    data = (args.data_dir or root / "data").expanduser().resolve()
    now = time.time()
    state = _load_state(data)
    issues = diagnose(root, data, max_worktrees=args.max_worktrees, state=state)
    _save_state(data, _keep_state(state), now)
    lines = list(issues)
    closed = []
    if args.notify_agent:
        try:
            closed = reconcile(issues, args.notify_agent, root, data, now)
        except Exception as exc:
            lines.append(f"诊断通知登记失败：{type(exc).__name__}；请人工查看诊断结果")
    output = render(lines)
    if closed:
        output = output.replace("##CRON_SIGNAL:",
                                "已自动关闭旧提醒：" + "、".join(closed) + "\n##CRON_SIGNAL:", 1)
    print(output)


if __name__ == "__main__":
    main()
