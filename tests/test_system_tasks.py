"""本机系统任务只读枚举的测试(见 vococo/cron/system_tasks.py 模块头)。

不碰真实机器的 launchd/crontab:_HOME、_LAUNCH_AGENTS_DIR、_read_crontab_text、
_read_launchctl_text 都是模块级可替换的"读入口",测试直接换成造好的假数据
(同 tests/test_cron_scheduler.py 用 monkeypatch 换 config.CRON_JOBS_PATH 的手法)。
"""
from __future__ import annotations

import plistlib

import pytest

from vococo.cron import system_tasks as st


def _write_plist(dir_, filename, data):
    path = dir_ / filename
    with path.open("wb") as f:
        plistlib.dump(data, f)
    return path


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    launch_dir = home / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True)
    monkeypatch.setattr(st, "_HOME", home)
    monkeypatch.setattr(st, "_LAUNCH_AGENTS_DIR", launch_dir)
    monkeypatch.setattr(st, "_read_launchctl_text", lambda: "")
    monkeypatch.setattr(st, "_read_crontab_text", lambda: "")
    return home, launch_dir


# ── launchd ──────────────────────────────────────────────────────────────


def test_launchd_keepalive_daemon_included_as_resident(fake_env):
    """KeepAlive 常驻进程(比如 watchdog 式文件监听)算「resident」类型,跟「scheduled」
    类型一起收进来——不用为了"接入监控"把这类脚本改造成定时跑一次就退出的形式。"""
    home, launch_dir = fake_env
    _write_plist(launch_dir, "com.wesley.daemon.plist", {
        "Label": "com.wesley.daemon",
        "ProgramArguments": [str(home / "scripts" / "daemon.sh")],
        "KeepAlive": True,
        "RunAtLoad": True,
    })
    tasks = st.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["task_type"] == "resident"
    assert tasks[0]["schedule_desc"] == "常驻"


def test_launchd_pure_runatload_without_keepalive_or_schedule_excluded(fake_env):
    """既没有调度周期、也不是 KeepAlive 常驻——纯"登录时跑一次"的脚本,不是要监控的对象。"""
    home, launch_dir = fake_env
    _write_plist(launch_dir, "com.wesley.login-once.plist", {
        "Label": "com.wesley.login-once",
        "ProgramArguments": [str(home / "scripts" / "login-once.sh")],
        "RunAtLoad": True,
    })
    assert st.list_tasks() == []


def test_launchd_vendor_updater_excluded_despite_schedule(fake_env):
    """Adobe/Google 这类厂商更新器也带 StartInterval,得靠路径过滤挡住(实测过的真坑)。"""
    home, launch_dir = fake_env
    _write_plist(launch_dir, "com.adobe.GC.Scheduler-1.0.plist", {
        "Label": "com.adobe.GC.Scheduler-1.0",
        "ProgramArguments": ["/Library/Application Support/Adobe/AGC/util", "-mode=scheduled"],
        "StartCalendarInterval": {"Hour": 4, "Minute": 48},
    })
    _write_plist(launch_dir, "com.google.updater.plist", {
        "Label": "com.google.updater",
        "ProgramArguments": [str(home / "Library" / "Application Support" / "Google" / "upd")],
        "StartInterval": 3600,
    })
    assert st.list_tasks() == []


def test_launchd_vendor_keepalive_daemon_excluded_too(fake_env):
    """路径过滤对 resident 类型同样生效,不只是 scheduled 类型。"""
    _, launch_dir = fake_env
    _write_plist(launch_dir, "com.google.keepalive.plist", {
        "Label": "com.google.keepalive",
        "ProgramArguments": ["/Library/Application Support/Google/helper"],
        "KeepAlive": True,
    })
    assert st.list_tasks() == []


def test_launchd_resident_running_status_from_launchctl(fake_env, monkeypatch):
    home, launch_dir = fake_env
    _write_plist(launch_dir, "com.wesley.watcher.plist", {
        "Label": "com.wesley.watcher",
        "ProgramArguments": [str(home / "scripts" / "watcher.py")],
        "KeepAlive": True,
        "StandardOutPath": "/tmp/watcher.log",
    })
    monkeypatch.setattr(
        st, "_read_launchctl_text",
        lambda: "PID\tStatus\tLabel\n4321\t0\tcom.wesley.watcher\n",
    )
    t = st.list_tasks()[0]
    assert t["task_type"] == "resident"
    assert t["enabled"] is True
    assert t["running"] is True

    # 常驻进程挂了(launchctl 里查不到 PID,只剩上次退出码)—— running=False 对
    # resident 类型来说才是异常信号,跟 scheduled 类型的"等下次触发"语义不同
    monkeypatch.setattr(
        st, "_read_launchctl_text",
        lambda: "PID\tStatus\tLabel\n-\t139\tcom.wesley.watcher\n",
    )
    t = st.list_tasks()[0]
    assert t["running"] is False
    assert t["last_exit_code"] == 139


def test_launchd_personal_script_with_calendar_interval_included(fake_env):
    home, launch_dir = fake_env
    script = home / "scripts" / "daily.sh"
    _write_plist(launch_dir, "com.wesley.daily.plist", {
        "Label": "com.wesley.daily",
        "ProgramArguments": ["/bin/bash", str(script)],
        "StartCalendarInterval": {"Hour": 10, "Minute": 10},
        "StandardOutPath": "/tmp/daily.out",
        "StandardErrorPath": "/tmp/daily.err",
    })
    tasks = st.list_tasks()
    assert len(tasks) == 1
    t = tasks[0]
    assert t["id"] == "launchd:com.wesley.daily"
    assert t["source"] == "launchd"
    assert t["schedule_desc"] == "每天 10:10"
    assert t["script_path"] == str(script)
    assert t["log_paths"] == ["/tmp/daily.out", "/tmp/daily.err"]
    assert t["enabled"] is False  # launchctl list 是空文本 → 没加载


