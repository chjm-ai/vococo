"""诊断只读、异常降级、去噪销账和静默信号协议，不读取真实运行目录。"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "development_diagnostics", Path(__file__).resolve().parents[1] / "scripts" / "diagnose_development.py")
diag = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(diag)

_MIB = 1048576
_GIB = 1073741824


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root, data = tmp_path / "source", tmp_path / "runtime"
    root.mkdir()
    data.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=测试", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-m", "初始内容"], check=True, capture_output=True)
    with sqlite3.connect(data / "state.db") as connection:
        connection.executescript("CREATE TABLE projects(path TEXT); "
                                 "CREATE TABLE session_meta(session_key TEXT, worktree_path TEXT);")
    (data / "heartbeat").write_text(str(time.time()))
    (data / "repository-cleanup.json").write_text(json.dumps({"at": time.time(), "results": []}))
    (data / "storage-maintenance.json").write_text(json.dumps(
        {"at": time.time(), "sizes": {}, "limits": {}, "samples": [], "errors": []}))
    monkeypatch.setattr(diag, "machine_issues", lambda _: [])
    return root, data


def fake_disk(monkeypatch, free, total=500 * _GIB):
    monkeypatch.setattr(diag.shutil, "disk_usage",
                        lambda _: type("Usage", (), {"free": free, "total": total})())


def test_storage_budgets_and_growth_only_report_safe_numeric_summary(environment, monkeypatch):
    _, data = environment
    fake_disk(monkeypatch, 400 * _GIB)
    now = time.time()
    report = {"at": now, "sizes": {"附件": 300 * _MIB},
              "limits": {"附件": 200 * _MIB}, "errors": ["PRIVATE_SECRET"],
              "samples": [{"at": now - 7 * 86400, "sizes": {"附件": 10 * _MIB}}]}
    (data / "storage-maintenance.json").write_text(json.dumps(report))
    issues = diag.storage_issues(data, now)
    # 附件看增长+磁盘，不看固定提醒线；其它类仍保留提醒线。
    assert not any("附件" in issue and "提醒线" in issue for issue in issues)
    assert any("附件增长偏快" in issue for issue in issues)
    assert "PRIVATE_SECRET" not in str(issues)
    assert any("1 项" in issue for issue in issues)


def test_fixed_reminder_line_alone_is_not_an_issue_when_disk_is_roomy(environment, monkeypatch):
    _, data = environment
    fake_disk(monkeypatch, 300 * _GIB)
    now = time.time()
    report = {"at": now, "sizes": {"附件": 2464 * _MIB, "工作树": 2503 * _MIB, "业务副本": 1800 * _MIB},
              "limits": {"附件": 2048 * _MIB, "工作树": 2048 * _MIB, "业务副本": 2048 * _MIB},
              "samples": [{"at": now - 86400, "sizes": {"附件": 2440 * _MIB, "工作树": 2490 * _MIB,
                                                        "业务副本": 1790 * _MIB}}]}
    (data / "storage-maintenance.json").write_text(json.dumps(report))
    assert diag.storage_issues(data, now) == []


def test_capacity_reports_when_remaining_disk_would_be_consumed(environment, monkeypatch):
    _, data = environment
    fake_disk(monkeypatch, 40 * _GIB)
    now = time.time()
    report = {"at": now, "sizes": {"附件": 30 * _GIB, "工作树": 2 * _GIB},
              "limits": {"附件": 2048 * _MIB, "工作树": 2048 * _MIB},
              "samples": [{"at": now - 7 * 86400, "sizes": {"附件": 6 * _GIB, "工作树": 2 * _GIB}}]}
    (data / "storage-maintenance.json").write_text(json.dumps(report))
    issues = diag.storage_issues(data, now)
    assert any("附件增长偏快" in issue for issue in issues)
    assert any("附件占用较大" in issue for issue in issues)
    assert not any("工作树" in issue for issue in issues)


@pytest.mark.parametrize("kind", ["missing", "expired", "future", "symlink", "corrupt"])
def test_storage_receipt_failures_are_actionable(environment, kind):
    root, data = environment
    path = data / "storage-maintenance.json"
    if kind == "missing":
        path.unlink()
    elif kind == "symlink":
        path.unlink()
        path.symlink_to(data / "state.db")
    elif kind == "corrupt":
        path.write_text("PRIVATE_SECRET")
    else:
        value = json.loads(path.read_text())
        value["at"] = 0 if kind == "expired" else time.time() + 86400
        path.write_text(json.dumps(value))
    output = diag.render(diag.diagnose(root, data))
    assert "存储养护" in output and "PRIVATE_SECRET" not in output
    assert output.endswith("##CRON_SIGNAL:1##")


def test_healthy_diagnostics_are_completely_silent_except_signal(environment):
    root, data = environment
    assert diag.diagnose(root, data) == []
    assert diag.render([]) == "##CRON_SIGNAL:0##"


@pytest.mark.parametrize("age", [0, 901, -120])
def test_heartbeat_boundaries(environment, age):
    _, data = environment
    now = time.time()
    os.utime(data / "heartbeat", (now - age, now - age))
    issues = diag.heartbeat_issues(data, now)
    assert bool(issues) == (age != 0)


@pytest.mark.parametrize("kind", ["missing", "corrupt", "symlink"])
def test_database_failures_produce_sanitized_actionable_anomaly(environment, kind):
    root, data = environment
    database = data / "state.db"
    if kind == "missing":
        database.unlink()
    elif kind == "corrupt":
        database.write_text("PRIVATE_SECRET_DATABASE_CONTENT")
    else:
        saved = data / "saved.db"
        database.rename(saved)
        database.symlink_to(saved)
    output = diag.render(diag.diagnose(root, data))
    assert "工作树检查失败" in output
    assert "PRIVATE_SECRET" not in output
    assert output.endswith("##CRON_SIGNAL:1##")
    assert "请人工" in output


def test_notifications_are_a_single_summary_without_repairs(isolated):
    from vococo.memory import notices

    root = Path(__file__).resolve().parents[1]
    diag.reconcile(["磁盘空间不足：剩余 9 GiB", "15 分钟负载偏高：30.0，逻辑核心 10"], "general", root, isolated)
    opened = notices.list_open()
    assert len(opened) == 1
    assert opened[0]["kind"] == "decision"
    assert "安排只读排查" in opened[0]["options"]
    assert "未修改、删除或重启" in opened[0]["prompt"]
    assert "磁盘" in opened[0]["reason"] and "负载" in opened[0]["reason"]


def test_same_class_does_not_duplicate_and_class_change_leaves_one(isolated):
    from vococo.memory import notices

    root = Path(__file__).resolve().parents[1]
    diag.reconcile(["运行心跳异常：距当前 20 分钟"], "general", root, isolated)
    diag.reconcile(["运行心跳异常：距当前 30 分钟"], "general", root, isolated)
    assert len(notices.list_open()) == 1
    diag.reconcile(["磁盘空间不足：剩余 5 GiB"], "general", root, isolated)
    opened = notices.list_open()
    assert len(opened) == 1 and "磁盘" in opened[0]["reason"]


def test_resolved_issue_closes_old_notice(isolated):
    from vococo.memory import notices

    root = Path(__file__).resolve().parents[1]
    diag.reconcile(["磁盘空间不足：剩余 9 GiB"], "general", root, isolated)
    assert len(notices.list_open()) == 1
    closed = diag.reconcile([], "general", root, isolated)
    assert notices.list_open() == []
    # 销账原因写进运行回执，铃铛里不是无声消失。
    assert any("磁盘" in label for label in closed)


def test_other_decisions_are_not_touched(isolated):
    from vococo.memory import notices

    root = Path(__file__).resolve().parents[1]
    notices.add(session_key="session:general", kind="decision", status="expired",
                reason="其他事项", detail="其他事项", prompt="别人登记的事项", options=["好"])
    diag.reconcile(["磁盘空间不足：剩余 9 GiB"], "general", root, isolated)
    diag.reconcile([], "general", root, isolated)
    opened = notices.list_open()
    assert [row["reason"] for row in opened] == ["其他事项"]


def test_stock_baseline_is_silent_and_only_changes_are_reported(environment):
    root, data = environment
    with sqlite3.connect(data / "state.db") as connection:
        connection.execute("INSERT INTO session_meta VALUES (?,?)", ("session:test", str(root / "missing")))
    first = diag.diagnose(root, data)
    assert not any("失效" in issue for issue in first)
    assert not data.joinpath("development-diagnostics.json").exists()  # diagnose 自己不写状态
    state = {"worktrees": {}, "cleanup": {}}
    facts = ["治理存量记录：失效/异常工作树绑定或 Git 登记 1 个（不改动）"]
    assert diag._compare(facts, {}) == []
    label = "失效/异常工作树绑定或 Git 登记"
    assert diag._compare(facts, {label: 3}) == [f"{label}存量减少：3 → 1"]
    assert diag._compare(facts, {"开发工作树": 2}) == ["失效/异常工作树绑定或 Git 登记存量变化：0 → 1"]
    # 状态里按标签存最新数字，下次同类变化能对上。
    assert state == {"worktrees": {}, "cleanup": {}}
    tracking = {"开发工作树": 2}
    diag._compare(facts, tracking)
    assert tracking == {label: 1}
    assert diag._compare(facts, tracking) == []


def test_legacy_inventory_is_counted_without_listing_identifiers(environment):
    _, data = environment
    rows = [{"path": f"/PRIVATE/path/{index}", "reason": "旧存量仅列清单；有本地配置"} for index in range(25)]
    (data / "repository-cleanup.json").write_text(json.dumps({"at": time.time(), "results": rows}))
    issues, facts = diag.cleanup_issues(data, time.time())
    assert issues == []
    assert len(facts) == 1 and "25 个" in facts[0]
    assert "PRIVATE" not in str(facts)


def test_readonly_database_does_not_allow_writes(environment):
    _, data = environment
    with diag.readonly_database(data / "state.db") as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden(value TEXT)")
    with sqlite3.connect(data / "state.db") as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='forbidden'").fetchone()


def test_unbound_managed_worktree_is_reported_without_removing_it(environment):
    root, data = environment
    path = root / "data" / "worktrees" / "hash" / "test"
    path.parent.mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-b", "vococo/test", str(path)],
                   check=True, capture_output=True)
    issues, facts = diag.worktree_issues(root, data, max_worktrees=1)
    assert any("数量偏多" in issue for issue in issues)
    assert any("无会话绑定" in issue for issue in issues)
    assert any("开发工作树 1 个" in fact for fact in facts)
    assert path.is_dir()


def test_broken_binding_is_stock_not_daily_issue(environment):
    root, data = environment
    with sqlite3.connect(data / "state.db") as connection:
        connection.execute("INSERT INTO session_meta VALUES (?,?)", ("session:test", str(root / "missing")))
    issues, facts = diag.worktree_issues(root, data, 30)
    assert not any("失效" in issue for issue in issues)
    assert any("失效/异常工作树绑定或 Git 登记 1 个" in fact for fact in facts)


def test_unreadable_project_repository_is_reported_not_silently_skipped(environment):
    root, data = environment
    broken = root.parent / "broken-project"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nonexistent")
    with sqlite3.connect(data / "state.db") as connection:
        connection.execute("INSERT INTO projects VALUES (?)", (str(broken),))
    issues, _ = diag.worktree_issues(root, data, 30)
    # 读不了的仓库不能悄悄少算，否则存量“看起来减少了”。
    unreadable = [issue for issue in issues if "1 个项目仓库读取工作树登记失败" in issue]
    assert unreadable and diag.classify(unreadable[0]) == "工作树"


@pytest.mark.parametrize("status", ["running", "queued", "done"])
def test_old_task_status_is_checked_without_reading_prompts(environment, status):
    _, data = environment
    folder = data / "voice"
    folder.mkdir()
    with sqlite3.connect(folder / "voice.db") as connection:
        connection.execute("CREATE TABLE tasks(status TEXT, updated_at REAL, prompt TEXT)")
        connection.execute("INSERT INTO tasks VALUES (?,?,?)", (status, time.time() - 90000, "PRIVATE_PROMPT"))
    issues = diag.task_issues(data, time.time())
    assert bool(issues) == (status != "done")
    assert "PRIVATE_PROMPT" not in str(issues)


def test_cleanup_inventory_only_exposes_counts_and_stable_hashes(environment):
    _, data = environment
    rows = [{"path": "/PRIVATE/path/credential-file", "reason": "旧存量仅列清单；有本地配置"},
            {"path": "/PRIVATE/another", "reason": "检查或回收失败：ValueError"}]
    (data / "repository-cleanup.json").write_text(json.dumps({"at": time.time(), "results": rows}))
    output = diag.render(diag.cleanup_issues(data, time.time())[0])
    assert "1 项检查失败" in output and "旧开发工作树" not in output
    assert "PRIVATE" not in output and "credential-file" not in output
    assert output.endswith("##CRON_SIGNAL:1##")


@pytest.mark.parametrize("corrupt", [False, True])
def test_stale_or_corrupt_cleanup_receipt_is_anomaly(environment, corrupt):
    root, data = environment
    report = data / "repository-cleanup.json"
    report.write_text("{BROKEN_SECRET" if corrupt else json.dumps({"at": 0, "results": []}))
    issues = diag.diagnose(root, data)
    assert issues
    assert "BROKEN_SECRET" not in diag.render(issues)


def test_probe_exception_does_not_expose_command_or_secret(environment, monkeypatch):
    root, data = environment
    def fail(_):
        raise RuntimeError("Authorization: Bearer SECRET_MUST_NOT_LEAK")
    monkeypatch.setattr(diag, "machine_issues", fail)
    output = diag.render(diag.diagnose(root, data))
    assert "机器状态检查失败：RuntimeError" in output
    assert "Authorization" not in output and "SECRET" not in output


def test_entrypoint_emits_exactly_one_signal_and_does_not_modify_receipts(environment, monkeypatch, capsys):
    root, data = environment
    names = ("repository-cleanup.json", "storage-maintenance.json", "state.db")
    before = {name: (data / name).read_bytes() for name in names}
    monkeypatch.setattr(sys, "argv", ["diagnose_development.py", "--root", str(root), "--data-dir", str(data)])
    diag.main()
    assert capsys.readouterr().out == "##CRON_SIGNAL:0##\n"
    assert before == {name: (data / name).read_bytes() for name in names}


def test_machine_disk_and_load_thresholds_are_numeric_only(tmp_path, monkeypatch):
    monkeypatch.setattr(diag.shutil, "disk_usage", lambda _: type("Usage", (), {"free": 1, "total": 100})())
    monkeypatch.setattr(diag.os, "getloadavg", lambda: (100, 100, 100))
    monkeypatch.setattr(diag.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(diag.sys, "platform", "linux")
    issues = diag.machine_issues(tmp_path)
    assert len(issues) == 2
    assert "磁盘空间不足" in issues[0] and "负载偏高" in issues[1]


def test_worktree_capacity_works_without_separate_business_copy_key(environment, monkeypatch):
    _, data = environment
    fake_disk(monkeypatch, 20 * _GIB)
    now = time.time()
    report = {"at": now, "sizes": {"工作树": 21 * _GIB}, "limits": {"工作树": 2048 * _MIB},
              "samples": [{"at": now - 21 * 86400, "sizes": {"工作树": 3 * _GIB}}]}
    (data / "storage-maintenance.json").write_text(json.dumps(report))
    issues = diag.storage_issues(data, now)
    assert any("工作树增长偏快" in issue for issue in issues)
    assert any("工作树占用较大" in issue for issue in issues)


def test_slow_growth_is_seen_over_the_full_sample_window(environment, monkeypatch):
    _, data = environment
    fake_disk(monkeypatch, 60 * _GIB)
    now = time.time()
    # 每天 ~30 MiB：单次对比看不出，30 天累计 ~900 MiB 要能被识别。
    samples = [{"at": now - day * 86400, "sizes": {"附件": (3072 - day * 30) * _MIB}} for day in range(29, -1, -1)]
    report = {"at": now, "sizes": {"附件": 3072 * _MIB}, "limits": {"附件": 2048 * _MIB}, "samples": samples}
    (data / "storage-maintenance.json").write_text(json.dumps(report))
    assert any("附件增长偏快" in issue for issue in diag.storage_issues(data, now))


def test_same_class_pending_notice_is_not_closed_and_other_sessions_untouched(isolated):
    from vococo.memory import agents, notices

    root = Path(__file__).resolve().parents[1]
    diag.reconcile(["运行心跳异常：距当前 20 分钟"], "general", root, isolated)
    owner_session = agents.main_session_key(agents.get("general"))
    notices.add(session_key="session:other", kind="decision", status="expired",
                reason="机器诊断：磁盘", detail="机器诊断：磁盘", prompt="别处登记", options=["好"])
    diag.reconcile([], "general", root, isolated)
    opened = notices.list_open()
    assert [row["session_key"] for row in opened] == ["session:other"]
    assert owner_session
