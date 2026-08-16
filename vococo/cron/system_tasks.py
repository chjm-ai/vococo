"""本机 macOS 定时系统任务只读枚举 —— 给「定时」Tab 的「本机系统任务」区块展示用。

跟 cron/scheduler.py 管理的 vococo 自身任务是两个概念:那边是 vococo 起的 AI 会话
任务(存在 cron_jobs.json,能增删改);这里读的是操作系统本来就在跑的东西
(launchd plist + crontab),纯只读展示,不提供增删改——用户要改就去改 plist/crontab
本身,这里只是让"我以为在跑的脚本是不是真的还在跑"这件事不用开终端就能核对。

识别"是任务"的标准(两种类型,都是本来就在跑的东西,vococo 只负责认出来+展示,
不负责触发执行——触发这件事交给 launchd/cron 本身做,不重新造一个执行引擎):
- launchd 里两类都收:
  ① scheduled(调度类):plist 带 StartInterval 或 StartCalendarInterval 键
  ② resident(常驻类):plist 带 KeepAlive(不含调度周期,长期活着的进程,比如
     watchdog 式的文件监听daemon)
  两类都再过一道路径过滤:命令/脚本路径落在用户目录下、但不在 ~/Library/ 里——
  单靠"有调度周期/KeepAlive"滤不掉 Adobe/Google 这类厂商自己装的常驻或定时 agent
  (实测 com.adobe.GC.Scheduler-1.0、com.google.GoogleUpdater.wake 都带
  StartInterval/StartCalendarInterval),它们的可执行文件要么在 /Library/…,
  要么在 ~/Library/Application Support/…,用这条就能跟用户自己写在 ~/scripts、
  ~/.local/bin 等地方的脚本分开,不用维护厂商黑名单
- crontab:crontab -l 里的活跃(非注释)行本身就是定时定义,恒为 scheduled 类型;
  注释掉的行不当"已禁用任务"处理,直接忽略——crontab 里的注释混杂着大量纯说明
  文字,没法可靠区分

task 字典结构:
{
  "id": "launchd:<Label>" 或 "cron:<8位hash>",
  "source": "launchd" | "cron",
  "task_type": "scheduled" | "resident",  # resident 只会来自 launchd(KeepAlive);
                                            # cron 恒为 scheduled
  "name": str,                 # launchd 用 Label;cron 用紧邻的上一行注释,没有则退到脚本名/命令片段
  "schedule_desc": str,        # scheduled:人话周期描述或 cron 原始 5 段表达式;resident:固定"常驻"
  "command": str,               # 完整命令行
  "script_path": str | None,    # 命令里识别出的脚本文件路径(.sh/.py/... 结尾的绝对路径)
  "log_paths": [str, ...],      # 0~2 个日志文件路径(launchd 的 stdout/stderr,或 crontab 里 >> 的目标)
  "enabled": bool,               # launchd:是否被 launchctl 加载;cron:恒 True(能读到即代表已装载)
  "running": bool | None,        # launchd 独有,launchctl list 里 PID 是否为数字;cron 恒 None。
                                  # 注意 scheduled 类型 running=False 是常态(等下次触发,不代表异常);
                                  # resident 类型 running=False 才是异常(该常驻的进程不在了),前端按
                                  # task_type 分别解读,这里只如实记录事实
  "last_exit_code": int | None,  # launchd 独有:launchctl list 里的 Status 列(上次退出码)
  "last_run_at": float | None,   # 近似值:取日志文件里最新的 mtime
}
"""
from __future__ import annotations

import hashlib
import plistlib
import re
import socket
import subprocess
from pathlib import Path

_HOME = Path.home()  # 模块级常量,测试用 monkeypatch 换成假目录(见 test_system_tasks.py)
_LAUNCH_AGENTS_DIR = _HOME / "Library" / "LaunchAgents"
_SCRIPT_READ_MAX = 200 * 1024  # 脚本预览读取上限,超过就不读(不太可能是"脚本"了)
_LOG_TAIL_LINES = 100
_SCRIPT_EXTS = (".sh", ".py", ".zsh", ".bash", ".js", ".mjs", ".rb", ".pl", ".command")
_REDIRECT_RE = re.compile(r">>?\s*(/\S+)")
_WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"]