def test_launchd_start_interval_seconds_description(fake_env):
    home, launch_dir = fake_env
    _write_plist(launch_dir, "com.wesley.poll.plist", {
        "Label": "com.wesley.poll",
        "ProgramArguments": [str(home / "scripts" / "poll.sh")],
        "StartInterval": 150,
    })
    tasks = st.list_tasks()
    assert tasks[0]["schedule_desc"] == "每 150 秒"


def test_launchd_status_cross_referenced_from_launchctl_list(fake_env, monkeypatch):
    home, launch_dir = fake_env
    _write_plist(launch_dir, "com.wesley.daily.plist", {
        "Label": "com.wesley.daily",
        "ProgramArguments": [str(home / "scripts" / "daily.sh")],
        "StartInterval": 300,
    })
    monkeypatch.setattr(
        st, "_read_launchctl_text",
        lambda: "PID\tStatus\tLabel\n1234\t0\tcom.wesley.daily\n",
    )
    t = st.list_tasks()[0]
    assert t["enabled"] is True
    assert t["running"] is True
    assert t["last_exit_code"] == 0


def test_launchd_last_exit_code_nonzero_surfaced(fake_env, monkeypatch):
    home, launch_dir = fake_env
    _write_plist(launch_dir, "com.wesley.flaky.plist", {
        "Label": "com.wesley.flaky",
        "ProgramArguments": [str(home / "scripts" / "flaky.sh")],
        "StartInterval": 300,
    })
    monkeypatch.setattr(
        st, "_read_launchctl_text",
        lambda: "PID\tStatus\tLabel\n-\t1\tcom.wesley.flaky\n",
    )
    t = st.list_tasks()[0]
    assert t["running"] is False
    assert t["last_exit_code"] == 1


# ── crontab ──────────────────────────────────────────────────────────────


def test_cron_active_line_parsed_with_preceding_comment_as_name(fake_env, monkeypatch, tmp_path):
    log = tmp_path / "sync.log"
    script = tmp_path / "sync.sh"
    monkeypatch.setattr(st, "_read_crontab_text", lambda: (
        "THINGS_AUTH_TOKEN=abc123\n"
        "\n"
        "# Obsidian 笔记同步 - 每 5 分钟\n"
        f"*/5 * * * * {script} >> {log} 2>&1\n"
    ))
    tasks = st.list_tasks()
    assert len(tasks) == 1
    t = tasks[0]
    assert t["source"] == "cron"
    assert t["name"] == "Obsidian 笔记同步 - 每 5 分钟"
    assert t["schedule_desc"] == "*/5 * * * *"
    assert t["script_path"] == str(script)
    assert t["log_paths"] == [str(log)]
    assert t["enabled"] is True
    assert t["running"] is None


def test_cron_commented_out_line_ignored_not_shown_as_disabled(fake_env, monkeypatch):
    monkeypatch.setattr(st, "_read_crontab_text", lambda: (
        "# 15 8 * * * cd /repo && ./crawl.sh >> log 2>&1\n"
        "# Step A: 运营规划 (00:30)\n"
    ))
    assert st.list_tasks() == []


def test_cron_line_without_preceding_comment_falls_back_to_script_stem(fake_env, monkeypatch, tmp_path):
    script = tmp_path / "redbook-queue-publish.sh"
    monkeypatch.setattr(st, "_read_crontab_text", lambda: f"30 7 * * * {script}\n")
    t = st.list_tasks()[0]
    assert t["name"] == "redbook-queue-publish"
    assert t["log_paths"] == []


# ── task_detail ──────────────────────────────────────────────────────────


def test_task_detail_reads_script_and_tails_log(fake_env, monkeypatch, tmp_path):
    home, launch_dir = fake_env
    script = home / "scripts" / "daily.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\necho hi\n")
    log = tmp_path / "daily.log"
    log.write_text("\n".join(f"line {i}" for i in range(1, 300)) + "\n")
    _write_plist(launch_dir, "com.wesley.daily.plist", {
        "Label": "com.wesley.daily",
        "ProgramArguments": [str(script)],
        "StartInterval": 300,
        "StandardOutPath": str(log),
    })
    detail = st.task_detail("launchd:com.wesley.daily")
    assert detail["script_content"] == "#!/bin/bash\necho hi\n"
    assert detail["script_error"] is None
    tail = detail["logs"][str(log)]
    assert tail.splitlines()[0] == "line 200"  # 只留最后 100 行
    assert tail.splitlines()[-1] == "line 299"


def test_task_detail_unknown_id_returns_none(fake_env):
    assert st.task_detail("launchd:ghost") is None


def test_task_detail_missing_script_file_reports_error(fake_env, monkeypatch):
    home, launch_dir = fake_env
    _write_plist(launch_dir, "com.wesley.gone.plist", {
        "Label": "com.wesley.gone",
        "ProgramArguments": [str(home / "scripts" / "gone.sh")],
        "StartInterval": 300,
    })
    detail = st.task_detail("launchd:com.wesley.gone")
    assert detail["script_content"] is None
    assert detail["script_error"] == "文件不存在"


def test_hostname_strips_local_suffix(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "Wesleys-MacBook.local")
    assert st.hostname() == "Wesleys-MacBook"
