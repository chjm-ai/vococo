"""诊断只读、异常降级和静默信号协议，不读取真实运行目录。"""
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


def test_storage_budgets_and_growth_only_report_safe_numeric_summary(environment):
    _, data = environment
    now = time.time()
    report = {"at": now, "sizes": {"附件": 300 * 1048576},
              "limits": {"附件": 200 * 1048576}, "errors": ["PRIVATE_SECRET"],
              "samples": [{"at": now - 86400, "sizes": {"附件": 10 * 1048576}}]}
    (data / "storage-maintenance.json").write_text(json.dumps(report))
    issues = diag.storage_issues(data, now)
    assert any("容量偏高" in issue for issue in issues)
    assert any("增长偏快" in issue for issue in issues)
    assert "PRIVATE_SECRET" not in str(issues)
    assert any("1 项" in issue for issue in issues)


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
    assert "请人工确认" in output


def test_notifications_are_deduplicated_and_do_not_perform_repairs(isolated):
    from vococo.memory import notices

    root = Path(__file__).resolve().parents[1]
    diag.notify_decisions(["磁盘空间不足：剩余 9 GiB"], "general", root)
    diag.notify_decisions(["磁盘空间不足：剩余 8 GiB"], "general", root)
    opened = notices.list_open()
    assert len(opened) == 1
    assert opened[0]["kind"] == "decision"
    assert "安排只读排查" in opened[0]["options"]
    assert "未修改、删除或重启" in opened[0]["prompt"]


def test_notification_counts_do_not_create_duplicate_decisions(isolated):
    from vococo.memory import notices

    root = Path(__file__).resolve().parents[1]
    diag.notify_decisions(["发现 2 个无会话绑定的受管工作树；仅列清单，不自动删除"], "general", root)
    diag.notify_decisions(["发现 3 个无会话绑定的受管工作树；仅列清单，不自动删除"], "general", root)
    assert len(notices.list_open()) == 1


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
    issues = diag.worktree_issues(root, data, max_worktrees=1)
    assert any("数量偏多" in issue for issue in issues)
    assert any("无会话绑定" in issue for issue in issues)
    assert path.is_dir()


def test_broken_binding_is_reported(environment):
    root, data = environment
    with sqlite3.connect(data / "state.db") as connection:
        connection.execute("INSERT INTO session_meta VALUES (?,?)", ("session:test", str(root / "missing")))
    assert any("失效" in issue for issue in diag.worktree_issues(root, data, 30))


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
    output = diag.render(diag.cleanup_issues(data, time.time()))
    assert "清单 ID" in output and "1 项检查失败" in output
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


def test_entrypoint_emits_exactly_one_signal_and_does_not_modify_files(environment, monkeypatch, capsys):
    root, data = environment
    before = {str(path): path.read_bytes() for path in data.rglob("*") if path.is_file()}
    monkeypatch.setattr(sys, "argv", ["diagnose_development.py", "--root", str(root), "--data-dir", str(data)])
    diag.main()
    assert capsys.readouterr().out == "##CRON_SIGNAL:0##\n"
    assert before == {str(path): path.read_bytes() for path in data.rglob("*") if path.is_file()}


def test_machine_disk_and_load_thresholds_are_numeric_only(tmp_path, monkeypatch):
    monkeypatch.setattr(diag.shutil, "disk_usage", lambda _: type("Usage", (), {"free": 1, "total": 100})())
    monkeypatch.setattr(diag.os, "getloadavg", lambda: (100, 100, 100))
    monkeypatch.setattr(diag.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(diag.sys, "platform", "linux")
    issues = diag.machine_issues(tmp_path)
    assert len(issues) == 2
    assert "磁盘空间不足" in issues[0] and "负载偏高" in issues[1]