# launchd Label → 中文显示名;未收录的 label 原样展示
_DISPLAY_NAMES: dict[str, str] = {
    "com.claude-to-im.bridge": "Claude 消息桥接",
    "com.cloudflared-obsidian": "Cloudflared 隧道",
    "com.cloudflared-health": "隧道健康检查",
    "com.wesley.calctld": "日历权限守护",
    "com.wesley.cli-proxy-api": "AI 代理转发",
    "com.wesley.personal-audio": "语音技能守护",
    "com.wesley.icloud-monitor": "iCloud 同步监控",
    "com.wesley.xiaoyuzhou-monitor": "小宇宙播客监控",
}


def hostname() -> str:
    name = socket.gethostname()
    return name.removesuffix(".local") if name else name


def list_tasks() -> list[dict]:
    tasks = _list_launchd_tasks() + _list_cron_tasks()
    tasks.sort(key=lambda t: (t["task_type"], t["source"], t["name"]))
    return tasks


def task_detail(task_id: str) -> dict | None:
    task = next((t for t in list_tasks() if t["id"] == task_id), None)
    if task is None:
        return None
    detail = dict(task)
    detail["script_content"], detail["script_error"] = _read_script(task.get("script_path"))
    detail["logs"] = {p: _tail_file(p) for p in task.get("log_paths") or []}
    return detail


# ── launchd ──────────────────────────────────────────────────────────────


def _list_launchd_tasks() -> list[dict]:
    if not _LAUNCH_AGENTS_DIR.is_dir():
        return []
    status_map = _launchctl_status()
    out = []
    for p in sorted(_LAUNCH_AGENTS_DIR.glob("*.plist")):
        try:
            with p.open("rb") as f:
                plist = plistlib.load(f)
        except Exception:
            continue
        is_scheduled = "StartCalendarInterval" in plist or "StartInterval" in plist
        is_resident = bool(plist.get("KeepAlive"))
        if not is_scheduled and not is_resident:
            continue  # 既没调度周期也不常驻(比如纯 RunAtLoad 登录脚本),不是要监控的对象

        args = [str(a) for a in (plist.get("ProgramArguments") or [])]
        script_tokens = args if args else ([str(plist["Program"])] if plist.get("Program") else [])
        if not _is_personal_path(script_tokens):
            continue  # 厂商自己装的定时/常驻 agent(Adobe/Google 等),不是用户自己的自动化脚本

        label = plist.get("Label") or p.stem
        task_type = "scheduled" if is_scheduled else "resident"
        schedule_desc = _describe_schedule(plist) if is_scheduled else "常驻"
        command = " ".join(args) if args else str(plist.get("Program") or "")
        log_paths = [
            lp for lp in (plist.get("StandardOutPath"), plist.get("StandardErrorPath"))
            if lp
        ]
        log_paths = list(dict.fromkeys(log_paths))  # 去重且保序(stdout/stderr 常指向同一文件)

        pid, code = status_map.get(label, (None, None))
        loaded = label in status_map
        out.append({
            "id": f"launchd:{label}",
            "source": "launchd",
            "task_type": task_type,
            "name": _DISPLAY_NAMES.get(label, label),
            "schedule_desc": schedule_desc,
            "command": command,
            "script_path": _extract_script_path(script_tokens),
            "log_paths": log_paths,
            "enabled": loaded,
            "running": (pid not in (None, "-")) if loaded else None,
            "last_exit_code": int(code) if (loaded and code not in (None, "-")) else None,
            "last_run_at": _latest_mtime(log_paths),
        })
    return out


def _describe_schedule(plist: dict) -> str:
    if "StartCalendarInterval" in plist:
        sci = plist["StartCalendarInterval"]
        entries = sci if isinstance(sci, list) else [sci]
        return " , ".join(_describe_calendar_entry(e) for e in entries if isinstance(e, dict))
    secs = plist.get("StartInterval")
    try:
        secs = int(secs)
    except (TypeError, ValueError):
        return "?"
    if secs and secs % 60 == 0:
        return f"每 {secs // 60} 分钟"
    return f"每 {secs} 秒"


def _describe_calendar_entry(e: dict) -> str:
    parts = []
    if "Weekday" in e:
        parts.append(f"每周{_WEEKDAYS[int(e['Weekday']) % 7]}")
    if "Day" in e:
        parts.append(f"每月{e['Day']}日")
    if "Month" in e:
        parts.append(f"{e['Month']}月")
    has_hour, has_minute = "Hour" in e, "Minute" in e
    if has_hour and has_minute:
        if not parts:
            parts.append("每天")
        parts.append(f"{int(e['Hour']):02d}:{int(e['Minute']):02d}")
    elif has_minute:
        parts.append(f"每小时第{int(e['Minute'])}分")
    elif has_hour:
        parts.append(f"每天 {int(e['Hour']):02d} 点")
    return " ".join(parts) if parts else "按日历调度"


def _read_launchctl_text() -> str:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout if out.returncode == 0 else ""


def _launchctl_status() -> dict[str, tuple[str, str]]:
    status: dict[str, tuple[str, str]] = {}
    lines = _read_launchctl_text().splitlines()
    for line in lines[1:]:  # 第一行是表头 "PID  Status  Label"
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid, code, label = parts
        status[label] = (pid, code)
    return status


# ── crontab ──────────────────────────────────────────────────────────────


def _read_crontab_text() -> str:
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout if out.returncode == 0 else ""


def _list_cron_tasks() -> list[dict]:
    tasks = []
    pending_name = ""
    for raw in _read_crontab_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            pending_name = line.lstrip("#").strip()
            continue
        tokens = line.split(None, 5)
        if len(tokens) < 6:  # 不是"5段调度 + 命令"的活跃任务行(环境变量赋值等)—— 忽略且不当注释用
            pending_name = ""
            continue
        schedule_fields, command = tokens[:5], tokens[5]
        m = _REDIRECT_RE.search(command)
        log_paths = [m.group(1)] if m else []
        script_path = _extract_script_path(command.split())
        name = pending_name or (Path(script_path).stem if script_path else command[:24])
        task_hash = hashlib.md5(line.encode("utf-8")).hexdigest()[:8]
        tasks.append({
            "id": f"cron:{task_hash}",
            "source": "cron",
            "task_type": "scheduled",
            "name": name,
            "schedule_desc": " ".join(schedule_fields),
            "command": command,
            "script_path": script_path,
            "log_paths": log_paths,
            "enabled": True,
            "running": None,
            "last_exit_code": None,
            "last_run_at": _latest_mtime(log_paths),
        })
        pending_name = ""
    return tasks


# ── 共用 ─────────────────────────────────────────────────────────────────


def _is_personal_path(tokens: list[str]) -> bool:
    home = str(_HOME)
    lib_prefix = home + "/Library/"
    return any(
        tok.startswith(home + "/") and not tok.startswith(lib_prefix)
        for tok in tokens
    )


def _extract_script_path(tokens: list[str]) -> str | None:
    for tok in tokens:
        if tok.startswith("/") and tok.endswith(_SCRIPT_EXTS):
            return tok
    return None


def _latest_mtime(paths: list[str]) -> float | None:
    best = None
    for p in paths:
        try:
            mt = Path(p).stat().st_mtime
        except OSError:
            continue
        if best is None or mt > best:
            best = mt
    return best


def _read_script(path: str | None) -> tuple[str | None, str | None]:
    if not path:
        return None, None
    p = Path(path)
    try:
        if not p.is_file():
            return None, "文件不存在"
        size = p.stat().st_size
        if size > _SCRIPT_READ_MAX:
            return None, f"脚本过大({size // 1024}KB),跳过预览"
        return p.read_text(encoding="utf-8", errors="replace"), None
    except OSError as exc:
        return None, str(exc)


def _tail_file(path: str, n: int = _LOG_TAIL_LINES) -> str:
    p = Path(path)
    try:
        if not p.is_file():
            return "(日志文件不存在)"
        with p.open("rb") as f:
            f.seek(0, 2)
            remaining = f.tell()
            block = 8192
            data = b""
            while remaining > 0 and data.count(b"\n") <= n:
                step = min(block, remaining)
                remaining -= step
                f.seek(remaining)
                data = f.read(step) + data
        lines = data.decode("utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError as exc:
        return f"(读取失败:{exc})"
